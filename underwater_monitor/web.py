from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import ServerConfig
from .processing import SettingsStore
from .service import CaptureService, FrameSnapshot

LOGGER = logging.getLogger(__name__)
ASSET_DIRECTORY = Path(__file__).with_name("assets")
MAX_SETTINGS_BODY = 16 * 1024


@dataclass(frozen=True)
class WebContext:
    service: CaptureService
    settings: SettingsStore
    config: ServerConfig


class MonitoringServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(context: WebContext) -> MonitoringServer:
    class Handler(MonitoringHandler):
        app = context

    return MonitoringServer((context.config.host, context.config.port), Handler)


class MonitoringHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app: WebContext

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        if parsed.path in {"/healthz", "/readyz"}:
            self._handle_probe(parsed.path)
            return
        if not self._authorized():
            self._request_authentication()
            return

        if parsed.path == "/":
            self._serve_asset("index.html", "text/html; charset=utf-8")
        elif parsed.path == "/app.js":
            self._serve_asset("app.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/style.css":
            self._serve_asset("style.css", "text/css; charset=utf-8")
        elif parsed.path == "/api/status":
            self._send_json(HTTPStatus.OK, self._status())
        elif parsed.path == "/api/settings":
            self._send_json(HTTPStatus.OK, self.app.settings.get().as_dict())
        elif parsed.path == "/snapshot.jpg":
            self._handle_snapshot(parse_qs(parsed.query))
        elif parsed.path == "/stream.mjpg":
            self._handle_stream(parse_qs(parsed.query))
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        if not self._authorized():
            self._request_authentication()
            return
        if parsed.path != "/api/settings":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length required"})
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
            return
        if length < 0 or length > MAX_SETTINGS_BODY:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            updated = self.app.settings.update(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, updated.as_dict())

    def _handle_probe(self, path: str) -> None:
        status = self._status()
        if path == "/healthz":
            healthy = self.app.service.is_alive()
        else:
            healthy = bool(status["healthy"])
        self._send_json(
            HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
            {"ok": healthy, "state": status["state"]},
        )

    def _handle_snapshot(self, query: dict[str, list[str]]) -> None:
        mode = _mode_from_query(query)
        if mode is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "mode must be original or enhanced"})
            return
        snapshot = self.app.service.frames.latest()
        if snapshot is None or self._is_stale(snapshot):
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "fresh frame unavailable"})
            return
        image = snapshot.original if mode == "original" else snapshot.enhanced
        self._send_bytes(
            HTTPStatus.OK,
            image,
            "image/jpeg",
            extra_headers={"Content-Disposition": f'inline; filename="{mode}-{snapshot.frame_id}.jpg"'},
        )

    def _handle_stream(self, query: dict[str, list[str]]) -> None:
        mode = _mode_from_query(query)
        if mode is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "mode must be original or enhanced"})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self._send_security_headers()
        self.end_headers()

        frame_id = 0
        try:
            while True:
                snapshot = self.app.service.frames.wait_after(frame_id, timeout=10.0)
                if snapshot is None:
                    if not self.app.service.is_alive():
                        break
                    continue
                frame_id = snapshot.frame_id
                image = snapshot.original if mode == "original" else snapshot.enhanced
                part = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(image)}\r\n\r\n".encode("ascii")
                    + image
                    + b"\r\n"
                )
                self.wfile.write(part)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def _status(self) -> dict[str, Any]:
        result = self.app.service.frames.status(
            self.app.config.frame_stale_seconds,
            self.app.service.source.name,
        )
        result["capture_thread_alive"] = self.app.service.is_alive()
        return result

    def _is_stale(self, snapshot: FrameSnapshot) -> bool:
        return (time.time() - snapshot.captured_at) > self.app.config.frame_stale_seconds

    def _authorized(self) -> bool:
        username = self.app.config.username
        password = self.app.config.password
        if username is None or password is None:
            return True
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            supplied_username, supplied_password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return secrets.compare_digest(supplied_username, username) and secrets.compare_digest(
            supplied_password, password
        )

    def _request_authentication(self) -> None:
        body = json.dumps({"error": "authentication required"}).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Underwater Monitor", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_asset(self, filename: str, content_type: str) -> None:
        try:
            body = (ASSET_DIRECTORY / filename).read_bytes()
        except OSError:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "asset unavailable"})
            return
        self._send_bytes(HTTPStatus.OK, body, content_type)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'",
        )

    def log_message(self, message: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.client_address[0], message % args)


def _mode_from_query(query: dict[str, list[str]]) -> str | None:
    values = query.get("mode", ["enhanced"])
    if len(values) != 1 or values[0] not in {"original", "enhanced"}:
        return None
    return values[0]
