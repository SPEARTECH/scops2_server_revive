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


def handle_ping(data: bytes, addr: tuple[str, int], now: float, nat_port: int = 45000):
    pid = utils.read_u32_be(data)
    log(f"PING from {addr[0]}:{addr[1]} id={data.hex()}" + (f" on :{nat_port}" if nat_port != 45000 else ""))
    found = find_by_ping(pid)
    if found is None:
        log(f"  unknown ping id {data.hex()} (no matching NAT request)")
        return []
    clt, r = found
    clt.last_seen = now
    if not r.get("extra") and nat_port == 45000:
        sx = SOCKET_EXT.setdefault(addr[0], {})
        if sx.get(str(r["sock"])) != addr[1]:
            log(f"  socket {r['sock']} of {addr[0]} shows external port {addr[1]}" + (f" (was {sx[str(r['sock'])]})" if str(r["sock"]) in sx else ""))
            sx[str(r["sock"])] = addr[1]
    if r.get("extra"):
        hit = r.get("hit", 0) + 1
        r["hit"] = hit
        if r.get("port"):
            HOST_TRAFFIC.setdefault(addr[0], {})[r["port"]] = now
            ext = HOST_EXT.setdefault(addr[0], {})
            locked = HOST_EXT_LOCK.setdefault(addr[0], set())
            # A ping only teaches the external port until the host's own game data has been seen on this hole: two
            # consoles behind one NAT both ping, and only the host sends game data (2026-11-05 00:16, port flip-flop).
            if r["port"] not in locked and ext.get(r["port"]) != addr[1]:
                log(f"  hole {r['port']}: {addr[0]} socket 1 shows external port {addr[1]}" + (f" (was {ext[r['port']]})" if r["port"] in ext else ""))
                ext[r["port"]] = addr[1]
        if hit <= 2 or hit % 50 == 0:
            log(f"  HOLE PING #{hit}: {addr[0]}:{addr[1]} reached port {r.get('port')}")
        return [(srp.ack_segment(clt), (clt.addr, clt.port))]
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
    packets = [(out, (clt.addr, clt.port))]
    if str(r["sock"]) == "1" and not r.get("extra") and RELAY_HOLES:
        clt.hole_msg = r["msg"]
        clt.hole_target = None       # port socket 1 currently pings (None = still 45000)
        clt.hole_last = 0.0
        clt.hole_count = 0
        log(f"  multi-hole: socket 1 of {clt.addr} will be steered to {RELAY_HOLES[0]} now, other holes on demand")
    if NAT_EXTRA_PORTID and not RELAY_HOLES and str(r["sock"]) == "1" and not r.get("extra"):
        for xport in NAT_EXTRA_PORTID:
            try:
                xr, xpid = nat_reply(r["msg"], gsm.NAT_MSG.PORT_ID, 0, xport, pick_id=True)
                clt.reqs[xpid] = {"msg": r["msg"], "sock": r["sock"], "pending": True, "extra": True, "port": xport}
                xout = bytes(srp.SRPResponse(None, clt, xr))
                packets.append((xout, (clt.addr, clt.port)))
                log(f"  -> EXTRA PORT_ID socket=1 ping_id={xpid:#010x} port={xport} ({len(xout)} bytes) [experiment: does the game ping it?]")
            except Exception as e:
                log(f"  EXTRA PORT_ID failed: {e!r}")
    return packets


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
# SCCT_RELAY_ALT_HOST_PORTS=1: a joiner's 2nd/3rd/4th connection (new source port) is delivered to the host's other
# presolved sockets (10071, 10072, 10073; all punched to 45000) instead of 3658, so the host sees distinct links and its
# replies (from that socket) can be routed back to the right joiner port. Natively the host tells links apart by the
# joiner's source port; through the relay they all arrive from <relay>:45000 and the host drops the 2nd connect.
RELAY_ALT_HOST_PORTS = [int(x) for x in os.environ.get("SCCT_RELAY_ALT_HOST_PORTS_LIST", "10071,10072,10073").split(",")] \
    if os.environ.get("SCCT_RELAY_ALT_HOST_PORTS", "0").strip() == "1" else []
