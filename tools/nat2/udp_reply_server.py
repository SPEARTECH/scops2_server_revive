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
PUBLIC_ADDR = os.environ.get("SCCT_NAT_PUBLIC_ADDR", "0") == "1"
PUBLIC_IP = os.environ.get("SCCT_PUBLIC_IP", "").strip()
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
    # The game copies this address into the room info it creates, and other players dial it. For internet play with
    # the server on a home PC, a LAN client must be reported with the PUBLIC address (SCCT_NAT_PUBLIC_ADDR=1 and
    # SCCT_PUBLIC_IP set), otherwise outside players receive a 192.168.x.x host address. LAN-only play then needs
    # NAT loopback on the router, so leave this off for pure LAN sessions.
    rep_ip = addr[0]
    if PUBLIC_ADDR and PUBLIC_IP:
        try:
            import ipaddress as _ipa
            _a = _ipa.ip_address(rep_ip)
            if _a.is_private or _a.is_loopback or _a.is_link_local:
                rep_ip = PUBLIC_IP
        except ValueError:
            pass
    msg, _ = nat_reply(r["msg"], gsm.NAT_MSG.ADDRESS, utils.ipv4_to_u32_be(rep_ip), addr[1])
    out = bytes(srp.SRPResponse(None, clt, msg))
    log(f"  -> ADDRESS socket={r['sock']} {rep_ip}:{addr[1]} ({len(out)} bytes) to {clt.addr}:{clt.port}" + (f" (observed {addr[0]})" if rep_ip != addr[0] else ""))
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


# ─── UDP relay (SCCT_RELAY) ─────────────────────────────────────────────────────────────────────────────────────
# A console behind a port-restricted NAT / inbound-filtering ISP can only be reached from the exact server endpoint it
# keeps talking to: this service on 45000, which the game pings from its 3658 socket every ~25 s. The relay uses that
# hole. The lobby server (ubigs_router_wm_server.py, SCCT_RELAY=1) hands an outside joiner the relay's address instead
# of the host's and tells this process "joiner J -> host H" over the control port. Joiner traffic arriving on the relay
# ports (3658, 10070-10080) is forwarded to H:3658 FROM 45000; whatever H sends back to 45000 from its game socket is
# forwarded to J from the relay port J used. v1: one relayed joiner per host (the host sees every relayed peer as
# <relay>:45000, so a second joiner would collide).
RELAY_PORTS: list[int] = []
RELAY_CTRL_PORT = 0
RELAY_HOST_PORT = int(os.environ.get("SCCT_RELAY_HOST_PORT", "3658"))
# Source port of the game's SRP/NAT control session (never relayed).
RELAY_SRP_PORT = int(os.environ.get("SCCT_RELAY_SRP_PORT", "10070"))
RELAY_IDLE_S = float(os.environ.get("SCCT_RELAY_IDLE_S", "600"))
RELAY_MAP: dict = {}        # joiner_ip -> {"host": host_ip, "room": ..., "user": ..., "ts": t}
RELAY_HOSTS: dict = {}      # host_ip -> {"joiner": (ip, port), "sock": relay socket, "rx": n, "tx": n, "last": t, "first": t}
RELAY_UNMAPPED_LOGGED: set = set()
RELAY_SNAPSHOT = os.environ.get("SCCT_RELAY_SNAPSHOT", "state/relay_map.json")
# SCCT_RELAY_DUMP=N: hex-dump the first N datagrams of each direction per host flow (to learn the peer protocol).
RELAY_DUMP = int(os.environ.get("SCCT_RELAY_DUMP", "0") or 0)
# SCCT_RELAY_FANOUT_S: the host's replies go to the LAST joiner endpoint that sent; with a value > 0 they also go to
# every other endpoint of the same joiner IP seen within that many seconds (a client may use two ports for one join).
RELAY_FANOUT_S = float(os.environ.get("SCCT_RELAY_FANOUT_S", "0") or 0)
NAT_MAIN_SOCK = None        # the 45000 socket (host-facing side of the relay)


