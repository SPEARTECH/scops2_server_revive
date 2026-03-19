#!/usr/bin/env python3
"""Minimal HTTP server for capturing/responding to PS2 Ubisoft GS init bootstrap.

This is intentionally simple:
- Listens on a host/port (default 0.0.0.0:80)
- Responds to GET /gsinit.php with the contents of a local file (default: ../gsinit.php)
- Logs requests (client IP, path, query string)

Why:
- The PS2 client is observed doing a TCP SYN to port 80 after resolving
  gsconnect.ubisoft.com to your LAN IP.
- If nothing is listening, Windows sends a TCP RST and the game fails early.

Usage (run in an elevated shell if binding to port 80 on Windows):
  python tools/gs_http_server.py --bind 0.0.0.0 --port 80 --gsinit-file gsinit.php

Optional static file serving:
    python tools/gs_http_server.py --bind 0.0.0.0 --port 80 --gsinit-file gsinit.php --web-root webroot --allow-any

Notes:
- This does NOT implement authentication or game services; it only unblocks the
  gsinit bootstrap and helps you observe what the client requests next.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TextIO
from urllib.parse import parse_qs, urlparse


def _guess_content_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".xml"):
        return "application/xml; charset=utf-8"
    if lower.endswith(".ini"):
        return "text/plain; charset=utf-8"
    if lower.endswith(".txt"):
        return "text/plain; charset=utf-8"
    if lower.endswith(".htm") or lower.endswith(".html"):
        return "text/html; charset=utf-8"
    return "application/octet-stream"


def _safe_join(web_root: pathlib.Path, url_path: str) -> pathlib.Path | None:
    """Map a URL path to a filesystem path under web_root.

    Returns None if the path is unsafe (path traversal) or maps outside web_root.
    """

    # URL paths are absolute and use '/'. We strip leading '/' and disallow '..'.
    rel = url_path.lstrip("/")
    if not rel or rel.startswith("..") or "/.." in rel or "\\.." in rel:
        return None
    try:
        candidate = (web_root / rel).resolve(strict=False)
        root = web_root.resolve(strict=False)
    except OSError:
        return None
    if candidate == root or root not in candidate.parents:
        return None
    return candidate


def now_ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log_line(msg: str, *, log_fp: TextIO | None = None) -> None:
    print(msg, flush=True)
    if log_fp is not None:
        log_fp.write(msg + "\n")
        log_fp.flush()


class _Handler(BaseHTTPRequestHandler):
    server_version = "SCCT-GSHTTP/0.1"

    def log_message(self, fmt: str, *args) -> None:
        # Silence default noisy logging; we print our own structured logs.
        return

    def _send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8", errors="replace")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query
        qs = parse_qs(query, keep_blank_values=True)

        client_ip, client_port = self.client_address[0], self.client_address[1]
        cfg = getattr(self.server, "_cfg")  # injected
        log_fp = cfg.log_fp

        log_line(f"[{now_ts()}] HTTP {client_ip}:{client_port}  GET {path}?{query}", log_fp=log_fp)
        if qs:
            # Print a stable-ish view of query params.
            for k in sorted(qs.keys()):
                v = qs[k]
                if len(v) == 1:
                    log_line(f"  q[{k}] = {v[0]!r}", log_fp=log_fp)
                else:
                    log_line(f"  q[{k}] = {v!r}", log_fp=log_fp)

        if path.rstrip("/") == "/gsinit.php":
            try:
                text = cfg.gsinit_path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                self._send_text(500, f"ERROR reading gsinit file: {e}\n")
                return
            self._send_text(200, text + ("\n" if not text.endswith("\n") else ""))
            cfg.handled += 1
        else:
            served_static = False
            if cfg.web_root is not None:
                fs_path = _safe_join(cfg.web_root, path)
                if fs_path is not None and fs_path.is_file():
                    try:
                        data = fs_path.read_bytes()
                    except OSError as e:
                        self._send_text(500, f"ERROR reading file: {e}\n")
                        return
                    self._send_bytes(200, data, _guess_content_type(str(fs_path)))
                    served_static = True

            if not served_static:
                if cfg.allow_any:
                    self._send_text(200, "ok\n")
                else:
                    self._send_text(404, "not found\n")
            cfg.handled += 1

        if cfg.max_requests and cfg.handled >= cfg.max_requests:
            # Graceful shutdown after responding.
            print(f"Stopping (handled {cfg.handled} requests).")
            try:
                self.server.shutdown()
            except Exception:
                pass


class _Config:
    def __init__(
        self,
        gsinit_path: pathlib.Path,
        max_requests: int,
        allow_any: bool,
        web_root: pathlib.Path | None,
        log_fp: TextIO | None,
    ) -> None:
        self.gsinit_path = gsinit_path
        self.max_requests = max_requests
        self.handled = 0
        self.allow_any = allow_any
        self.web_root = web_root
        self.log_fp = log_fp


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Minimal HTTP server for /gsinit.php")
    ap.add_argument("--bind", default="0.0.0.0", help="IP to bind (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=80, help="TCP port (default: 80)")
    ap.add_argument(
        "--gsinit-file",
        default="gsinit.php",
        help="Path to response body for /gsinit.php (default: gsinit.php in workspace root)",
    )
    ap.add_argument(
        "--max-requests",
        type=int,
        default=0,
        help="If >0, exit after handling this many HTTP requests (useful for quick tests)",
    )
    ap.add_argument(
        "--allow-any",
        action="store_true",
        help="If set, return 200 OK for any GET path (body 'ok'), not just /gsinit.php",
    )
    ap.add_argument(
        "--web-root",
        default="",
        help="If set, serve static files from this directory for any GET path (in addition to /gsinit.php)",
    )
    ap.add_argument(
        "--log-file",
        default="",
        help="If set, append request logs to this file",
    )
    args = ap.parse_args(argv)

    if not (1 <= args.port <= 65535):
        print("ERROR: invalid port")
        return 2

    gsinit_path = pathlib.Path(args.gsinit_file)
    if not gsinit_path.exists():
        print(f"ERROR: gsinit file does not exist: {gsinit_path}")
        return 2

    web_root: pathlib.Path | None = None
    if str(args.web_root).strip():
        candidate = pathlib.Path(str(args.web_root))
        if not candidate.exists() or not candidate.is_dir():
            print(f"ERROR: web root is not a directory: {candidate}")
            return 2
        web_root = candidate

    log_fp: TextIO | None = None
    if str(args.log_file).strip():
        try:
            log_fp = pathlib.Path(str(args.log_file)).open("a", encoding="utf-8")
        except OSError as e:
            print(f"ERROR: failed to open log file: {e}")
            return 2

    httpd = ThreadingHTTPServer((args.bind, int(args.port)), _Handler)
    httpd._cfg = _Config(
        gsinit_path=gsinit_path,
        max_requests=int(args.max_requests),
        allow_any=bool(args.allow_any),
        web_root=web_root,
        log_fp=log_fp,
    )  # type: ignore[attr-defined]

    log_line("GS HTTP server listening", log_fp=log_fp)
    log_line(f"  bind:  {args.bind}:{args.port}", log_fp=log_fp)
    log_line(f"  file:  {gsinit_path}", log_fp=log_fp)
    if web_root is not None:
        log_line(f"  web:   {web_root}", log_fp=log_fp)
    if log_fp is not None:
        log_line(f"  log:   {pathlib.Path(str(args.log_file))}", log_fp=log_fp)
    log_line("  ctrl+c to stop", log_fp=log_fp)

    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
        if log_fp is not None:
            try:
                log_fp.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