# SCCT_NAT_EXTRA_PORTID=45001[,45002]: after socket 1's ADDRESS reply, send the console extra PORT_ID messages for socket 1
# pointing at these ports. If the game pings them, its 3658 socket has a second NAT hole (<relay>:45001 -> host:3658) that
# the relay could use as a distinct source for a second link. Experiment: only logs the pings (bare ACK, no ADDRESS).
NAT_EXTRA_PORTID = [int(x) for x in os.environ.get("SCCT_NAT_EXTRA_PORTID", "").replace(";", ",").split(",") if x.strip()]
# SCCT_RELAY_HOLES=45001,45002: multi-hole mode. Test of 2026-11-04 17:08 showed that an unsolicited PORT_ID makes the
# console's 3658 socket ping the NEW port instead of the old one (it replaces the target, it does not add one). So the
# service re-sends a PORT_ID for socket 1 every SCCT_RELAY_HOLE_REFRESH_S seconds, cycling through these ports: the console
# pings each in turn and keeps every hole open. The relay then uses one hole port per joiner link as its source, so the
# host sees distinct source ports per link (as it would natively) and its replies arrive on distinct relay sockets.
RELAY_HOLES = [int(x) for x in os.environ.get("SCCT_RELAY_HOLES", "").replace(";", ",").split(",") if x.strip()]
# Steering budget: the console's SRP receive window is 536 B and unsolicited PORT_IDs (~46 B) are apparently never freed
# from it; after ~11 of them the console stopped reacting (2026-11-04 17:19). So a steer is sent only when a hole that is
# NEEDED (the first hole, or one a live joiner link uses) has seen no host traffic for SCCT_RELAY_HOLE_STALE_S seconds,
# at most one every SCCT_RELAY_HOLE_MIN_S seconds per console. Typical session: login, game link, match start, match end.
# A consumer (Linux conntrack) router keeps an established UDP 5-tuple ~180 s; refresh a needed hole after this much idle.
HOLE_STALE_S = float(os.environ.get("SCCT_RELAY_HOLE_STALE_S", "100"))
HOLE_BUDGET = int(os.environ.get("SCCT_RELAY_HOLE_BUDGET", "10"))   # hard cap on steering messages per login
# Joiner source port -> hole index (the console uses sockets 10071-10073 for its outbound links and reuses each link
# later - a game link went idle for minutes and came back for a rematch - so the mapping must be permanent).
HOLE_PORT_BASE = int(os.environ.get("SCCT_RELAY_HOLE_PORT_BASE", "10071"))
# A steer does not make the console ping at once (it only re-targets its 25 s cycle), so a steered hole counts as fresh
# for HOLE_MIN_S and steers are at least HOLE_MIN_S apart per console.
HOLE_MIN_S = float(os.environ.get("SCCT_RELAY_HOLE_MIN_S", "30"))
HOLE_SOCKS: dict = {}       # port -> socket (bound hole ports)
HOST_TRAFFIC: dict = {}     # host ip -> {hole port: monotonic time of the last datagram from that host's 3658 on it}
# host ip -> {hole port: external port the host's 3658 socket shows on that hole}. A router may give a re-created
# mapping a different external port (2026-11-04 18:26: 3658 became 1096 on two holes while the third kept 3658).
HOST_EXT: dict = {}
HOST_EXT_LOCK: dict = {}   # host ip -> {hole ports whose external port was confirmed by the host's game data}
# host ip -> {socket no ("1".."4"): external port seen on that socket's own NAT pings to 45000}. Used by the alt-socket
# mode: every console socket keeps its 45000 hole open by itself (ping every 25 s), so a link delivered to socket k
# from 45000 needs no steering at all.
SOCKET_EXT: dict = {}
# Hole keepalive: a conntrack entry that has carried traffic both ways is refreshed by ANY packet through it, so the relay
# keeps such a hole open itself by sending a datagram inbound through it when it has been quiet for this long.
HOLE_KEEPALIVE_S = float(os.environ.get("SCCT_RELAY_HOLE_KEEPALIVE_S", "45"))
HOLE_KEEPALIVE_PAYLOAD = bytes.fromhex(os.environ.get("SCCT_RELAY_HOLE_KEEPALIVE_HEX", "0000"))  # 2 B (some routers drop empty UDP)
# Dead-hole rule: a joiner link is sending through a hole but the host has been silent on it for this long -> steer.
HOLE_DEAD_S = float(os.environ.get("SCCT_RELAY_HOLE_DEAD_S", "15"))
# Pre-open this many holes per console right after login (one per ping cycle, each confirmed by its ping before the
# next steer) and keep them warm with keepalives, so joiners' room and game links find open holes immediately.
HOLE_PREOPEN = int(os.environ.get("SCCT_RELAY_HOLE_PREOPEN", "1"))   # (informational; one spare hole is kept ready)
RELAY_TRAFFIC: dict = {}    # host ip -> {hole port: time of the last datagram the relay sent through it (joiner data or keepalive)}
KA_STATE: dict = {}         # keepalive counters for consoles that have no joiner flow yet (pre-opened holes)
LINK_SUMMARY_S = float(os.environ.get("SCCT_RELAY_LINK_SUMMARY_S", "30"))   # per-link activity summary interval (0 = off)
LAST_SUMMARY = 0.0
LINK_IDLE_S = 5.0           # a joiner endpoint silent for longer is not "live"
LINK_ESTABLISHED = 20       # (no longer used by pick_slot: every joiner endpoint keeps a dedicated hole)
RELAY_IDLE_S = float(os.environ.get("SCCT_RELAY_IDLE_S", "300"))   # flow dropped after this long without traffic (mappings no longer follow lobby leaves)
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
        prev = RELAY_MAP.get(j)
        if prev is not None and (prev.get("host") != h or (prev.get("user") and m.get("user") and prev.get("user") != m.get("user"))):
            reset_joiner(j, f"re-mapped by {m.get('user')} (was {prev.get('user')} -> {prev.get('host')})")
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
    elif op == "reset_joiner":
        reset_joiner(str(m.get("joiner", "")), f"{m.get('why')} ({m.get('user')})")
    elif op == "clear":
        RELAY_MAP.clear()
        RELAY_HOSTS.clear()
        log("RELAY ctrl: cleared")
    else:
        log(f"RELAY ctrl: unknown op {op!r}: {m}")
    relay_snapshot()


