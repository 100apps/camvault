from __future__ import annotations

import logging
import signal
import socket
from types import FrameType

import uvicorn

from camvault.service import CamVaultService

logger = logging.getLogger(__name__)


class CamVaultServer(uvicorn.Server):
    """Drain recorders while HTTP ingest is still listening after a stop signal."""

    def __init__(self, config: uvicorn.Config, service: CamVaultService) -> None:
        super().__init__(config)
        self.service = service

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        logger.info("received %s; preparing final archive upload", signal.Signals(sig).name)
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        # Uvicorn normally closes listeners before lifespan shutdown. FFmpeg needs the
        # listener for the final segment emitted on termination, so stop/drain first.
        # The lifespan call to service.stop() afterwards is deliberately idempotent.
        try:
            await self.service.stop()
        finally:
            await super().shutdown(sockets=sockets)
