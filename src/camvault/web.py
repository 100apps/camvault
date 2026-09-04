from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import math
import re
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.background import BackgroundTask
from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from camvault.config_store import ConfigConflictError, ConfigStoreError
from camvault.service import CamVaultService
from camvault.storage import StorageBackendError

_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,180}$")
_SESSION_COOKIE = "camvault_session"


def _asset_text(name: str) -> str:
    return files("camvault").joinpath("web_assets", name).read_text(encoding="utf-8")


class SecurityHeadersMiddleware:
    """Pure ASGI middleware; avoids buffering/disconnect quirks of BaseHTTPMiddleware."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault(
                    "Content-Security-Policy",
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                    "connect-src 'self'; media-src 'self' blob:; worker-src blob:",
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _script_json(value: object) -> str:
    # JSON alone does not escape the `</script>` delimiter in inline script blocks.
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _constant_time_text_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _parse_datetime(value: str | None, timezone_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid ISO datetime: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(UTC)


def create_app(
    service: CamVaultService,
    *,
    manage_service: bool = True,
    start_recorders: bool = True,
) -> FastAPI:
    delayed_start = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal delayed_start
        if manage_service:
            await service.start(start_supervisors=False)
            if start_recorders:
                # Uvicorn starts listening only after lifespan startup completes. Delaying
                # recorder launch avoids the first HLS PUT racing the HTTP listener.
                delayed_start = asyncio.create_task(service.start_supervisors_after(0.35))
        try:
            yield
        finally:
            if delayed_start is not None:
                if not delayed_start.done():
                    delayed_start.cancel()
                await asyncio.gather(delayed_start, return_exceptions=True)
            if manage_service:
                await service.stop()

    app = FastAPI(
        title="CamVault",
        version="0.5.0",
        description="RAM-buffered ONVIF/RTSP recorder with local and WebDAV archives",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )

    app.add_middleware(SecurityHeadersMiddleware)

    def supplied_token(request: Request, *, allow_query: bool) -> str | None:
        provided = request.query_params.get("token") if allow_query else None
        header_token = request.headers.get("X-CamVault-Token")
        if header_token:
            provided = header_token.strip()
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        return provided

    def has_browser_session(request: Request) -> bool:
        supplied = request.cookies.get(_SESSION_COOKIE, "")
        return bool(service.web_password and service.valid_browser_session(supplied))

    def has_playback_auth(request: Request, *, allow_query: bool = True) -> bool:
        if has_browser_session(request):
            return True
        expected = service.playback_token
        if expected:
            provided = supplied_token(request, allow_query=allow_query)
            return bool(provided and _constant_time_text_equal(provided, expected))
        return not service.web_password

    async def require_playback_auth(request: Request) -> None:
        if has_playback_auth(request):
            return
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def require_control_auth(request: Request) -> None:
        if has_browser_session(request):
            csrf = request.headers.get("X-CamVault-CSRF", "")
            if not csrf or not _constant_time_text_equal(csrf, service.csrf_token):
                raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
            return
        expected = service.playback_token
        if expected:
            provided = supplied_token(request, allow_query=False)
            if not provided or not _constant_time_text_equal(provided, expected):
                raise HTTPException(
                    status_code=401,
                    detail="control API requires a token header",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return
        if not _is_loopback(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="control API is loopback-only")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/") -> Response:
        if has_browser_session(request) or not service.web_password:
            return RedirectResponse(url="/", status_code=303)
        safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
        page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · CamVault</title>
<style>{_asset_text("login.css")}</style></head><body><main><section class="login-card">
<div class="mark" aria-hidden="true"><span></span></div><p class="eyebrow">CAMVAULT</p>
<h1>欢迎回来</h1><p class="lead">登录以查看实时画面与录像归档</p>
<form method="post" action="/login"><input type="hidden" name="next" value="{html.escape(safe_next, quote=True)}">
<label for="password">访问密码</label><div class="password-row"><input id="password" name="password" type="password" autocomplete="current-password" autofocus required maxlength="512"><button type="button" id="reveal" aria-label="显示密码">显示</button></div>
<button class="submit" type="submit">进入监控中心</button></form>
<p class="foot">凭证仅用于当前 CamVault 服务，不会保存在浏览器存储中。</p>
</section></main><script>const p=document.getElementById('password'),b=document.getElementById('reveal');b.onclick=()=>{{const show=p.type==='password';p.type=show?'text':'password';b.textContent=show?'隐藏':'显示';}};</script></body></html>"""
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.post("/login")
    async def login(request: Request) -> Response:
        if not service.web_password:
            return RedirectResponse(url="/", status_code=303)
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(status_code=413, detail="login request is too large")
        form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        provided = form.get("password", [""])[0]
        if not _constant_time_text_equal(provided, service.web_password):
            await asyncio.sleep(0.4)
            return HTMLResponse(
                _asset_text("login_failed.html"),
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        destination = form.get("next", ["/"])[0]
        if not destination.startswith("/") or destination.startswith("//"):
            destination = "/"
        response = RedirectResponse(url=destination, status_code=303)
        response.set_cookie(
            _SESSION_COOKIE,
            service.create_browser_session(),
            max_age=service.config.server.session_hours * 3600,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/logout", dependencies=[Depends(require_control_auth)])
    async def logout(request: Request) -> Response:
        service.revoke_browser_session(request.cookies.get(_SESSION_COOKIE, ""))
        response = JSONResponse({"status": "logged_out"})
        response.delete_cookie(_SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return response

    @app.get("/assets/dashboard.css")
    async def dashboard_css() -> Response:
        return Response(
            _asset_text("dashboard.css"),
            media_type="text/css",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/assets/dashboard.js")
    async def dashboard_js() -> Response:
        return Response(
            _asset_text("dashboard.js"),
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.put("/_ingest/{camera_id}/{filename}")
    async def ingest(camera_id: str, filename: str, request: Request) -> Response:
        if not _is_loopback(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="ingest is loopback-only")
        supplied_secret = request.headers.get("X-CamVault-Ingest", "")
        if not secrets.compare_digest(supplied_secret, service.ingest_secret):
            raise HTTPException(status_code=403, detail="invalid ingest secret")
        if not _FILENAME_RE.fullmatch(filename):
            raise HTTPException(status_code=400, detail="invalid ingest filename")

        try:
            upload_lock = service.upload_lock(camera_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown camera") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        max_bytes = (
            1024 * 1024
            if filename.endswith(".m3u8")
            else service.config.recording.max_ingest_segment_mb * 1024 * 1024
        )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise HTTPException(status_code=413, detail="ingest object is too large")
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid content-length")

        # Acquire before consuming the body. At most one full HTTP object per camera can
        # reside in the application while storage backpressure is active.
        async with upload_lock:
            data = bytearray()
            try:
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise HTTPException(status_code=413, detail="ingest object is too large")
            except ClientDisconnect:
                # FFmpeg may close a superseded playlist PUT while reconnecting. The body
                # is incomplete, so discard it without emitting a noisy ASGI traceback.
                return Response(status_code=499)
            try:
                result = await service.ingest_upload(camera_id, filename, bytes(data))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="unknown camera") from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(status_code=200 if result == "duplicate" else 201)

    @app.get("/api/status", dependencies=[Depends(require_playback_auth)])
    async def api_status() -> JSONResponse:
        return JSONResponse(service.status())

    @app.get("/api/cameras", dependencies=[Depends(require_playback_auth)])
    async def api_cameras() -> list[dict[str, object]]:
        return [runtime.as_dict() for runtime in service.runtimes.values()]

    @app.get("/api/timeline", dependencies=[Depends(require_playback_auth)])
    async def api_timeline(start: str, end: str) -> JSONResponse:
        start_dt = _parse_datetime(start, service.config.storage.timezone)
        end_dt = _parse_datetime(end, service.config.storage.timezone)
        assert start_dt is not None and end_dt is not None
        if start_dt >= end_dt:
            raise HTTPException(status_code=422, detail="start must be before end")
        if end_dt - start_dt > timedelta(days=31):
            raise HTTPException(status_code=422, detail="timeline window cannot exceed 31 days")
        enabled = [camera for camera in service.config.cameras if camera.enabled]
        try:
            results = await asyncio.gather(
                *(
                    service.archive_records(camera.id, start=start_dt, end=end_dt)
                    for camera in enabled
                )
            )
        except StorageBackendError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        cameras: list[dict[str, object]] = []
        tolerance = max(1.0, service.config.recording.hls_segment_seconds * 2)
        for camera, records in zip(enabled, results, strict=True):
            ranges: list[dict[str, object]] = []
            for record in records:
                clipped_start = max(record.start, start_dt)
                clipped_end = min(record.end, end_dt)
                if clipped_start >= clipped_end:
                    continue
                if ranges:
                    previous_end = datetime.fromisoformat(str(ranges[-1]["end"]))
                    if (clipped_start - previous_end).total_seconds() <= tolerance:
                        ranges[-1]["end"] = max(previous_end, clipped_end).isoformat()
                        ranges[-1]["bytes"] = int(ranges[-1]["bytes"]) + record.size_bytes
                        ranges[-1]["records"] = int(ranges[-1]["records"]) + 1
                        continue
                ranges.append(
                    {
                        "start": clipped_start.isoformat(),
                        "end": clipped_end.isoformat(),
                        "bytes": record.size_bytes,
                        "records": 1,
                    }
                )
            cameras.append(
                {
                    "id": camera.id,
                    "name": camera.name or camera.id,
                    "ranges": ranges,
                    "record_count": len(records),
                    "bytes": sum(record.size_bytes for record in records),
                }
            )
        return JSONResponse(
            {
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "archive_chunk_seconds": service.config.storage.archive_chunk_seconds,
                "cameras": cameras,
            }
        )

    @app.get("/api/config", dependencies=[Depends(require_control_auth)])
    async def api_config() -> JSONResponse:
        try:
            snapshot = await service.read_config()
        except (ValueError, ConfigStoreError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(
            {
                "content": snapshot.content,
                "revision": snapshot.revision,
                "restart_required": False,
            }
        )

    @app.put("/api/config", dependencies=[Depends(require_control_auth)])
    async def api_update_config(request: Request) -> JSONResponse:
        if request.headers.get("content-length"):
            try:
                if int(request.headers["content-length"]) > 2 * 1024 * 1024:
                    raise HTTPException(
                        status_code=413, detail="configuration request is too large"
                    )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content-length") from exc
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="request body must be an object")
        content = payload.get("content")
        revision = payload.get("revision")
        if not isinstance(content, str) or not isinstance(revision, str):
            raise HTTPException(status_code=422, detail="content and revision must both be strings")
        try:
            snapshot = await service.write_config(content, expected_revision=revision)
        except ConfigConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ConfigStoreError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(
            {
                "revision": snapshot.revision,
                "restart_required": True,
                "message": "configuration saved and backed up; restart CamVault to apply it",
            }
        )

    @app.get("/api/logs", dependencies=[Depends(require_control_auth)])
    async def api_logs(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> JSONResponse:
        entries = service.log_entries(after=after, limit=limit)
        return JSONResponse({"entries": entries, "count": len(entries)})

    @app.post("/api/retention/run", dependencies=[Depends(require_control_auth)])
    async def api_retention_run() -> JSONResponse:
        try:
            result = await service.run_retention(reason="manual-console")
        except (OSError, ValueError, StorageBackendError) as exc:
            raise HTTPException(status_code=502, detail=f"retention failed: {exc}") from exc
        return JSONResponse(
            {
                "deleted_files": result.deleted_files,
                "deleted_bytes": result.deleted_bytes,
                "remaining_bytes": result.remaining_bytes,
                "free_bytes": result.free_bytes,
            }
        )

    @app.get(
        "/api/cameras/{camera_id}/recordings",
        dependencies=[Depends(require_playback_auth)],
    )
    async def api_recordings(
        camera_id: str,
        start: str | None = None,
        end: str | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> list[dict[str, object]]:
        start_dt = _parse_datetime(start, service.config.storage.timezone)
        end_dt = _parse_datetime(end, service.config.storage.timezone)
        try:
            records = await service.archive_records(
                camera_id,
                start=start_dt,
                end=end_dt,
                limit=limit,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown camera") from exc
        return [record.as_dict() for record in records]

    @app.get(
        "/live/{camera_id}/index.m3u8",
        dependencies=[Depends(require_playback_auth)],
    )
    async def live_playlist(camera_id: str, request: Request) -> Response:
        try:
            buffer = service.live_buffer(camera_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown camera") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        request_token = request.query_params.get("token")
        encoded_token = quote(request_token, safe="") if request_token else None
        playlist = buffer.render_playlist(token=encoded_token)
        if not playlist:
            raise HTTPException(
                status_code=503,
                detail="camera has no live segments yet",
                headers={"Retry-After": "2"},
            )
        return Response(
            content=playlist,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get(
        "/live/{camera_id}/{filename}",
        dependencies=[Depends(require_playback_auth)],
    )
    async def live_segment(camera_id: str, filename: str) -> Response:
        if not _FILENAME_RE.fullmatch(filename) or not filename.endswith(".ts"):
            raise HTTPException(status_code=404, detail="segment not found")
        try:
            segment = service.live_buffer(camera_id).get(filename)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="segment not found") from exc
        if segment is None:
            raise HTTPException(status_code=404, detail="segment expired")
        return Response(
            content=segment.data,
            media_type="video/mp2t",
            headers={
                "Cache-Control": "private, max-age=30, immutable",
                "Content-Length": str(len(segment.data)),
            },
        )

    @app.get(
        "/vod/{camera_id}/index.m3u8",
        dependencies=[Depends(require_playback_auth)],
    )
    async def vod_playlist(
        camera_id: str,
        request: Request,
        start: str | None = None,
        end: str | None = None,
    ) -> Response:
        end_dt = _parse_datetime(end, service.config.storage.timezone) or datetime.now(UTC)
        start_dt = _parse_datetime(start, service.config.storage.timezone) or (
            end_dt - timedelta(hours=1)
        )
        if start_dt > end_dt:
            raise HTTPException(status_code=422, detail="start must be before end")
        try:
            records = await service.archive_records(camera_id, start=start_dt, end=end_dt)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown camera") from exc
        if not records:
            raise HTTPException(status_code=404, detail="no recordings in this time range")

        target_duration = max(1, math.ceil(max(record.duration for record in records)))
        request_token = request.query_params.get("token")
        encoded_token = quote(request_token, safe="") if request_token else None
        token_suffix = f"?token={encoded_token}" if encoded_token else ""
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        # The first returned archive may start before the requested instant because
        # archives are intentionally minute-sized. EXT-X-START makes the player seek to
        # the requested wall-clock point without exposing that storage detail in the UI.
        start_offset = (start_dt - records[0].start).total_seconds()
        if start_offset > 0:
            total_duration = sum(record.duration for record in records)
            precise_offset = min(start_offset, max(0.0, total_duration - 0.001))
            lines.append(f"#EXT-X-START:TIME-OFFSET={precise_offset:.3f},PRECISE=YES")
        previous_record = None
        for record in records:
            if previous_record is not None:
                stream_changed = previous_record.stream_id != record.stream_id and (
                    previous_record.stream_id is not None or record.stream_id is not None
                )
                gap_seconds = (record.start - previous_record.end).total_seconds()
                if stream_changed or gap_seconds > max(
                    1.0, service.config.recording.hls_segment_seconds * 2
                ):
                    lines.append("#EXT-X-DISCONTINUITY")
            pdt = (
                record.start.astimezone(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            path = quote(record.relative_path, safe="/")
            lines += [
                f"#EXT-X-PROGRAM-DATE-TIME:{pdt}",
                f"#EXTINF:{record.duration:.3f},",
                f"/recordings/{camera_id}/{path}{token_suffix}",
            ]
            previous_record = record
        lines.append("#EXT-X-ENDLIST")
        return Response(
            content="\n".join(lines) + "\n",
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get(
        "/recordings/{camera_id}/{relative_path:path}",
        dependencies=[Depends(require_playback_auth)],
    )
    async def recording_file(camera_id: str, relative_path: str, request: Request) -> Response:
        try:
            local_path = service.local_recording_path(camera_id, relative_path)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown camera") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if local_path is not None:
            if not local_path.is_file():
                raise HTTPException(status_code=404, detail="recording not found")
            return FileResponse(
                local_path,
                media_type="video/mp2t",
                filename=None,
                headers={"Cache-Control": "private, max-age=3600"},
            )

        try:
            remote = await service.open_remote_recording(
                camera_id, relative_path, request.headers.get("range")
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="recording not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StorageBackendError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if remote is None:
            raise HTTPException(status_code=500, detail="storage backend cannot serve recordings")

        headers = remote.headers
        media_type = headers.pop("content-type", "video/mp2t")
        headers["Cache-Control"] = "private, max-age=3600"
        return StreamingResponse(
            remote.iter_bytes(),
            status_code=remote.status_code,
            media_type=media_type,
            headers=headers,
            background=BackgroundTask(remote.close),
        )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_v2(request: Request) -> Response:
        if not has_playback_auth(request):
            if service.web_password:
                return RedirectResponse(url="/login?next=/", status_code=303)
            await require_playback_auth(request)
        token = request.query_params.get("token", "")
        camera_cards = []
        for camera in service.config.cameras:
            state = "已停用" if not camera.enabled else "正在连接"
            codec_mode = camera.video_codec or service.config.recording.video_codec
            quality_label = "原码直通" if codec_mode == "copy" else "H.264 主码流"
            camera_cards.append(
                f"""<article class="camera-card" data-camera="{camera.id}">
<div class="camera-head"><div><span class="status-dot" id="dot-{camera.id}"></span>
<strong>{html.escape(camera.name or camera.id)}</strong></div>
<span class="camera-state" id="state-{camera.id}">{state}</span></div>
<div class="video-shell"><video id="video-{camera.id}" controls muted playsinline preload="none"></video>
<div class="video-placeholder" id="message-{camera.id}">{state}</div><span class="quality-badge">{quality_label}</span></div>
<div class="camera-foot"><span id="profile-{camera.id}">等待码流信息</span>
<span id="rate-{camera.id}">0 B / 分钟</span></div></article>"""
            )
        bootstrap = _script_json(
            {
                "cameras": [
                    {
                        "id": c.id,
                        "name": c.name or c.id,
                        "enabled": c.enabled,
                        "codecMode": c.video_codec or service.config.recording.video_codec,
                    }
                    for c in service.config.cameras
                ],
                "token": token,
                "csrf": service.csrf_token if has_browser_session(request) else "",
                "timezone": service.config.storage.timezone,
                "hasLogin": bool(service.web_password),
            }
        )
        page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark">
<title>CamVault · 监控中心</title><link rel="stylesheet" href="/assets/dashboard.css?v=6">
<script defer src="https://cdn.jsdelivr.net/npm/hls.js@1.7.2/dist/hls.min.js"></script>
<script>window.CAMVAULT_BOOTSTRAP={bootstrap};</script><script defer src="/assets/dashboard.js?v=6"></script></head>
<body><header class="app-header"><a class="brand" href="/"><span class="brand-mark"><i></i></span><span><b>CamVault</b><small>视频归档系统</small></span></a>
<nav><button class="nav-item active" data-view="monitor">监控中心</button><button class="nav-item" data-view="system">系统管理</button></nav>
<div class="header-actions"><span class="health-chip" id="overall"><i></i>正在连接</span><button class="icon-button" id="logout" title="退出登录" hidden>退出</button></div></header>
<main><section id="monitorView"><div class="section-heading"><div><p class="eyebrow">OVERVIEW</p><h1>监控中心</h1><p>主码流实时预览与连续录像归档</p></div><time id="clock"></time></div>
<section class="metrics"><article><span>存储使用</span><strong id="storageUsage">—</strong><small id="storageDetail">正在读取容量</small><div class="meter"><i id="storageMeter"></i></div></article>
<article><span>最近 60 秒写入</span><strong id="writeRate">—</strong><small id="writeBitrate">—</small></article>
<article><span>归档策略</span><strong id="retention">—</strong><small id="retentionDetail">—</small></article>
<article><span>内存媒体缓冲</span><strong id="memory">—</strong><small>实时分片与待上传数据</small></article></section>
<section class="playback-panel"><div class="playback-top"><div class="mode-switch"><button id="modeLive" class="active"><i></i>实时监控</button><button id="modeHistory">历史回放</button></div>
<p id="playbackNotice">低延迟转发，页面隐藏时自动暂停拉流</p></div>
<div class="timeline-panel" id="timelinePanel" hidden><div class="timeline-toolbar"><div class="presets"><button data-span="3600000">1 小时</button><button data-span="21600000" class="active">6 小时</button><button data-span="86400000">24 小时</button><button data-span="604800000">7 天</button></div>
<div class="zoom-tools"><button id="zoomOut" title="缩小时间范围">−</button><button id="zoomIn" title="放大时间范围">＋</button><button id="jumpNow">回到现在</button></div></div>
<div class="timeline-wrap"><canvas id="timeline" tabindex="0" role="slider" aria-label="录像时间轴"></canvas><div class="timeline-loading" id="timelineLoading">正在载入录像索引</div></div>
<div class="selection-row"><div><span>回放开始</span><input id="historyStart" type="datetime-local" step="1"></div><div><span>回放结束</span><input id="historyEnd" type="datetime-local" step="1"></div>
<button id="applyHistory" class="primary">播放所选时段</button><small>拖动框选 · 滚轮缩放 · 按住 Shift 拖动平移</small></div></div></section>
<section class="camera-grid">{"".join(camera_cards)}</section></section>
<section id="systemView" hidden><div class="section-heading"><div><p class="eyebrow">SYSTEM</p><h1>系统管理</h1><p>修改配置、检查运行日志与执行归档清理</p></div></div>
<div class="system-grid"><article class="panel"><div class="panel-head"><div><h2>运行配置</h2><p>保存时自动校验，并保留上一份备份</p></div><button id="reload" class="quiet">重新读取</button></div>
<textarea id="config" spellcheck="false" aria-label="CamVault TOML configuration"></textarea><div class="panel-actions"><button id="save" class="primary">保存配置</button><span id="notice">修改在重启 CamVault 后生效</span></div></article>
<article class="panel"><div class="panel-head"><div><h2>运行日志</h2><p>敏感凭证在进入日志前会被脱敏</p></div><button id="refreshLogs" class="quiet">刷新</button></div><pre id="logs">正在读取日志…</pre>
<div class="panel-actions"><button id="cleanup" class="danger">执行归档清理</button><span>按日期、容量和空余空间策略处理</span></div></article></div></section></main>
<div class="toast" id="toast" role="status"></div></body></html>"""
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    async def _legacy_dashboard(request: Request) -> HTMLResponse:
        token = request.query_params.get("token", "")
        cards = []
        token_query = f"?token={quote(token, safe='')}" if token else ""
        for camera in service.config.cameras:
            cards.append(
                f"""
                <article class="camera-card" data-camera="{camera.id}">
                  <div class="video-wrap">
                    <video id="video-{camera.id}" controls muted playsinline preload="metadata"></video>
                    <span class="video-message" id="message-{camera.id}">
                      {"摄像头已停用" if not camera.enabled else "正在连接实时画面…"}
                    </span>
                  </div>
                  <div class="camera-meta">
                    <div><span class="dot" id="dot-{camera.id}"></span>
                      <strong>{html.escape(camera.name or camera.id)}</strong>
                      <small id="state-{camera.id}">{"disabled" if not camera.enabled else "starting"}</small>
                    </div>
                    <a href="/player/{camera.id}{token_query}">单独查看</a>
                  </div>
                </article>
                """
            )
        token_json = _script_json(token)
        cameras_json = _script_json(
            [
                {"id": camera.id, "name": camera.name or camera.id, "enabled": camera.enabled}
                for camera in service.config.cameras
            ]
        )
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CamVault</title><script src="https://cdn.jsdelivr.net/npm/hls.js@1.7.2/dist/hls.min.js"></script><style>
:root{{--bg:#090e1b;--card:#141c2e;--text:#e8edf7;--muted:#92a2bb;--line:#293653;--ok:#42d392;--bad:#ff6b6b;--accent:#3979ef;--warn:#f8c15c}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1440px;margin:0 auto;padding:28px 20px 48px}}h1,h2{{margin:0}}.top{{display:flex;justify-content:space-between;align-items:start;gap:16px;margin-bottom:20px}}.lead,small,.muted{{color:var(--muted)}}.lead{{margin:7px 0 0}}
.pill{{border:1px solid var(--line);border-radius:999px;padding:7px 12px;color:var(--muted)}}.metrics,.grid,.tools{{display:grid;gap:14px}}.metrics{{grid-template-columns:repeat(3,1fr);margin-bottom:14px}}.metric,.camera-card,.panel{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}}.metric strong{{display:block;font-size:21px;margin-top:6px}}
.viewer{{margin-bottom:28px}}.playback-bar{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:14px}}.playback-bar label{{font-size:13px;color:var(--muted)}}.mode{{display:flex;gap:4px;background:#080d18;border-radius:10px;padding:4px}}.mode button{{background:transparent;color:var(--muted);padding:7px 12px}}.mode button.active{{background:var(--accent);color:white}}input{{font:inherit;color:var(--text);background:#080d18;border:1px solid var(--line);border-radius:8px;padding:8px}}input:disabled{{opacity:.45}}
.grid{{grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));margin-bottom:0}}.camera-card{{padding:0;overflow:hidden;min-width:0}}.video-wrap{{position:relative;aspect-ratio:16/9;background:#020409}}video{{display:block;width:100%;height:100%;object-fit:contain;background:#020409}}.video-message{{position:absolute;left:12px;bottom:12px;max-width:calc(100% - 24px);background:#050914cc;color:#dce6f7;border:1px solid #ffffff20;border-radius:8px;padding:6px 9px;font-size:12px;pointer-events:none}}.video-message:empty{{display:none}}.camera-meta{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 14px}}.camera-meta>div{{display:flex;align-items:center;min-width:0}}.camera-meta small{{margin-left:9px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.camera-meta a,a{{color:#8bb9ff;text-decoration:none}}.dot{{display:inline-block;flex:0 0 auto;width:10px;height:10px;border-radius:50%;background:var(--muted);margin-right:8px}}
.button,button{{border:0;display:inline-block;text-align:center;text-decoration:none;color:white;background:var(--accent);padding:10px 13px;border-radius:9px;font:inherit;cursor:pointer}}button.secondary{{background:#273550}}button.danger{{background:#9c3f48}}button:disabled{{opacity:.55;cursor:wait}}
.tools{{grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr)}}.panel h2{{font-size:18px;margin-bottom:12px}}textarea{{width:100%;min-height:440px;resize:vertical;background:#080d18;color:#dce6f7;border:1px solid var(--line);border-radius:9px;padding:13px;font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}}
.actions{{display:flex;flex-wrap:wrap;align-items:center;gap:9px;margin-top:10px}}#notice{{font-size:13px;color:var(--muted)}}pre{{height:410px;overflow:auto;white-space:pre-wrap;word-break:break-word;background:#080d18;border:1px solid var(--line);border-radius:9px;padding:13px;color:#ccd7e9;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}}
footer{{color:var(--muted);margin-top:28px;font-size:13px}}@media(max-width:780px){{main{{padding:18px 10px 36px}}.metrics,.tools{{grid-template-columns:1fr}}.top{{align-items:flex-start;flex-direction:column}}.playback-bar label{{width:100%}}.playback-bar input{{width:100%}}}}
</style></head><body><main><div class="top"><div><h1>CamVault 控制台</h1><p class="lead">状态、配置、日志、实时画面与历史回放</p></div><span class="pill" id="overall">正在连接…</span></div>
<section class="metrics"><article class="metric"><small>存储后端</small><strong id="storage">—</strong></article><article class="metric"><small>RAM 媒体缓冲</small><strong id="memory">—</strong></article><article class="metric"><small>最近清理</small><strong id="retention">—</strong></article></section>
<section class="viewer"><div class="playback-bar"><div class="mode"><button id="modeLive" class="active">全部实时</button><button id="modeHistory">历史回放</button></div><label>从 <input id="historyStart" type="datetime-local" disabled></label><label>到 <input id="historyEnd" type="datetime-local" disabled></label><button id="applyHistory" disabled>播放所选时间</button><small id="playbackNotice">实时画面直接从内存缓冲转发</small></div><div class="grid">{"".join(cards)}</div></section><section class="tools"><article class="panel"><h2>配置文件</h2><textarea id="config" spellcheck="false" aria-label="CamVault TOML configuration"></textarea><div class="actions"><button id="save">校验并保存</button><button class="secondary" id="reload">重新读取</button><span id="notice">保存后需重启服务生效</span></div></article>
<article class="panel"><h2>诊断与清理</h2><pre id="logs">正在读取内存日志…</pre><div class="actions"><button class="secondary" id="refreshLogs">刷新日志</button><button class="danger" id="cleanup">立即清理旧录像</button></div></article></section>
<footer>配置写入采用修订号校验、原子替换并保留 <code>config.toml.bak</code>。公网访问请放在 WireGuard / Tailscale / HTTPS 反向代理之后。</footer></main>
<script>
const token={token_json},cameras={cameras_json};let revision=null,lastLog=0,currentMode='live';const players=new Map();
const headers=()=>token?{{'X-CamVault-Token':token}}:{{}};
const human=n=>{{if(n===null||n===undefined)return '—';const u=['B','KiB','MiB','GiB','TiB'];let i=0;while(n>=1024&&i<u.length-1){{n/=1024;i++}}return n.toFixed(i?1:0)+' '+u[i]}};
async function api(path,options={{}}){{options.headers=Object.assign({{}},headers(),options.headers||{{}});const r=await fetch(path,options);let data;try{{data=await r.json()}}catch(_e){{data={{detail:await r.text()}}}}if(!r.ok)throw new Error(data.detail||('HTTP '+r.status));return data}}
function localValue(date){{const shifted=new Date(date.getTime()-date.getTimezoneOffset()*60000);return shifted.toISOString().slice(0,16)}}
function message(id,text){{const box=document.getElementById('message-'+id);if(box)box.textContent=text}}
function destroyPlayer(id){{const current=players.get(id);if(current&&current.hls)current.hls.destroy();players.delete(id);const video=document.getElementById('video-'+id);if(video){{video.pause();video.removeAttribute('src');video.load()}}}}
function streamUrl(camera){{const base=currentMode==='live'?('/live/'+camera.id+'/index.m3u8'):('/vod/'+camera.id+'/index.m3u8');const params=new URLSearchParams();if(currentMode==='history'){{params.set('start',new Date(document.getElementById('historyStart').value).toISOString());params.set('end',new Date(document.getElementById('historyEnd').value).toISOString())}}if(token)params.set('token',token);const query=params.toString();return base+(query?'?'+query:'')}}
function attachCamera(camera){{if(!camera.enabled)return;destroyPlayer(camera.id);const video=document.getElementById('video-'+camera.id),src=streamUrl(camera),label=currentMode==='live'?'实时':'历史';message(camera.id,'正在连接'+label+'画面…');if(window.Hls&&Hls.isSupported()){{const hls=new Hls({{enableWorker:true,lowLatencyMode:currentMode==='live',liveSyncDurationCount:2,liveMaxLatencyDurationCount:5,maxLiveSyncPlaybackRate:1.5,maxBufferLength:currentMode==='live'?12:120,backBufferLength:currentMode==='live'?0:60}});players.set(camera.id,{{hls,src}});hls.loadSource(src);hls.attachMedia(video);hls.on(Hls.Events.MANIFEST_PARSED,()=>{{if(players.get(camera.id)?.hls!==hls)return;message(camera.id,'');video.play().catch(()=>{{message(camera.id,label+'已就绪，点击画面播放')}})}});hls.on(Hls.Events.ERROR,(_event,data)=>{{if(players.get(camera.id)?.hls!==hls||!data.fatal)return;if(data.type===Hls.ErrorTypes.NETWORK_ERROR){{message(camera.id,'网络中断，正在重试…');setTimeout(()=>{{if(players.get(camera.id)?.hls===hls)hls.startLoad()}},1500)}}else if(data.type===Hls.ErrorTypes.MEDIA_ERROR){{message(camera.id,'媒体解码恢复中…');hls.recoverMediaError()}}else{{message(camera.id,'无法播放：'+data.details);destroyPlayer(camera.id)}}}})}}else if(video.canPlayType('application/vnd.apple.mpegurl')){{video.src=src;players.set(camera.id,{{hls:null,src}});video.addEventListener('loadedmetadata',()=>{{message(camera.id,'');video.play().catch(()=>{{message(camera.id,label+'已就绪，点击画面播放')}})}},{{once:true}});video.addEventListener('error',()=>message(camera.id,'无法加载'+label+'画面'),{{once:true}})}}else{{message(camera.id,'浏览器不支持 HLS')}}}}
function attachAll(){{for(const camera of cameras)attachCamera(camera)}}
function validHistory(){{const start=new Date(document.getElementById('historyStart').value),end=new Date(document.getElementById('historyEnd').value);return !isNaN(start)&&!isNaN(end)&&start<end}}
function setMode(mode){{currentMode=mode;const history=mode==='history';document.getElementById('modeLive').classList.toggle('active',!history);document.getElementById('modeHistory').classList.toggle('active',history);for(const id of ['historyStart','historyEnd','applyHistory'])document.getElementById(id).disabled=!history;document.getElementById('playbackNotice').textContent=history?'选择时间后，所有摄像头会同步回放':'实时画面直接从内存缓冲转发';if(!history||validHistory())attachAll()}}
async function refresh(){{try{{const data=await api('/api/status'+(token?'?token='+encodeURIComponent(token):''));document.getElementById('overall').textContent=data.status==='ok'?'服务正常':'服务异常';document.getElementById('storage').textContent=data.storage.backend;document.getElementById('memory').textContent=human(data.bounded_media_memory_bytes);const rr=data.retention;document.getElementById('retention').textContent=rr.last_error?'失败':(rr.last_completed_at?(rr.deleted_files+' 个 / '+human(rr.deleted_bytes)):'等待首次运行');for(const c of data.cameras){{const s=document.getElementById('state-'+c.id),d=document.getElementById('dot-'+c.id);if(s)s.textContent=c.state+(c.detail?' · '+c.detail:'');if(d)d.style.background=c.state==='recording'?'var(--ok)':(c.state==='disabled'?'var(--muted)':'var(--bad)')}}}}catch(e){{document.getElementById('overall').textContent='连接失败'}}}}
async function loadConfig(){{const n=document.getElementById('notice');try{{const data=await api('/api/config');document.getElementById('config').value=data.content;revision=data.revision;n.textContent='已读取当前配置'}}catch(e){{n.textContent='读取失败：'+e.message}}}}
async function saveConfig(){{const b=document.getElementById('save'),n=document.getElementById('notice');b.disabled=true;try{{const data=await api('/api/config',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{content:document.getElementById('config').value,revision}})}});revision=data.revision;n.textContent='保存成功；请重启 CamVault 使配置生效'}}catch(e){{n.textContent='保存失败：'+e.message}}finally{{b.disabled=false}}}}
async function loadLogs(){{try{{const data=await api('/api/logs?limit=500');const box=document.getElementById('logs');box.textContent=data.entries.map(e=>e.message).join('\\n')||'暂无日志';if(data.entries.length)lastLog=data.entries[data.entries.length-1].sequence;box.scrollTop=box.scrollHeight}}catch(e){{document.getElementById('logs').textContent='读取失败：'+e.message}}}}
async function cleanup(){{const b=document.getElementById('cleanup');b.disabled=true;try{{const d=await api('/api/retention/run',{{method:'POST'}});alert('清理完成：删除 '+d.deleted_files+' 个文件，释放 '+human(d.deleted_bytes))}}catch(e){{alert('清理失败：'+e.message)}}finally{{b.disabled=false;refresh();loadLogs()}}}}
const now=new Date();document.getElementById('historyEnd').value=localValue(now);document.getElementById('historyStart').value=localValue(new Date(now-3600000));document.getElementById('modeLive').onclick=()=>setMode('live');document.getElementById('modeHistory').onclick=()=>setMode('history');document.getElementById('applyHistory').onclick=()=>{{if(!validHistory()){{document.getElementById('playbackNotice').textContent='请选择有效的起止时间';return}}attachAll()}};document.addEventListener('visibilitychange',()=>{{for(const camera of cameras){{const current=players.get(camera.id),video=document.getElementById('video-'+camera.id);if(!current||!video)continue;if(document.hidden){{video.pause();if(current.hls)current.hls.stopLoad()}}else{{if(current.hls)current.hls.startLoad(currentMode==='live'?-1:video.currentTime);video.play().catch(()=>{{}})}}}}}});window.addEventListener('beforeunload',()=>{{for(const camera of cameras)destroyPlayer(camera.id)}});document.getElementById('save').onclick=saveConfig;document.getElementById('reload').onclick=loadConfig;document.getElementById('refreshLogs').onclick=loadLogs;document.getElementById('cleanup').onclick=cleanup;refresh();loadConfig();loadLogs();attachAll();setInterval(refresh,3000);setInterval(loadLogs,10000);
</script>
</body></html>"""
        return HTMLResponse(page)

    @app.get(
        "/player/{camera_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_playback_auth)],
    )
    async def player(camera_id: str, request: Request) -> HTMLResponse:
        camera = service.camera_map.get(camera_id)
        if camera is None:
            raise HTTPException(status_code=404, detail="unknown camera")
        token = request.query_params.get("token", "")
        token_suffix = f"?token={quote(token, safe='')}" if token else ""
        live_url = f"/live/{camera_id}/index.m3u8{token_suffix}"
        vod_base = f"/vod/{camera_id}/index.m3u8"
        page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(camera.name or camera_id)} · CamVault</title><script src="https://cdn.jsdelivr.net/npm/hls.js@1.7.2/dist/hls.min.js"></script><style>
body{{margin:0;background:#080c16;color:#eef3fb;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}}main{{max-width:1100px;margin:auto;padding:24px}}video{{width:100%;background:#000;border-radius:14px;max-height:70vh}}a{{color:#87b6ff}}.bar,.controls{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}}.bar{{margin-bottom:16px}}.controls{{justify-content:flex-start;background:#141c2e;border:1px solid #293653;border-radius:12px;padding:12px;margin-top:14px}}button,input{{font:inherit;border-radius:8px;border:1px solid #364766;padding:8px 10px}}button{{background:#3979ef;color:white;cursor:pointer}}input{{background:#0a101d;color:#eef3fb}}#message{{color:#aebbd0;margin:12px 0}}
</style></head><body><main><div class="bar"><h2>{html.escape(camera.name or camera_id)}</h2><a href="/{token_suffix}">返回控制台</a></div><video id="video" controls autoplay muted playsinline></video><p id="message">正在连接直播流…</p><div class="controls"><button id="live">实时画面</button><label>从 <input id="start" type="datetime-local"></label><label>到 <input id="end" type="datetime-local"></label><button id="playback">回放</button><a id="external" href="#">外部播放器 M3U8</a></div></main>
<script>const video=document.getElementById('video'),liveSrc={_script_json(live_url)},vodBase={_script_json(vod_base)},token={_script_json(token)},msg=document.getElementById('message');let hls=null;
function attach(src,label){{if(hls){{hls.destroy();hls=null}}video.removeAttribute('src');video.load();document.getElementById('external').href=src;msg.textContent='正在连接'+label+'…';if(window.Hls&&Hls.isSupported()){{hls=new Hls({{liveSyncDurationCount:3,maxLiveSyncPlaybackRate:1.5}});hls.loadSource(src);hls.attachMedia(video);hls.on(Hls.Events.MANIFEST_PARSED,()=>{{msg.textContent=label+'已连接';video.play().catch(()=>{{}})}});hls.on(Hls.Events.ERROR,(_e,d)=>{{msg.textContent='播放错误：'+d.details+'。若摄像头是 H.265，请改用 VLC，或把 video_codec 设为 h264。'}})}}else if(video.canPlayType('application/vnd.apple.mpegurl')){{video.src=src;video.addEventListener('loadedmetadata',()=>{{msg.textContent=label+'已连接';video.play().catch(()=>{{}})}},{{once:true}})}}else{{msg.textContent='此浏览器不支持 HLS，请使用 VLC/IINA 打开下方 M3U8。'}}}}
function localValue(date){{const shifted=new Date(date.getTime()-date.getTimezoneOffset()*60000);return shifted.toISOString().slice(0,16)}}const now=new Date();document.getElementById('end').value=localValue(now);document.getElementById('start').value=localValue(new Date(now-3600000));
document.getElementById('live').onclick=()=>attach(liveSrc,'直播');document.getElementById('playback').onclick=()=>{{const start=new Date(document.getElementById('start').value),end=new Date(document.getElementById('end').value);if(isNaN(start)||isNaN(end)||start>=end){{msg.textContent='请选择有效的回放起止时间';return}}const p=new URLSearchParams({{start:start.toISOString(),end:end.toISOString()}});if(token)p.set('token',token);attach(vodBase+'?'+p.toString(),'回放')}};attach(liveSrc,'直播');
</script></body></html>"""
        return HTMLResponse(page)

    @app.exception_handler(KeyError)
    async def key_error_handler(_request: Request, _exc: KeyError) -> PlainTextResponse:
        return PlainTextResponse("not found", status_code=404)

    return app
