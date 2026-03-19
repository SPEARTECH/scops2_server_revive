#!/usr/bin/env python3
"""Ubisoft GS Persistent Proxy "Wait Module" (TCP) — text protocol.

Same text protocol as pers_proxy_server (44001) but on port 44002.
The WM router delivers this address via ct34 push in the PROXY_HANDLER message.

Protocol:
  Server→Game:  HELLO REVISION=0 MINVER=2000 VER=2110 REASON=0\n
  Game→Server:  CHALLENGE VER=2110 CHALLENGE=<nonce> STATS=0\n
  Server→Game:  LOGIN RESPONSE=0 URL=\n
  Game→Server:  WELCOME LEVEL=<level> LONE=0\n   (or FAILURE CHALLENGE\n)
"""

from __future__ import annotations

import argparse
import pathlib
import socket
import threading
from typing import TextIO

from ubigs_common import (
    file_ts,
    log_line,
    now_ts,
    safe_slug,
    save_blob,
)

HELLO_MINVER = 2000
HELLO_VER = 2110
HELLO_LINE = f"HELLO REVISION=0 MINVER={HELLO_MINVER} VER={HELLO_VER} REASON=0\n"


def parse_fields(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    parts = line.strip().split()
    if parts:
        fields["_type"] = parts[0]
    for tok in parts[1:]:
        if "=" in tok:
            k, _, v = tok.partition("=")
            fields[k] = v
    return fields


def client_thread(conn: socket.socket, addr: tuple[str, int], args: argparse.Namespace, *, log_fp: TextIO | None):
    src_ip, src_port = addr
    local_ip, local_port = conn.getsockname()
    tag = f"PERS_PROXY_WM {src_ip}:{src_port}"
    log_line(f"\n[{now_ts()}] {tag} -> {local_ip}:{local_port} CONNECT", log_fp=log_fp)

    conn.settimeout(float(args.idle_timeout))
    recv_buf = b""

    def send_line(line: str):
        data = line.encode()
        tx_name = f"{file_ts()}_{safe_slug(local_ip)}_{local_port}_to_{safe_slug(src_ip)}_{src_port}_tx.bin"
        save_blob(args.save_tx_dir, tx_name, data)
        log_line(f"[{now_ts()}] {tag} TX: {line.strip()!r}", log_fp=log_fp)
        conn.sendall(data)

    def recv_line(timeout: float = 30.0) -> str | None:
        nonlocal recv_buf
        import time
        t0 = time.monotonic()
        while True:
            if b"\n" in recv_buf:
                idx = recv_buf.index(b"\n")
                line_bytes = recv_buf[:idx + 1]
                recv_buf = recv_buf[idx + 1:]
                rx_name = f"{file_ts()}_{safe_slug(src_ip)}_{src_port}_to_{safe_slug(local_ip)}_{local_port}_rx.bin"
                save_blob(args.save_rx_dir, rx_name, line_bytes)
                line = line_bytes.decode("latin-1").strip()
                log_line(f"[{now_ts()}] {tag} RX: {line!r}", log_fp=log_fp)
                return line
            elapsed = time.monotonic() - t0
            remaining = timeout - elapsed
            if remaining <= 0:
                return None
            try:
                conn.settimeout(min(remaining, 2.0))
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            recv_buf += chunk

    try:
        send_line(HELLO_LINE)

        line = recv_line(timeout=30.0)
        if line is None:
            log_line(f"[{now_ts()}] {tag} No CHALLENGE received", log_fp=log_fp)
            return

        fields = parse_fields(line)
        if fields.get("_type") != "CHALLENGE":
            log_line(f"[{now_ts()}] {tag} Expected CHALLENGE, got: {line!r}", log_fp=log_fp)
            for _ in range(5):
                line = recv_line(timeout=5.0)
                if line is None:
                    break
                fields = parse_fields(line)
                if fields.get("_type") == "CHALLENGE":
                    break
            else:
                return

        nonce_str = fields.get("CHALLENGE", "0")
        try:
            nonce = int(nonce_str)
        except ValueError:
            nonce = 0
        log_line(f"[{now_ts()}] {tag} CHALLENGE nonce={nonce}", log_fp=log_fp)

        # FUN_001b3e60 returns 0 → response is always 0
        send_line(f"LOGIN RESPONSE=0 URL=\n")

        line = recv_line(timeout=30.0)
        if line is None:
            log_line(f"[{now_ts()}] {tag} No WELCOME/FAILURE", log_fp=log_fp)
            return

        fields = parse_fields(line)
        msg_type = fields.get("_type", "")
        if msg_type == "WELCOME":
            log_line(f"[{now_ts()}] {tag} WELCOME level={fields.get('LEVEL', '')!r}", log_fp=log_fp)
        elif msg_type == "FAILURE":
            log_line(f"[{now_ts()}] {tag} FAILURE: {line!r}", log_fp=log_fp)
            return
        else:
            log_line(f"[{now_ts()}] {tag} Unexpected after LOGIN: {line!r}", log_fp=log_fp)

        log_line(f"[{now_ts()}] {tag} Handshake complete, entering data session", log_fp=log_fp)
        while True:
            line = recv_line(timeout=float(args.idle_timeout))
            if line is None:
                break
            log_line(f"[{now_ts()}] {tag} DATA: {line!r}", log_fp=log_fp)

    except Exception as e:
        log_line(f"[{now_ts()}] {tag} Exception: {e}", log_fp=log_fp)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        log_line(f"[{now_ts()}] {tag} DISCONNECT", log_fp=log_fp)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ubisoft GS persistent proxy WM service (text protocol)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=44002)
    ap.add_argument("--idle-timeout", type=float, default=30.0)
    ap.add_argument("--dump-max", type=int, default=256)
    ap.add_argument("--save-rx-dir", default="captures/tcp/pers_proxy_wm_rx")
    ap.add_argument("--save-tx-dir", default="captures/tcp/pers_proxy_wm_tx")
    ap.add_argument("--log-file", default="logs/pers_proxy_wm_44002.log")
    args = ap.parse_args(argv)

    log_fp = None
    if args.log_file:
        pathlib.Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        log_fp = pathlib.Path(args.log_file).open("a", encoding="utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, int(args.port)))
    sock.listen(16)

    log_line(f"[{now_ts()}] Ubisoft GS Persistent Proxy WM (text) listening on {args.bind}:{args.port}", log_fp=log_fp)

    try:
        while True:
            conn, addr = sock.accept()
            th = threading.Thread(
                target=client_thread,
                args=(conn, addr, args),
                kwargs={"log_fp": log_fp},
                daemon=True,
            )
            th.start()
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            sock.close()
        except Exception:
            pass
        if log_fp is not None:
            try:
                log_fp.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