def _parse_port_list(spec: str) -> list[int]:
    out: list[int] = []
    for part in str(spec).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def relay_snapshot() -> None:
    if not RELAY_SNAPSHOT:
        return
    try:
        import json as _json
        p = pathlib.Path(RELAY_SNAPSHOT)
        p.parent.mkdir(parents=True, exist_ok=True)
        snap = {
            "map": RELAY_MAP,
            "hosts": {h: {"joiner": list(v["joiner"]), "relay_port": v["sock"].getsockname()[1], "rx": v["rx"], "tx": v["tx"]}
                      for h, v in RELAY_HOSTS.items()},
            "ts": now_ts(),
        }
        p.write_text(_json.dumps(snap, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"RELAY snapshot failed: {e!r}")


def relay_control(data: bytes, addr: tuple[str, int]) -> None:
    """Control messages from the lobby server (JSON, one per datagram, loopback only)."""
    import json as _json
    try:
        m = _json.loads(data.decode("utf-8", errors="replace"))
    except Exception as e:
        log(f"RELAY ctrl: bad message from {addr[0]}:{addr[1]}: {e!r}")
        return
    op = str(m.get("op", ""))
    if op == "map":
        j, h = str(m.get("joiner", "")), str(m.get("host", ""))
        if not j or not h:
            log(f"RELAY ctrl: map without joiner/host: {m}")
            return
        RELAY_MAP[j] = {"host": h, "room": m.get("room"), "user": m.get("user"), "ts": now_ts()}
        RELAY_UNMAPPED_LOGGED.discard(j)
        log(f"RELAY ctrl: map joiner {j} ({m.get('user')}) -> host {h} room {m.get('room')}")
    elif op == "unmap":
        j = str(m.get("joiner", ""))
        ent = RELAY_MAP.pop(j, None)
        if ent is not None:
            h = ent["host"]
            hv = RELAY_HOSTS.get(h)
            if hv is not None and hv["joiner"][0] == j:
                RELAY_HOSTS.pop(h, None)
            log(f"RELAY ctrl: unmap joiner {j} (was -> {h})")
    elif op == "unmap_host":
        h = str(m.get("host", ""))
        gone = [j for j, e in RELAY_MAP.items() if e["host"] == h]
        for j in gone:
            RELAY_MAP.pop(j, None)
        RELAY_HOSTS.pop(h, None)
        log(f"RELAY ctrl: unmap host {h} (joiners dropped: {gone})")
    elif op == "unmap_any":
        ip = str(m.get("ip", ""))
        gone = [j for j, e in RELAY_MAP.items() if j == ip or e["host"] == ip]
        for j in gone:
            RELAY_MAP.pop(j, None)
        RELAY_HOSTS.pop(ip, None)
        for h, hv in list(RELAY_HOSTS.items()):
            if hv["joiner"][0] == ip:
                RELAY_HOSTS.pop(h, None)
        if gone:
            log(f"RELAY ctrl: unmap_any {ip} (entries dropped: {gone})")
    elif op == "clear":
        RELAY_MAP.clear()
        RELAY_HOSTS.clear()
        log("RELAY ctrl: cleared")
    else:
        log(f"RELAY ctrl: unknown op {op!r}: {m}")
    relay_snapshot()


def relay_from_joiner(sock: socket.socket, data: bytes, addr: tuple[str, int], now: float) -> None:
    """Datagram from an outside joiner on a relay port -> host:3658 from 45000."""
    ent = RELAY_MAP.get(addr[0])
    if ent is None:
        if addr[0] not in RELAY_UNMAPPED_LOGGED:
            RELAY_UNMAPPED_LOGGED.add(addr[0])
            log(f"RELAY drop: {len(data)}B on :{sock.getsockname()[1]} from {addr[0]}:{addr[1]} - no mapping from the lobby server (logged once)")
        return
    host = ent["host"]
    hv = RELAY_HOSTS.get(host)
    if hv is None:
        hv = {"joiner": addr, "sock": sock, "rx": 0, "tx": 0, "last": now, "first": now, "eps": {}, "dump_rx": 0, "dump_tx": 0}
        RELAY_HOSTS[host] = hv
        log(f"RELAY host {host}: flow opened by {addr[0]}:{addr[1]} via relay port {sock.getsockname()[1]} -> {host}:{RELAY_HOST_PORT} from {NAT_MAIN_SOCK.getsockname()[1]}")
    elif hv["joiner"] != addr or hv["sock"] is not sock:
        if hv["joiner"][0] != addr[0]:
            log(f"RELAY host {host}: joiner IP changes {hv['joiner'][0]} -> {addr[0]} (v1: one joiner per host, replies follow the last sender)")
        elif addr not in hv["eps"]:
            log(f"RELAY host {host}: joiner {addr[0]} now also sends from port {addr[1]} (replies follow the last sender)")
        hv["joiner"] = addr
        hv["sock"] = sock
    hv["eps"][addr] = (sock, now)
    hv["rx"] += 1
    hv["last"] = now
    if RELAY_DUMP and hv["dump_rx"] < RELAY_DUMP:
        hv["dump_rx"] += 1
        log(f"RELAY DUMP J->H {addr[0]}:{addr[1]} {len(data)}B: {data[:64].hex(' ')}")
    try:
        NAT_MAIN_SOCK.sendto(data, (host, RELAY_HOST_PORT))
    except OSError as e:
        log(f"RELAY -> host {host} send failed: {e}")
        return
    if hv["rx"] in (1, 2, 5, 10, 50) or hv["rx"] % 500 == 0:
        log(f"RELAY J->H {addr[0]}:{addr[1]} -> {host}:{RELAY_HOST_PORT} {len(data)}B (#{hv['rx']})")


def relay_from_host(data: bytes, addr: tuple[str, int], now: float) -> bool:
    """Datagram on 45000 from a relayed host's game socket -> its joiner. Returns True if consumed."""
    hv = RELAY_HOSTS.get(addr[0])
    if hv is None:
        return False
    hv["tx"] += 1
    hv["last"] = now
    if RELAY_DUMP and hv["dump_tx"] < RELAY_DUMP:
        hv["dump_tx"] += 1
        log(f"RELAY DUMP H->J {addr[0]}:{addr[1]} {len(data)}B: {data[:64].hex(' ')}")
    try:
        hv["sock"].sendto(data, hv["joiner"])
    except OSError as e:
        log(f"RELAY -> joiner {hv['joiner']} send failed: {e}")
        return True
    if RELAY_FANOUT_S > 0:
        for ep, (esock, et) in list(hv["eps"].items()):
            if ep == hv["joiner"] or ep[0] != hv["joiner"][0]:
                continue
            if now - et > RELAY_FANOUT_S:
                hv["eps"].pop(ep, None)
                continue
            try:
                esock.sendto(data, ep)
            except OSError:
                pass
    if hv["tx"] in (1, 2, 5, 10, 50) or hv["tx"] % 500 == 0:
        log(f"RELAY H->J {addr[0]}:{addr[1]} -> {hv['joiner'][0]}:{hv['joiner'][1]} {len(data)}B (#{hv['tx']}) from :{hv['sock'].getsockname()[1]}")
    return True


def relay_expire(now: float) -> None:
    for h, hv in list(RELAY_HOSTS.items()):
        if now - hv["last"] > RELAY_IDLE_S:
            RELAY_HOSTS.pop(h, None)
            log(f"RELAY host {h}: idle {RELAY_IDLE_S:.0f}s, flow dropped (rx={hv['rx']} tx={hv['tx']})")


def handle_packet(data: bytes, addr: tuple[str, int], now: float, nat_port: int):
    if len(data) == 4:
        return handle_ping(data, addr, now)
    # Relayed host: anything from it that is not a 4-byte NAT ping and does not come from its SRP control port is game
    # data for its joiner. Classification is by SOURCE PORT only: the game's SRP/NAT session always runs on 10070, the
    # game sockets (3658, 10071-10073) only ever send pings to this service. Inspecting the scrambled game payload for
    # SRP flag bits misfired on 2026-11-04 16:48 (a game packet "looked like" an SRP SYN, the host's game socket was
    # registered as a NAT client and every later packet from it went to the SRP parser instead of the joiner).
    if RELAY_HOSTS and addr[0] in RELAY_HOSTS and addr[1] != RELAY_SRP_PORT:
        stale = client.NatClient.find(addr, CLIENTS)
        if stale is not None:
            CLIENTS.remove(stale)
            log(f"RELAY: dropped stray NAT-client entry for game socket {addr[0]}:{addr[1]}")
        if relay_from_host(data, addr, now):
            return []
    if len(data) < srp.SRP_HEADER_SIZE:
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
    ap.add_argument("--relay-ports", default=os.environ.get("SCCT_RELAY_PORTS", ""),
                    help="UDP relay: joiner-facing ports, e.g. 3658,10070-10080 (empty = relay off)")
    ap.add_argument("--relay-ctrl-port", type=int, default=int(os.environ.get("SCCT_RELAY_CTRL_PORT", "45099")),
                    help="loopback UDP control port the lobby server talks to (default 45099)")
    args, _ = ap.parse_known_args(argv)
    global RELAY_PORTS, RELAY_CTRL_PORT, NAT_MAIN_SOCK
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
        if NAT_MAIN_SOCK is None:
            NAT_MAIN_SOCK = s
    relay_socks: dict = {}
    ctrl_sock = None
    RELAY_PORTS = _parse_port_list(args.relay_ports) if str(args.relay_ports).strip() else []
    if RELAY_PORTS:
        RELAY_CTRL_PORT = int(args.relay_ctrl_port)
        for port in RELAY_PORTS:
            rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rs.bind((args.bind, port))
            relay_socks[rs] = port
        ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ctrl_sock.bind(("127.0.0.1", RELAY_CTRL_PORT))
    log("GSNAT v3 listening")
    log(f"  PORT_ID limit: {PORTID_LIMIT or 'all'}  gap: {GAP_S*1000:.0f} ms")
    log(f"  ADDRESS delay: {ADDRESS_DELAY_S*1000:.0f} ms  repeat-ping ACKs: {'off' if NO_REPEAT_ACK else 'on'}  ADDRESS: {'NEVER' if NO_ADDRESS else 'sent'}  even-length datagrams: {'on' if EVEN_LEN else 'off'}")
    log(f"  bind: {args.bind}  ports: {', '.join(str(p) for p in ports)}")
    log(f"  ubi-gs modules: {NEW_UBIGS}")
    if RELAY_PORTS:
        log(f"  RELAY: joiner ports {', '.join(str(p) for p in RELAY_PORTS)} -> host:{RELAY_HOST_PORT} from :{socks[NAT_MAIN_SOCK]}; control 127.0.0.1:{RELAY_CTRL_PORT}; idle {RELAY_IDLE_S:.0f}s; dump {RELAY_DUMP} pkts/dir; fanout {RELAY_FANOUT_S:.0f}s")
    else:
        log("  RELAY: off")
    all_socks = list(socks) + list(relay_socks) + ([ctrl_sock] if ctrl_sock is not None else [])
    while True:
        r, _, _ = select.select(all_socks, [], [], 10.0)
        now = time.monotonic()
        for c in [c for c in CLIENTS if now - c.last_seen > CLIENT_TIMEOUT]:
            CLIENTS.remove(c)
            log(f"expired client {c.addr}:{c.port}")
        if RELAY_HOSTS:
            relay_expire(now)
        for s in r:
            try:
                data, addr = s.recvfrom(2048)
            except OSError as e:
                log(f"recv error: {e}")
                continue
            if s is ctrl_sock:
                relay_control(data, addr)
                continue
            if s in relay_socks:
                try:
                    relay_from_joiner(s, data, addr, now)
                except Exception as e:
                    log(f"RELAY error from {addr[0]}:{addr[1]}: {e!r}")
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
