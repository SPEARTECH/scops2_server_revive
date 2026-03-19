#!/usr/bin/env python3
"""UDP logging server for protocol RE.

Listens on one or more UDP ports and logs:
- source/destination
- payload length
- a hex + ASCII preview
- optional header field decoding for the SCCT packets we've observed

This is intentionally *non-emulative*: by default it does NOT reply.

Usage:
  python tools/udp_log_server.py --ports 19325,19321 --bind 0.0.0.0

Notes:
- If you redirect `gsconnect.ubisoft.com` to your PC, the game should start
  sending its UDP here.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import socket
import sys
from typing import TextIO


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
    # stable dedupe
    seen: set[int] = set()
    out: list[int] = []
    for p in ports:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def hexdump(b: bytes, width: int = 16, max_len: int = 256) -> str:
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


def decode_scct_header(payload: bytes) -> str | None:
    """Decode fields we saw in captures when payload is long enough.

    Observed client->server:
      [0:8]   changing bytes
      [8:12]  u32 BE (00000451/00000452/00000453/000005B8)
      [12:14] u16 BE cookie (BEDE)
      [14:16] u16 BE type (0002/0003/0007)
    """

    if len(payload) < 16:
        return None
    ver = int.from_bytes(payload[8:12], "big", signed=False)
    cookie = int.from_bytes(payload[12:14], "big", signed=False)
    ptype = int.from_bytes(payload[14:16], "big", signed=False)
    return f"ver=0x{ver:08X} cookie=0x{cookie:04X} type=0x{ptype:04X}"


def now_ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log_line(msg: str, *, log_fp: TextIO | None = None) -> None:
    print(msg, flush=True)
    if log_fp is not None:
        log_fp.write(msg + "\n")
        log_fp.flush()


def log_block(msg: str, *, log_fp: TextIO | None = None) -> None:
    # Preserve embedded newlines for multi-line hexdumps.
    for line in msg.splitlines():
        log_line(line, log_fp=log_fp)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="UDP logging server")
    ap.add_argument("--bind", default="0.0.0.0", help="IP to bind (default: 0.0.0.0)")
    ap.add_argument("--ports", default="19325,19321", help="Comma-separated UDP ports")
    ap.add_argument("--max-bytes", type=int, default=256, help="Max bytes to print per packet")
    ap.add_argument(
        "--log-file",
        default="",
        help="If set, append logs to this file (useful when run minimized/backgrounded)",
    )
    ap.add_argument(
        "--no-hexdump",
        action="store_true",
        help="Print only a one-line summary per packet",
    )
    args = ap.parse_args(argv)

    log_fp: TextIO | None = None
    if str(args.log_file).strip():
        try:
            log_fp = open(str(args.log_file).strip(), "a", encoding="utf-8")
        except OSError as e:
            print(f"ERROR: failed to open log file: {e}")
            return 2

    ports = parse_ports(args.ports)

    socks: list[socket.socket] = []
    try:
        for p in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((args.bind, p))
            s.setblocking(True)
            socks.append(s)
    except OSError as e:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
        log_line(f"ERROR: bind failed: {e}", log_fp=log_fp)
        return 2

    log_line("UDP logger listening", log_fp=log_fp)
    log_line(f"  bind: {args.bind}", log_fp=log_fp)
    log_line(f"  ports: {', '.join(str(p) for p in ports)}", log_fp=log_fp)
    if log_fp is not None:
        log_line(f"  log:  {str(args.log_file).strip()}", log_fp=log_fp)
    log_line("  ctrl+c to stop", log_fp=log_fp)

    # Simple select loop
    import select

    while True:
        try:
            r, _, _ = select.select(socks, [], [], 1.0)
        except KeyboardInterrupt:
            log_line("\nStopping.", log_fp=log_fp)
            break

        for s in r:
            try:
                data, addr = s.recvfrom(65535)
            except OSError:
                continue

            src_ip, src_port = addr[0], addr[1]
            dst_ip, dst_port = s.getsockname()[0], s.getsockname()[1]

            hdr = decode_scct_header(data)
            hdr_s = f"  {hdr}" if hdr else ""

            log_line(
                f"\n[{now_ts()}] {src_ip}:{src_port} -> {dst_ip}:{dst_port}  len={len(data)}{hdr_s}",
                log_fp=log_fp,
            )
            if not args.no_hexdump:
                log_block(hexdump(data, max_len=max(0, int(args.max_bytes))), log_fp=log_fp)


    for s in socks:
        try:
            s.close()
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
