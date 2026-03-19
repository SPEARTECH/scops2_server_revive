#!/usr/bin/env python3
"""Minimal DNAS probe TLS server for PS2-era clients.

This server is intentionally lightweight:
- Accepts TLS on 443 using provided cert/key.
- Parses POST requests (notably /us-gw/v2.5_i-connect).
- Logs request metadata and DNAS fingerprint fields from the POST body.
- Returns an HTTP/1.0 image/gif payload (default: DNASrep error.raw).

It does not implement full DNAS encryption/replay yet; the goal is to capture
the exact packet fingerprint and query type needed to build a title-specific
response path.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import pathlib
import socket
import ssl
import threading


def now_ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log_line(msg: str, *, fp=None) -> None:
    print(msg, flush=True)
    if fp is not None:
        fp.write(msg + "\n")
        fp.flush()


def read_http_request(conn: ssl.SSLSocket, timeout_s: float = 3.0) -> bytes:
    conn.settimeout(timeout_s)
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 0x20000:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk

    if b"\r\n\r\n" not in buf:
        return buf

    head, rest = buf.split(b"\r\n\r\n", 1)
    headers = head.decode("iso-8859-1", errors="replace").split("\r\n")
    content_len = 0
    for h in headers[1:]:
        if ":" not in h:
            continue
        k, v = h.split(":", 1)
        if k.strip().lower() == "content-length":
            try:
                content_len = int(v.strip(), 10)
            except ValueError:
                content_len = 0
            break

    body = rest
    while len(body) < content_len and len(body) < 0x40000:
        chunk = conn.recv(4096)
        if not chunk:
            break
        body += chunk

    return head + b"\r\n\r\n" + body


def parse_request(raw: bytes):
    if b"\r\n\r\n" not in raw:
        return None, None, {}, b""
    head, body = raw.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    req_line = lines[0] if lines else ""
    method = ""
    path = ""
    parts = req_line.split(" ")
    if len(parts) >= 2:
        method, path = parts[0], parts[1]
    headers = {}
    for h in lines[1:]:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return method, path, headers, body


def dnas_fields(body: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    if len(body) >= 0x34:
        qry = body[0:4]
        gid = body[0x2C:0x34]
        fields["qrytype_hex"] = qry.hex()
        fields["gameid_hex"] = gid.hex()
        fields["fname_key"] = f"{gid.hex()}_{qry.hex()}"
    if len(body) >= 0x134:
        chk1 = hashlib.sha1(body[0x34:0x134]).hexdigest()
        fields["sha1_0x34_0x100"] = chk1
        chk2 = hashlib.sha1(body[0x48:0x134]).hexdigest()
        fields["sha1_0x48_0xec"] = chk2
        fullkey = chk2[:0x28] + chk1[:0x18]
        fields["fullkey_hex"] = fullkey
    return fields


def http_image_response(payload: bytes) -> bytes:
    hdr = (
        "HTTP/1.0 200 OK\r\n"
        "Content-Type: image/gif\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    return hdr + payload


def handle_client(conn, addr, conn_id, log_fp, out_dir, fallback_payload, packet_dir):
    try:
        log_line(f"[{now_ts()}] #{conn_id} TLS CONNECT {addr[0]}:{addr[1]}", fp=log_fp)
        conn.do_handshake()
        log_line(f"[{now_ts()}] #{conn_id} TLS OK version={conn.version()} cipher={conn.cipher()}", fp=log_fp)

        raw = read_http_request(conn)
        method, path, headers, body = parse_request(raw)
        log_line(f"[{now_ts()}] #{conn_id} HTTP method={method} path={path} body_len={len(body)}", fp=log_fp)
        if headers:
            ua = headers.get("user-agent", "")
            ct = headers.get("content-type", "")
            log_line(f"[{now_ts()}] #{conn_id} headers ua={ua!r} ct={ct!r}", fp=log_fp)

        if body:
            fields = dnas_fields(body)
            for k, v in fields.items():
                log_line(f"[{now_ts()}] #{conn_id} {k}={v}", fp=log_fp)

            fname_key = fields.get("fname_key", "unknown")
            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            out_file = out_dir / f"{ts}_{fname_key}.bin"
            out_file.write_bytes(body)
            log_line(f"[{now_ts()}] #{conn_id} wrote_body={out_file}", fp=log_fp)

            pkt = packet_dir / fname_key
            log_line(f"[{now_ts()}] #{conn_id} dnasrep_packet_exists={pkt.exists()} packet={pkt}", fp=log_fp)

        conn.sendall(http_image_response(fallback_payload))
    except Exception as e:
        log_line(f"[{now_ts()}] #{conn_id} ERROR {e}", fp=log_fp)
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        log_line(f"[{now_ts()}] #{conn_id} CLOSE", fp=log_fp)


def main() -> int:
    ap = argparse.ArgumentParser(description="DNAS probe TLS server")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--log-file", default="logs/dnas_probe_443.log")
    ap.add_argument("--out-dir", default="logs/dnas_probe")
    ap.add_argument("--fallback-raw", default="DNASrep-master/DNASrep-master/www/dnas/us-gw/error.raw")
    ap.add_argument("--packet-dir", default="DNASrep-master/DNASrep-master/www/dnas/us-gw/packets")
    args = ap.parse_args()

    log_path = pathlib.Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload_path = pathlib.Path(args.fallback_raw)
    if payload_path.exists():
        fallback_payload = payload_path.read_bytes()
    else:
        fallback_payload = b"ERROR"

    packet_dir = pathlib.Path(args.packet_dir)

    with log_path.open("a", encoding="utf-8") as log_fp:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except Exception:
            pass
        try:
            ctx.set_ciphers("DES-CBC3-SHA:@SECLEVEL=0")
        except Exception:
            ctx.set_ciphers("ALL:@SECLEVEL=0")

        ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((args.bind, args.port))
        srv.listen(16)

        log_line("DNAS probe TLS server listening", fp=log_fp)
        log_line(f"  bind: {args.bind}:{args.port}", fp=log_fp)
        log_line(f"  cert: {args.cert}", fp=log_fp)
        log_line(f"  key:  {args.key}", fp=log_fp)
        log_line(f"  fallback_raw: {payload_path}", fp=log_fp)
        log_line(f"  packet_dir:   {packet_dir}", fp=log_fp)

        conn_id = 0
        try:
            while True:
                raw, addr = srv.accept()
                conn_id += 1
                try:
                    tls_conn = ctx.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
                except Exception as e:
                    log_line(f"[{now_ts()}] #{conn_id} wrap_socket ERROR {e}", fp=log_fp)
                    try:
                        raw.close()
                    except Exception:
                        pass
                    continue
                t = threading.Thread(
                    target=handle_client,
                    args=(tls_conn, addr, conn_id, log_fp, out_dir, fallback_payload, packet_dir),
                    daemon=True,
                )
                t.start()
        except KeyboardInterrupt:
            log_line("Stopping DNAS probe server.", fp=log_fp)
        finally:
            srv.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