def reset_joiner(j: str, why: str) -> None:
    """Forget a joiner IP's endpoints and slots in every host flow (the mapping itself stays), so a new console or a new
    session from that IP starts with free slots. Two consoles behind one NAT share an IP: the old one's established links
    otherwise keep all slots and the new one's links are crammed into one (2026-11-04 19:13)."""
    n = 0
    for h, hv in RELAY_HOSTS.items():
        eps = [e for e in hv["eps"] if e[0] == j]
        for e in eps:
            hv["eps"].pop(e, None)
            hv.get("src", {}).pop(e, None)
            hv.get("slot", {}).pop(e, None)
            hv.get("ep_rx", {}).pop(e, None)
            hv.get("hports", {}).pop(e, None)
            hv.get("src_logged", {}).pop(e, None)
            hv.get("probe", set()).discard(e)
            hv.get("first_seen", {}).pop(e, None)
            n += 1
        if hv["joiner"][0] == j and eps:
            hv["joiner"] = eps[0]   # stale, replaced by the next packet
    if n:
        log(f"RELAY ctrl: reset joiner {j}: {n} endpoint(s)/slot(s) forgotten - {why}")


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
    prev_t = hv["eps"][addr][1] if addr in hv["eps"] else None
    hv["eps"][addr] = (sock, now)
    hv["rx"] += 1
    hv["last"] = now
    # host destination port for this joiner endpoint: first endpoint -> 3658, later ones -> alt host sockets (if enabled)
    hports = hv.setdefault("hports", {})
    hport = RELAY_HOST_PORT
    if RELAY_ALT_HOST_PORTS and not RELAY_HOLES:
        slot = pick_slot(hv, addr, now, prev_t, 1 + len(RELAY_ALT_HOST_PORTS))
        nominal = ([RELAY_HOST_PORT] + RELAY_ALT_HOST_PORTS)[slot]
        hport = SOCKET_EXT.get(host, {}).get(str(slot + 1)) or nominal
        if hports.get(addr) != hport:
            hports[addr] = hport
            log(f"RELAY host {host}: joiner link {addr[0]}:{addr[1]} -> host socket {slot + 1} (port {nominal}, external {hport}) from 45000")
    if RELAY_DUMP and hv["dump_rx"] < RELAY_DUMP:
        hv["dump_rx"] += 1
        log(f"RELAY DUMP J->H {addr[0]}:{addr[1]} -> :{hport} {len(data)}B: {data[:64].hex(' ')}")
    src_sock, src_port = NAT_MAIN_SOCK, NAT_MAIN_SOCK.getsockname()[1]
    if RELAY_HOLES:
        hport = RELAY_HOST_PORT
        src_port = pick_hole_port(hv, addr, now, prev_t, host)
        src_sock = HOLE_SOCKS[src_port]
    hv.setdefault("ep_rx", {})[addr] = hv.setdefault("ep_rx", {}).get(addr, 0) + 1
    if RELAY_HOLES:
        ext = HOST_EXT.get(host, {})
        hport = ext.get(src_port) or (next(iter(ext.values())) if ext else RELAY_HOST_PORT)
        if hv.get("dst_logged", {}).get(src_port) != hport:
            hv.setdefault("dst_logged", {})[src_port] = hport
            log(f"RELAY host {host}: hole {src_port} -> external port {hport}" + ("" if src_port in ext else " (not yet observed on this hole; guessing)"))
    try:
        src_sock.sendto(data, (host, hport))
        if RELAY_HOLES:
            RELAY_TRAFFIC.setdefault(host, {})[src_port] = now
            js = hv.setdefault("joiner_since", {})
            if src_port not in js or HOST_TRAFFIC.get(host, {}).get(src_port, 0.0) > js[src_port]:
                js[src_port] = now
    except OSError as e:
        log(f"RELAY -> host {host} send failed: {e}")
        return
    if hv["rx"] in (1, 2, 5, 10, 50) or hv["rx"] % 500 == 0:
        log(f"RELAY J->H {addr[0]}:{addr[1]} -> {host}:{hport} {len(data)}B (#{hv['rx']})")


