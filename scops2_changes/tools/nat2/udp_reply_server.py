#!/usr/bin/env python3
"""GSNAT responder v3 for the SCCT server (replaces tools/udp_reply_server.py srp_nat mode).

v3 fix: the game batches up to 4 NAT address requests (one per presolved UDP socket: 3658, 10071, 10072, 10073)
into one SRP segment (data_size 208 = 4 x 52). v2 answered only the first, so the game waited 30 x 500 ms = 15 s in
ConnectThread (PresolvedSocketRequestInProgress) and then failed with "Cannot access Ubi.Com". v3 answers every request.

Follows the handshake documented by the newer ubi-gs (homm5/gsnat.py, verified there with HoMM5):

  1. game  -> SYN                      server -> SYN+ACK (window)
  2. game  -> NAT address request      server -> PORT_ID  [socketId, <random 32-bit ping id>, <this NAT port>]
  3. game  -> 4-byte ping (id, BE) from its own port to <this NAT port>
                                       server -> ADDRESS  [socketId, <ip seen in the ping>, <port seen>]
  4. later pings -> bare ACK (the game keeps unasked-for replies in a 536-byte receive buffer)

The old server answered step 2 with PORT_ID *and* ADDRESS at once, using the client's own source
port as the "NAT port", and mirrored every reply from a second port. This one does not.

Uses the newer ubi-gs modules in ../../ubi-gs-new (separate from the older ubi-gs the other tools use).
"""
from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import secrets
import select
import socket
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
NEW_UBIGS = None
for cand in (HERE.parent.parent / "ubi-gs-new", HERE.parent / "ubi-gs-new", HERE / "ubi-gs-new"):
    if (cand / "srp.py").is_file():
        NEW_UBIGS = cand
        break
if NEW_UBIGS is None:
    print("ERROR: ubi-gs-new folder not found next to tools/", file=sys.stderr)
    sys.exit(2)
sys.path.insert(0, str(NEW_UBIGS))

import client  # noqa: E402
import gsm  # noqa: E402
import srp  # noqa: E402
import utils  # noqa: E402

CLIENT_TIMEOUT = 150.0
# Gap between datagrams sent back to back (the 4 PORT_ID replies). A real PS2 has a small IOP/UDP receive path and may
# drop a burst that an emulator accepts. Set SCCT_NAT_GAP_MS=0 to send them all at once.
GAP_S = float(os.environ.get("SCCT_NAT_GAP_MS", "25")) / 1000.0
# Test option: answer only the first N NAT requests of a segment with a PORT_ID (0 = all). Used to find which reply
# makes a real PS2 hang. Unanswered requests are ignored, like the first server version ignored all but the first.
PORTID_LIMIT = int(os.environ.get("SCCT_NAT_PORTID_LIMIT", "0"))
# Test options for the real PS2, which answers a PORT_ID and pings within ~65 ms (PCSX2 needs ~450 ms):
#  SCCT_NAT_ADDRESS_DELAY_MS - wait this long after a ping before sending the ADDRESS reply
#  SCCT_NAT_NO_REPEAT_ACK=1  - ignore repeat pings instead of answering each with a bare ACK
ADDRESS_DELAY_S = float(os.environ.get("SCCT_NAT_ADDRESS_DELAY_MS", "0")) / 1000.0
NO_REPEAT_ACK = os.environ.get("SCCT_NAT_NO_REPEAT_ACK", "0") == "1"
#  SCCT_NAT_NO_ADDRESS=1     - never send the ADDRESS reply (tests whether the ADDRESS is what stalls the console)
NO_ADDRESS = os.environ.get("SCCT_NAT_NO_ADDRESS", "0") == "1"
# SCCT_NAT_EVEN (default on): every datagram sent to the game gets an EVEN length. On the real PS2 every run hung on the first
# odd-length datagram (45 B) and handled even ones (44/46 B); PCSX2 handles both. PORT_ID: pick a ping id that gives an
# even length. ADDRESS: if odd, write the port with a leading '+' (atoi/strtol/sscanf accept it, no length change otherwise).
EVEN_LEN = os.environ.get("SCCT_NAT_EVEN", "1") != "0"
CLIENTS: list = []
LOG_FP = None


def now_ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    line = f"[{now_ts()}] {msg}"
    print(line, flush=True)
    if LOG_FP is not None:
        LOG_FP.write(line + "\n")
        LOG_FP.flush()


