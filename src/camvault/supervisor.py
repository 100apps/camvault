from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from camvault.config import AppConfig, CameraConfig
from camvault.ffmpeg import build_camera_command, redacted_command
from camvault.onvif import resolve_camera_rtsp
from camvault.runtime import CameraRuntime
from camvault.security import redact_text, redact_url

logger = logging.getLogger(__name__)


class CameraSupervisor:
    def __init__(
        self,
        *,
        app_config: AppConfig,
        camera: CameraConfig,
        runtime: CameraRuntime,
        ingest_secret: str,
        on_stream_end: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.app_config = app_config
        self.camera = camera
        self.runtime = runtime
        self.ingest_secret = ingest_secret
        self.on_stream_end = on_stream_end
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None

    def start(self) -> None:
        if self._task is None:
            if self._stop.is_set():
                self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name=f"camera-{self.camera.id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._process is not None and self._process.returncode is None:
            await self._terminate(self._process)
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        recording = self.app_config.recording
        backoff = recording.restart_min_seconds
        while not self._stop.is_set():
            cycle_started_monotonic = time.monotonic()
            segments_at_launch = self.runtime.segments_ingested
            stderr_tail: list[str] = []
            stderr_task: asyncio.Task[None] | None = None
            process_started = False
            try:
                self._set_state("resolving", "resolving ONVIF/RTSP stream")
                rtsp_url, profile = await resolve_camera_rtsp(self.camera)
                self.runtime.resolved_stream = redact_url(rtsp_url)
                self.runtime.resolved_profile = (
                    f"{profile.name} ({profile.width or '?'}x{profile.height or '?'})"
                    if profile
                    else "direct RTSP"
                )
                command = build_camera_command(
                    camera=self.camera,
                    recording=recording,
                    rtsp_url=rtsp_url,
                    ingest_host=self.app_config.server.ingest_host(),
                    ingest_port=self.app_config.server.port,
                    ingest_secret=self.ingest_secret,
                )
                logger.info(
                    "camera %s launching: %s",
                    self.camera.id,
                    redacted_command(command, secrets_to_hide=(self.ingest_secret,)),
                )
                self._set_state("starting", "starting FFmpeg")
                self.runtime.started_at = datetime.now(UTC)
                self._process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.runtime.process_pid = self._process.pid
                process_started = True
                process_started_monotonic = time.monotonic()
                self._set_state("recording", "waiting for media segments")
                stderr_task = asyncio.create_task(
                    self._read_stderr(self._process, stderr_tail),
                    name=f"stderr-{self.camera.id}",
                )
                exit_code, watchdog_reason = await self._watch_process(
                    self._process, process_started_monotonic, segments_at_launch
                )
                await asyncio.gather(stderr_task, return_exceptions=True)
                if self._stop.is_set():
                    break

                detail = watchdog_reason or f"FFmpeg exited with code {exit_code}"
                if stderr_tail:
                    detail += f": {stderr_tail[-1]}"
                self.runtime.last_error = detail
                self._set_state("offline", detail)
                self.runtime.restarts += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - supervisor must restart after any failure
                password = self.camera.resolved_password() or ""
                message = redact_text(str(exc), (password, self.ingest_secret))
                self.runtime.last_error = message
                self.runtime.restarts += 1
                self._set_state("offline", message)
                logger.warning("camera %s failed: %s", self.camera.id, message)
            finally:
                process = self._process
                if process is not None and process.returncode is None:
                    await self._terminate(process)
                if stderr_task is not None:
                    await asyncio.gather(stderr_task, return_exceptions=True)
                self.runtime.process_pid = None
                self._process = None
                if process_started and self.on_stream_end is not None:
                    try:
                        await self.on_stream_end(self.camera.id)
                    except Exception as exc:  # noqa: BLE001 - cleanup must not stop supervision
                        logger.error(
                            "camera %s could not seal stream tail: %s",
                            self.camera.id,
                            exc,
                        )

            if self._stop.is_set():
                break
            stable = (
                self.runtime.segments_ingested > segments_at_launch
                and time.monotonic() - cycle_started_monotonic
                > max(30.0, recording.no_segment_timeout_seconds)
            )
            if stable:
                backoff = recording.restart_min_seconds
            delay = min(recording.restart_max_seconds, backoff)
            jittered = delay * random.uniform(0.85, 1.15)
            self.runtime.detail = f"restarting in {jittered:.1f}s"
            self.runtime.touch()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=jittered)
            except TimeoutError:
                pass
            backoff = min(recording.restart_max_seconds, max(backoff * 2, delay + 0.1))

        self._set_state("stopped", "recorder stopped")

    async def _watch_process(
        self,
        process: asyncio.subprocess.Process,
        launch_monotonic: float,
        segments_at_launch: int,
    ) -> tuple[int | None, str | None]:
        recording = self.app_config.recording
        wait_task = asyncio.create_task(process.wait())
        reason: str | None = None
        while not self._stop.is_set():
            done, _ = await asyncio.wait({wait_task}, timeout=recording.health_check_seconds)
            if done:
                return wait_task.result(), reason

            now = datetime.now(UTC)
            if self.runtime.segments_ingested <= segments_at_launch:
                if time.monotonic() - launch_monotonic > recording.startup_timeout_seconds:
                    reason = "no media segment arrived before startup timeout"
                    await self._terminate(process)
                    return await wait_task, reason
            else:
                age = (now - self.runtime.last_segment_at).total_seconds()
                if age > recording.no_segment_timeout_seconds:
                    reason = f"no new media segment for {age:.0f}s"
                    await self._terminate(process)
                    return await wait_task, reason

        await self._terminate(process)
        return await wait_task, "stopped"

    async def _read_stderr(
        self,
        process: asyncio.subprocess.Process,
        tail: list[str],
    ) -> None:
        if process.stderr is None:
            return
        secrets_to_hide = (
            self.camera.resolved_password() or "",
            self.ingest_secret,
        )
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = redact_text(line.decode("utf-8", errors="replace").strip(), secrets_to_hide)
            if not text:
                continue
            tail.append(text)
            del tail[:-20]
            logger.warning("camera %s ffmpeg: %s", self.camera.id, text)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=8)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    def _set_state(self, state: str, detail: str) -> None:
        self.runtime.state = state
        self.runtime.detail = detail
        self.runtime.touch()


class SupervisorManager:
    def __init__(
        self,
        *,
        config: AppConfig,
        runtimes: dict[str, CameraRuntime],
        ingest_secret: str,
        on_stream_end: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.supervisors = {
            camera.id: CameraSupervisor(
                app_config=config,
                camera=camera,
                runtime=runtimes[camera.id],
                ingest_secret=ingest_secret,
                on_stream_end=on_stream_end,
            )
            for camera in config.cameras
            if camera.enabled
        }

    def start(self) -> None:
        for supervisor in self.supervisors.values():
            supervisor.start()

    async def stop(self) -> None:
        await asyncio.gather(
            *(supervisor.stop() for supervisor in self.supervisors.values()),
            return_exceptions=True,
        )