def relay_from_host(data: bytes, addr: tuple[str, int], now: float, nat_port: int = 45000) -> bool:
    """Datagram from a relayed host's game socket (on 45000 or a hole port) -> its joiner. Returns True if consumed."""
    hv = RELAY_HOSTS.get(addr[0])
    if hv is None:
        return False
    hv["tx"] += 1
    hv["last"] = now
    dest, dsock = hv["joiner"], hv["sock"]
    cands = []
    if RELAY_ALT_HOST_PORTS and not RELAY_HOLES:
        SOCKET_EXT.setdefault(addr[0], {})   # replies come from the host socket the link was delivered to
    if RELAY_HOLES and nat_port in RELAY_HOLES:
        HOST_TRAFFIC.setdefault(addr[0], {})[nat_port] = now
        ext = HOST_EXT.setdefault(addr[0], {})
        if ext.get(nat_port) != addr[1]:
            log(f"RELAY host {addr[0]}: game data on hole {nat_port} from external port {addr[1]}" + (f" (was {ext[nat_port]})" if nat_port in ext else ""))
            ext[nat_port] = addr[1]
        HOST_EXT_LOCK.setdefault(addr[0], set()).add(nat_port)
        # multi-hole: the hole port the reply arrived on identifies the joiner link
        owners = [e for e, p in hv.get("src", {}).items() if p == nat_port and e in hv["eps"]]
        if owners:
            best = max(owners, key=lambda e: hv["eps"][e][1])
            dest, dsock = best, hv["eps"][best][0]
    else:
        # Route by the host's source socket: the joiner endpoints mapped to that host port are the candidates; with one
        # candidate it is unambiguous, with several (alt ports off: all on 3658) the most recent sender among them wins.
        cands = [ep for ep, hp in hv.get("hports", {}).items() if hp == addr[1] and ep in hv["eps"]]
    if cands:
        if hv["joiner"] in cands:
            dest, dsock = hv["joiner"], hv["sock"]
        else:
            best = max(cands, key=lambda ep: hv["eps"][ep][1])
            dest, dsock = best, hv["eps"][best][0]
    if RELAY_DUMP and hv["dump_tx"] < RELAY_DUMP:
        hv["dump_tx"] += 1
        log(f"RELAY DUMP H->J {addr[0]}:{addr[1]} -> {dest[0]}:{dest[1]} {len(data)}B: {data[:64].hex(' ')}")
    try:
        dsock.sendto(data, dest)
        hv.setdefault("ep_tx", {})[dest] = hv.setdefault("ep_tx", {}).get(dest, 0) + 1
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
        log(f"RELAY H->J {addr[0]}:{addr[1]} -> {dest[0]}:{dest[1]} {len(data)}B (#{hv['tx']}) from :{dsock.getsockname()[1]}")
    return True


