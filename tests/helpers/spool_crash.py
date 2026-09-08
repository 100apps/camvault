"""Disposable child killed by the durable-outbox crash recovery test."""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from camvault.archive import ArchiveBatch
from camvault.buffer import LiveSegment
from camvault.config import StorageConfig
from camvault.storage import create_storage_backend


async def main() -> None:
    async def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("injected outage", request=request)

    storage = StorageConfig.model_validate_json(Path(sys.argv[1]).read_text())
    client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
    backend = create_storage_backend(storage, ["front"], client=client)
    segment = LiveSegment(0, "s.ts", b"durable-before-kill", datetime.now(UTC), 2, "crash")
    await backend.write_batch(ArchiveBatch(camera_id="front", segments=(segment,)))
    print("DURABLE", flush=True)
    await asyncio.Event().wait()


asyncio.run(main())
