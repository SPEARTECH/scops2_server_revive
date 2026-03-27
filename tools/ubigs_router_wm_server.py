#!/usr/bin/env python3
"""Ubisoft GS Router "Wait Module" (TCP).

The router redirects the client here via JOINWAITMODULE. This module typically:
- completes login (LOGINWAITMODULE)
- handles friend list bootstrap (LOGINFRIENDS)
- answers PROXY_HANDLER module discovery (persistent data proxy, ladder proxy, etc)

This implementation is intentionally minimal and primarily exists to let us see
what the Chaos Theory PS2 client does next.

"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import threading
import time
from typing import TextIO

from ubigs_common import (
    ClientState,
    decode_gsm_message,
    ensure_ubigs_importable,
    file_ts,
    hexdump,
    log_line,
    now_ts,
    pop_gsm_frames,
    safe_slug,
    save_blob,
    sha16,
)

# Thread-safe lobby player counter
_lobby_lock = threading.Lock()
_lobby_player_count = 0


def lobby_player_join():
    global _lobby_player_count
    with _lobby_lock:
        _lobby_player_count += 1
        return _lobby_player_count


def lobby_player_leave():
    global _lobby_player_count
    with _lobby_lock:
        _lobby_player_count = max(0, _lobby_player_count - 1)
        return _lobby_player_count


def lobby_player_count():
    with _lobby_lock:
        return _lobby_player_count


# ─── Shared Lobby State (room tracking + broadcasting) ────────────
LOBBY_ROOMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "lobby_rooms.json")


class LobbyState:
    """Thread-safe shared state for lobby room broadcasting between sessions.

    Rooms are also persisted to state/lobby_rooms.json so that separate
    server processes (Router on 40000 vs standalone WM on 40005) can share
    the room list.
    """

    def __init__(self):
        self.lock = threading.Lock()
        # room_id -> {"room": Room, "owner_key": str}
        self.rooms = {}
        # session_key -> {"conn": socket, "username": str, "source": str}
        # Multiple sessions per player (WM conn + lobby server conn) are fine;
        # they have different session keys (different src ports).
        self.sessions = {}
        self._next_room_id = 100
        self._last_removed_username = None

    # --- File-backed room persistence for cross-process sharing ---

    def _save_rooms_to_file(self):
        """Write current rooms to JSON file (call while holding self.lock)."""
        import base64
        try:
            ensure_ubigs_importable()
            data = {}
            for rid, info in self.rooms.items():
                room = info["room"]
                # Save ALL room fields so cross-process loading is complete
                info_b64 = ""
                if room.info and isinstance(room.info, (bytes, bytearray)):
                    info_b64 = base64.b64encode(bytes(room.info)).decode("ascii")
                data[str(rid)] = {
                    "group_id": room.group_id,
                    "group_name": room.group_name,
                    "master": room.master,
                    "allowed_games": room.allowed_games,
                    "max_players": room.max_players,
                    "event_id": room.event_id,
                    "game_version": room.game_version,
                    "gs_version": room.gs_version,
                    "config": room.config,
                    "parent_id": room.parent_id,
                    "ip_addr": room.ip_addr,
                    "alt_ip_addr": room.alt_ip_addr,
                    "info_b64": info_b64,
                    "nb_players": room.nb_players,
                    "max_visitors": room.max_visitors,
                    "nb_visitors": room.nb_visitors,
                    "games": room.games,
                    "owner_key": info["owner_key"],
                    "raw_frame": info.get("raw_frame", b"").hex() if info.get("raw_frame") else None,
                }
            p = pathlib.Path(LOBBY_ROOMS_FILE)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load_rooms_from_file(self):
        """Read rooms from JSON file (for cross-process queries like subtype 109)."""
        import base64
        try:
            ensure_ubigs_importable()
            from group import Room
            p = pathlib.Path(LOBBY_ROOMS_FILE)
            if not p.exists():
                return []
            data = json.loads(p.read_text())
            rooms = []
            for rid_str, info in data.items():
                room = Room(
                    id=int(info["group_id"]),
                    name=info.get("group_name", ""),
                    master=info.get("master", ""),
                    game_mode=int(info.get("event_id", 0)),
                )
                room.allowed_games = info.get("allowed_games", "")
                room.max_players = int(info.get("max_players", 8))
                room.game_version = info.get("game_version", "")
                room.gs_version = info.get("gs_version", "")
                room.config = int(info.get("config", 0))
                room.parent_id = int(info.get("parent_id", 1))
                room.ip_addr = info.get("ip_addr", "")
                room.alt_ip_addr = info.get("alt_ip_addr", "")
                room.nb_players = int(info.get("nb_players", 1))
                room.max_visitors = int(info.get("max_visitors", 8))
                room.nb_visitors = int(info.get("nb_visitors", 0))
                room.games = info.get("games", "")
                # Restore binary info blob from base64
                info_b64 = info.get("info_b64", "")
                if info_b64:
                    room.info = base64.b64decode(info_b64)
                else:
                    room.info = b''
                rooms.append(room)
            return rooms
        except Exception:
            return []

    def register_session(self, key, conn, username, source="unknown"):
        with self.lock:
            self.sessions[key] = {"conn": conn, "username": username, "source": source}

    def get_session_usernames(self, exclude_key=None):
        """Return list of usernames of all registered sessions (excluding one)."""
        with self.lock:
            return [s["username"] for k, s in self.sessions.items()
                    if k != exclude_key and s.get("username")]

    def unregister_session(self, key):
        with self.lock:
            removed_username = None
            session = self.sessions.pop(key, None)
            if session:
                removed_username = session.get("username")
            # Also store removed username for MEMBER_LEAVE broadcast
            self._last_removed_username = removed_username
            to_remove = [rid for rid, info in self.rooms.items() if info["owner_key"] == key]
            removed = []
            for rid in to_remove:
                removed.append(self.rooms.pop(rid))
            if removed:
                self._save_rooms_to_file()
            return removed  # list of {"room": Room, "owner_key": str}

    def create_room(self, room, owner_key, raw_frame=None):
        """Store a Room object built from CREATE_ROOM data.

        raw_frame: the original CREATE_ROOM frame bytes (with b5 set to S->P)
                   for relaying to other players.
        """
        with self.lock:
            self.rooms[room.group_id] = {"room": room, "owner_key": owner_key, "raw_frame": raw_frame}
            self._save_rooms_to_file()
            return room

    def remove_room_by_owner(self, owner_key):
        with self.lock:
            to_remove = [rid for rid, info in self.rooms.items() if info["owner_key"] == owner_key]
            removed = []
            for rid in to_remove:
                removed.append(self.rooms.pop(rid))
            if removed:
                self._save_rooms_to_file()
            return removed

    def get_rooms(self):
        """Return list of Room objects."""
        with self.lock:
            return [info["room"] for info in self.rooms.values()]

    def get_rooms_with_frames(self):
        """Return list of (Room, raw_frame_bytes_or_None) tuples."""
        with self.lock:
            return [(info["room"], info.get("raw_frame")) for info in self.rooms.values()]

    def next_room_id(self):
        with self.lock:
            rid = self._next_room_id
            self._next_room_id += 1
            return rid

    def broadcast(self, frame_bytes, exclude_key=None, log_fp=None):
        """Send frame bytes to all registered lobby sessions except exclude_key."""
        with self.lock:
            targets = [(k, s) for k, s in self.sessions.items() if k != exclude_key]
        for key, session in targets:
            try:
                session["conn"].sendall(frame_bytes)
                log_line(f"[{now_ts()}] LOBBY_BROADCAST -> {key} len={len(frame_bytes)}", log_fp=log_fp)
            except Exception as e:
                log_line(f"[{now_ts()}] LOBBY_BROADCAST -> {key} FAILED: {e}", log_fp=log_fp)


_lobby_state = LobbyState()


def _build_lobby_msg_frame(dl_list, b5=0x24):
    """Build a raw LOBBY_MSG frame with GS property for broadcasting.

    Uses b5=0x24 (S->P) by default — lobby server pushes come from the
    Server entity (2) to the Player entity (4).
    """
    ensure_ubigs_importable()
    import gsm
    from data import List

    # Create a fake request so GSMResponse can build the frame
    fake_hdr = bytes([0, 0, 6, 0, gsm.MESSAGE_TYPE.LOBBY_MSG.value, 0x82])

    class _Fake:
        pass

    fake_msg = _Fake()
    fake_msg.header = gsm.GSMessageHeader(fake_hdr)
    fake_msg.dl = None

    res = gsm.GSMResponse(fake_msg)
    res.header.property = gsm.PROPERTY.GS
    res.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
    res.dl = List(dl_list)

    out = bytearray(bytes(res))
    if len(out) >= 6:
        out[5] = b5  # Force b5 for broadcast
    return bytes(out)


def _load_or_create_fixed_rsa_keypair(path: str, *, log_fp: TextIO | None):
    """Load or create a persistent RSA keypair shared across router processes."""
    ensure_ubigs_importable()
    import rsa
    import pkc

    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    def _read_pair() -> tuple:
        raw = json.loads(p.read_text(encoding="utf-8"))
        n = int(raw["n"])
        e = int(raw["e"])
        d = int(raw["d"])
        p1 = int(raw["p"])
        q1 = int(raw["q"])
        pub = rsa.PublicKey(n, e)
        priv = rsa.PrivateKey(n, e, d, p1, q1)
        return (pub, priv)

    # Avoid startup races: creator uses O_EXCL, others wait and load.
    for _ in range(40):
        if p.exists():
            try:
                pair = _read_pair()
                log_line(f"[{now_ts()}] ROUTER_WM fixed-rsa loaded from {p}", log_fp=log_fp)
                return pair
            except Exception:
                time.sleep(0.05)
                continue

        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            time.sleep(0.05)
            continue

        try:
            pub, priv = pkc.keygen()
            payload = {
                "n": str(pub.n),
                "e": str(pub.e),
                "d": str(priv.d),
                "p": str(priv.p),
                "q": str(priv.q),
            }
            os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
            log_line(f"[{now_ts()}] ROUTER_WM fixed-rsa generated at {p}", log_fp=log_fp)
            return (pub, priv)
        finally:
            os.close(fd)

    # Final fallback.
    pair = _read_pair()
    log_line(f"[{now_ts()}] ROUTER_WM fixed-rsa loaded (fallback) from {p}", log_fp=log_fp)
    return pair


def _build_boot_ke1(*, fixed_rsa: tuple) -> bytes:
    """Build a server-first KEY_EXCHANGE(1) frame from the fixed RSA public key.

    Sends the server's RSA public key to the client, which is the trigger for the
    Login flow: the game encrypts its Blowfish key with this pubkey in KE2, then
    sends encrypted LOGINWAITMODULE.  Must be followed by setting clt.sv_pubkey/sv_privkey.
    """
    ensure_ubigs_importable()
    import gsm
    import pkc
    from data import List

    pub_key, _priv_key = fixed_rsa
    pub_key_obj = pkc.RsaPublicKey.from_pubkey(pub_key)
    buf = bytes(pub_key_obj)

    dl = List(['1', ['1', str(len(buf)), buf]])

    # Fake a UNK->S received header (b5=0x82) so GSMResponse swaps to S->UNK (b5=0x28).
    # WM connections use entity types S(2) and UNK(8), NOT R(1)/P(4) as on the router.
    # Game's KE1 to WM arrives with b5=0x82 (UNK->S); server KE1 must reply b5=0x28 (S->UNK).
    fake_hdr_bytes = bytes([0, 0, 6, 0, gsm.MESSAGE_TYPE.KEY_EXCHANGE.value, 0x82])

    class _FakeMsg:
        pass

    fake_msg = _FakeMsg()
    fake_msg.header = gsm.GSMessageHeader(fake_hdr_bytes)
    fake_msg.dl = None

    res = gsm.GSMResponse(fake_msg)
    res.header.type = gsm.MESSAGE_TYPE.KEY_EXCHANGE
    res.dl = dl
    return bytes(res)


def _enc_num(value: int, size: int = 2) -> bytes:
    """Encode integer as little-endian bytes for scalar payload experiments."""
    if size not in (1, 2, 4):
        raise ValueError(f"unsupported size: {size}")
    return int(value).to_bytes(size, "little", signed=False)


def _build_ct34_dl(
    *,
    mode: str,
    base: str,
    host: str,
    port: int,
    proxy_id: int,
    num_a: int,
    num_b: int,
    subtype: str = "1",
):
    result = "38"

    if mode == "a":
        # PROXY_HANDLER (0xCC) DL uses STRING encoding for numeric fields.
        # (Theory C binary shorts apply only to GSSUCCESS sub-types C8/C9/LWM,
        #  NOT to PROXY_HANDLER which has its own parser.)
        module_info = [base, str(num_a), str(num_b),
                       [[str(proxy_id), host, str(port)]]]
    elif mode == "b":
        module_info = [
            base,
            str(num_a),
            str(num_b),
            [[str(proxy_id), host, str(port)], [str(proxy_id + 1), host, str(port + 1)]],
        ]
    elif mode == "c":
        module_info = [
            base,
            _enc_num(num_a, 2),
            _enc_num(num_b, 2),
            [[_enc_num(proxy_id, 2), host, _enc_num(port, 2)]],
        ]
    else:
        module_info = [base, str(num_a), str(num_b), [[str(proxy_id), host, str(port)]]]

    # 2 top-level fields: result, [subtype, module_info]
    # Game expects subtype nested inside module list, NOT as a separate top-level field.
    # 3-field format ['38', '1', [...]] broke pers_proxy connections.
    # 2-field format ['38', ['1', [...]]] is what the game parses correctly.
    return [result, [subtype, module_info]]


def handle_message(
    clt: ClientState,
    msg,
    *,
    frame_bytes: bytes | None,
    log_fp: TextIO | None,
    proxy_addr: tuple[str, int],
    keyex2_mode: str,
    post_ke2_push: str,
    ct34_enable: bool,
    ct34_mode: str,
    ct34_base: str,
    ct34_host: str,
    ct34_port: int,
    ct34_id: int,
    ct34_num_a: int,
    ct34_num_b: int,
    post_ke2_replay_frames: list[bytes],
    fixed_rsa: tuple | None,
    userdb=None,
    conn: socket.socket | None = None,
    session_key: str | None = None,
):
    import gsm
    import pkc
    from data import List
    import copy

    if msg.header.type == gsm.MESSAGE_TYPE.STILLALIVE:
        # Echo STILLALIVE back to prevent game timeout
        res = gsm.GSMResponse(msg)
        res.header.property = gsm.PROPERTY.GS
        res.header.type = gsm.MESSAGE_TYPE.STILLALIVE
        res.dl = None
        return res

    if msg.header.type == gsm.MESSAGE_TYPE.KEY_EXCHANGE:
        req_id = str(msg.dl.lst[0]) if msg.dl is not None else ""
        if req_id == "1":
            pub = pkc.RsaPublicKey.from_buf(msg.dl.lst[1][2])
            clt.game_pubkey = pub.to_pubkey()
            log_line(
                f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(1) client pubkey bits={pub.bits} n_bits={clt.game_pubkey.n.bit_length()} e={clt.game_pubkey.e}",
                log_fp=log_fp,
            )
            if clt.sv_pubkey is not None:
                # Server KE1 (pubkey) was already pre-sent from the accept loop.
                # The game read pre-KE1 on connect, then sent its KE1.
                # Now the game expects a KE1 RESPONSE before it will send KE2(2).
                # Returning None caused a 15-second timeout → game aborted.
                # Fix: re-send the server pubkey as the expected KE1 response.
                # The game already has the pubkey from pre-KE1, but its state
                # machine requires reading the response before proceeding to KE2.
                log_line(
                    f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(1) pre-KE1 already sent, re-sending KE1 response",
                    log_fp=log_fp,
                )
                return _build_boot_ke1(fixed_rsa=fixed_rsa)
            if fixed_rsa is not None:
                pub_key, priv_key = fixed_rsa
            else:
                pub_key, priv_key = pkc.keygen()
            clt.sv_pubkey = pub_key
            clt.sv_privkey = priv_key
            rsp = gsm.KeyExchangeResponse(msg, clt)
            out = bytes(rsp)
            if len(out) >= 6:
                log_line(
                    f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(1) native hdr b5={out[5]:02x}",
                    log_fp=log_fp,
                )
            return out
        if req_id == "2":
            if clt.sv_privkey is None:
                raise RuntimeError("KEY_EXCHANGE(2) before KEY_EXCHANGE(1)")
            try:
                enc_bf_key = bytes(msg.dl.lst[1][2])
                clt.game_bf_key = pkc.decrypt(enc_bf_key, clt.sv_privkey)
                log_line(
                    f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(2) decrypted client BF key len={len(clt.game_bf_key)}",
                    log_fp=log_fp,
                )
            except Exception:
                clt.game_bf_key = None
                log_line(f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(2) decrypt FAILED", log_fp=log_fp)
            mode = str(keyex2_mode or "random").strip().lower()
            if mode in {"echo-exact", "echo_exact"} and frame_bytes:
                clt.sv_bf_key = clt.game_bf_key
                res = bytes(frame_bytes)
                log_line(f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(2) mode=echo-exact", log_fp=log_fp)
            elif mode in {"echo-raw", "echo_raw"}:
                enc_key = bytes(msg.dl.lst[1][2])
                clt.sv_bf_key = clt.game_bf_key
                res = gsm.GSMResponse(msg)
                res.header = copy.deepcopy(msg.header)
                res.dl = List(["2", ["1", str(len(enc_key)), enc_key]])
                log_line(f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(2) mode=echo-raw", log_fp=log_fp)
            elif mode in {"echo", "echo-client", "echo_client"} and clt.game_bf_key and clt.game_pubkey:
                clt.sv_bf_key = clt.game_bf_key
                enc_key = pkc.encrypt(clt.sv_bf_key, clt.game_pubkey)
                res = gsm.GSMResponse(msg)
                res.header = copy.deepcopy(msg.header)
                res.dl = List(["2", ["1", str(len(enc_key)), enc_key]])
                log_line(f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(2) mode=echo-client", log_fp=log_fp)
            elif mode in {"echo", "echo-client", "echo_client"} and clt.game_bf_key and not clt.game_pubkey:
                # Lobby connection: game didn't send KE1(1), so no game pubkey.
                # Fall back to echo-raw: echo the encrypted key bytes back as-is.
                # Set sv_bf_key = game_bf_key so both sides use the same key.
                enc_key = bytes(msg.dl.lst[1][2])
                clt.sv_bf_key = clt.game_bf_key
                res = gsm.GSMResponse(msg)
                res.header = copy.deepcopy(msg.header)
                res.dl = List(["2", ["1", str(len(enc_key)), enc_key]])
                log_line(f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(2) mode=echo-client-nopub (lobby fallback)", log_fp=log_fp)
            elif mode in {"random-rawhdr", "random_rawhdr"}:
                # Build a normal server KE2 payload, then force-copy raw header bytes from RX.
                # This avoids serializer/header-class normalization that may flip sender/receiver.
                rsp = gsm.KeyExchangeResponse(msg, clt)
                out = bytearray(bytes(rsp))
                if frame_bytes and len(frame_bytes) >= 6 and len(out) >= 6:
                    out[3] = frame_bytes[3]
                    out[4] = frame_bytes[4]
                    out[5] = frame_bytes[5]
                res = bytes(out)
                log_line(f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(2) mode=random-rawhdr", log_fp=log_fp)
            else:
                log_line(f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(2) mode=random", log_fp=log_fp)
                res = gsm.KeyExchangeResponse(msg, clt)
                res.header = copy.deepcopy(res.header)

            if frame_bytes and len(frame_bytes) >= 6:
                log_line(
                    f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(2) RX hdr b3={frame_bytes[3]:02x} b4={frame_bytes[4]:02x} b5={frame_bytes[5]:02x}",
                    log_fp=log_fp,
                )
            # Keep WM KEY_EXCHANGE(2) header bytes as produced by selected mode.
            # For experiments, allow opt-in b5=0x41 forcing via env var.
            if os.environ.get("UBIGS_WM_FORCE_KE2_B5_41", "").strip().lower() in {"1", "true", "yes", "on"}:
                try:
                    ke2_bytes = bytes(res)
                    if len(ke2_bytes) >= 6 and ke2_bytes[5] != 0x41:
                        b = bytearray(ke2_bytes)
                        b[5] = 0x41
                        res = bytes(b)
                except Exception:
                    pass
            # Proactive KE2 was sent without pushes during KE1 to prevent FIN.
            # Now send ct-bootstrap pushes with the actual KE2(2) response.
            push_mode = str(post_ke2_push or "off").strip().lower()
            if push_mode in {"off", "none", "0", "false"}:
                return (res, [])
            def _typed_for(msg_type):
                r = gsm.GSMResponse(msg)
                r.header = copy.deepcopy(msg.header)
                r.header.property = gsm.PROPERTY.GS
                r.header.type = msg_type
                r.dl = List([])
                return bytes(r)
            def _lobby_login_push():
                r = gsm.GSMResponse(msg)
                r.header = copy.deepcopy(msg.header)
                r.header.property = gsm.PROPERTY.GS
                r.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
                # Keep classic string shape used by existing lobbylogin mode.
                r.dl = List([str(gsm.MESSAGE_TYPE.GSSUCCESS.value), [str(gsm.LOBBY_MSG.LOGIN.value)]])
                return r

            extras = []
            if push_mode in {"loginwait", "loginwait+friends", "loginwait+friends+lobbylogin", "all"}:
                extras.append(_typed_for(gsm.MESSAGE_TYPE.LOGINWAITMODULE))
            if push_mode in {"loginwait+friends", "loginwait+friends+lobbylogin", "all"}:
                extras.append(_typed_for(gsm.MESSAGE_TYPE.LOGINFRIENDS))
            if push_mode in {"lobbylogin", "loginwait+friends+lobbylogin", "all"}:
                r = gsm.GSMResponse(msg)
                r.header = copy.deepcopy(msg.header)
                r.header.property = gsm.PROPERTY.GS
                r.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
                r.dl = List([str(gsm.MESSAGE_TYPE.GSSUCCESS.value), [str(gsm.LOBBY_MSG.LOGIN.value)]])
                extras.append(r)
            if push_mode in {"ct-bootstrap", "all"} and ct34_enable:
                # Theory D push order: ct34 → C8 → C9 → LOGINFRIENDS → LWM (last)
                # C8/C9 register the "persistantdata" module before LWM triggers connection.
                # LWM is last because it's the "go" signal for pers_proxy.
                # These are best-effort since the game FINs quickly after KE2(2);
                # the proactive pushes (KE1) already delivered everything.
                r = gsm.GSMResponse(msg)
                r.header = copy.deepcopy(msg.header)
                r.header.property = gsm.PROPERTY.GS
                r.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
                r.dl = List(
                    _build_ct34_dl(
                        mode=str(ct34_mode).strip().lower(),
                        base=str(ct34_base),
                        host=str(ct34_host),
                        port=int(ct34_port),
                        proxy_id=int(ct34_id),
                        num_a=int(ct34_num_a),
                        num_b=int(ct34_num_b),
                    )
                )
                extras.append(r)                                              # push0: ct34

                # --- Theory E: PROXY_HANDLER data + GSSUCCESS triggers ---
                def _ph2(dl):
                    rr = gsm.GSMResponse(msg)
                    rr.header = copy.deepcopy(msg.header)
                    rr.header.property = gsm.PROPERTY.GS
                    rr.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
                    rr.dl = dl
                    return rr

                def _gs2(subtype_byte):
                    rr = gsm.GSMResponse(msg)
                    rr.header = copy.deepcopy(msg.header)
                    rr.header.property = gsm.PROPERTY.GS
                    rr.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                    rr.dl = List([subtype_byte])
                    return rr

                # PROXY_HANDLER data frames
                extras.append(_ph2(List(['200', ['persistantdata', '0', '0']])))  # C8 data
                extras.append(_ph2(List(['201', ['persistantdata', '0', '0']])))  # C9 data
                extras.append(_ph2(List(['38', ['77', ['1']]])))                   # LWM data

                # GSSUCCESS triggers
                extras.append(_gs2(b'\xC8'))                                      # C8 trigger
                extras.append(_gs2(b'\xC9'))                                      # C9 trigger
                extras.append(_typed_for(gsm.MESSAGE_TYPE.LOGINFRIENDS))          # LOGINFRIENDS
                extras.append(_gs2(b'\x4D'))                                      # LWM trigger (last)
            if push_mode in {"ct-replay", "all"} and post_ke2_replay_frames:
                # Replay captured WM frames byte-for-byte.
                # Some message classes (e.g., lobby/proxy payloads) are sensitive to header bytes.
                extras.extend(post_ke2_replay_frames)
            log_line(
                f"[{now_ts()}] ROUTER_WM KEY_EXCHANGE(2) post-push mode={push_mode} count={len(extras)}",
                log_fp=log_fp,
            )
            return (res, extras)
        return None

    if msg.header.type == gsm.MESSAGE_TYPE.LOGIN:
        # Game sends LOGIN on WM just like on Router.
        # Must respond with proper GSSUCCESS(LOGIN) so game proceeds to LOGINWAITMODULE.
        username = None
        password = None
        try:
            if msg.dl is not None and getattr(msg.dl, "lst", None):
                lst = msg.dl.lst
                username = lst[0] if len(lst) >= 1 and isinstance(lst[0], str) else None
                password = lst[1] if len(lst) >= 2 and isinstance(lst[1], str) else None
        except Exception:
            pass
        if username:
            clt.username = username

        # Validate against userdb
        login_ok = True
        login_encrypted_unparsed = (
            msg.header.property == gsm.PROPERTY.GS_ENCRYPT and msg.dl is None
        )
        if userdb is not None and not login_encrypted_unparsed:
            if username is None:
                login_ok = False
            elif userdb.get_user(username) is None:
                login_ok = False
            elif password is not None and not userdb.check_password(username=username, password=password):
                login_ok = False

        if login_ok:
            log_line(f"[{now_ts()}] ROUTER_WM LOGIN OK: user={username!r}", log_fp=log_fp)
        else:
            log_line(f"[{now_ts()}] ROUTER_WM LOGIN REJECT: user={username!r}", log_fp=log_fp)
            fail_res = gsm.GSMResponse(msg)
            fail_res.header = copy.deepcopy(msg.header)
            fail_res.header.property = gsm.PROPERTY.GS
            fail_res.header.type = gsm.MESSAGE_TYPE.GSFAIL
            fail_res.dl = List([b'\x66'])
            return fail_res

        # Build GSSUCCESS(LOGIN) + ct34 bootstrap extras
        login_extras = []
        if ct34_enable:
            def _mk_ph_14(dl):
                r = gsm.GSMResponse(msg)
                r.header = copy.deepcopy(msg.header)
                r.header.property = gsm.PROPERTY.GS
                r.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
                r.header.sender = gsm.SENDER_RECEIVER.R
                r.header.receiver = gsm.SENDER_RECEIVER.P
                r.dl = dl
                return r

            def _mk_gs_14(sub):
                r = gsm.GSMResponse(msg)
                r.header = copy.deepcopy(msg.header)
                r.header.property = gsm.PROPERTY.GS
                r.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                r.header.sender = gsm.SENDER_RECEIVER.R
                r.header.receiver = gsm.SENDER_RECEIVER.P
                r.dl = List([sub])
                return r

            # ct34 with WM proxy info (44002)
            ct34_r = _mk_ph_14(List(
                _build_ct34_dl(
                    mode=str(ct34_mode).strip().lower(),
                    base=str(ct34_base),
                    host=str(ct34_host),
                    port=int(ct34_port),
                    proxy_id=int(ct34_id),
                    num_a=int(ct34_num_a),
                    num_b=int(ct34_num_b),
                )
            ))
            r_ph_c8 = _mk_ph_14(List([b'\xC8', ['persistantdata', '0', '0']]))
            r_ph_c9 = _mk_ph_14(List([b'\xC9', ['persistantdata', '0', '0']]))
            r_ph_lwm = _mk_ph_14(List([b'\x4D', [b'\x4D', ['1']]]))
            r_gs_c8 = _mk_gs_14(b'\xC8')
            r_gs_c9 = _mk_gs_14(b'\xC9')
            r_gs_lwm = _mk_gs_14(b'\x4D')
            push_loginfriends_14 = bytes.fromhex("000006004e14")

            login_extras.append(ct34_r)
            login_extras.append(r_ph_c8)
            login_extras.append(r_ph_c9)
            login_extras.append(r_ph_lwm)
            login_extras.append(r_gs_c8)
            login_extras.append(r_gs_c9)
            login_extras.append(push_loginfriends_14)
            login_extras.append(r_gs_lwm)
            log_line(f"[{now_ts()}] ROUTER_WM LOGIN: appended ct34+bootstrap proxy={ct34_host}:{ct34_port} b5=0x14", log_fp=log_fp)

        login_res = gsm.GSMResponse(msg)
        login_res.header = copy.deepcopy(msg.header)
        login_res.header.property = gsm.PROPERTY.GS
        login_res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
        login_res.dl = List([b'\x66'])  # 0x66 = LOGIN type
        log_line(f"[{now_ts()}] ROUTER_WM LOGIN: total extras={len(login_extras)}", log_fp=log_fp)
        return (login_res, login_extras)

    if msg.header.type == gsm.MESSAGE_TYPE.LOBBYSERVERLOGIN:
        # Game connected to lobby server and sent LOBBYSERVERLOGIN
        # DL: ['username', 'server_id', 'client_ip', 'netmask', 'flags']
        try:
            if msg.dl is not None and msg.dl.lst and isinstance(msg.dl.lst[0], str):
                clt.username = msg.dl.lst[0]
            svid = str(msg.dl.lst[1]) if msg.dl and len(msg.dl.lst) > 1 else "?"
            log_line(
                f"[{now_ts()}] ROUTER_WM LOBBYSERVERLOGIN user={clt.username} svid={svid}",
                log_fp=log_fp,
            )
        except Exception:
            pass
        # Register this lobby-server connection for room broadcasting.
        # This is the connection that handles CREATE_ROOM / NEW_GROUP traffic,
        # separate from the WM login connection.
        sk = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
        if conn is not None:
            _lobby_state.register_session(sk, conn, clt.username or "unknown", source="LOBBYSERVERLOGIN")
            log_line(
                f"[{now_ts()}] ROUTER_WM LOBBY session registered (lobby-server conn): {sk} user={clt.username}",
                log_fp=log_fp,
            )
        return gsm.LobbyServerLoginResponse(msg)

    if msg.header.type == gsm.MESSAGE_TYPE.LOGINWAITMODULE:
        try:
            if msg.dl is not None and msg.dl.lst and isinstance(msg.dl.lst[0], str):
                clt.username = msg.dl.lst[0]
        except Exception:
            pass
        return gsm.LoginWaitModuleResponse(msg)

    if msg.header.type == gsm.MESSAGE_TYPE.LOGINFRIENDS:
        return gsm.LoginFriendsResponse(msg)

    if msg.header.type == gsm.MESSAGE_TYPE.PLAYERINFO:
        return gsm.PlayerInfoResponse(msg, clt.username or "noname")

    if msg.header.type == gsm.MESSAGE_TYPE.PROXY_HANDLER:
        # Parse subtype and module name from request DL
        req_subtype = "?"
        req_module = "?"
        try:
            if msg.dl is not None and msg.dl.lst:
                req_subtype = str(msg.dl.lst[0])
                if len(msg.dl.lst) > 1 and hasattr(msg.dl.lst[1], 'lst'):
                    req_module = str(msg.dl.lst[1].lst[0])
                elif len(msg.dl.lst) > 1 and isinstance(msg.dl.lst[1], list):
                    req_module = str(msg.dl.lst[1][0])
        except Exception:
            pass

        log_line(
            f"[{now_ts()}] ROUTER_WM PROXY_HANDLER subtype={req_subtype} module={req_module}",
            log_fp=log_fp,
        )

        if req_subtype == "2":
            # Subtype 2 = unregister/disconnect module. Respond with ack.
            res = gsm.GSMResponse(msg)
            res.header.property = gsm.PROPERTY.GS
            res.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
            try:
                mod_id = str(msg.dl.lst[1].lst[0]) if hasattr(msg.dl.lst[1], 'lst') else str(msg.dl.lst[1][0])
            except Exception:
                mod_id = "1"
            res.dl = List(["38", ["2", [mod_id]]])
            return res

        if ct34_enable and req_subtype == "1":
            # Subtype 1 = module registration. Use the REQUESTED module name.
            use_base = req_module if req_module != "?" else str(ct34_base)
            # Assign different proxy_id/port per module type
            proxy_map = {
                "persistantdata": (1, int(ct34_port)),
                "ladderquery": (2, int(ct34_port)),
                "remotealgorithm": (3, int(ct34_port)),
                "clanservice": (4, int(ct34_port)),
            }
            pid, pport = proxy_map.get(use_base, (int(ct34_id), int(ct34_port)))

            res = gsm.GSMResponse(msg)
            res.header.property = gsm.PROPERTY.GS
            res.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
            res.dl = List(
                _build_ct34_dl(
                    mode=str(ct34_mode).strip().lower(),
                    base=use_base,
                    host=str(ct34_host),
                    port=pport,
                    proxy_id=pid,
                    num_a=int(ct34_num_a),
                    num_b=int(ct34_num_b),
                )
            )
            log_line(
                f"[{now_ts()}] ROUTER_WM PROXY_HANDLER ct34 base={use_base} id={pid} host={ct34_host}:{pport}",
                log_fp=log_fp,
            )
            return res

        # Default behavior.
        return gsm.ProxyHandlerResponse(msg, proxy_addr, proxy_id=1)

    if msg.header.type == gsm.MESSAGE_TYPE.LOBBY_MSG:
        # Parse lobby subtype
        lobby_subtype = None
        try:
            if msg.dl is not None and msg.dl.lst:
                lobby_subtype = int(msg.dl.lst[0])
        except Exception:
            pass

        log_line(
            f"[{now_ts()}] ROUTER_WM LOBBY_MSG subtype={lobby_subtype}",
            log_fp=log_fp,
        )

        if lobby_subtype == 21:
            # LOBBY_MSG.LOGIN — respond with GSSUCCESS, then push lobby list
            login_res = gsm.LobbyMsgResponse(msg)

            # Build GROUP_INFO push with one lobby
            from group import Lobby
            lobby1 = Lobby(id=1, name="www.splintercellonline.net", master="", game_mode=0)
            lobby1.max_members = 16
            lobby1.nb_members = lobby_player_count()

            group_info = gsm.GSMResponse(msg)
            group_info.header.property = gsm.PROPERTY.GS
            group_info.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
            lobby_list = [lobby1.to_list()]
            group_info.dl = List([
                str(53),  # LOBBY_MSG.GROUP_INFO
                ["1", str(0x100), ["0"], lobby_list]
            ])

            extras = [group_info]

            # Also push room list if any rooms exist (file-backed cross-process)
            all_rooms = _lobby_state.get_rooms()
            if not all_rooms:
                all_rooms = _lobby_state._load_rooms_from_file()
            if all_rooms:
                room_lists = [r.to_list() for r in all_rooms]
                room_group_info = gsm.GSMResponse(msg)
                room_group_info.header.property = gsm.PROPERTY.GS
                room_group_info.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
                room_group_info.dl = List([
                    str(53),  # GROUP_INFO
                    ["1", str(0x100), ["1"], room_lists]
                ])
                extras.append(room_group_info)
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY_MSG LOGIN -> pushing GROUP_INFO with 1 lobby + {len(all_rooms)} room(s)",
                    log_fp=log_fp,
                )
            else:
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY_MSG LOGIN -> pushing GROUP_INFO with 1 lobby",
                    log_fp=log_fp,
                )
            return (login_res, extras)

        if lobby_subtype == 3:
            # LOBBY_MSG.JOIN_SERVER — game wants to join a lobby server
            # Respond with GSSUCCESS + lobby server address (point to WM server itself)
            lobby_sv_ip = str(ct34_host)
            lobby_sv_port = 40005  # WM server port (self)
            log_line(
                f"[{now_ts()}] ROUTER_WM LOBBY_MSG JOIN_SERVER id={msg.dl.lst[1][0] if msg.dl and len(msg.dl.lst) > 1 else '?'} -> {lobby_sv_ip}:{lobby_sv_port}",
                log_fp=log_fp,
            )
            return gsm.JoinLobbyServerResponse(msg, (lobby_sv_ip, lobby_sv_port))

        if lobby_subtype == 23:
            # LOBBY_MSG.JOIN_LOBBY
            count = lobby_player_join()
            lobby_id = "1"
            try:
                lobby_id = str(msg.dl.lst[1][0]) if msg.dl and len(msg.dl.lst) > 1 else "1"
            except Exception:
                pass
            log_line(
                f"[{now_ts()}] ROUTER_WM LOBBY_MSG JOIN_LOBBY id={lobby_id} players_now={count}",
                log_fp=log_fp,
            )
            resp = gsm.JoinLobbyResponse(msg)
            resp._player_joined = True  # Signal to client_thread for disconnect tracking

            sk = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
            username = clt.username or "unknown"

            # Get existing members BEFORE registering (for pushing to new joiner)
            existing_members = _lobby_state.get_session_usernames(exclude_key=sk)

            # Register/update this lobby session for room broadcasting
            if conn is not None:
                _lobby_state.register_session(sk, conn, username, source="JOIN_LOBBY")
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY session registered (JOIN_LOBBY): {sk} user={username}",
                    log_fp=log_fp,
                )

            # --- Player Presence: Broadcast MEMBER_JOIN (50) to existing members ---
            # Tell other players that this user joined the lobby
            member_join_frame = _build_lobby_msg_frame(
                [str(50), [lobby_id, username]],  # MEMBER_JOIN: [lobby_id, username]
            )
            _lobby_state.broadcast(member_join_frame, exclude_key=sk, log_fp=log_fp)
            log_line(
                f"[{now_ts()}] ROUTER_WM MEMBER_JOIN broadcast: {username} joined lobby {lobby_id}",
                log_fp=log_fp,
            )

            extras = []

            # --- Push existing members to the new joiner as MEMBER_JOIN (50) ---
            for member_name in existing_members:
                mj_frame = _build_lobby_msg_frame(
                    [str(50), [lobby_id, member_name]],
                )
                extras.append(mj_frame)
            if existing_members:
                log_line(
                    f"[{now_ts()}] ROUTER_WM pushing {len(existing_members)} existing member(s) to {username}",
                    log_fp=log_fp,
                )

            # Push existing rooms to the new player using raw CREATE_ROOM relay frames
            existing = _lobby_state.get_rooms_with_frames()
            for room, raw_frame in existing:
                if raw_frame:
                    extras.append(raw_frame)
                else:
                    # Fallback: build NEW_GROUP if no raw frame stored
                    new_group_dl = [str(54), room.to_list()]
                    frame = _build_lobby_msg_frame(new_group_dl)
                    extras.append(frame)

            if extras:
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY pushing {len(extras)} extras (members+rooms) to {username}",
                    log_fp=log_fp,
                )
                return (resp, extras)
            return resp

        if lobby_subtype == 42:
            # LOBBY_MSG.SET_PLAYER_INFO — player broadcasting their info to lobby
            # DL: ['42', [lobby_id, player_data...]]
            # Respond with GSSUCCESS and broadcast PLAYER_INFO_UPDATE (66) to others
            sk = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
            username = clt.username or "unknown"
            log_line(
                f"[{now_ts()}] ROUTER_WM SET_PLAYER_INFO from {username} raw DL: {msg.dl.lst if msg.dl else 'None'}",
                log_fp=log_fp,
            )

            # Relay the raw SET_PLAYER_INFO frame to other lobby members as
            # PLAYER_INFO_UPDATE (66) — same DL structure but different subtype
            try:
                raw_dl = msg.dl.lst if msg.dl else []
                if len(raw_dl) > 1:
                    # Build PLAYER_INFO_UPDATE (66) with same sub-data
                    update_frame = _build_lobby_msg_frame(
                        [str(66), raw_dl[1]],  # PLAYER_INFO_UPDATE with original sub_data
                    )
                    _lobby_state.broadcast(update_frame, exclude_key=sk, log_fp=log_fp)
                    log_line(
                        f"[{now_ts()}] ROUTER_WM PLAYER_INFO_UPDATE broadcast for {username}",
                        log_fp=log_fp,
                    )
            except Exception as e:
                log_line(f"[{now_ts()}] ROUTER_WM SET_PLAYER_INFO broadcast error: {e}", log_fp=log_fp)

            try:
                return gsm.LobbyMsgResponse(msg)
            except Exception:
                res = gsm.GSMResponse(msg)
                res.header.property = gsm.PROPERTY.GS
                res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                res.dl = List([])
                return res

        if lobby_subtype == 12:
            # LOBBY_MSG.CREATE_ROOM — player creating a game room
            # CREATE_ROOM DL: ['12', [lobby_id, username, game_name, max_players,
            #                         game_mode, flags, binary_blob, '', numeric_id,
            #                         version, empty_bytes]]
            # We build a Room object and broadcast its to_list() as NEW_GROUP (54).

            # Log raw DL for debugging
            try:
                log_line(f"[{now_ts()}] ROUTER_WM CREATE_ROOM raw DL: {msg.dl.lst}", log_fp=log_fp)
            except Exception:
                pass

            from group import Room

            # Parse fields from CREATE_ROOM sub_data
            username = clt.username or "unknown"
            room_name = "Unknown"
            sub_data = []
            try:
                if msg.dl and len(msg.dl.lst) > 1:
                    sd = msg.dl.lst[1]
                    if hasattr(sd, 'lst'):
                        sub_data = list(sd.lst)
                    elif isinstance(sd, list):
                        sub_data = list(sd)
                    else:
                        sub_data = [sd]
            except Exception as e:
                log_line(f"[{now_ts()}] ROUTER_WM CREATE_ROOM parse error: {e}", log_fp=log_fp)

            # Map CREATE_ROOM fields to Room fields:
            #   sub_data[0] = lobby_id (parent)     -> room.parent_id
            #   sub_data[1] = username               -> room.master
            #   sub_data[2] = game_name              -> room.allowed_games
            #   sub_data[3] = max_players            -> room.max_players
            #   sub_data[4] = game_mode              -> room.event_id
            #   sub_data[5] = flags                  -> room.config
            #   sub_data[6] = binary blob (room name + IPs) -> room.info
            #   sub_data[7] = ''
            #   sub_data[8] = numeric_id
            #   sub_data[9] = version                -> room.game_version
            #   sub_data[10] = empty bytes
            parent_id = 1
            game_mode = 0
            info_blob = b''
            max_players = 8
            config = 0
            allowed_games = ""
            game_version = ""

            if len(sub_data) > 0:
                try: parent_id = int(sub_data[0])
                except: pass
            if len(sub_data) > 1 and isinstance(sub_data[1], str):
                username = sub_data[1]
            if len(sub_data) > 2:
                allowed_games = str(sub_data[2])
            if len(sub_data) > 3:
                try: max_players = int(sub_data[3])
                except: pass
            if len(sub_data) > 4:
                try: game_mode = int(sub_data[4])
                except: pass
            if len(sub_data) > 5:
                try: config = int(sub_data[5])
                except: pass
            if len(sub_data) > 6 and isinstance(sub_data[6], (bytes, bytearray)):
                info_blob = bytes(sub_data[6])
                # Extract room name from binary blob (null-terminated string after header)
                try:
                    nulls = [i for i, b in enumerate(info_blob) if b == 0]
                    for ni in range(len(nulls)):
                        candidate = info_blob[nulls[ni]+1:]
                        null_pos = candidate.find(b'\x00')
                        if null_pos > 0:
                            cs = candidate[:null_pos].decode('ascii', errors='ignore')
                            if cs and cs.isprintable() and len(cs) > 1 and not cs.startswith('CAU'):
                                room_name = cs
                                break
                except Exception:
                    pass
            if len(sub_data) > 9:
                game_version = str(sub_data[9])

            # Build Room object with proper fields
            rid = _lobby_state.next_room_id()
            room = Room(id=rid, name=room_name, master=username, game_mode=game_mode)
            room.parent_id = parent_id
            room.config = config
            room.allowed_games = allowed_games
            room.info = info_blob
            room.max_players = max_players
            room.nb_players = 1
            room.game_version = game_version
            # Set host IP from the creator's connection address
            creator_ip = clt.addr[0] if hasattr(clt, 'addr') and clt.addr else ""
            room.ip_addr = creator_ip
            room.alt_ip_addr = creator_ip

            sk = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
            # Store raw CREATE_ROOM frame (with b5 set to S->P) for relaying
            relay_frame = None
            raw_frame = frame_bytes or b""
            if raw_frame and len(raw_frame) >= 6:
                relay_frame = bytearray(raw_frame)
                relay_frame[5] = 0x24  # S->P
                relay_frame = bytes(relay_frame)
            _lobby_state.create_room(room=room, owner_key=sk, raw_frame=relay_frame)

            log_line(
                f"[{now_ts()}] ROUTER_WM LOBBY_MSG CREATE_ROOM name={room_name!r} master={username!r} "
                f"room_id={rid} parent={parent_id} max={max_players} mode={game_mode} info_len={len(info_blob)}",
                log_fp=log_fp,
            )

            # --- Broadcast strategies (try raw relay first, keep NEW_GROUP as fallback) ---
            # Strategy 1: Relay the raw CREATE_ROOM frame to other players.
            # The game knows how to parse CREATE_ROOM (subtype 12) since it creates them.
            # Re-encode with b5=0x24 (S->P) so the game accepts it from the server entity.
            raw_frame = frame_bytes or b""
            if raw_frame and len(raw_frame) >= 6:
                relay = bytearray(raw_frame)
                relay[5] = 0x24  # S->P for lobby server connection
                log_line(f"[{now_ts()}] ROUTER_WM CREATE_ROOM relay frame len={len(relay)} b5=0x{relay[5]:02x}", log_fp=log_fp)
                _lobby_state.broadcast(bytes(relay), exclude_key=sk, log_fp=log_fp)
            else:
                # Fallback: Build NEW_GROUP notification using Room.to_list() format
                new_group_frame = _build_lobby_msg_frame([str(54), room.to_list()])
                log_line(f"[{now_ts()}] ROUTER_WM NEW_GROUP frame hex: {new_group_frame.hex()}", log_fp=log_fp)
                _lobby_state.broadcast(new_group_frame, exclude_key=sk, log_fp=log_fp)

            # Respond with GSSUCCESS to the creator
            try:
                return gsm.LobbyMsgResponse(msg)
            except Exception:
                res = gsm.GSMResponse(msg)
                res.header.property = gsm.PROPERTY.GS
                res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                res.dl = List([])
                return res

        if lobby_subtype == 8:
            # LOBBY_MSG.GROUP_LEAVE — player leaving a room/lobby
            sk = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
            removed = _lobby_state.remove_room_by_owner(sk)
            for room_entry in removed:
                room = room_entry["room"]
                remove_frame = _build_lobby_msg_frame([str(55), [str(room.group_id)]])
                _lobby_state.broadcast(remove_frame, log_fp=log_fp)
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY room removed (GROUP_LEAVE): id={room.group_id} name={room.group_name}",
                    log_fp=log_fp,
                )
            try:
                return gsm.LobbyMsgResponse(msg)
            except Exception:
                res = gsm.GSMResponse(msg)
                res.header.property = gsm.PROPERTY.GS
                res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                res.dl = List([])
                return res

        if lobby_subtype == 109:
            # LOBBY_MSG subtype 109 — CHANGE_REQUESTED_LOBBIES / Find Game
            # DL: ['109', ['SPLINTERCELL3PS2US']]
            # Respond with GSSUCCESS + GROUP_INFO push + individual NEW_GROUP pushes
            game_filter = ""
            try:
                if msg.dl and len(msg.dl.lst) > 1:
                    sd = msg.dl.lst[1]
                    if hasattr(sd, 'lst') and sd.lst:
                        game_filter = str(sd.lst[0])
                    elif isinstance(sd, list) and sd:
                        game_filter = str(sd[0])
                    elif isinstance(sd, str):
                        game_filter = sd
            except Exception:
                pass

            all_rooms = _lobby_state.get_rooms()
            # Cross-process fallback: if in-memory state is empty, read from shared file
            if not all_rooms:
                all_rooms = _lobby_state._load_rooms_from_file()

            # Also get raw frames for relay approach
            all_rooms_with_frames = _lobby_state.get_rooms_with_frames()

            log_line(
                f"[{now_ts()}] ROUTER_WM FIND_GAME DEBUG: total_rooms={len(all_rooms)} "
                f"room_ids={[r.group_id for r in all_rooms]} "
                f"allowed_games={[r.allowed_games for r in all_rooms]} "
                f"game_filter={game_filter!r}",
                log_fp=log_fp,
            )
            existing_rooms = all_rooms
            if game_filter:
                existing_rooms = [r for r in existing_rooms
                                  if not r.allowed_games or r.allowed_games == game_filter]

            log_line(
                f"[{now_ts()}] ROUTER_WM LOBBY_MSG FIND_GAME game={game_filter!r} rooms={len(existing_rooms)}",
                log_fp=log_fp,
            )

            # Build GSSUCCESS as raw bytes (avoid shared-header bug with GSMResponse)
            gssuccess_bytes = bytes([0, 0, 6, 0, gsm.MESSAGE_TYPE.GSSUCCESS.value, 0x14])

            if existing_rooms:
                extras = []

                # Strategy 1: GROUP_INFO (53) with room data
                room_lists = [r.to_list() for r in existing_rooms]
                group_info_frame = _build_lobby_msg_frame(
                    [str(53), ["1", str(0x100), ["1"], room_lists]],
                    b5=0x24,  # S->P (lobby server push)
                )
                extras.append(group_info_frame)

                # Strategy 2: Individual NEW_GROUP (54) for each room
                for room in existing_rooms:
                    new_group_frame = _build_lobby_msg_frame(
                        [str(54), room.to_list()],
                        b5=0x24,
                    )
                    extras.append(new_group_frame)

                # Strategy 3: Relay raw CREATE_ROOM frames if available
                for room, raw_frame in all_rooms_with_frames:
                    if raw_frame:
                        # Adjust b5 for this connection context
                        relay = bytearray(raw_frame)
                        if len(relay) >= 6:
                            relay[5] = 0x24  # S->P
                        extras.append(bytes(relay))

                log_line(
                    f"[{now_ts()}] ROUTER_WM FIND_GAME pushing {len(existing_rooms)} room(s) "
                    f"via {len(extras)} frames (GROUP_INFO + NEW_GROUP + raw relay)",
                    log_fp=log_fp,
                )
                return (gssuccess_bytes, extras)
            return gssuccess_bytes

        # Handle other lobby subtypes
        try:
            return gsm.LobbyMsgResponse(msg)
        except Exception:
            # Unknown subtype — respond with empty GSSUCCESS
            res = gsm.GSMResponse(msg)
            res.header.property = gsm.PROPERTY.GS
            res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
            res.dl = List([])
            return res

    # MODIFYUSER (0x62) — password change
    if msg.header.type == gsm.MESSAGE_TYPE.MODIFYUSER:
        # DL format: ['', new_password, username, findme3, findme6, findme5]
        #   lst[0] = '' (empty)
        #   lst[1] = new password
        #   lst[2] = username
        username = None
        new_password = None
        if msg.dl is not None and len(msg.dl.lst) >= 3:
            try:
                username = str(msg.dl.lst[2])
                new_password = str(msg.dl.lst[1])
            except Exception:
                pass
        # Fall back to clt.username if DL parsing failed
        if not username and hasattr(clt, 'username') and clt.username:
            username = clt.username

        if userdb is not None and username and new_password is not None:
            userdb.upsert_user(username=username, password=new_password)
            try:
                userdb.save()
            except Exception:
                pass
            log_line(f"[{now_ts()}] ROUTER_WM MODIFYUSER OK: user={username!r} password changed", log_fp=log_fp)
        else:
            log_line(f"[{now_ts()}] ROUTER_WM MODIFYUSER: user={username!r} new_pw={'set' if new_password else 'none'} db={'yes' if userdb else 'no'}", log_fp=log_fp)

        res = gsm.GSMResponse(msg)
        res.header.property = gsm.PROPERTY.GS
        res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
        res.dl = List([gsm.MESSAGE_TYPE.MODIFYUSER.value.to_bytes(1, "little")])
        return res

    # REMOVEACCOUNT (0x09) — delete account
    if msg.header.type == gsm.MESSAGE_TYPE.REMOVEACCOUNT:
        username = None
        if msg.dl is not None and len(msg.dl.lst) >= 1:
            try:
                username = str(msg.dl.lst[0])
            except Exception:
                pass
        if not username and hasattr(clt, 'username') and clt.username:
            username = clt.username

        if userdb is not None and username:
            userdb.remove_user(username)
            try:
                userdb.save()
            except Exception:
                pass
            log_line(f"[{now_ts()}] ROUTER_WM REMOVEACCOUNT OK: user={username!r} deleted", log_fp=log_fp)
        else:
            log_line(f"[{now_ts()}] ROUTER_WM REMOVEACCOUNT: user={username!r} db={'yes' if userdb else 'no'}", log_fp=log_fp)

        res = gsm.GSMResponse(msg)
        res.header.property = gsm.PROPERTY.GS
        res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
        res.dl = List([gsm.MESSAGE_TYPE.REMOVEACCOUNT.value.to_bytes(1, "little")])
        return res

    # Handle unknown message types (like 0xDE/222) gracefully
    try:
        msg_type_val = msg.header.type.value if hasattr(msg.header.type, 'value') else int(msg.header.type)
    except Exception:
        msg_type_val = "?"
    log_line(f"[{now_ts()}] ROUTER_WM unhandled msg type={msg_type_val} — sending empty GSSUCCESS", log_fp=log_fp)
    # Respond with a generic GSSUCCESS to avoid silent drops
    res = gsm.GSMResponse(msg)
    res.header.property = gsm.PROPERTY.GS
    res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
    res.dl = List([])
    return res


def client_thread(conn: socket.socket, addr: tuple[str, int], args: argparse.Namespace, *, log_fp: TextIO | None, pre_ke1_sent: bool = False):
    ensure_ubigs_importable()
    import gsm

    clt = ClientState(addr=addr)
    # If server-first KE1 was already sent from the accept loop, prime key state now
    # so KE2 decryption works when the game responds with bundled KE1+KE2+LOGINWAITMODULE.
    if pre_ke1_sent and getattr(args, '_fixed_rsa', None) is not None:
        clt.sv_pubkey, clt.sv_privkey = args._fixed_rsa
    conn.settimeout(float(args.idle_timeout))

    src_ip, src_port = addr
    local_ip, local_port = conn.getsockname()
    log_line(f"\n[{now_ts()}] ROUTER_WM {src_ip}:{src_port} -> {local_ip}:{local_port} CONNECT pre_ke1={'yes' if pre_ke1_sent else 'no'}", log_fp=log_fp)

    _joined_lobby = False  # Track whether this connection joined a lobby (for player count)
    stream_buf = bytearray()

    # Login-flow boot prelude: if the client sends nothing within login_boot_delay seconds,
    # send a server-first KE1 (RSA public key) to trigger the WM login flow.
    # The real Ubisoft WM server sends its RSA public key first; the game then replies with
    # KE1 (its own pubkey) + KE2 (BF key encrypted with server pubkey) + LOGINWAITMODULE bundled.
    _login_boot_delay = float(getattr(args, 'login_boot_delay', 0.0))
    if _login_boot_delay > 0.0:
        import select as _select
        _rlist, _, _ = _select.select([conn], [], [], _login_boot_delay)
        if not _rlist and getattr(args, '_fixed_rsa', None) is not None:
            try:
                boot_bytes = _build_boot_ke1(fixed_rsa=args._fixed_rsa)
                # Prime clt key state so KE2 decryption works when game responds
                clt.sv_pubkey, clt.sv_privkey = args._fixed_rsa
                tx_name = f"{file_ts()}_{safe_slug(local_ip)}_{local_port}_to_{safe_slug(src_ip)}_{src_port}_boot_ke1.bin"
                save_blob(args.save_tx_dir, tx_name, boot_bytes)
                log_line(f"[{now_ts()}] ROUTER_WM TX[boot_ke1] len={len(boot_bytes)} sha256={sha16(boot_bytes)}", log_fp=log_fp)
                log_line(hexdump(boot_bytes, max_len=int(args.dump_max)), log_fp=log_fp)
                conn.sendall(boot_bytes)
            except Exception as e:
                log_line(f"[{now_ts()}] ROUTER_WM boot_ke1 error: {e}", log_fp=log_fp)

    # Optional server-first bootstrap nudge for titles that connect and wait silently.
    # We only do this in aggressive mode to avoid perturbing conservative runs.
    if str(args.post_ke2_push or "off").strip().lower() == "all" and os.environ.get("UBIGS_WM_BOOT_PRELUDE", "").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            # First try a known-good raw WM KEY_EXCHANGE(1) frame captured from this title.
            # If present, send original + b5-patched(0x41) variant.
            try:
                cap_dir = pathlib.Path(args.save_tx_dir)
                keyex_candidates = sorted(
                    [p for p in cap_dir.glob("*_tx.bin") if p.is_file() and p.stat().st_size == 284],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if keyex_candidates:
                    raw_ke1 = keyex_candidates[0].read_bytes()
                    boot_ke1_frames = [raw_ke1]
                    if len(raw_ke1) >= 6:
                        patched = bytearray(raw_ke1)
                        patched[5] = 0x41
                        boot_ke1_frames.append(bytes(patched))
                    for i, out in enumerate(boot_ke1_frames):
                        tx_name = f"{file_ts()}_{safe_slug(local_ip)}_{local_port}_to_{safe_slug(src_ip)}_{src_port}_boot_ke1_{i}.bin"
                        save_blob(args.save_tx_dir, tx_name, out)
                        log_line(f"[{now_ts()}] ROUTER_WM TX[boot_ke1_{i}] len={len(out)} sha256={sha16(out)}", log_fp=log_fp)
                        log_line(hexdump(out, max_len=int(args.dump_max)), log_fp=log_fp)
                        conn.sendall(out)
            except Exception as e:
                log_line(f"[{now_ts()}] ROUTER_WM boot keyex1 send error: {e}", log_fp=log_fp)

            # Also send small GS nudges as fallback.
            boot_frames = [
                bytes.fromhex("000006004D41"),  # LOGINWAITMODULE (GS, b5=41)
                bytes.fromhex("000006004E41"),  # LOGINFRIENDS  (GS, b5=41)
                bytes.fromhex("000006002641"),  # GSSUCCESS     (GS, b5=41)
                bytes.fromhex("000006004D82"),  # LOGINWAITMODULE (GS, b5=82)
                bytes.fromhex("000006004E82"),  # LOGINFRIENDS  (GS, b5=82)
                bytes.fromhex("000006002682"),  # GSSUCCESS     (GS, b5=82)
            ]
            for i, out in enumerate(boot_frames):
                tx_name = f"{file_ts()}_{safe_slug(local_ip)}_{local_port}_to_{safe_slug(src_ip)}_{src_port}_boot{i}.bin"
                save_blob(args.save_tx_dir, tx_name, out)
                log_line(f"[{now_ts()}] ROUTER_WM TX[boot{i}] len={len(out)} sha256={sha16(out)}", log_fp=log_fp)
                log_line(hexdump(out, max_len=int(args.dump_max)), log_fp=log_fp)
                conn.sendall(out)
        except Exception as e:
            log_line(f"[{now_ts()}] ROUTER_WM boot send error: {e}", log_fp=log_fp)

    try:
        while True:
            try:
                data = conn.recv(8192)
            except socket.timeout:
                continue
            if not data:
                break

            rx_name = f"{file_ts()}_{safe_slug(src_ip)}_{src_port}_to_{safe_slug(local_ip)}_{local_port}_rx.bin"
            save_blob(args.save_rx_dir, rx_name, data)
            log_line(f"[{now_ts()}] ROUTER_WM RX len={len(data)} sha256={sha16(data)}", log_fp=log_fp)
            log_line(hexdump(data, max_len=int(args.dump_max)), log_fp=log_fp)

            stream_buf.extend(data)
            for frame in pop_gsm_frames(stream_buf):
                try:
                    msg, used_key = decode_gsm_message(
                        gsm,
                        frame,
                        keys=[clt.last_bf_key, clt.game_bf_key, clt.sv_bf_key, b""],
                    )
                except Exception as e:
                    log_line(f"[{now_ts()}] ROUTER_WM decode error: {e}", log_fp=log_fp)
                    continue

                if msg.header.property == gsm.PROPERTY.GS_ENCRYPT:
                    clt.last_bf_key = used_key

                log_line(str(msg).rstrip(), log_fp=log_fp)
                res = None
                extras = []
                try:
                    handled = handle_message(
                        clt,
                        msg,
                        frame_bytes=bytes(frame),
                        log_fp=log_fp,
                        proxy_addr=(args.proxy_ip, int(args.proxy_port)),
                        keyex2_mode=str(args.keyex2_mode),
                        post_ke2_push=str(args.post_ke2_push),
                        ct34_enable=bool(args.ct34_enable),
                        ct34_mode=str(args.ct34_mode),
                        ct34_base=str(args.ct34_base),
                        ct34_host=str(args.ct34_host),
                        ct34_port=int(args.ct34_port),
                        ct34_id=int(args.ct34_id),
                        ct34_num_a=int(args.ct34_num_a),
                        ct34_num_b=int(args.ct34_num_b),
                        post_ke2_replay_frames=args._post_ke2_replay_frames,
                        fixed_rsa=args._fixed_rsa,
                        userdb=args._userdb,
                        conn=conn,
                        session_key=f"{src_ip}:{src_port}",
                    )
                    if isinstance(handled, tuple):
                        res, extras = handled
                    else:
                        res = handled
                except Exception as e:
                    log_line(f"[{now_ts()}] ROUTER_WM handler error: {e}", log_fp=log_fp)

                if res is None:
                    continue

                # Track if this connection joined a lobby (for player count on disconnect)
                if getattr(res, '_player_joined', False):
                    _joined_lobby = True

                push_gap_ms = 0.0
                try:
                    push_gap_ms = float(os.environ.get("UBIGS_WM_PUSH_GAP_MS", "0").strip() or "0")
                except Exception:
                    push_gap_ms = 0.0

                # Build all outgoing bytes first (log individually for diagnostics),
                # then send the primary response + all pushes in ONE conn.sendall call.
                # This is critical for CT PS2: the game closes its write-side immediately
                # after sending KE2(2), so the RST arrives ~1 ms after the server starts
                # writing.  Separate sendall calls lose the race; a single batched write
                # gets all frames into the kernel's TCP send buffer before the RST is
                # processed, ensuring the game receives every frame.
                all_out_parts = []
                tx_bytes = bytes(res)
                save_blob(args.save_tx_dir,
                          f"{file_ts()}_{safe_slug(local_ip)}_{local_port}_to_{safe_slug(src_ip)}_{src_port}_tx.bin",
                          tx_bytes)
                log_line(f"[{now_ts()}] ROUTER_WM TX[tx] len={len(tx_bytes)} sha256={sha16(tx_bytes)}", log_fp=log_fp)
                log_line(hexdump(tx_bytes, max_len=int(args.dump_max)), log_fp=log_fp)
                all_out_parts.append(tx_bytes)

                keep_push_b5 = os.environ.get("UBIGS_WM_PUSH_KEEP_B5", "").strip().lower() in {"1", "true", "yes", "on"}
                for i, extra in enumerate(extras):
                    out = bytes(extra)
                    if (not keep_push_b5) and str(args.post_ke2_push).strip().lower() != "ct-replay" and len(out) >= 6:
                        # Preserve b5 for LOBBY_MSG frames — lobby server pushes
                        # use S→P (0x24), not R→P (0x14)
                        ensure_ubigs_importable()
                        import gsm as _gsm_mod
                        if out[4] != _gsm_mod.MESSAGE_TYPE.LOBBY_MSG.value:
                            b = bytearray(out)
                            b[5] = 0x14
                            out = bytes(b)
                    save_blob(args.save_tx_dir,
                              f"{file_ts()}_{safe_slug(local_ip)}_{local_port}_to_{safe_slug(src_ip)}_{src_port}_push{i}.bin",
                              out)
                    log_line(f"[{now_ts()}] ROUTER_WM TX[push{i}] len={len(out)} sha256={sha16(out)}", log_fp=log_fp)
                    log_line(hexdump(out, max_len=int(args.dump_max)), log_fp=log_fp)
                    all_out_parts.append(out)
                    if push_gap_ms > 0 and i < len(extras) - 1:
                        # Gap requested: flush what we have so far, then sleep.
                        conn.sendall(b"".join(all_out_parts))
                        all_out_parts = []
                        time.sleep(push_gap_ms / 1000.0)

                if all_out_parts:
                    conn.sendall(b"".join(all_out_parts))

    finally:
        if _joined_lobby:
            count = lobby_player_leave()
            log_line(f"[{now_ts()}] ROUTER_WM {src_ip}:{src_port} left lobby, players_now={count}", log_fp=log_fp)
        # Clean up lobby session and broadcast room removal + member leave
        _sk = f"{src_ip}:{src_port}"
        removed_rooms = _lobby_state.unregister_session(_sk)
        # Broadcast MEMBER_LEAVE (51) for the departing player
        _left_username = getattr(_lobby_state, '_last_removed_username', None)
        if _left_username and _joined_lobby:
            member_leave_frame = _build_lobby_msg_frame(
                [str(51), ["1", _left_username]],  # MEMBER_LEAVE: [lobby_id, username]
            )
            _lobby_state.broadcast(member_leave_frame, log_fp=log_fp)
            log_line(
                f"[{now_ts()}] ROUTER_WM MEMBER_LEAVE broadcast: {_left_username} left lobby",
                log_fp=log_fp,
            )
        if removed_rooms:
            for room_entry in removed_rooms:
                room = room_entry["room"]
                remove_frame = _build_lobby_msg_frame([str(55), [str(room.group_id)]])
                _lobby_state.broadcast(remove_frame, log_fp=log_fp)
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY room removed on disconnect: id={room.group_id} name={room.group_name}",
                    log_fp=log_fp,
                )
        try:
            conn.close()
        except Exception:
            pass
        log_line(f"[{now_ts()}] ROUTER_WM {src_ip}:{src_port} DISCONNECT", log_fp=log_fp)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ubisoft GS router wait-module service")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=40005)
    ap.add_argument("--proxy-ip", default="192.168.0.213")
    ap.add_argument("--proxy-port", type=int, default=44001)
    ap.add_argument("--idle-timeout", type=float, default=2.0)
    ap.add_argument("--dump-max", type=int, default=256)
    ap.add_argument("--save-rx-dir", default="captures/tcp/router_wm_rx")
    ap.add_argument("--save-tx-dir", default="captures/tcp/router_wm_tx")
    ap.add_argument("--log-file", default="logs/router_wm_40005.log")
    ap.add_argument(
        "--fixed-rsa-key-file",
        default="",
        help="Optional JSON path to persist/reuse a fixed RSA keypair across GS/WM modules.",
    )
    ap.add_argument(
        "--keyex2-mode",
        default="random",
        choices=["random", "random-rawhdr", "echo-client", "echo-raw", "echo-exact"],
        help="KEY_EXCHANGE(2) response style",
    )
    ap.add_argument(
        "--post-ke2-push",
        default="off",
        choices=["off", "loginwait", "loginwait+friends", "lobbylogin", "loginwait+friends+lobbylogin", "ct-bootstrap", "ct-replay", "all"],
        help="Optional extra WM messages sent immediately after KEY_EXCHANGE(2) response.",
    )
    ap.add_argument(
        "--post-ke2-replay-files",
        default="",
        help="Optional ';' or ',' separated list of raw frame files to send when post-ke2-push includes ct-replay.",
    )
    ap.add_argument("--ct34-profile", default="off", choices=["off", "ct_ps2"], help="One-flag ct34 preset profile")
    ap.add_argument("--ct34-enable", action="store_true", help="Enable experimental PROXY_HANDLER(0xCC) response builder")
    ap.add_argument("--ct34-mode", default="a", choices=["a", "b", "c"], help="ct34 payload variant mode")
    ap.add_argument("--ct34-base", default="persistantdata", help="ct34 module name/base token")
    ap.add_argument("--ct34-host", default="192.168.0.213", help="ct34 proxy host in response")
    ap.add_argument("--ct34-port", type=int, default=44001, help="ct34 proxy port in response")
    ap.add_argument("--ct34-id", type=int, default=1, help="ct34 proxy id in response")
    ap.add_argument("--ct34-num-a", type=int, default=0, help="ct34 numeric slot A")
    ap.add_argument("--ct34-num-b", type=int, default=0, help="ct34 numeric slot B")
    ap.add_argument("--login-boot-delay", type=float, default=0.0,
                    help="If > 0, send a server-first KE1 after this many seconds of silence on connect (WM login flow fix).")
    ap.add_argument("--user-db", default="state/users.json", help="Path to user DB JSON file")
    args = ap.parse_args(argv)

    if str(args.ct34_profile).strip().lower() == "ct_ps2":
        args.ct34_enable = True
        args.ct34_mode = "a"
        args.ct34_base = "persistantdata"
        args.ct34_host = str(args.proxy_ip)
        args.ct34_port = int(args.proxy_port)
        args.ct34_id = 1
        args.ct34_num_a = 0
        args.ct34_num_b = 0

    # Load user DB for MODIFYUSER / REMOVEACCOUNT support
    from ubigs_userdb import UserDB
    args._userdb = None
    try:
        db = UserDB(str(args.user_db))
        db.load()
        args._userdb = db
    except Exception:
        args._userdb = None

    log_fp = None
    if args.log_file:
        pathlib.Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        log_fp = pathlib.Path(args.log_file).open("a", encoding="utf-8")

    args._fixed_rsa = None
    args._post_ke2_replay_frames = []
    if str(args.fixed_rsa_key_file).strip():
        try:
            args._fixed_rsa = _load_or_create_fixed_rsa_keypair(str(args.fixed_rsa_key_file), log_fp=log_fp)
        except Exception as e:
            log_line(f"[{now_ts()}] ROUTER_WM fixed-rsa error: {e}", log_fp=log_fp)
            args._fixed_rsa = None

    # Pre-build server-first KE1 bytes for immediate send on accept() (before thread spawn).
    # This is the only reliable way to beat the game's sub-10ms client-first KE1 on the WM.
    args._boot_ke1_bytes = None
    if args._fixed_rsa is not None:
        try:
            args._boot_ke1_bytes = _build_boot_ke1(fixed_rsa=args._fixed_rsa)
            log_line(f"[{now_ts()}] ROUTER_WM pre-built boot_ke1 len={len(args._boot_ke1_bytes)}", log_fp=log_fp)
        except Exception as e:
            log_line(f"[{now_ts()}] ROUTER_WM pre-build boot_ke1 error: {e}", log_fp=log_fp)

    replay_spec = str(args.post_ke2_replay_files or "").strip()
    if replay_spec:
        parts = [p.strip() for p in replay_spec.replace(";", ",").split(",") if p.strip()]
        for p in parts:
            try:
                args._post_ke2_replay_frames.append(pathlib.Path(p).read_bytes())
            except Exception as e:
                log_line(f"[{now_ts()}] ROUTER_WM post-ke2 replay read error: {p} ({e})", log_fp=log_fp)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, int(args.port)))
    sock.listen(16)

    log_line("Ubisoft GS Router WM listening", log_fp=log_fp)
    log_line(f"  bind:  {args.bind}:{args.port}", log_fp=log_fp)
    log_line(f"  proxy: {args.proxy_ip}:{args.proxy_port}", log_fp=log_fp)
    log_line(f"  keyex2-mode: {args.keyex2_mode}", log_fp=log_fp)
    log_line(f"  post-ke2-push: {args.post_ke2_push}", log_fp=log_fp)
    log_line(f"  post-ke2-replay-files: {len(args._post_ke2_replay_frames)} frame(s)", log_fp=log_fp)
    log_line(f"  fixed-rsa-key-file: {args.fixed_rsa_key_file or 'off'}", log_fp=log_fp)
    log_line(f"  login-boot-delay: {args.login_boot_delay}s", log_fp=log_fp)
    log_line(
        f"  ct34: profile={args.ct34_profile} enabled={args.ct34_enable} mode={args.ct34_mode} base={args.ct34_base} proxy={args.ct34_host}:{args.ct34_port}",
        log_fp=log_fp,
    )

    try:
        while True:
            conn, addr = sock.accept()
            # Send server-first KE1 immediately, before spawning the thread.
            # The game sends client-first KE1 within ~1-10ms of connect(); thread startup
            # overhead (~20-50ms) makes any thread-based timing approach unreliable.
            # Sending here wins the race and causes the game to bundle KE2+LOGINWAITMODULE.
            pre_ke1_sent = False
            if args._boot_ke1_bytes is not None:
                try:
                    conn.sendall(args._boot_ke1_bytes)
                    pre_ke1_sent = True
                except Exception:
                    pass
            th = threading.Thread(target=client_thread, args=(conn, addr, args), kwargs={"log_fp": log_fp, "pre_ke1_sent": pre_ke1_sent}, daemon=True)
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

