def link_summary(now: float) -> None:
    """Every LINK_SUMMARY_S: one line per host flow with each joiner link's packets in/out since the last summary."""
    global LAST_SUMMARY
    if LINK_SUMMARY_S <= 0 or now - LAST_SUMMARY < LINK_SUMMARY_S:
        return
    LAST_SUMMARY = now
    for host, hv in RELAY_HOSTS.items():
        rx, tx = hv.setdefault("ep_rx", {}), hv.setdefault("ep_tx", {})
        prev = hv.setdefault("sum_prev", {})
        parts = []
        for ep in sorted(hv["eps"], key=lambda e: hv["eps"][e][1], reverse=True):
            r, t = rx.get(ep, 0), tx.get(ep, 0)
            pr, pt = prev.get(ep, (0, 0))
            prev[ep] = (r, t)
            dr, dt = r - pr, t - pt
            if dr == 0 and dt == 0 and now - hv["eps"][ep][1] > LINK_SUMMARY_S:
                continue
            hole = hv.get("src", {}).get(ep, "?")
            parts.append(f"{ep[0]}:{ep[1]}@{hole} in={dr} out={dt} idle={now - hv['eps'][ep][1]:.0f}s")
        if parts:
            log(f"RELAY links host {host}: " + " | ".join(parts))


def relay_expire(now: float) -> None:
    for h, hv in list(RELAY_HOSTS.items()):
        if now - hv["last"] > RELAY_IDLE_S:
            RELAY_HOSTS.pop(h, None)
            HOST_EXT.pop(h, None)
            HOST_EXT_LOCK.pop(h, None)
            RELAY_TRAFFIC.pop(h, None)
            log(f"RELAY host {h}: idle {RELAY_IDLE_S:.0f}s, flow dropped (rx={hv['rx']} tx={hv['tx']})")


