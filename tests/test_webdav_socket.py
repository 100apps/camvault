from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import pytest

from camvault.config import StorageConfig, WebDAVConfig
from camvault.storage import WebDAVStorageBackend


class _SocketWebDAVHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    directories: ClassVar[set[str]] = {"/dav"}
    files: ClassVar[dict[str, bytes]] = {}
    expected_authorization = "Basic " + base64.b64encode(b"camvault:secret").decode("ascii")

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _reply(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == self.expected_authorization:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="CamVault test"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_OPTIONS(self) -> None:
        if not self._authorized():
            return
        self.send_response(204)
        self.send_header("DAV", "1, 2")
        self.send_header("Allow", "OPTIONS, MKCOL, PUT, MOVE, GET, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_MKCOL(self) -> None:
        if not self._authorized():
            return
        path = self.path.rstrip("/")
        if path in self.directories:
            self._reply(405)
            return
        parent = str(Path(path).parent).replace("\\", "/")
        if parent not in self.directories:
            self._reply(409)
            return
        self.directories.add(path)
        self._reply(201)

    def do_PUT(self) -> None:
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length", "-1"))
        if length < 0:
            self._reply(411)
            return
        payload = self.rfile.read(length)
        if len(payload) != length:
            self._reply(400)
            return
        self.files[self.path] = payload
        self._reply(201)

    def do_MOVE(self) -> None:
        if not self._authorized():
            return
        destination = urlsplit(self.headers.get("Destination", "")).path
        if self.path not in self.files:
            self._reply(404)
            return
        self.files[destination] = self.files.pop(self.path)
        self._reply(201)

    def do_GET(self) -> None:
        if not self._authorized():
            return
        payload = self.files.get(self.path)
        if payload is None:
            self._reply(404)
            return
        self._reply(200, payload)

    def do_DELETE(self) -> None:
        if not self._authorized():
            return
        if self.path in self.files:
            self.files.pop(self.path)
            self._reply(204)
            return
        self._reply(404)


@pytest.mark.asyncio
async def test_webdav_health_check_over_real_tcp_without_local_spool(tmp_path: Path) -> None:
    handler = type(
        "IsolatedSocketWebDAVHandler",
        (_SocketWebDAVHandler,),
        {"directories": {"/dav"}, "files": {}},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    storage = StorageConfig(
        backend="webdav",
        root=tmp_path / "must-not-exist",
        min_free_gb=0,
        webdav=WebDAVConfig(
            url=f"http://127.0.0.1:{port}/dav",
            root="/Cloud/CamVault",
            username="camvault",
            username_env=None,
            password="secret",
            password_env=None,
        ),
    )
    backend = WebDAVStorageBackend(storage, ["front"])
    try:
        health = await backend.health_check()
        assert health.diskless_media_path is True
        assert not storage.root.exists()
        assert not handler.files
    finally:
        await backend.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
