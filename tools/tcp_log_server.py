#!/usr/bin/env python3
"""TCP logging server for protocol RE.

Listens on one or more TCP ports and logs:
- connect/disconnect
- first N bytes sent by the client (hex + ASCII)

Why:
- After the client downloads gsinit.php, it resolves other Ubisoft hosts (router/nat/irc/cdkey)
  and connects to various ports (e.g. 40000/40005/45000/44000/6668).
- Before implementing a real emulator, this lets us observe what the client sends.

Usage:
  python tools/tcp_log_server.py --bind 0.0.0.0 --ports 40000,40005,45000,44000,6668

Notes:
- This tool does not attempt to speak the protocol; it just logs and then closes the connection
  after an idle timeout.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import pathlib
import socket
import sys
import threading
from typing import Iterable


def log_line(msg: str, log_fp: "TextIO" | None = None) -> None:
    print(msg, flush=True)
    if log_fp is not None:
        log_fp.write(msg + "\n")
        log_fp.flush()


def parse_ports(s: str) -> list[int]:
    ports: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        p = int(part, 10)
        if not (1 <= p <= 65535):
            raise ValueError(f"Invalid port: {p}")
        ports.append(p)
    if not ports:
        raise ValueError("No ports provided")
    seen: set[int] = set()
    out: list[int] = []
    for p in ports:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def hexdump(b: bytes, width: int = 16, max_len: int = 512) -> str:
    b = b[:max_len]
    lines: list[str] = []
    for off in range(0, len(b), width):
        chunk = b[off : off + width]
        hex_part = " ".join(f"{x:02x}" for x in chunk)
        ascii_part = "".join(chr(x) if 32 <= x <= 126 else "." for x in chunk)
        lines.append(f"{off:04x}  {hex_part:<{width*3}}  {ascii_part}")
    if len(b) >= max_len:
        lines.append(f"… (truncated to {max_len} bytes)")
    return "\n".join(lines)


def now_ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _u16be(b: bytes, off: int) -> int:
    return (b[off] << 8) | b[off + 1]


def _u24be(b: bytes, off: int) -> int:
    return (b[off] << 16) | (b[off + 1] << 8) | b[off + 2]


def try_parse_http_host(payload: bytes) -> str | None:
    # Very small heuristic: only attempt if it looks like an HTTP request line.
    if not payload:
        return None
    if payload.startswith(b"GET ") or payload.startswith(b"POST ") or payload.startswith(b"HEAD "):
        try:
            text = payload.decode("iso-8859-1", errors="replace")
        except Exception:
            return None
        for line in text.split("\r\n"):
            if line.lower().startswith("host:"):
                return line.split(":", 1)[1].strip()
    return None


def try_parse_tls_sni(payload: bytes) -> str | None:
    """Best-effort parse of TLS ClientHello SNI.

    Expects payload to start at a TLS record boundary.
    Returns hostname if an SNI extension is found.
    """

    # TLS record: 0x16 0x03 0x01..0x04  len(2)
    if len(payload) < 5:
        return None
    if payload[0] != 0x16 or payload[1] != 0x03:
        return None

    rec_len = _u16be(payload, 3)
    rec_end = 5 + rec_len
    if rec_end > len(payload):
        # Need more bytes; best-effort only.
        rec_end = len(payload)

    # Handshake header: type(1)=0x01 client_hello, len(3)
    if rec_end - 5 < 4:
        return None
    hs_type = payload[5]
    if hs_type != 0x01:
        return None
    hs_len = _u24be(payload, 6)
    hs_start = 5 + 4
    hs_end = min(hs_start + hs_len, rec_end)
    if hs_end > len(payload):
        hs_end = len(payload)

    i = hs_start
    # client_version (2)
    if i + 2 > hs_end:
        return None
    i += 2
    # random (32)
    if i + 32 > hs_end:
        return None
    i += 32
    # session_id
    if i + 1 > hs_end:
        return None
    sid_len = payload[i]
    i += 1 + sid_len
    if i > hs_end:
        return None
    # cipher_suites
    if i + 2 > hs_end:
        return None
    cs_len = _u16be(payload, i)
    i += 2 + cs_len
    if i > hs_end:
        return None
    # compression_methods
    if i + 1 > hs_end:
        return None
    comp_len = payload[i]
    i += 1 + comp_len
    if i > hs_end:
        return None
    # extensions
    if i + 2 > hs_end:
        return None
    ext_len = _u16be(payload, i)
    i += 2
    ext_end = min(i + ext_len, hs_end)
    if ext_end > len(payload):
        ext_end = len(payload)

    while i + 4 <= ext_end:
        ext_type = _u16be(payload, i)
        ext_size = _u16be(payload, i + 2)
        i += 4
        if i + ext_size > ext_end:
            break
        if ext_type == 0x0000:
            # server_name
            if ext_size < 2:
                return None
            list_len = _u16be(payload, i)
            j = i + 2
            list_end = min(j + list_len, i + ext_size)
            while j + 3 <= list_end:
                name_type = payload[j]
                name_len = _u16be(payload, j + 1)
                j += 3
                if j + name_len > list_end:
                    break
                if name_type == 0:
                    try:
                        return payload[j : j + name_len].decode("ascii", errors="replace")
                    except Exception:
                        return None
                j += name_len
        i += ext_size
    return None


def handle_client(
    conn: socket.socket,
    addr: tuple[str, int],
    local_port: int,
    max_bytes: int,
    idle_timeout_s: float,
    out_dir: pathlib.Path | None,
    log_fp: "TextIO" | None,
) -> None:
    src_ip, src_port = addr
    log_line(f"\n[{now_ts()}] TCP {src_ip}:{src_port} -> :{local_port}  CONNECT", log_fp)

    conn.settimeout(idle_timeout_s)
    chunks: list[bytes] = []
    total = 0
    try:
        while total < max_bytes:
            try:
                data = conn.recv(min(4096, max_bytes - total))
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
            total += len(data)
    except OSError:
        pass
    finally:
        payload = b"".join(chunks)
        if payload:
            sha = hashlib.sha256(payload).hexdigest()[:16]
            log_line(f"[{now_ts()}] TCP :{local_port}  RX len={len(payload)}", log_fp)
            log_line(f"[{now_ts()}] TCP :{local_port}  sha256={sha}", log_fp)

            http_host = try_parse_http_host(payload)
            if http_host:
                log_line(f"[{now_ts()}] TCP :{local_port}  HTTP Host: {http_host}", log_fp)

            tls_sni = try_parse_tls_sni(payload)
            if tls_sni:
                log_line(f"[{now_ts()}] TCP :{local_port}  TLS SNI: {tls_sni}", log_fp)

            log_line(hexdump(payload, max_len=max_bytes), log_fp)

            if out_dir is not None:
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                    safe_ip = src_ip.replace(":", "_")
                    p = out_dir / f"tcp_{safe_ip}_{src_port}_to_{local_port}_{ts}.bin"
                    p.write_bytes(payload)
                    log_line(f"[{now_ts()}] TCP :{local_port}  wrote {p}", log_fp)
                except OSError as e:
                    log_line(f"[{now_ts()}] TCP :{local_port}  ERROR writing payload: {e}", log_fp)
        log_line(f"[{now_ts()}] TCP {src_ip}:{src_port} -> :{local_port}  CLOSE", log_fp)
        try:
            conn.close()
        except Exception:
            pass


def serve_port(
    bind_ip: str,
    port: int,
    max_bytes: int,
    idle_timeout_s: float,
    out_dir: pathlib.Path | None,
    log_fp: "TextIO" | None,
) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind_ip, port))
    s.listen(64)

    while True:
        conn, addr = s.accept()
        t = threading.Thread(
            target=handle_client,
            args=(conn, addr, port, max_bytes, idle_timeout_s, out_dir, log_fp),
            daemon=True,
        )
        t.start()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="TCP logging server")
    ap.add_argument("--bind", default="0.0.0.0", help="IP to bind (default: 0.0.0.0)")
    ap.add_argument(
        "--ports",
        default="40000,40005,45000,44000,6668",
        help="Comma-separated TCP ports to listen on",
    )
    ap.add_argument("--max-bytes", type=int, default=512, help="Max bytes to print per connection")
    ap.add_argument(
        "--idle-timeout",
        type=float,
        default=1.0,
        help="Seconds of recv() inactivity before closing (default 1.0)",
    )
    ap.add_argument(
        "--out-dir",
        default="",
        help="If set, write each received payload to this directory as a .bin file",
    )
    ap.add_argument(
        "--log-file",
        default="",
        help="If set, append human-readable connection logs to this file",
    )
    args = ap.parse_args(argv)

    ports = parse_ports(args.ports)

    log_fp = None
    if str(args.log_file).strip():
        try:
            log_fp = pathlib.Path(str(args.log_file)).open("a", encoding="utf-8")
        except OSError as e:
            print(f"ERROR: failed to open log file: {e}")
            return 2

    log_line("TCP logger listening", log_fp)
    log_line(f"  bind:  {args.bind}", log_fp)
    log_line(f"  ports: {', '.join(str(p) for p in ports)}", log_fp)
    if log_fp is not None:
        log_line(f"  log:   {pathlib.Path(str(args.log_file))}", log_fp)
    log_line("  ctrl+c to stop", log_fp)

    out_dir: pathlib.Path | None = None
    if str(args.out_dir).strip():
        out_dir = pathlib.Path(str(args.out_dir).strip())

    threads: list[threading.Thread] = []
    try:
        for p in ports:
            t = threading.Thread(
                target=serve_port,
                args=(args.bind, p, int(args.max_bytes), float(args.idle_timeout), out_dir, log_fp),
                daemon=True,
            )
            t.start()
            threads.append(t)
    except OSError as e:
        log_line(f"ERROR: bind failed: {e}", log_fp)
        return 2

    try:
        while True:
            # Keep main thread alive.
            threading.Event().wait(1.0)
    except KeyboardInterrupt:
        log_line("\nStopping.", log_fp)

    if log_fp is not None:
        try:
            log_fp.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