def new_ping_id() -> int:
    while True:
        pid = secrets.randbits(32)
        if pid != 0 and find_by_ping(pid) is None:
            return pid


def find_by_ping(ping_id: int):
    """(client, request dict) for a NAT ping id, or None."""
    for c in CLIENTS:
        r = getattr(c, "reqs", {}).get(ping_id)
        if r is not None:
            return c, r
    return None


def nat_reply(req_msg, subtype, ip_or_id, port, pick_id=False):
    """NatResponse whose SRP datagram (12 B header + message) has an even length. Returns (response, ping_id_or_ip)."""
    for _ in range(64 if pick_id else 1):
        val = new_ping_id() if pick_id else ip_or_id
        nr = gsm.NatResponse(req_msg, subtype, val, port)
        if not EVEN_LEN or (srp.SRP_HEADER_SIZE + len(bytes(nr))) % 2 == 0:
            return nr, val
    if (srp.SRP_HEADER_SIZE + len(bytes(nr))) % 2:
        nr.dl.lst[1][2] = "+" + nr.dl.lst[1][2]
    return nr, val


def split_messages(data: bytes):
    """Cuts an SRP data region into GS messages by the 24-bit size at the start of each (size includes the header)."""
    out = []
    off = 0
    while off + gsm.GSMSG_HEADER_SIZE <= len(data):
        size = (data[off] << 16) + (data[off + 1] << 8) + data[off + 2]
        if size < gsm.GSMSG_HEADER_SIZE or off + size > len(data):
            break
        out.append(data[off:off + size])
        off += size
    return out


def handle_ping(data: bytes, addr: tuple[str, int], now: float):
    pid = utils.read_u32_be(data)
    log(f"PING from {addr[0]}:{addr[1]} id={data.hex()}")
    found = find_by_ping(pid)
    if found is None:
        log(f"  unknown ping id {data.hex()} (no matching NAT request)")
        return []
    clt, r = found
    clt.last_seen = now
    if not r["pending"]:
        if NO_REPEAT_ACK:
            log(f"  repeat ping ignored (socket {r['sock']})")
            return []
        log(f"  -> bare ACK to {clt.addr}:{clt.port} (socket {r['sock']})")
        return [(srp.ack_segment(clt), (clt.addr, clt.port))]
    if NO_ADDRESS:
        log("  ping received, ADDRESS deliberately NOT sent (SCCT_NAT_NO_ADDRESS=1)")
        return []
    r["pending"] = False
    if ADDRESS_DELAY_S > 0:
        log(f"  waiting {ADDRESS_DELAY_S*1000:.0f} ms before ADDRESS")
        time.sleep(ADDRESS_DELAY_S)
    msg, _ = nat_reply(r["msg"], gsm.NAT_MSG.ADDRESS, utils.ipv4_to_u32_be(addr[0]), addr[1])
    out = bytes(srp.SRPResponse(None, clt, msg))
    log(f"  -> ADDRESS socket={r['sock']} {addr[0]}:{addr[1]} ({len(out)} bytes) to {clt.addr}:{clt.port}")
    return [(out, (clt.addr, clt.port))]


