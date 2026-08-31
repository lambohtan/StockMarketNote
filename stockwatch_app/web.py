"""Loopback-only HTTP API and static dashboard server."""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from stockwatch_app.config import ConfigError
from stockwatch_app.controller import ApplicationController
from stockwatch_app.tasks import TaskError


class DashboardServer:
    def __init__(
        self,
        controller: ApplicationController,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        assets_root: Path | None = None,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("StockWatch dashboard must bind to 127.0.0.1")
        self.controller = controller
        self.assets_root = Path(assets_root or Path(__file__).with_name("assets")).resolve()
        self.token = secrets.token_urlsafe(32)
        handler = self._handler_type()
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="stockwatch-http", daemon=True
        )

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _handler_type(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "StockWatch/0.1"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:
                # Never print request headers, tokens, query values, or article contents.
                print(f"[dashboard] {self.address_string()} {fmt % args}")

            def do_GET(self) -> None:  # noqa: N802
                try:
                    self._get()
                except (TaskError, ValueError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"{type(exc).__name__}: {exc}"},
                    )

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized_mutation():
                    self._json(HTTPStatus.FORBIDDEN, {"error": "invalid local session"})
                    return
                try:
                    self._post()
                except (TaskError, ConfigError, ValueError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"{type(exc).__name__}: {exc}"},
                    )

            def _get(self) -> None:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                path = parsed.path
                if path == "/healthz":
                    self._json(HTTPStatus.OK, {"ok": True, "service": "stockwatch"})
                elif path == "/api/v1/bootstrap":
                    payload = outer.controller.bootstrap()
                    payload["csrf_token"] = outer.token
                    self._json(HTTPStatus.OK, payload)
                elif path == "/api/v1/config":
                    self._json(HTTPStatus.OK, outer.controller.load_config())
                elif path == "/api/v1/pool":
                    self._json(HTTPStatus.OK, outer.controller.pool(_integer(query, "limit", 100)))
                elif path == "/api/v1/analyses":
                    self._json(
                        HTTPStatus.OK, outer.controller.analyses(_integer(query, "limit", 100))
                    )
                elif path == "/api/v1/analysis":
                    self._json(
                        HTTPStatus.OK,
                        outer.controller.analysis_article(_required(query, "id")),
                    )
                elif path == "/api/v1/reddit":
                    self._json(HTTPStatus.OK, outer.controller.reddit(_integer(query, "limit", 50)))
                elif path == "/api/v1/runs":
                    self._json(HTTPStatus.OK, outer.controller.runs(_integer(query, "limit", 50)))
                elif path == "/api/v1/log":
                    self._json(HTTPStatus.OK, outer.controller.log(_required(query, "run_id")))
                else:
                    self._static(path)

            def _post(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/")
                body = self._body()
                if path == "/api/v1/config":
                    self._json(HTTPStatus.OK, outer.controller.save_config(body))
                    return
                match = _task_action(path)
                if match:
                    task_name, action = match
                    payload = (
                        outer.controller.run_task(task_name)
                        if action == "run"
                        else outer.controller.stop_task(task_name)
                    )
                    self._json(HTTPStatus.ACCEPTED, payload)
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})

            def _authorized_mutation(self) -> bool:
                host = self.headers.get("Host", "")
                if host not in {f"127.0.0.1:{outer.port}", f"localhost:{outer.port}"}:
                    return False
                origin = self.headers.get("Origin")
                if origin not in {f"http://127.0.0.1:{outer.port}", f"http://localhost:{outer.port}"}:
                    return False
                return secrets.compare_digest(
                    self.headers.get("X-StockWatch-Token", ""), outer.token
                )

            def _body(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise ValueError("invalid content length") from exc
                if not 0 <= length <= 1_000_000:
                    raise ValueError("request body is too large")
                if length == 0:
                    return {}
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("content type must be application/json")
                try:
                    payload = json.loads(self.rfile.read(length))
                except json.JSONDecodeError as exc:
                    raise ValueError("invalid JSON") from exc
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                return payload

            def _json(self, status: HTTPStatus, payload: Any) -> None:
                encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self._security_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _static(self, raw_path: str) -> None:
                relative = unquote(raw_path).lstrip("/") or "index.html"
                if relative not in {"index.html", "app.css", "app.js"}:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                path = (outer.assets_root / relative).resolve()
                if path.parent != outer.assets_root or not path.is_file():
                    self._json(HTTPStatus.NOT_FOUND, {"error": "asset not found"})
                    return
                data = path.read_bytes()
                media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self._security_headers()
                self.send_header("Content-Type", f"{media}; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _security_headers(self) -> None:
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                    "form-action 'self'; frame-ancestors 'none'",
                )

        return Handler


def _integer(query: dict[str, list[str]], name: str, default: int) -> int:
    raw = query.get(name, [str(default)])[0]
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _required(query: dict[str, list[str]], name: str) -> str:
    value = query.get(name, [""])[0]
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _task_action(path: str) -> tuple[str, str] | None:
    parts = path.strip("/").split("/")
    if len(parts) != 5 or parts[:3] != ["api", "v1", "tasks"]:
        return None
    # Expected: api/v1/tasks/<task-name>/<run|stop>
    task_name, action = parts[3], parts[4]
    if action not in {"run", "stop"}:
        return None
    return task_name, action
