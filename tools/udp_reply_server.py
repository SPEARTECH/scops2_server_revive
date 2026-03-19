#!/usr/bin/env python3
"""UDP reply server for protocol RE.

This is a sibling to `udp_log_server.py`.

Why it exists:
- Some PS2-era online stacks will *send UDP probes* and expect *any* response
  (or a minimally-structured ACK) before they proceed to the next stage.
- The generic logger intentionally does not respond, which can cause the client
  to stall and eventually disconnect.

Behavior:
- Logs the same one-line summary + optional hexdump.
- Optionally replies.

Reply modes:
- echo: sends the exact payload back.
- scct_ack: heuristic ACK mode for observed SCCT probes.
    - If payload is at least 16 bytes, treats bytes [12:16] as a big-endian
        (cookie,type) pair and returns an ACK by OR-ing bit 0 into type.
    - If payload is exactly 12 bytes and matches the common probe pattern
        (starts with 0xB6CF), returns an ACK by filling bytes [8:10] with the
                client's source UDP port (big-endian). This is a guess, but it matches
                the shape of a simple NAT/port-mapping probe.

Additionally:
- For the 12-byte 0xB6CF probe, the client appears to behave like a NAT/probe
    check. Some stacks expect the response to come from an *alternate* server port.
    If multiple UDP ports are bound (e.g. 45000 and 45001), this server will
    opportunistically send the same reply from the other bound ports too
    (rate-limited) to mimic a basic “alternate port” test.

NOTE: This is a best-effort heuristic responder, not a full emulator.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import select
import socket
import sys
import time
from typing import TextIO

from ubigs_common import ensure_ubigs_importable


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
    for line in msg.splitlines():
        log_line(line, log_fp=log_fp)


def make_reply(payload: bytes, mode: str, *, src_port: int) -> bytes:
    if mode == "echo":
        return payload

    if mode == "srp_nat":
        # Handled in the main loop because it requires client state + may emit multiple replies.
        return payload

    if mode == "scct_ack":
        # For the observed SCCT UDP frames, bytes [12:16] appear to be
        # (cookie,type) in BE. We flip bit0 in the type as a generic ACK.
        if len(payload) >= 16:
            out = bytearray(payload)
            ptype = int.from_bytes(out[14:16], "big", signed=False)
            ack_type = ptype | 0x0001
            out[14:16] = ack_type.to_bytes(2, "big", signed=False)
            return bytes(out)
        # 12-byte probe seen very frequently: b6 cf 00 00 00 00 49 30 ff ff 00 00
        # Heuristic response: treat this like a NAT/port-mapping probe.
        # Conservative default reply:
        # - Keep the tag unchanged (client sends 'I0').
        # - Replace 0xFFFF with the observed client source UDP port (BE).
        # - Keep the trailing u16 unchanged.
        if len(payload) == 12 and payload[0:2] == b"\xb6\xcf":
            out = bytearray(payload)
            out[8:10] = int(src_port).to_bytes(2, "big", signed=False)
            return bytes(out)
        return payload

    raise ValueError(f"Unknown reply mode: {mode}")


def is_scct_12b_probe(payload: bytes) -> bool:
    return len(payload) == 12 and payload[0:2] == b"\xb6\xcf"


def make_scct_12b_variant_reply(
    payload: bytes,
    *,
    src_ip: str,
    src_port: int,
    port_endian: str,
    advertise_ip: str | None,
) -> bytes:
    out = bytearray(payload)
    if advertise_ip:
        ip_to_embed = advertise_ip
        if advertise_ip.lower() in {"src", "client"}:
            ip_to_embed = src_ip
        try:
            out[2:6] = socket.inet_aton(ip_to_embed)
        except OSError:
            pass
    if port_endian == "be":
        out[8:10] = int(src_port).to_bytes(2, "big", signed=False)
    elif port_endian == "le":
        out[8:10] = int(src_port).to_bytes(2, "little", signed=False)
    else:
        raise ValueError(f"Unknown port_endian: {port_endian}")
    return bytes(out)


def make_scct_12b_tag_variant(payload: bytes, *, tag: bytes) -> bytes:
    """Change the 2-byte ASCII-ish tag at bytes [6:8] (e.g. b'I0')."""
    if len(payload) != 12:
        return payload
    if len(tag) != 2:
        raise ValueError("tag must be 2 bytes")
    out = bytearray(payload)
    out[6:8] = tag
    return bytes(out)


def make_scct_12b_port_at_end(payload: bytes, *, src_port: int, port_endian: str) -> bytes:
    """Put the observed source port into the last u16 (bytes [10:12])."""
    if len(payload) != 12:
        return payload
    out = bytearray(payload)
    if port_endian == "be":
        out[10:12] = int(src_port).to_bytes(2, "big", signed=False)
    elif port_endian == "le":
        out[10:12] = int(src_port).to_bytes(2, "little", signed=False)
    else:
        raise ValueError(f"Unknown port_endian: {port_endian}")
    return bytes(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="UDP reply server")
    ap.add_argument("--bind", default="0.0.0.0", help="IP to bind (default: 0.0.0.0)")
    ap.add_argument("--ports", default="45000", help="Comma-separated UDP ports")
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
    ap.add_argument(
        "--reply-mode",
        default="scct_ack",
        choices=["echo", "scct_ack", "srp_nat"],
        help="How to respond (default: scct_ack)",
    )
    ap.add_argument(
        "--alt-reply-interval-ms",
        type=int,
        default=50,
        help=(
            "For the 12-byte 0xB6CF probe, also send the reply from the other bound "
            "UDP ports (if any). Rate-limit per client to this interval in ms (default: 50). "
            "Set to 0 to disable."
        ),
    )
    ap.add_argument(
        "--scct12-extra",
        default="off",
        choices=["off", "light", "full"],
        help=(
            "For the 12-byte 0xB6CF probe, optionally send a small set of extra reply variants "
            "(rate-limited per client). 'light' sends a couple conservative alternatives; 'full' sends more."
        ),
    )
    ap.add_argument(
        "--scct12-extra-interval-ms",
        type=int,
        default=250,
        help=(
            "Rate-limit interval for extra 12-byte probe variants per client in ms (default: 250). "
            "Set to 0 to disable even if --scct12-extra is set."
        ),
    )
    ap.add_argument(
        "--advertise-ip",
        default="",
        help=(
            "Optional IPv4 address to embed into the 12-byte 0xB6CF probe reply (bytes [2:6]). "
            "Use 'src' to embed the sender's observed source IP. Useful if the probe is expecting "
            "an IP+port mapping style response."
        ),
    )
    args = ap.parse_args(argv)

    log_fp: TextIO | None = None
    if str(args.log_file).strip():
        try:
            log_fp = open(str(args.log_file).strip(), "a", encoding="utf-8")
        except OSError as e:
            print(f"ERROR: failed to open log file: {e}")
            return 2

    ensure_ubigs_importable()
    # Late imports (ubi-gs). We log failures so background/minimized runs aren't silent.
    try:
        import client as ubiclient
        import gsm as ubigsm
        import srp as ubisrp
    except Exception as e:
        log_line(f"ERROR: failed to import ubi-gs modules: {e}", log_fp=log_fp)
        return 2

    advertise_ip: str | None = None
    if str(args.advertise_ip).strip():
        advertise_ip = str(args.advertise_ip).strip()
        if advertise_ip.lower() not in {"src", "client"}:
            try:
                socket.inet_aton(advertise_ip)
            except OSError:
                print(f"ERROR: invalid --advertise-ip: {advertise_ip}")
                return 2

    ports = parse_ports(args.ports)

    socks: list[socket.socket] = []
    port_to_sock: dict[int, socket.socket] = {}
    try:
        for p in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((args.bind, p))
            s.setblocking(True)
            socks.append(s)
            port_to_sock[p] = s
    except OSError as e:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
        log_line(f"ERROR: bind failed: {e}", log_fp=log_fp)
        return 2

    log_line("UDP reply server listening", log_fp=log_fp)
    log_line(f"  bind: {args.bind}", log_fp=log_fp)
    log_line(f"  ports: {', '.join(str(p) for p in ports)}", log_fp=log_fp)
    log_line(f"  reply-mode: {args.reply_mode}", log_fp=log_fp)
    if advertise_ip:
        log_line(f"  advertise-ip: {advertise_ip}", log_fp=log_fp)
    if int(args.alt_reply_interval_ms) <= 0 or len(ports) < 2:
        log_line("  alt-reply: disabled", log_fp=log_fp)
    else:
        log_line(f"  alt-reply: enabled interval={int(args.alt_reply_interval_ms)}ms", log_fp=log_fp)

    if args.scct12_extra == "off" or int(args.scct12_extra_interval_ms) <= 0:
        log_line("  scct12-extra: disabled", log_fp=log_fp)
    else:
        log_line(
            f"  scct12-extra: {args.scct12_extra} interval={int(args.scct12_extra_interval_ms)}ms",
            log_fp=log_fp,
        )
    if log_fp is not None:
        log_line(f"  log:  {str(args.log_file).strip()}", log_fp=log_fp)
    log_line("  ctrl+c to stop", log_fp=log_fp)

    # (src_ip, src_port) -> last time (monotonic seconds) an alternate-port reply was sent.
    last_alt_reply: dict[tuple[str, int], float] = {}
    # (src_ip, src_port) -> last time (monotonic seconds) extra variants were sent.
    last_extra_reply: dict[tuple[str, int], float] = {}

    nat_clients: list[ubiclient.NatClient] = []

    def _srp_checksum(data: bytes) -> int:
        # Equivalent to srp.SRPResponse.__make_checksum but local (u16).
        trunc_pos = 0
        check_base = 0
        half_len = len(data) >> 1
        odd_len = (len(data) % 2) == 1
        if odd_len:
            check_base += data[0]
            trunc_pos += 1
        if half_len > 0:
            import struct

            for _ in range(half_len):
                check_base += struct.unpack_from('<H', data, trunc_pos)[0]
                trunc_pos += 2
        # fold
        while check_base >> 16:
            check_base = (check_base & 0xFFFF) + (check_base >> 16)
        return (~check_base) & 0xFFFF

    def _make_srp_ack_only(req: ubisrp.SRPRequest, clt: ubiclient.NatClient) -> bytes:
        # Build a minimal SRP ACK with data_size=0.
        # Keep checksum_init/sender_sig if we learned them from a prior SYN.
        checksum = clt.checksum_init or 0
        signature = clt.sender_sig or 0
        data_size = 0
        flags = ubisrp.SRPHeaderFlags.SRP_ID.value | ubisrp.SRPHeaderFlags.ACK.value
        # Echo FIN if present; some stacks use FIN as a keepalive-ish probe.
        if ubisrp.SRPHeaderFlags.FIN.name in req.segment.header.flags:
            flags |= ubisrp.SRPHeaderFlags.FIN.value
        seg = clt.segment_id & 0xFFFF
        clt.segment_id += 1
        ack = req.segment.header.seg & 0xFFFF
        header_bytes = bytearray()
        header_bytes.extend((checksum & 0xFFFF).to_bytes(2, 'little'))
        header_bytes.extend((signature & 0xFFFF).to_bytes(2, 'little'))
        header_bytes.extend((data_size & 0xFFFF).to_bytes(2, 'little'))
        header_bytes.extend((flags & 0xFFFF).to_bytes(2, 'little'))
        header_bytes.extend((seg & 0xFFFF).to_bytes(2, 'little'))
        header_bytes.extend((ack & 0xFFFF).to_bytes(2, 'little'))
        # Compute checksum over the full segment with checksum field set to 0.
        header_bytes[0:2] = b"\x00\x00"
        csum = _srp_checksum(bytes(header_bytes))
        header_bytes[0:2] = (csum & 0xFFFF).to_bytes(2, 'little')
        return bytes(header_bytes)

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

            try:
                # SRP NAT mode is stateful and may emit multiple replies.
                if args.reply_mode == "srp_nat":
                    if len(data) < ubisrp.SRP_HEADER_SIZE:
                        s.sendto(data, addr)
                        log_line(f"  srp_nat ping-echo {len(data)} bytes to {src_ip}:{src_port}", log_fp=log_fp)
                        continue

                    try:
                        req = ubisrp.SRPRequest(data)
                    except Exception as e:
                        log_line(f"  srp_nat parse failed: {e}", log_fp=log_fp)
                        continue

                    clt = ubiclient.NatClient.find((src_ip, int(src_port)), nat_clients)
                    if clt is None and ubisrp.SRPHeaderFlags.FIN.name not in req.segment.header.flags:
                        clt = ubiclient.NatClient((src_ip, int(src_port)), 0, 0)
                        nat_clients.append(clt)

                    # If we don't have a client yet (e.g., FIN-only), still respond with a generic ACK.
                    if clt is None:
                        clt = ubiclient.NatClient((src_ip, int(src_port)), 0, 0)

                    # SYN/window handshake.
                    primary_resp: bytes | None = None
                    if req.segment.window is not None:
                        res = ubisrp.SRPResponse(req, clt, int(dst_port))
                        out = bytes(res)
                        s.sendto(out, addr)
                        primary_resp = out
                        log_line(f"  srp_nat syn-ack {len(out)} bytes to {src_ip}:{src_port}", log_fp=log_fp)
                    # NAT message.
                    elif req.segment.msg is not None:
                        try:
                            # Most NAT servers report the client's *observed* external mapping.
                            # Use the sender's source port here (not our server port).
                            res1 = ubisrp.SRPResponse(req, clt, int(src_port), ubigsm.NAT_MSG.PORT_ID)
                            out1 = bytes(res1)
                            s.sendto(out1, addr)
                            log_line(f"  srp_nat nat PORT_ID {len(out1)} bytes", log_fp=log_fp)
                            res2 = ubisrp.SRPResponse(req, clt, int(src_port), ubigsm.NAT_MSG.ADDRESS)
                            out2 = bytes(res2)
                            s.sendto(out2, addr)
                            primary_resp = out2
                            log_line(f"  srp_nat nat ADDRESS {len(out2)} bytes", log_fp=log_fp)
                        except Exception as e:
                            log_line(f"  srp_nat nat response failed: {e}", log_fp=log_fp)
                    else:
                        out = _make_srp_ack_only(req, clt)
                        s.sendto(out, addr)
                        primary_resp = out
                        log_line(f"  srp_nat ack-only {len(out)} bytes", log_fp=log_fp)

                    # If FIN, forget the client.
                    if ubisrp.SRPHeaderFlags.FIN.name in req.segment.header.flags:
                        existing = ubiclient.NatClient.find((src_ip, int(src_port)), nat_clients)
                        if existing is not None:
                            try:
                                nat_clients.remove(existing)
                            except ValueError:
                                pass

                    # Optional alternate-port response.
                    if int(args.alt_reply_interval_ms) > 0 and len(ports) >= 2:
                        key = (src_ip, int(src_port))
                        now_m = time.monotonic()
                        last_m = last_alt_reply.get(key, 0.0)
                        interval_s = int(args.alt_reply_interval_ms) / 1000.0
                        if now_m - last_m >= interval_s:
                            last_alt_reply[key] = now_m
                            alt_sent_from: list[int] = []
                            for p, alt_sock in port_to_sock.items():
                                if alt_sock is s:
                                    continue
                                try:
                                    # Mirror the most recent response from the alternate port.
                                    alt_sock.sendto(primary_resp if primary_resp is not None else data, addr)
                                    alt_sent_from.append(p)
                                except OSError:
                                    continue
                            if alt_sent_from:
                                alt_list = ",".join(str(p) for p in sorted(alt_sent_from))
                                log_line(
                                    f"  srp_nat alt-replied to {src_ip}:{src_port} from port(s) {alt_list}",
                                    log_fp=log_fp,
                                )
                    continue

                reply = make_reply(data, args.reply_mode, src_port=src_port)
                if is_scct_12b_probe(data) and advertise_ip:
                    # Override the conservative base reply for this probe to also carry an IP.
                    reply = make_scct_12b_variant_reply(
                        data,
                        src_ip=src_ip,
                        src_port=src_port,
                        port_endian="be",
                        advertise_ip=advertise_ip,
                    )
                s.sendto(reply, addr)
                rh = decode_scct_header(reply)
                rh_s = f"  {rh}" if rh else ""
                changed = " (changed)" if reply != data else ""
                log_line(
                    f"  replied {len(reply)} bytes to {src_ip}:{src_port}{rh_s}{changed}",
                    log_fp=log_fp,
                )
                if reply != data and not args.no_hexdump:
                    log_block(hexdump(reply, max_len=max(0, int(args.max_bytes))), log_fp=log_fp)

                # Optionally send additional conservative variants for the 12-byte probe.
                if (
                    is_scct_12b_probe(data)
                    and args.scct12_extra != "off"
                    and int(args.scct12_extra_interval_ms) > 0
                ):
                    key = (src_ip, int(src_port))
                    now_m = time.monotonic()
                    last_m = last_extra_reply.get(key, 0.0)
                    interval_s = int(args.scct12_extra_interval_ms) / 1000.0
                    if now_m - last_m >= interval_s:
                        last_extra_reply[key] = now_m

                        variants: list[tuple[str, bytes]] = []
                        # 1) Same-port LE port encoding variant.
                        variants.append(
                            (
                                "le-port@8",
                                make_scct_12b_variant_reply(
                                    data,
                                    src_ip=src_ip,
                                    src_port=src_port,
                                    port_endian="le",
                                    advertise_ip=advertise_ip,
                                ),
                            )
                        )
                        # 2) Tag variant (I1) with BE port.
                        v_i1 = make_scct_12b_tag_variant(data, tag=b"I1")
                        v_i1 = make_scct_12b_variant_reply(
                            v_i1,
                            src_ip=src_ip,
                            src_port=src_port,
                            port_endian="be",
                            advertise_ip=advertise_ip,
                        )
                        variants.append(("tag=I1", v_i1))

                        if args.scct12_extra == "full":
                            # 3) Put port into last u16 as BE and LE.
                            v_end_be = make_scct_12b_port_at_end(
                                make_scct_12b_variant_reply(
                                    data,
                                    src_ip=src_ip,
                                    src_port=src_port,
                                    port_endian="be",
                                    advertise_ip=advertise_ip,
                                ),
                                src_port=src_port,
                                port_endian="be",
                            )
                            variants.append(("be-port@end", v_end_be))

                            v_end_le = make_scct_12b_port_at_end(
                                make_scct_12b_variant_reply(
                                    data,
                                    src_ip=src_ip,
                                    src_port=src_port,
                                    port_endian="be",
                                    advertise_ip=advertise_ip,
                                ),
                                src_port=src_port,
                                port_endian="le",
                            )
                            variants.append(("le-port@end", v_end_le))

                            # 4) Tag variant (E0) sometimes used by NAT probes.
                            v_e0 = make_scct_12b_tag_variant(data, tag=b"E0")
                            v_e0 = make_scct_12b_variant_reply(
                                v_e0,
                                src_ip=src_ip,
                                src_port=src_port,
                                port_endian="be",
                                advertise_ip=advertise_ip,
                            )
                            variants.append(("tag=E0", v_e0))

                        sent_labels: list[str] = []
                        for label, v in variants:
                            if v == reply:
                                continue
                            try:
                                s.sendto(v, addr)
                                sent_labels.append(label)
                            except OSError:
                                continue

                        if sent_labels:
                            log_line(
                                f"  scct12-extra sent: {', '.join(sent_labels)}",
                                log_fp=log_fp,
                            )
                            if not args.no_hexdump:
                                # Dump just the first extra variant for RE convenience.
                                first_label, first_v = variants[0]
                                log_line(
                                    f"  scct12-extra first payload ({first_label}):",
                                    log_fp=log_fp,
                                )
                                log_block(
                                    hexdump(first_v, max_len=max(0, int(args.max_bytes))),
                                    log_fp=log_fp,
                                )

                # Alternate-port reply for the 12-byte probe (rate-limited).
                if (
                    is_scct_12b_probe(data)
                    and int(args.alt_reply_interval_ms) > 0
                    and len(ports) >= 2
                ):
                    key = (src_ip, int(src_port))
                    now_m = time.monotonic()
                    last_m = last_alt_reply.get(key, 0.0)
                    interval_s = int(args.alt_reply_interval_ms) / 1000.0
                    if now_m - last_m >= interval_s:
                        last_alt_reply[key] = now_m
                        alt_sent_from: list[int] = []
                        alt_hexdumped = False
                        for p, alt_sock in port_to_sock.items():
                            if alt_sock is s:
                                continue
                            try:
                                # Send a slightly different variant from the alternate port:
                                # many stacks encode the mapped port in either BE or LE.
                                alt_reply = make_scct_12b_variant_reply(
                                    data,
                                    src_ip=src_ip,
                                    src_port=src_port,
                                    port_endian="le",
                                    advertise_ip=advertise_ip,
                                )
                                alt_sock.sendto(alt_reply, addr)
                                alt_sent_from.append(p)
                                if (
                                    not alt_hexdumped
                                    and alt_reply != data
                                    and not args.no_hexdump
                                ):
                                    alt_hexdumped = True
                                    log_line(
                                        "  alt-reply payload (le port):",
                                        log_fp=log_fp,
                                    )
                                    log_block(
                                        hexdump(
                                            alt_reply,
                                            max_len=max(0, int(args.max_bytes)),
                                        ),
                                        log_fp=log_fp,
                                    )
                            except OSError:
                                continue
                        if alt_sent_from:
                            alt_list = ",".join(str(p) for p in sorted(alt_sent_from))
                            log_line(
                                f"  alt-replied {len(reply)} bytes to {src_ip}:{src_port} from port(s) {alt_list}",
                                log_fp=log_fp,
                            )
            except OSError as e:
                log_line(f"  reply failed: {e}", log_fp=log_fp)

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