def handle_segment(data: bytes, addr: tuple[str, int], now: float, nat_port: int):
    packets = []
    clt = client.NatClient.find(addr, CLIENTS)
    req = srp.SRPRequest(data)
    hdr = req.segment.header
    flags = hdr.flags
    log(f"SRP {len(data)}B from {addr[0]}:{addr[1]} flags={[f for f in flags if f != 'SRP_ID']} seg={hdr.seg:#x} ack={hdr.ack:#x} data_size={hdr.data_size}")
    if clt is not None and srp.SRPHeaderFlags.SYN.name in flags:
        CLIENTS.remove(clt)
        clt = None
    if clt is None and srp.SRPHeaderFlags.FIN.name not in flags:
        if len(CLIENTS) >= 4096:
            log("  NAT table full, ignoring")
            return packets
        clt = client.NatClient(addr, None, None)
        clt.reqs = {}
        CLIENTS.append(clt)
        log(f"  added client {clt.addr}:{clt.port}")
    if clt is not None:
        clt.last_seen = now
    if req.segment.window and clt is not None:
        out = bytes(srp.SRPResponse(req, clt))
        packets.append((out, addr))
        log(f"  -> SYN+ACK ({len(out)} bytes)")
    elif req.segment.msg and clt is not None:
        region = data[srp.SRP_HEADER_SIZE:srp.SRP_HEADER_SIZE + hdr.data_size]
        chunks = split_messages(region)
        log(f"  data region {len(region)}B holds {len(chunks)} message(s)")
        answered = 0
        for chunk in chunks:
            try:
                m = gsm.Message(b'', in_buf=chunk)
            except Exception as e:
                log(f"  cannot parse message ({len(chunk)}B): {e!r}")
                continue
            if m.header.type != gsm.MESSAGE_TYPE.NAT:
                log(f"  non-NAT message {m.header.type.name} ignored")
                continue
            sock = m.dl.lst[1][0]
            if PORTID_LIMIT and answered >= PORTID_LIMIT:
                log(f"  NAT request socket={sock}: NOT answered (SCCT_NAT_PORTID_LIMIT={PORTID_LIMIT})")
                continue
            answered += 1
            nr, pid = nat_reply(m, gsm.NAT_MSG.PORT_ID, 0, nat_port, pick_id=True)
            clt.reqs[pid] = {"msg": m, "sock": sock, "pending": True}
            out = bytes(srp.SRPResponse(req, clt, nr))
            packets.append((out, addr))
            log(f"  NAT request socket={sock}: {m.dl}")
            log(f"  -> PORT_ID socket={sock} ping_id={pid:#010x} port={nat_port} ({len(out)} bytes)")
    elif clt is not None and srp.SRPHeaderFlags.FIN.name not in flags:
        log("  bare ACK from client (no reply needed)")
    if srp.SRPHeaderFlags.FIN.name in flags:
        if clt is not None and client.NatClient.find((clt.addr, clt.port), CLIENTS):
            CLIENTS.remove(clt)
            log(f"  removed client {clt.addr}:{clt.port}")
    return packets


def handle_packet(data: bytes, addr: tuple[str, int], now: float, nat_port: int):
    if len(data) < srp.SRP_HEADER_SIZE:
        if len(data) == 4:
            return handle_ping(data, addr, now)
        log(f"ignored {len(data)}B datagram from {addr[0]}:{addr[1]}: {data.hex()}")
        return []
    return handle_segment(data, addr, now, nat_port)


def main(argv=None) -> int:
    global LOG_FP
    ap = argparse.ArgumentParser(description="GSNAT responder v3")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--ports", default="45000")
    ap.add_argument("--max-bytes", type=int, default=1024)  # accepted for compatibility
    ap.add_argument("--reply-mode", default="srp_nat")      # accepted for compatibility
    ap.add_argument("--log-file", default="logs/udp_45000_reply.log")
    args, _ = ap.parse_known_args(argv)
    if args.log_file:
        p = pathlib.Path(args.log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        LOG_FP = open(p, "a", encoding="utf-8", errors="replace")
    ports = [int(x) for x in str(args.ports).replace(";", ",").split(",") if x.strip()]
    socks = {}
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((args.bind, port))
        socks[s] = port
    log("GSNAT v3 listening")
    log(f"  PORT_ID limit: {PORTID_LIMIT or 'all'}  gap: {GAP_S*1000:.0f} ms")
    log(f"  ADDRESS delay: {ADDRESS_DELAY_S*1000:.0f} ms  repeat-ping ACKs: {'off' if NO_REPEAT_ACK else 'on'}  ADDRESS: {'NEVER' if NO_ADDRESS else 'sent'}  even-length datagrams: {'on' if EVEN_LEN else 'off'}")
    log(f"  bind: {args.bind}  ports: {', '.join(str(p) for p in ports)}")
    log(f"  ubi-gs modules: {NEW_UBIGS}")
    while True:
        r, _, _ = select.select(list(socks), [], [], 10.0)
        now = time.monotonic()
        for c in [c for c in CLIENTS if now - c.last_seen > CLIENT_TIMEOUT]:
            CLIENTS.remove(c)
            log(f"expired client {c.addr}:{c.port}")
        for s in r:
            try:
                data, addr = s.recvfrom(2048)
            except OSError as e:
                log(f"recv error: {e}")
                continue
            try:
                packets = handle_packet(data, addr, now, socks[s])
            except Exception as e:  # one bad packet must not stop the service
                import traceback
                log(f"bad packet from {addr[0]}:{addr[1]} ({len(data)}B): {e!r}")
                log(traceback.format_exc())
                continue
            for i, (pkt, dest) in enumerate(packets):
                if i and GAP_S > 0:
                    time.sleep(GAP_S)
                s.sendto(pkt, dest)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