def hole_steering(now: float) -> None:
    """Multi-hole mode. A steer (unsolicited PORT_ID for socket 1) is sent (a) once after login, to open the first hole,
    (b) when a joiner link is sending through a hole on which the host has been silent for HOLE_DEAD_S (never opened or
    expired), and (c) when the hole the console currently pings has been taken by a joiner link, to open the next spare.
    The console keeps only the hole it pings; established links keep theirs with their own traffic."""
    main = NAT_MAIN_SOCK
    for clt in list(CLIENTS):
        msg = getattr(clt, "hole_msg", None)
        if msg is None:
            continue
        if clt.hole_count >= HOLE_BUDGET:
            if not getattr(clt, "hole_budget_logged", False):
                clt.hole_budget_logged = True
                log(f"HOLE budget: {clt.addr} reached {HOLE_BUDGET} steering messages; no more steers this login")
            continue
        traffic = HOST_TRAFFIC.get(clt.addr, {})
        hv = RELAY_HOSTS.get(clt.addr)
        # serialize: the previous steer must have been confirmed by a ping (or be HOLE_MIN_S old) before the next one
        confirmed = clt.hole_target is None or traffic.get(clt.hole_target, 0.0) >= clt.hole_last
        if not confirmed and now - clt.hole_last < HOLE_MIN_S:
            continue
        target = None
        why = ""
        if clt.hole_target is None:
            target, why = RELAY_HOLES[0], "login"
        else:
            if hv is not None:
                dead = []
                since = hv.get("joiner_since", {})
                for ep, port in hv.get("src", {}).items():
                    if ep not in hv["eps"] or now - hv["eps"][ep][1] > LINK_IDLE_S:
                        continue      # link not sending right now
                    if port == clt.hole_target:
                        continue      # already the ping target
                    if port not in traffic:
                        dead.append((1e9, port, ep))   # never opened: steer at once
                    elif now - traffic.get(port, 0.0) > HOLE_DEAD_S and now - since.get(port, now) > HOLE_DEAD_S:
                        dead.append((now - traffic.get(port, 0.0), port, ep))
                if dead:
                    age, target, ep = max(dead)
                    why = f"link {ep[0]}:{ep[1]} sending, host silent on {target} " + ("(never opened)" if age >= 1e9 else f"for {age:.0f}s")
            if target is None and confirmed and hv is not None:
                # keep one spare: the hole the console pings is the only one its NAT keeps open, so once a joiner link
                # has taken it, move the console's pings to the next unassigned hole for the next link
                assigned = set(hv.get("src", {}).values())
                if clt.hole_target in assigned:
                    nxt = [p for p in RELAY_HOLES if p not in assigned]
                    if nxt:
                        target = min(nxt, key=lambda p: traffic.get(p, 0.0))
                        why = f"spare: hole {clt.hole_target} now carries a joiner link"
        if target is None:
            continue
        try:
            xr, xpid = nat_reply(msg, gsm.NAT_MSG.PORT_ID, 0, target, pick_id=True)
            clt.reqs[xpid] = {"msg": msg, "sock": "1", "pending": True, "extra": True, "port": target, "ts": now}
            main.sendto(bytes(srp.SRPResponse(None, clt, xr)), (clt.addr, clt.port))
            clt.hole_target = target
            clt.hole_last = now
            clt.hole_count += 1
            if not hasattr(clt, "hole_steer_time"):
                clt.hole_steer_time = {}
            clt.hole_steer_time[target] = now
            ka_n = (hv or {}).get("ka", {}).get(target, 0) if hv else 0
            ka_t = RELAY_TRAFFIC.get(clt.addr, {}).get(target)
            log(f"HOLE steer #{clt.hole_count}: {clt.addr} socket 1 -> ping port {target} ({why}; " +
                ", ".join(f"{p}: {now - traffic[p]:.0f}s ago" if p in traffic else f"{p}: never" for p in RELAY_HOLES if p in traffic or p == target) +
                f"; keepalives on {target}: {ka_n}" + (f", last relay send {now - ka_t:.0f}s ago" if ka_t else "") + ")")
            if clt.hole_count >= HOLE_BUDGET - 2:
                log(f"HOLE WARNING: {clt.addr} has received {clt.hole_count} steering messages (cap {HOLE_BUDGET}; the console stops reacting around 11)")
        except Exception as e:
            log(f"HOLE steer failed for {clt.addr}: {e!r}")
        old = [k for k, v in clt.reqs.items() if v.get("extra") and now - v.get("ts", now) > 600]
        for k in old:
            clt.reqs.pop(k, None)


def hole_keepalive(now: float) -> None:
    """Keep every hole that has carried host traffic open by sending a datagram inbound through it when quiet."""
    for host in list(HOST_TRAFFIC):
        hv = RELAY_HOSTS.get(host) or KA_STATE.setdefault(host, {})
        traffic = HOST_TRAFFIC.get(host, {})
        sent = RELAY_TRAFFIC.setdefault(host, {})
        ext = HOST_EXT.get(host, {})
        for port in RELAY_HOLES:
            if port not in traffic or port not in ext:
                continue   # hole never carried host traffic: nothing to keep alive yet
            last = max(traffic.get(port, 0.0), sent.get(port, 0.0))
            if now - last < HOLE_KEEPALIVE_S:
                continue
            try:
                HOLE_SOCKS[port].sendto(HOLE_KEEPALIVE_PAYLOAD, (host, ext[port]))
                sent[port] = now
                n = hv.setdefault("ka", {}).get(port, 0) + 1
                hv["ka"][port] = n
                if n <= 2 or n % 50 == 0:
                    log(f"HOLE keepalive #{n}: {len(HOLE_KEEPALIVE_PAYLOAD)}B from :{port} -> {host}:{ext[port]} (hole quiet {now - last:.0f}s)")
            except OSError as e:
                log(f"HOLE keepalive from :{port} failed: {e}")


