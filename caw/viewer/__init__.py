"""Trajectory viewer web interface.

Provides a simple HTTP server that serves a self-contained trajectory viewer.
The viewer loads trajectory JSON files by absolute path.

Usage:

    from caw.viewer import start_viewer_server

    server = start_viewer_server()          # auto host/port
    server = start_viewer_server(port=8080) # fixed port
    print(server.url)                       # http://localhost:8080
    server.check_status()                   # True / False
    server.stop()
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

__all__ = ["ViewerServer", "start_viewer_server"]

STATIC_DIR = Path(__file__).parent / "static"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class ViewerServer:
    """Handle for a running trajectory viewer server."""

    def __init__(
        self,
        httpd: HTTPServer,
        thread: threading.Thread,
        host: str,
        port: int,
    ) -> None:
        self._httpd = httpd
        self._thread = thread
        self.host = host
        self.port = port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def check_status(self) -> bool:
        """Return True if the server is running."""
        return self._thread.is_alive()

    def stop(self) -> None:
        """Shut down the server."""
        self._httpd.shutdown()
        self._thread.join(timeout=5)

    def __repr__(self) -> str:
        status = "running" if self.check_status() else "stopped"
        return f"ViewerServer({self.url}, {status})"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass  # suppress request logging

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_file(STATIC_DIR / "index.html", "text/html")
        elif path == "/api/trajectory":
            self._handle_trajectory(parse_qs(parsed.query))
        else:
            self.send_error(404)

    def _send_json(self, data, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filepath: Path, content_type: str):
        if not filepath.is_file():
            self.send_error(404)
            return
        content = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _handle_trajectory(self, params):
        paths = params.get("path", [])
        if not paths:
            self._send_json({"detail": "Missing 'path' parameter"}, 400)
            return
        filepath = Path(paths[0]).resolve()
        if not filepath.is_file():
            self._send_json({"detail": f"File not found: {paths[0]}"}, 404)
            return
        try:
            raw = filepath.read_text()
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json({"detail": f"Invalid JSON: {e}"}, 500)
            return
        self._send_json(data)


def start_viewer_server(
    host: str | None = None,
    port: int | None = None,
) -> ViewerServer:
    """Start a trajectory viewer web server.

    Args:
        host: Host to bind to.  Defaults to ``"localhost"``.
        port: Port to bind to.  If *None*, a free port is chosen automatically.

    Returns:
        A `ViewerServer` handle.
    """
    host = host or "localhost"
    port = port or _find_free_port()

    httpd = _ThreadedHTTPServer((host, port), _Handler)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    return ViewerServer(httpd, thread, host, port)