def pick_slot(hv: dict, ep, now: float, prev_t, nslots: int) -> int:
    """One dedicated hole slot per joiner endpoint (ip, port), kept until the joiner is reset (GROUP_LEAVE) or the flow
    expires. This mirrors a direct path: the host keys its peer by the joiner's (ip, port) for the whole session and copes
    by itself with the client reusing a port across roles (probe link -> game link), so the relay must present one fixed
    source port per joiner endpoint. The earlier "share slot 0 until established / re-evaluate after idling" rule broke
    both joins on 2026-11-05 15:09: COOLDOR's room link (3 packets, then quiet 7 s - normal in the lobby) was moved from
    hole 45001 to 45002 when it resumed, so the host saw a new unknown peer and COOLDOR timed out; and DORDOR's join
    request was put on 45001 as well, where the host already had COOLDOR's peer, and the replies of both were misrouted."""
    slots = hv.setdefault("slot", {})
    if ep in slots:
        return slots[ep]
    held = set(slots.values())
    free = [i for i in range(nslots) if i not in held]
    if free:
        slot = free[0]
    else:
        # exhausted: take over the slot of the endpoint idle the longest (a probe link that finished long ago, or a
        # peer the host has dropped since)
        lru = min(slots, key=lambda e: hv["eps"][e][1] if e in hv["eps"] else 0.0)
        slot = slots.pop(lru)
        idle = now - hv["eps"][lru][1] if lru in hv["eps"] else 0.0
        hv.get("src", {}).pop(lru, None)
        hv.get("src_logged", {}).pop(lru, None)
        log(f"RELAY host: all {nslots} holes in use, {ep[0]}:{ep[1]} takes over hole slot {slot} from {lru[0]}:{lru[1]} (idle {idle:.0f}s)")
    slots[ep] = slot
    return slot


def host_nat_client(host: str):
    """The NatClient (SRP session on 10070) of a relayed host console, if it is logged in."""
    for c in CLIENTS:
        if c.addr == host and getattr(c, "hole_msg", None) is not None:
            return c
    return None


def pick_hole_port(hv: dict, ep, now: float, prev_t=None, host: str = None) -> int:
    """Source hole port for a joiner endpoint (ip, port).

    Facts this is built on (2026-11-05): the host's NAT keeps a hole only while the HOST sends through it - its 3658 socket
    pings exactly one hole (the last PORT_ID target) every 25 s, and a hole it stopped pinging is gone after ~60 s; the
    relay's inbound keepalives do not refresh it. A steer (PORT_ID) costs budget (~10 per login) and takes effect at the
    console's next ping, up to 25 s later. A link that the host answers keeps its own hole open with its game traffic.

    So: one hole per endpoint for good (the host keys the peer by (ip, port) and never sees it move), a new endpoint takes
    the READY hole (the one the host is pinging right now, unassigned), after which hole_steering steers the console to the
    next spare. Exceptions: a joiner's second link right behind its join probe shares the probe's hole (the host copes with
    the two from one source; this keeps the spare for the next joiner and saves a steer), and the probe's port, when the
    client reuses it later for another link, gets a hole of its own again."""
    src = hv.setdefault("src", {})
    probes = hv.setdefault("probe", set())
    first = hv.setdefault("first_seen", {})
    traffic = HOST_TRAFFIC.get(host, {}) if host else {}
    if ep in src:
        if ep in probes and prev_t is not None and now - prev_t > LINK_IDLE_S:
            old = src.pop(ep)
            probes.discard(ep)
            log(f"RELAY host {host}: join-probe port {ep[0]}:{ep[1]} reused after {now - prev_t:.0f}s idle; leaves shared hole {old} for one of its own")
        else:
            return src[ep]
    first[ep] = now
    assigned = set(src.values())
    clt = host_nat_client(host) if host else None
    target = clt.hole_target if clt is not None else None
    ready = target is not None and target not in assigned and traffic.get(target, 0.0) >= clt.hole_last
    port, why = None, ""
    # same joiner, second link within 3 s of its join probe (which has sent only a couple of packets): share the probe's hole
    for e, p in src.items():
        if e[0] == ep[0] and e not in probes and now - first.get(e, 0.0) <= 3.0 and hv.get("ep_rx", {}).get(e, 0) <= 4 \
                and list(src.values()).count(p) == 1 and traffic.get(p, 0.0) > 0:
            port, why = p, f"shares the hole of join probe {e[0]}:{e[1]}"
            probes.add(e)
            break
    if port is None and ready:
        port, why = target, "ready: the host is pinging it"
    elif port is None and target is not None and target not in assigned:
        port, why = target, f"steered {now - clt.hole_last:.0f}s ago, waiting for the host's ping"
    elif port is None:
        free = [p for p in RELAY_HOLES if p not in assigned]
        if free:
            port = min(free, key=lambda p: traffic.get(p, 0.0))
            why = "free hole, needs a steer" + (" (never pinged)" if port not in traffic else f" (host silent {now - traffic[port]:.0f}s)")
        else:
            lru = min(src, key=lambda e: hv["eps"][e][1] if e in hv["eps"] else 0.0)
            port = src.pop(lru)
            probes.discard(lru)
            idle = now - hv["eps"][lru][1] if lru in hv["eps"] else 0.0
            why = f"all {len(RELAY_HOLES)} holes in use, taken over from {lru[0]}:{lru[1]} (idle {idle:.0f}s)"
    src[ep] = port
    log(f"RELAY host {host}: joiner link {ep[0]}:{ep[1]} <-> host:{RELAY_HOST_PORT} via hole port {port} ({why})")
    return port


def handle_packet(data: bytes, addr: tuple[str, int], now: float, nat_port: int):
    if len(data) == 4:
        return handle_ping(data, addr, now, nat_port)
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
        if relay_from_host(data, addr, now, nat_port):
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
    global HOLE_SOCKS
    for hp in RELAY_HOLES:
        if hp not in socks.values():
            hs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            hs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            hs.bind((args.bind, hp))
            socks[hs] = hp
        HOLE_SOCKS[hp] = next(sk for sk, pt in socks.items() if pt == hp)
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
        log(f"  RELAY: joiner ports {', '.join(str(p) for p in RELAY_PORTS)} -> host:{RELAY_HOST_PORT} from :{socks[NAT_MAIN_SOCK]}; control 127.0.0.1:{RELAY_CTRL_PORT}; idle {RELAY_IDLE_S:.0f}s; dump {RELAY_DUMP} pkts/dir; fanout {RELAY_FANOUT_S:.0f}s; alt host ports {RELAY_ALT_HOST_PORTS or 'off'}")
    if NAT_EXTRA_PORTID and not RELAY_HOLES:
        log(f"  EXTRA PORT_ID experiment: socket 1 also told to ping {NAT_EXTRA_PORTID} (these ports must be in --ports)")
    if RELAY_HOLES:
        log(f"  MULTI-HOLE: {len(RELAY_HOLES)} holes; one hole per joiner endpoint; the console pings one spare hole, a new link takes it and the next spare is steered; steer also when a sending link finds a hole never opened / host silent >{HOLE_DEAD_S:.0f}s (budget {HOLE_BUDGET}); {len(HOLE_KEEPALIVE_PAYLOAD)}B keepalives every {HOLE_KEEPALIVE_S:.0f}s")
    elif RELAY_ALT_HOST_PORTS:
        log(f"  ALT-SOCKET mode: links delivered from 45000 to host sockets 3658 then {RELAY_ALT_HOST_PORTS} (external ports learned from each socket's pings); no steering")
    else:
        log("  RELAY: off")
    all_socks = list(socks) + list(relay_socks) + ([ctrl_sock] if ctrl_sock is not None else [])
    while True:
        r, _, _ = select.select(all_socks, [], [], 1.0 if RELAY_HOLES else 10.0)
        now = time.monotonic()
        if RELAY_HOLES:
            hole_steering(now)
            hole_keepalive(now)
        for c in [c for c in CLIENTS if now - c.last_seen > CLIENT_TIMEOUT]:
            CLIENTS.remove(c)
            log(f"expired client {c.addr}:{c.port}")
            if not any(o.addr == c.addr for o in CLIENTS) and c.addr not in RELAY_HOSTS:
                for d in (HOST_TRAFFIC, HOST_EXT, HOST_EXT_LOCK, RELAY_TRAFFIC, KA_STATE):
                    d.pop(c.addr, None)
        if RELAY_HOSTS:
            relay_expire(now)
            link_summary(now)
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
