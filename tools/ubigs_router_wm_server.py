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
import copy
import json
import os
import pathlib
import socket
import threading
import time
import traceback
from typing import TextIO

from net_utils import public_ip_for, is_private_addr

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


def _unique_lobby_users(exclude_key=None):
    """Round 37: players currently connected to the lobby server = unique user names among the registered lobby
    sessions (the old counter went up on every JOIN_LOBBY, so a player rejoining was counted again)."""
    try:
        with _lobby_state.lock:
            return len({(inf.get("username") or k) for k, inf in _lobby_state.sessions.items()
                        if k != exclude_key and inf.get("source") in ("JOIN_LOBBY", "LOBBYSERVERLOGIN")})
    except Exception:
        return 0


def lobby_player_join():
    return _unique_lobby_users()


def lobby_player_leave(exclude_key=None):
    return _unique_lobby_users(exclude_key)


def lobby_player_count():
    return _unique_lobby_users()


# ─── Shared Lobby State (room tracking + broadcasting) ────────────
LOBBY_ROOMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "lobby_rooms.json")

# --- Room tuning knobs (environment variables, so no code edits are needed) -------------
#   SCCT_ROOM_CONFIG     LSM flag mask for new rooms. Default 0x1C0 (LSM_ALLINFO, as ubi-gs uses).
#   SCCT_ROOM_OVERRIDE   JSON object of Room attributes forced onto every new room, e.g.
#                        {"group_type": 5, "config": 16, "event_id": 0, "games": "SPLINTERCELL3PS2US"}
#   SCCT_ROOM_NAME_FROM  "host" (default: CREATE_ROOM field 1) or "blob" (name found inside the info blob)
#   SCCT_NEW_GROUP       NEW_GROUP(54) payload: "request" (default, old behaviour), "room", or "off"
#   SCCT_CREATE_ACK      Reply to the creator: "legacy" (default) or "roomid"
#   SCCT_KEEP_ROOMS=1    Keep state/lobby_rooms.json across restarts (default: wiped on startup)
#   SCCT_LOBBY_CONFIG    LSM flag mask on the lobby entry itself (default 0), e.g. 0x1C0
#   SCCT_PUSH_ROOMS_AFTER_42=1  Re-push the room list after the client's SET_PLAYER_INFO
LSM_PRIVATE = 0x1
LSM_ALLINFO = 0x1C0

_ROOM_INT_ATTRS = {
    "group_type", "config", "event_id", "max_players", "max_visitors", "nb_players",
    "nb_visitors", "parent_id", "lobby_sv_id", "group_level",
}
_ROOM_STR_ATTRS = {
    "group_name", "master", "allowed_games", "games", "game_version", "gs_version",
    "ip_addr", "alt_ip_addr",
}


def _default_room_config(password: str) -> int:
    raw = os.environ.get("SCCT_ROOM_CONFIG", "").strip()
    try:
        cfg = int(raw, 0) if raw else LSM_ALLINFO
    except ValueError:
        cfg = LSM_ALLINFO
    if password:
        cfg |= LSM_PRIVATE
    return cfg


def _apply_room_overrides(room, *, log_fp=None) -> None:
    raw = os.environ.get("SCCT_ROOM_OVERRIDE", "").strip()
    if not raw:
        return
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("must be a JSON object")
    except Exception as e:
        log_line(f"[{now_ts()}] ROOM_OVERRIDE ignored (bad JSON): {e}", log_fp=log_fp)
        return
    applied = {}
    for key, val in data.items():
        try:
            if key in _ROOM_INT_ATTRS:
                setattr(room, key, int(val))
            elif key in _ROOM_STR_ATTRS:
                setattr(room, key, str(val))
            else:
                log_line(f"[{now_ts()}] ROOM_OVERRIDE unknown key ignored: {key!r}", log_fp=log_fp)
                continue
            applied[key] = val
        except Exception as e:
            log_line(f"[{now_ts()}] ROOM_OVERRIDE {key!r}={val!r} failed: {e}", log_fp=log_fp)
    if applied:
        log_line(f"[{now_ts()}] ROOM_OVERRIDE applied: {applied}", log_fp=log_fp)


def _room_name_from_blob(info_blob: bytes) -> str:
    """Best-effort map/room name embedded in the 96-byte info blob (e.g. 'Cinema')."""
    try:
        nulls = [i for i, b in enumerate(info_blob) if b == 0]
        for ni in range(len(nulls)):
            candidate = info_blob[nulls[ni] + 1:]
            null_pos = candidate.find(b"\x00")
            if null_pos > 0:
                cs = candidate[:null_pos].decode("ascii", errors="ignore")
                if cs and cs.isprintable() and len(cs) > 1 and not cs.startswith("CAU"):
                    return cs
    except Exception:
        pass
    return ""


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
                    "group_type": getattr(room, "group_type", 1),
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
                    "real_ip_addr": getattr(room, "real_ip_addr", ""),
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
                if info.get("real_ip_addr"):
                    room.real_ip_addr = info.get("real_ip_addr")
                room.nb_players = int(info.get("nb_players", 1))
                room.max_visitors = int(info.get("max_visitors", 8))
                room.nb_visitors = int(info.get("nb_visitors", 0))
                room.games = info.get("games", "")
                room.group_type = int(info.get("group_type", 1))
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

    def set_ctx(self, key, room_id):
        with self.lock:
            self.__dict__.setdefault("ctx", {})[key] = str(room_id)

    def clear_ctx(self, key):
        with self.lock:
            self.__dict__.setdefault("ctx", {}).pop(key, None)

    def get_ctx(self, key):
        with self.lock:
            return self.__dict__.setdefault("ctx", {}).get(key)

    def send_rooms_per_session(self, build_frame, exclude_key=None, log_fp=None):
        """build_frame(ctx_room_id_or_None) -> bytes. Sessions still 'inside' a room they hosted or joined
        (the game never sends a leave for a host) get room entries re-parented to that room."""
        with self.lock:
            targets = [(k, s) for k, s in self.sessions.items() if k != exclude_key]
        for key, session in targets:
            ctx = self.get_ctx(key) if os.environ.get("SCCT_CTX_PARENT", "1").strip().lower() not in ("0", "off", "false", "no") else None
            try:
                session["conn"].sendall(build_frame(ctx))
                log_line(f"[{now_ts()}] LOBBY_BROADCAST(rooms) -> {key} ctx={ctx}", log_fp=log_fp)
            except Exception as e:
                log_line(f"[{now_ts()}] LOBBY_BROADCAST(rooms) -> {key} FAILED: {e}", log_fp=log_fp)

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

    def create_room(self, room, owner_key, raw_frame=None, create_sub_data=None):
        """Store a Room object built from CREATE_ROOM data.

        raw_frame: the original CREATE_ROOM frame bytes (with b5 set to S->P)
                   for relaying to other players.
        create_sub_data: the original CREATE_ROOM DL sub_data list (msg.dl.lst[1])
                         for building NEW_GROUP(54) frames in the game's own format.
        """
        with self.lock:
            self.rooms[room.group_id] = {
                "room": room,
                "owner_key": owner_key,
                "raw_frame": raw_frame,
                "create_sub_data": create_sub_data,
            }
            self._save_rooms_to_file()
        _friends_room_changed(getattr(room, "master", None))
        return room

    def remove_room_by_owner(self, owner_key):
        with self.lock:
            to_remove = [rid for rid, info in self.rooms.items() if info["owner_key"] == owner_key]
            removed = []
            for rid in to_remove:
                removed.append(self.rooms.pop(rid))
            if removed:
                self._save_rooms_to_file()
        for _e in removed:
            _friends_room_changed(getattr(_e["room"], "master", None))
        return removed

    def remove_rooms_by_master(self, master):
        """Drop every room hosted by this username (the game never tells us when a host backs out)."""
        with self.lock:
            to_remove = [rid for rid, info in self.rooms.items()
                         if str(getattr(info["room"], "master", "")).lower() == str(master).lower()]
            removed = [self.rooms.pop(rid) for rid in to_remove]
            if removed:
                self._save_rooms_to_file()
        for _e in removed:
            _friends_room_changed(getattr(_e["room"], "master", None))
        return removed

    def get_rooms(self):
        """Return list of Room objects."""
        with self.lock:
            return [info["room"] for info in self.rooms.values()]

    def get_rooms_with_frames(self):
        """Return list of (Room, raw_frame_bytes_or_None) tuples."""
        with self.lock:
            return [(info["room"], info.get("raw_frame")) for info in self.rooms.values()]

    def get_rooms_with_create_data(self):
        """Return list of (Room, create_sub_data_or_None) tuples."""
        with self.lock:
            return [(info["room"], info.get("create_sub_data")) for info in self.rooms.values()]

    def next_room_id(self):
        with self.lock:
            rid = self._next_room_id
            self._next_room_id += 1
            return rid

    def broadcast(self, frame_bytes, exclude_key=None, log_fp=None):
        """Send frame bytes to all registered lobby sessions except exclude_key."""
        with self.lock:
            targets = [(k, s) for k, s in self.sessions.items() if k != exclude_key]
        if not targets:
            log_line(
                f"[{now_ts()}] LOBBY_BROADCAST: no other registered sessions (len={len(frame_bytes)})",
                log_fp=log_fp,
            )
        for key, session in targets:
            try:
                session["conn"].sendall(frame_bytes)
                log_line(f"[{now_ts()}] LOBBY_BROADCAST -> {key} len={len(frame_bytes)}", log_fp=log_fp)
            except Exception as e:
                log_line(f"[{now_ts()}] LOBBY_BROADCAST -> {key} FAILED: {e}", log_fp=log_fp)


_lobby_state = LobbyState()


def _removal_frames(room_id):
    """GROUP_REMOVE(55) frames. SCCT_REMOVE_B5 = comma list of hex sender/receiver bytes (default 24 = S->P).
    Experiment: the client may file rooms under the sender that delivered them (R=1,S=2,W=3), so a removal
    from another sender would not find the entry. e.g. SCCT_REMOVE_B5=24,14,34"""
    try:
        _b5s = [int(x, 16) for x in os.environ.get("SCCT_REMOVE_B5", "24").split(",") if x.strip()]
    except Exception:
        _b5s = [0x24]
    # Round 26: the client's GROUP_REMOVE parser (CGSLobbyLib::LobbyRcv_GroupRemove) reads index 1 as a nested LIST
    # and index 0 as the int group id. A one-element payload fails that parse, so the room was never removed.
    # SCCT_REMOVE_SHAPE=old restores [id].
    if os.environ.get("SCCT_REMOVE_SHAPE", "old").strip().lower() == "old":
        _pl55 = [str(room_id)]
    else:
        _pl55 = [str(room_id), ["1"]]
    return [_build_lobby_msg_frame([str(55), _pl55], b5=_b) for _b in (_b5s or [0x24])]


def _broadcast_removal(room_id, log_fp=None):
    """GROUP_REMOVE(55) now, and again ~0.8s / ~2.5s later: right after a leave the game may still be
    switching back to the lobby context and silently drop the first one."""
    import threading
    frames = _removal_frames(room_id)
    for _f in frames:
        _lobby_state.broadcast(_f, log_fp=log_fp)
    _push_room_list_all(log_fp)
    if os.environ.get("SCCT_REPEAT_REMOVE", "1").strip().lower() in ("0", "off", "false", "no"):
        return
    try:
        _delays = [float(x) for x in os.environ.get("SCCT_REMOVE_DELAYS", "0.8,2.5").split(",") if x.strip()]
    except Exception:
        _delays = [0.8, 2.5]
    for delay in _delays:
        t = threading.Timer(delay, lambda: [_lobby_state.broadcast(_f, log_fp=log_fp) for _f in frames])
        t.daemon = True
        t.start()


def _push_room_list_all(log_fp=None, delays=None):
    """Authoritative GROUP_INFO(53) of lobby 1 with the CURRENT rooms (possibly none) to every session,
    so clients that cache the old list replace it (the game itself asks for this with msg 9 after leaving a room)."""
    import threading
    if os.environ.get("SCCT_PUSH_AFTER_REMOVE", "1").strip().lower() in ("0", "off", "false", "no"):
        return

    def _send():
        rooms = _lobby_state.get_rooms()
        frame = _build_lobby_msg_frame([str(53), ["1", str(0x100), ["1"], [r.to_list() for r in rooms]]], b5=0x24)
        log_line(f"[{now_ts()}] ROUTER_WM pushing authoritative room list ({len(rooms)} room(s)) to all sessions", log_fp=log_fp)
        _lobby_state.broadcast(frame, log_fp=log_fp)

    if delays is None:
        try:
            delays = [float(x) for x in os.environ.get("SCCT_PUSH_DELAYS", "0.4,1.6").split(",") if x.strip()]
        except Exception:
            delays = [0.4, 1.6]
    for d in delays:
        t = threading.Timer(d, _send)
        t.daemon = True
        t.start()


def _room_as_lobby_format(room):
    """Convert a Room to a 14-field lobby-style list for GROUP_INFO.

    The PS2 game's GROUP_INFO parser appears to use the same 14-field format
    for both lobbies and rooms, differentiating by group_type (field 0):
      0 = lobby, 1 = room.

    Room.to_list() produces 20 fields which likely breaks the parser.
    This function maps room data to the 14-field format the game expects.
    """
    return [
        str(1),                       # [0]  group_type = ROOM
        room.group_name,              # [1]  group_name
        str(room.group_id),           # [2]  group_id
        str(room.lobby_sv_id),        # [3]  lobby_sv_id
        str(room.parent_id),          # [4]  parent_id (lobby this room belongs to)
        str(room.config),             # [5]  config / flags
        str(room.group_level),        # [6]  group_level
        room.master,                  # [7]  master (room creator)
        room.allowed_games,           # [8]  allowed_games (game filter)
        room.games,                   # [9]  games
        room.info,                    # [10] info (binary blob with room name, IPs)
        str(room.event_id),           # [11] event_id (game mode)
        str(room.max_players),        # [12] max_members / max_players
        str(room.nb_players),         # [13] nb_members / nb_players
    ]


def _lobby_reply(msg, subtype, payload):
    """Proper LOBBY_MSG reply: ['38', [subtype, payload]] (same shape the JOIN_ROOM ack uses)."""
    import gsm
    from data import List
    r = gsm.GSMResponse(msg)
    r.header.property = gsm.PROPERTY.GS
    r.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
    r.dl = List([str(gsm.MESSAGE_TYPE.GSSUCCESS.value), [str(subtype), payload]])
    return r


def _lobby_raw_reply(msg, subtype, payload):
    """Reply WITHOUT the GSSUCCESS wrapper: ['<subtype>', payload] (same shape as pushed notifications such as GROUP_INFO)."""
    import gsm
    from data import List
    r = gsm.GSMResponse(msg)
    r.header.property = gsm.PROPERTY.GS
    r.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
    r.dl = List([str(subtype), payload])
    return r


def _lobby_fail(msg, subtype, reason, echo):
    """Refusal reply like the reference server: ['39', [subtype, [reason, echo]]]."""
    import gsm
    from data import List
    r = gsm.GSMResponse(msg)
    r.header.property = gsm.PROPERTY.GS
    r.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
    r.dl = List([str(gsm.MESSAGE_TYPE.GSFAIL.value) if hasattr(gsm.MESSAGE_TYPE, "GSFAIL") else "39",
                 [str(subtype), [str(reason), str(echo)]]])
    return r


def _req_payload_strs(msg):
    try:
        pl = msg.dl.lst[1]
        lst = pl.lst if hasattr(pl, "lst") else pl
        return [str(x) for x in lst]
    except Exception:
        return []


_WM_CONNS = {}


_LIVE_COUNT_STARTED = False


def _start_live_lobby_count(log_fp=None):
    """Round 73: keep the 'Lobby selection' player count live. A watcher thread notices when the set of players inside the
    lobby changes and pushes a fresh lobby-list GROUP_INFO (with the count) on the Online-menu connection of every
    logged-in player who is NOT inside the lobby (i.e. sitting on the selection screen). Every logged-in player gets the count (lobby
    players plus themselves, like the count sent at login) whenever it changes. Disable with SCCT_LOBBY_COUNT_LIVE=0."""
    global _LIVE_COUNT_STARTED
    if _LIVE_COUNT_STARTED or os.environ.get("SCCT_LOBBY_COUNT_LIVE", "1").strip().lower() in ("0", "off", "false", "no"):
        return
    _LIVE_COUNT_STARTED = True
    import threading as _t
    import time as _tm

    def _users():
        with _lobby_state.lock:
            return {(inf.get("username") or k) for k, inf in _lobby_state.sessions.items()
                    if inf.get("source") in ("JOIN_LOBBY", "LOBBYSERVERLOGIN")}

    def _loop():
        sent = {}   # user -> count last shown to that user
        while True:
            try:
                _tm.sleep(float(os.environ.get("SCCT_LOBBY_COUNT_POLL", "2")))
                cur = frozenset(_users())
                for name, wc in list(_WM_CONNS.items()):
                    n = len(cur | {name})
                    if name not in sent or sent[name][0] is not wc:
                        sent[name] = (wc, n)      # count the player got at login
                        continue
                    if sent[name][1] == n:
                        continue
                    try:
                        wc.sendall(_lobby_list_frame(count=n))
                        sent[name] = (wc, n)
                        log_line(f"[{now_ts()}] LOBBY_COUNT live update -> {name}: {n}", log_fp=log_fp)
                    except Exception as _e:
                        log_line(f"[{now_ts()}] LOBBY_COUNT push to {name} failed: {_e!r}; dropping its WM conn", log_fp=log_fp)
                        _WM_CONNS.pop(name, None)
                        sent.pop(name, None)
            except Exception as _e2:
                log_line(f"[{now_ts()}] LOBBY_COUNT watcher error: {_e2!r}", log_fp=log_fp)
    _th = _t.Thread(target=_loop, daemon=True)
    _th.start()
_LEFT_LOBBY = set()

# --- Friend list (round 39) -------------------------------------------------------------------------
# ADDFRIEND(75) / DELFRIEND(76) / FRIENDLIST(89) / LOGINFRIENDS(78) / UPDATEFRIEND(74, pushed by the server).
# Modelled on the ubi-gs-go reference: the friend list lives in state/friends.json (user -> friend names);
# a friend must be a registered user; the game adds a friend to its list when it receives the UPDATEFRIEND
# push, not from the reply.  UPDATEFRIEND = [name, status u32, "", u32 0, u32 0, ""]; status 0 = online.
#   SCCT_FRIENDS=0                 switch the whole feature off (old behaviour: empty replies)
#   SCCT_FRIEND_ONLINE_STATUS      status value for an online friend (default 0)
#   SCCT_FRIEND_OFFLINE_STATUS     status value for an offline friend (default 1)
_FRIENDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "friends.json")
_FRIENDS_LOCK = threading.Lock()
_ONLINE = {}  # lower-case name -> (display name, connection)


def _friends_enabled():
    return os.environ.get("SCCT_FRIENDS", "1").strip().lower() not in ("0", "off", "false", "no")


def _friends_load():
    try:
        with open(_FRIENDS_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _friends_save(d):
    try:
        os.makedirs(os.path.dirname(_FRIENDS_FILE), exist_ok=True)
        tmp = _FRIENDS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2, sort_keys=True)
        os.replace(tmp, _FRIENDS_FILE)
    except Exception:
        pass


def _friend_list(user):
    with _FRIENDS_LOCK:
        return list(_friends_load().get(user.lower(), []))


def _friend_add(user, friend):
    with _FRIENDS_LOCK:
        d = _friends_load()
        lst = d.setdefault(user.lower(), [])
        if friend.lower() not in [x.lower() for x in lst]:
            lst.append(friend)
        _friends_save(d)


def _friend_remove(user, friend):
    with _FRIENDS_LOCK:
        d = _friends_load()
        lst = d.get(user.lower(), [])
        d[user.lower()] = [x for x in lst if x.lower() != friend.lower()]
        _friends_save(d)


def _friend_of(user):
    """Lower-case names of the users that have `user` on their friend list."""
    with _FRIENDS_LOCK:
        d = _friends_load()
    return [k for k, v in d.items() if user.lower() in [x.lower() for x in v]]


def _find_registered_user(userdb, name):
    """Canonical (stored) spelling of a registered user name, or None."""
    if userdb is None or not name:
        return None
    try:
        with userdb._lock:
            for k in (userdb._data.get("users") or {}):
                if k.lower() == name.lower():
                    return k
    except Exception:
        return None
    return None


def _build_gs_frame(msg_type, dl_list, b5=0x14):
    """A raw GS frame of the given message type (router -> player by default)."""
    ensure_ubigs_importable()
    import gsm
    from data import List

    class _Fake:
        pass

    fake = _Fake()
    fake.header = gsm.GSMessageHeader(bytes([0, 0, 6, 0, gsm.MESSAGE_TYPE.LOBBY_MSG.value, 0x82]))
    fake.dl = None
    res = gsm.GSMResponse(fake)
    res.header.property = gsm.PROPERTY.GS
    res.header.type = msg_type
    res.dl = List(dl_list)
    out = bytearray(bytes(res))
    if len(out) >= 6:
        out[5] = b5
    return bytes(out)


_INVITES = {}  # (inviter lower, invitee lower) -> last invite payload (16 bytes: cmd, 1, 1, room id)


def _friends_on_join_room(joiner, room, log_fp=None):
    """A player joined a room. If its host had invited them, tell the host's game the invite was taken
    (PEERMSG command 2 from the joiner), which clears the host's 'Cancel Invite' state."""
    try:
        if not _friends_enabled():
            return
        host = str(getattr(room, "master", ""))
        key = (host.lower(), str(joiner).lower())
        pb = _INVITES.get(key)
        ent = _ONLINE.get(host.lower())
        if pb is None or ent is None or str(joiner).lower() == host.lower():
            return
        ensure_ubigs_importable()
        import gsm
        # Round 48: decoded from the game's own decline code (AnswerGameInvite): cmd 2 is a ONE-byte message (len 1, byte 2).
        # The receiving handler ignores cmd 2 unless the length is exactly 1 (cmd 1 and 3 need 16), so the 16-byte
        # version sent in r41-r47 was dropped by the host's game.
        ent[1].sendall(_build_gs_frame(gsm.MESSAGE_TYPE.PEERMSG, [str(joiner), [(1).to_bytes(4, "little"), b"\x02"]]))
        _INVITES.pop(key, None)
        if os.environ.get("SCCT_INVITE_REFRESH", "1").strip().lower() not in ("0", "off", "false", "no"):
            # Round 47: the game clears the 'invite sent' flag on PEERMSG cmd 2, but its friend screen can keep showing
            # 'Cancel Invite'. Follow up with fresh UPDATEFRIEND frames so the screen is refreshed.
            import threading
            def _refresh(_h=host, _j=str(joiner)):
                try:
                    e2 = _ONLINE.get(_h.lower())
                    if e2 is not None:
                        e2[1].sendall(_update_friend_frame(_j, _j.lower() in _ONLINE))
                        log_line(f"[{now_ts()}] FRIENDS refresh UPDATEFRIEND {_j} -> {_h}", log_fp=log_fp)
                except Exception:
                    pass
            for _d in (0.3, 2.0):
                _t = threading.Timer(_d, _refresh); _t.daemon = True; _t.start()
        log_line(f"[{now_ts()}] FRIENDS invite accepted: {joiner} joined {host}'s room -> PEERMSG cmd 2 sent to {host}", log_fp=log_fp)
    except Exception as e:
        log_line(f"[{now_ts()}] FRIENDS invite-accepted notify failed: {e}", log_fp=log_fp)


def _hosts_room(name):
    try:
        with _lobby_state.lock:
            return any(str(getattr(i["room"], "master", "")).lower() == str(name).lower()
                       for i in _lobby_state.rooms.values())
    except Exception:
        return False


def _friends_room_changed(master, log_fp=None):
    """A room appeared/disappeared for this host: refresh the status their friends see."""
    try:
        if _friends_enabled() and master and str(master).lower() in _ONLINE:
            _notify_friends_of(_ONLINE[str(master).lower()][0], True, log_fp=log_fp)
    except Exception:
        pass


def _update_friend_frame(name, online):
    ensure_ubigs_importable()
    import gsm
    on = int(os.environ.get("SCCT_FRIEND_ONLINE_STATUS", "0"))
    off = int(os.environ.get("SCCT_FRIEND_OFFLINE_STATUS", "1"))
    # The game treats status 10 as "joinable" (friend is hosting a room); 1 = offline; anything else = online.
    joinable = int(os.environ.get("SCCT_FRIEND_JOINABLE_STATUS", "10"))
    status = off
    if online:
        status = joinable if (os.environ.get("SCCT_FRIEND_JOINABLE", "1") != "0" and _hosts_room(name)) else on
    u32 = lambda v: int(v).to_bytes(4, "little")
    # The game flags a friend as "playing this game" when the last field equals its own game id (required for the
    # Invite Friend / Join Friend buttons). Only sent for online friends.
    gid = os.environ.get("SCCT_FRIEND_GAME", "SPLINTERCELL3PS2US") if online else ""
    return _build_gs_frame(gsm.MESSAGE_TYPE.UPDATEFRIEND, [name, u32(status), "", u32(0), u32(0), gid])


def _notify_friends_of(me, online, log_fp=None):
    """Tell every online player who has `me` as a friend that `me` came online / went offline."""
    for lower in _friend_of(me):
        ent = _ONLINE.get(lower)
        if not ent:
            continue
        try:
            ent[1].sendall(_update_friend_frame(me, online))
            log_line(f"[{now_ts()}] FRIENDS notified {ent[0]}: {me} {'online' if online else 'offline'}", log_fp=log_fp)
        except Exception as e:
            log_line(f"[{now_ts()}] FRIENDS notify of {ent[0]} failed: {e}", log_fp=log_fp)


def _friends_conn_closed(conn, log_fp=None):
    for lower, ent in list(_ONLINE.items()):
        if ent[1] is conn:
            _ONLINE.pop(lower, None)
            log_line(f"[{now_ts()}] FRIENDS {ent[0]} offline (connection closed)", log_fp=log_fp)
            _notify_friends_of(ent[0], False, log_fp=log_fp)


def _handle_friends(clt, msg, userdb, conn, log_fp):
    """Returns (response, extra frames) for friend messages, or None to fall back to the old handling."""
    ensure_ubigs_importable()
    import gsm
    from data import List
    me = getattr(clt, "username", None)
    mt = msg.header.type
    mt_byte = int(mt.value).to_bytes(1, "little")
    first = None
    try:
        if msg.dl is not None and msg.dl.lst and isinstance(msg.dl.lst[0], str):
            first = msg.dl.lst[0]
    except Exception:
        first = None

    def _ok(*data):
        r = gsm.GSMResponse(msg)
        r.header.property = gsm.PROPERTY.GS
        r.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
        r.dl = List([mt_byte, *data])
        return r

    def _fail(reason):
        r = gsm.GSMResponse(msg)
        r.header.property = gsm.PROPERTY.GS
        r.header.type = gsm.MESSAGE_TYPE.GSFAIL
        r.dl = List([mt_byte, [int(reason).to_bytes(4, "little")]])
        return r

    if mt == gsm.MESSAGE_TYPE.DISCONNECTFRIENDS:
        if conn is not None:
            _friends_conn_closed(conn, log_fp)
        return None

    if not me:
        log_line(f"[{now_ts()}] FRIENDS {mt.name}: no user name on this connection, old handling", log_fp=log_fp)
        return None

    if mt == gsm.MESSAGE_TYPE.LOGINFRIENDS:
        if conn is not None:
            _ONLINE[me.lower()] = (me, conn)
        friends = _friend_list(me)
        extras = [_update_friend_frame(f, f.lower() in _ONLINE) for f in friends]
        log_line(f"[{now_ts()}] FRIENDS LOGINFRIENDS {me}: {len(friends)} friend(s) {friends}", log_fp=log_fp)
        _notify_friends_of(me, True, log_fp=log_fp)
        return (gsm.LoginFriendsResponse(msg), extras)

    if mt == gsm.MESSAGE_TYPE.FRIENDLIST:
        friends = _friend_list(me)
        log_line(f"[{now_ts()}] FRIENDS FRIENDLIST {me}: {friends}", log_fp=log_fp)
        return (_ok(), [_update_friend_frame(f, f.lower() in _ONLINE) for f in friends])

    if mt == gsm.MESSAGE_TYPE.ADDFRIEND:
        target = _find_registered_user(userdb, first)
        if target is None or target.lower() == me.lower():
            log_line(f"[{now_ts()}] FRIENDS ADDFRIEND {me} -> {first!r}: refused (unknown user or self)", log_fp=log_fp)
            # The game's ADDFRIEND failure handler only has specific messages for reasons 13 and 14
            # (any other reason shows "Unexpected error"). Default 14; set SCCT_FRIEND_UNKNOWN_REASON=13 to try the other.
            try:
                _reason = int(os.environ.get("SCCT_FRIEND_UNKNOWN_REASON", "14"))
            except ValueError:
                _reason = 14
            return (_fail(_reason), [])
        _friend_add(me, target)
        log_line(f"[{now_ts()}] FRIENDS ADDFRIEND {me} -> {target}: added (online={target.lower() in _ONLINE})", log_fp=log_fp)
        return (_ok(target), [_update_friend_frame(target, target.lower() in _ONLINE)])

    if mt == gsm.MESSAGE_TYPE.PEERPLAYER:
        try:
            _pb = msg.dl.lst[1][1] if len(msg.dl.lst) > 1 else b""
            _cmd = int.from_bytes(_pb[:4], "little") if len(_pb) >= 4 else 0
            _key = (me.lower(), (first or "").lower())
            if _cmd == 1:
                _INVITES[_key] = bytes(_pb)
            elif _cmd == 3:
                _INVITES.pop(_key, None)
        except Exception:
            pass
        # Game invites / friend-to-friend messages: client sends [target, [u32 len, payload]];
        # we forward [sender, [u32 len, payload]] as PEERMSG to the target if online. Payload is opaque.
        inner = None
        try:
            if msg.dl is not None and len(msg.dl.lst) > 1:
                inner = msg.dl.lst[1]
        except Exception:
            inner = None
        log_line(f"[{now_ts()}] FRIENDS PEERPLAYER {me} -> {first!r} payload={inner!r}", log_fp=log_fp)
        ent = _ONLINE.get((first or "").lower())
        if not first or ent is None or inner is None:
            try:
                _reason = int(os.environ.get("SCCT_PEER_OFFLINE_REASON", "14"))
            except ValueError:
                _reason = 14
            log_line(f"[{now_ts()}] FRIENDS PEERPLAYER refused (target offline/unknown), reason {_reason}", log_fp=log_fp)
            return (_fail(_reason), [])
        try:
            ent[1].sendall(_build_gs_frame(gsm.MESSAGE_TYPE.PEERMSG, [me, inner]))
            log_line(f"[{now_ts()}] FRIENDS PEERMSG forwarded {me} -> {ent[0]}", log_fp=log_fp)
        except Exception as e:
            log_line(f"[{now_ts()}] FRIENDS PEERMSG forward to {ent[0]} failed: {e}", log_fp=log_fp)
            return (_fail(14), [])
        return (_ok(ent[0]), [])

    if mt == gsm.MESSAGE_TYPE.DELFRIEND:
        if first:
            _friend_remove(me, first)
        log_line(f"[{now_ts()}] FRIENDS DELFRIEND {me} -> {first!r}", log_fp=log_fp)
        return (_ok(first or ""), [])

    return None


def _lobby_list_frame(count=None):
    """Round 29: the same lobby-list GROUP_INFO that LOGIN pushes (b5=0x14)."""
    ensure_ubigs_importable()
    from group import Lobby
    lobby1 = Lobby(id=1, name="www.splintercellonline.net", master="", game_mode=0)
    lobby1.max_members = 16
    lobby1.nb_members = lobby_player_count() if count is None else count
    return _build_lobby_msg_frame([str(53), ["1", str(0x100), ["0"], [lobby1.to_list()]]], b5=0x14)


# ─── UDP relay (SCCT_RELAY=1) ───────────────────────────────────────────────────────────────────────────────────
# Outside joiners whose public IP differs from the host's are told to dial the relay (SCCT_RELAY_IP, default
# SCCT_PUBLIC_IP) instead of the host; the NAT responder (tools/nat2/udp_reply_server.py --relay-ports) forwards their
# traffic to the host through the hole the host's own NAT pings keep open. The mapping is sent over a loopback UDP
# control port (SCCT_RELAY_CTRL_PORT, default 45099).
_RELAY_SOCK = None


def _relay_enabled() -> bool:
    return os.environ.get("SCCT_RELAY", "0").strip().lower() in ("1", "on", "true", "yes")


def _relay_ip() -> str:
    return (os.environ.get("SCCT_RELAY_IP", "").strip() or os.environ.get("SCCT_PUBLIC_IP", "").strip())


def _relay_notify(payload: dict, *, log_fp=None) -> None:
    global _RELAY_SOCK
    if not _relay_enabled():
        return
    try:
        if _RELAY_SOCK is None:
            _RELAY_SOCK = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port = int(os.environ.get("SCCT_RELAY_CTRL_PORT", "45099"))
        _RELAY_SOCK.sendto(json.dumps(payload).encode("utf-8"), ("127.0.0.1", port))
        log_line(f"[{now_ts()}] RELAY ctrl -> {payload}", log_fp=log_fp)
    except Exception as e:
        log_line(f"[{now_ts()}] RELAY ctrl send failed: {e!r}", log_fp=log_fp)


def _room_host_ip(room) -> str:
    """The host's real address (room.ip_addr holds the relay address for a relay-advertised room)."""
    return str(getattr(room, "real_ip_addr", "") or getattr(room, "ip_addr", "") or "")


def _relay_room_at_create(room, creator_ip: str, *, log_fp=None) -> bool:
    """SCCT_RELAY=1: a room hosted by an outside (public) client is advertised with the relay address everywhere
    (fields 18/19 and both blob slots), because the game dials the address from its room list, not from the
    JOIN_ROOM reply. The real host address is kept in room.real_ip_addr for the relay mapping."""
    if not _relay_enabled():
        return False
    rip = _relay_ip()
    if not rip or not creator_ip or is_private_addr(creator_ip):
        return False
    room.real_ip_addr = creator_ip
    room.ip_addr = rip
    room.alt_ip_addr = rip
    room.info = _rewrite_room_blob(room.info, rip)
    log_line(f"[{now_ts()}] ROUTER_WM CREATE_ROOM relay: room by {room.master} at {creator_ip} advertised as {rip} (blob slots rewritten)", log_fp=log_fp)
    return True


def _relay_for_joiner(joiner_ip: str, room, is_host: bool) -> str:
    """Relay address this joiner should dial for `room`, or '' for a direct connection."""
    if not _relay_enabled() or is_host or room is None:
        return ""
    if not getattr(room, "real_ip_addr", ""):
        return ""   # room not relay-advertised (LAN host): direct
    return str(room.ip_addr or _relay_ip())


def _rewrite_room_blob(blob: bytes, ip: str) -> bytes:
    """Replace both address slots of the 96-byte room info blob (ASCII at 48/72, binary at 64/88; ports kept)."""
    try:
        if not isinstance(blob, (bytes, bytearray)) or len(blob) != 96:
            return blob
        b = bytearray(blob)
        raw = socket.inet_aton(ip)
        txt = ip.encode("ascii")[:15].ljust(16, b"\x00")
        b[48:64] = txt
        b[64:68] = raw
        b[72:88] = txt
        b[88:92] = raw
        return bytes(b)
    except Exception:
        return blob


def _room_list_via_relay(room, relay_ip: str) -> list:
    """room.to_list() with the host address (fields 18/19 and the info blob) replaced by the relay address."""
    lst = list(room.to_list())
    if len(lst) >= 20:
        lst[10] = _rewrite_room_blob(lst[10], relay_ip)
        lst[18] = relay_ip
        lst[19] = relay_ip
    return lst

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

    if msg.header.type == gsm.MESSAGE_TYPE.NEWUSERREQUEST:
        # Account creation — can arrive here if the game's first connection (Router)
        # failed and the second connection (WM) gets routed the full flow.
        # Parse and create the user, then respond with the same format the Router uses.
        username = None
        password = None
        dp = None
        try:
            if msg.dl is not None and getattr(msg.dl, "lst", None):
                lst = msg.dl.lst
                # NEWUSERREQUEST payload: [dp, username, password, ...]
                dp = lst[0] if len(lst) >= 1 and isinstance(lst[0], str) else None
                username = lst[1] if len(lst) >= 2 and isinstance(lst[1], str) else None
                password = lst[2] if len(lst) >= 3 and isinstance(lst[2], str) else None
        except Exception:
            pass

        if userdb is not None:
            try:
                userdb.load()
            except Exception:
                pass
        if userdb is not None and username:
            if userdb.get_user(username) is not None:
                log_line(f"[{now_ts()}] ROUTER_WM NEWUSERREQUEST SKIP: user={username!r} already exists", log_fp=log_fp)
            elif password is not None:
                userdb.upsert_user(username=username, password=password, dp=dp)
                try:
                    userdb.save()
                except Exception:
                    pass
                log_line(f"[{now_ts()}] ROUTER_WM NEWUSERREQUEST OK: created user={username!r}", log_fp=log_fp)
        else:
            log_line(f"[{now_ts()}] ROUTER_WM NEWUSERREQUEST: user={username!r} db={'yes' if userdb else 'no'}", log_fp=log_fp)

        if username:
            clt.username = username

        # Respond with PROXY_HANDLER (0xCC) b5=0x41 + hardcoded extras
        # (same format the Router uses — game expects this specific pattern)
        res = gsm.GSMResponse(msg)
        res.header = copy.deepcopy(msg.header)
        res.header.property = gsm.PROPERTY.GS
        res.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
        res.header.sender = gsm.SENDER_RECEIVER.P
        res.header.receiver = gsm.SENDER_RECEIVER.R
        msg_id = gsm.MESSAGE_TYPE.NEWUSERREQUEST.value
        res.dl = List([msg_id.to_bytes(1, "little")])
        extras = [
            bytes.fromhex("00000c00cc41eb8be88a8c8c"),
            bytes.fromhex("00002600cc41eb8bd3abafae8a8ca0a0ada28ce3a4a9a1a6d4bab4a039a1a9fde1f5b69ea7a7"),
            bytes.fromhex("00000c002641eb8b8f8a8c8c"),
            bytes.fromhex("000006002641"),
            bytes.fromhex("00004400cc41fab3fde1fef9a4acb9d6cae5e8939a858c90e7f5d1d98388bef1fba1f29f8a8ae3edc1f3829989c2ebeba79686cff098ff969f9bbbf099d6de8c898f9e9b"),
        ]
        return (res, extras)

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

        if os.environ.get("SCCT_LADDER_MODULE","0").strip()=="1":
            log_line(f"[{now_ts()}] LADDER_MODULE LOGIN accepted: {lst!r}" if False else f"[{now_ts()}] LADDER_MODULE LOGIN accepted user={username!r} game={password!r}", log_fp=log_fp)
            _lr = gsm.GSMResponse(msg)
            _lr.header = copy.deepcopy(msg.header)
            _lr.header.property = gsm.PROPERTY.GS
            _lr.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
            _lr.dl = List([b'\x66', []])
            return _lr

        # Validate against userdb
        login_ok = True
        login_encrypted_unparsed = (
            msg.header.property == gsm.PROPERTY.GS_ENCRYPT and msg.dl is None
        )
        if userdb is not None and not login_encrypted_unparsed:
            try:
                userdb.load()  # pick up accounts the router (40000) just created
            except Exception:
                pass
            if username is None:
                login_ok = False
            elif userdb.get_user(username) is None:
                if os.environ.get("SCCT_AUTO_REGISTER", "0").strip() == "1" and password:
                    try:
                        userdb.upsert_user(username=username, password=password)
                        userdb.save()
                        log_line(f"[{now_ts()}] ROUTER_WM LOGIN AUTO-REGISTER: created user={username!r}", log_fp=log_fp)
                    except Exception:
                        login_ok = False
                else:
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
        if os.environ.get("SCCT_LADDER_MODULE","0").strip()=="1":
            log_line(f"[{now_ts()}] LADDER_MODULE LOGINWAITMODULE user={clt.username!r}", log_fp=log_fp)
            _wr = gsm.GSMResponse(msg)
            _wr.header = copy.deepcopy(msg.header)
            _wr.header.property = gsm.PROPERTY.GS
            _wr.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
            _wr.dl = List([b'\x4d', []])
            return _wr
        return gsm.LoginWaitModuleResponse(msg)

    if _friends_enabled() and msg.header.type in (
        gsm.MESSAGE_TYPE.LOGINFRIENDS, gsm.MESSAGE_TYPE.ADDFRIEND, gsm.MESSAGE_TYPE.DELFRIEND,
        gsm.MESSAGE_TYPE.FRIENDLIST, gsm.MESSAGE_TYPE.DISCONNECTFRIENDS,
        gsm.MESSAGE_TYPE.PEERPLAYER,
    ):
        _fr = _handle_friends(clt, msg, userdb, conn, log_fp)
        if _fr is not None:
            return _fr

    if msg.header.type == gsm.MESSAGE_TYPE.LOGINFRIENDS:
        return gsm.LoginFriendsResponse(msg)

    if msg.header.type == gsm.MESSAGE_TYPE.JOINWAITMODULE:
        # JOINWAITMODULE arrives on WM when the game reconnects for lobby-server.
        # Respond with GSSUCCESS pointing back to ourselves (WM server).
        try:
            if msg.dl is not None and msg.dl.lst and isinstance(msg.dl.lst[0], str):
                clt.username = msg.dl.lst[0]
        except Exception:
            pass
        log_line(
            f"[{now_ts()}] ROUTER_WM JOINWAITMODULE user={clt.username} -> self {ct34_host}:40005",
            log_fp=log_fp,
        )
        if os.environ.get("SCCT_LADDER_MODULE","0").strip()=="1":
            log_line(f"[{now_ts()}] LADDER_MODULE JOINWAITMODULE -> {ct34_host}:44002", log_fp=log_fp)
            _jr = gsm.GSMResponse(msg)
            _jr.header = copy.deepcopy(msg.header)
            _jr.header.property = gsm.PROPERTY.GS
            _jr.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
            _jr.dl = List([b'\x5d', [str(clt.username or '')[:16], str(ct34_host), "44002"]])
            return _jr
        return gsm.JoinWaitModuleResponse(msg, (str(ct34_host), 40005))

    if msg.header.type == gsm.MESSAGE_TYPE.PLAYERINFO:
        return gsm.PlayerInfoResponse(msg, clt.username or "noname")

    if msg.header.type == gsm.MESSAGE_TYPE.PROXY_HANDLER and os.environ.get("SCCT_LADDER_MODULE", "0").strip() == "1":
        # Round 54: this process is the proxy MODULE server (port 44002: ladderquery = Ranking screen, persistantdata =
        # profile). Module data arrives as [code, seq, payload] and is answered [code, seq, result] (same connection).
        # Step 1: log every request in full so the Ranking formats can be read from the log, answer with a failure.
        def _dlr(x, depth=0):
            try:
                if hasattr(x, "lst"):
                    x = x.lst
                if isinstance(x, (list, tuple)):
                    return "[" + ", ".join(_dlr(i, depth + 1) for i in x) + "]"
                if isinstance(x, (bytes, bytearray)):
                    return "b" + repr(bytes(x)[:200])
                return repr(x)
            except Exception as _e:
                return f"<{_e}>"
        _items = list(msg.dl.lst) if msg.dl is not None else []
        log_line(f"[{now_ts()}] LADDER_MODULE request from {clt.username!r}: {_dlr(_items)}", log_fp=log_fp)
        if len(_items) >= 3 and str(_items[0]) == "1281" and os.environ.get("SCCT_LADDER_REPLY","1").strip()!="0":
            try:
                _body = _items[2]
                _rq = _body[1]
                _req_id = str(_rq[0]); _qtype = str(_rq[4][0]); _qargs = [str(a) for a in _rq[4][1]]
                _cols = [str(c) for c in _rq[5][0]]
                _sortspec = [str(x) for x in (_rq[5][1] if len(_rq[5]) > 1 else [])]
                _db = {}
                try:
                    import json as _json
                    _sp = pathlib.Path(__file__).resolve().parent.parent / "state" / "ladder_stats.json"
                    if _sp.exists():
                        _db = _json.loads(_sp.read_text(encoding="utf-8-sig"))
                except Exception as _e:
                    log_line(f"[{now_ts()}] LADDER_MODULE stats read error: {_e}", log_fp=log_fp)
                # every player starts at level 1 / 0 points; a player is added to the file on first query
                try:
                    _me = str(clt.username or "")
                    if _me and _me not in _db:
                        _db[_me] = {"GEN_LEVEL": 1, "GEN_SCORE": 0, "GEN_GAMES": 0, "GEN_WON": 0, "GEN_TIME": 0,
                                    "SPY_SCORE": 0, "SPY_GAMES": 0, "SPY_WON": 0, "SPY_TIME": 0,
                                    "SEN_SCORE": 0, "SEN_GAMES": 0, "SEN_WON": 0, "SEN_TIME": 0}
                        _sp.parent.mkdir(exist_ok=True); _sp.write_text(_json.dumps(_db, indent=1), encoding="utf-8")
                        log_line(f"[{now_ts()}] LADDER_MODULE new ranking player {_me!r} added at level 1 / 0 points", log_fp=log_fp)
                except Exception as _e:
                    log_line(f"[{now_ts()}] LADDER_MODULE add player error: {_e!r}", log_fp=log_fp)
                # also add every registered account (state/users.json) at level 1 / 0 points
                try:
                    _up = _sp.parent / "users.json"
                    if _up.exists():
                        _ud = _json.loads(_up.read_text(encoding="utf-8-sig")).get("users", {})
                        _added = [u for u in _ud if u not in _db]
                        for u in _added:
                            _db[u] = {"GEN_LEVEL": 1, "GEN_SCORE": 0, "GEN_GAMES": 0, "GEN_WON": 0, "GEN_TIME": 0,
                                      "SPY_SCORE": 0, "SPY_GAMES": 0, "SPY_WON": 0, "SPY_TIME": 0,
                                      "SEN_SCORE": 0, "SEN_GAMES": 0, "SEN_WON": 0, "SEN_TIME": 0}
                        if _added:
                            _sp.write_text(_json.dumps(_db, indent=1), encoding="utf-8")
                            log_line(f"[{now_ts()}] LADDER_MODULE added registered users at level 1 / 0 points: {_added}", log_fp=log_fp)
                except Exception as _e:
                    log_line(f"[{now_ts()}] LADDER_MODULE users.json sync error: {_e!r}", log_fp=log_fp)
                _ppl = max(1, int(os.environ.get("SCCT_LADDER_PTS_PER_LEVEL", "500")))
                _maxlvl = max(1, int(os.environ.get("SCCT_LADDER_MAX_LEVEL", "15")))
                _lin = os.environ.get("SCCT_LADDER_PTS_PER_LEVEL", "").strip()
                def _level_for(_pts):
                    _pts = max(0, int(float(_pts or 0)))
                    if _lin:   # optional linear override
                        return min(_maxlvl, 1 + _pts // max(1, int(_lin)))
                    # Round 69: curve fitted to the original server's ranking screenshots (level L needs 50*(L-1)*(L+9) points)
                    _lv = 1
                    while _lv < _maxlvl and _pts >= 50 * _lv * (_lv + 10):
                        _lv += 1
                    return _lv
                for _n in _db:
                    if "SPY_SCORE" in _db[_n] or "SEN_SCORE" in _db[_n]:   # overall = spy + mercenary totals
                        try: _db[_n]["GEN_SCORE"] = int(float(_db[_n].get("SPY_SCORE", 0) or 0)) + int(float(_db[_n].get("SEN_SCORE", 0) or 0))
                        except Exception: pass
                    _db[_n]["GEN_LEVEL"] = _level_for(_db[_n].get("GEN_SCORE", 0))
                _skey = _sortspec[0] if _sortspec else "GEN_SCORE"
                def _num(v):
                    try: return int(float(v))
                    except Exception: return 0
                _ranked = sorted(_db.keys(), key=lambda n: (-_num(_db[n].get(_skey, 0)), n.upper()))
                def _row_for(name):
                    st = dict(_db.get(name) or {})
                    st["ALIAS"] = name
                    _rk = (_ranked.index(name) + 1) if name in _ranked else 0
                    st["GLOBAL_RANK"] = _rk
                    st["QUERY_RANK"] = _rk
                    for _pfx in ("GEN", "SPY", "SEN"):
                        g = _num(st.get(_pfx + "_GAMES", 0)); w = _num(st.get(_pfx + "_WON", 0))
                        if (_pfx + "_WINRATIO") not in st:
                            st[_pfx + "_WINRATIO"] = (100 * w // g) if g else 0
                    # the player's level is one overall value: every view (teams/modes) shows GEN_LEVEL
                    for _c in _cols:
                        if _c.endswith("_LEVEL") and _c != "GEN_LEVEL":
                            st[_c] = st.get("GEN_LEVEL", 0)
                    return [str(st.get(c, 0)) for c in _cols]
                _total = len(_ranked)
                if _qtype == "3":
                    _off = _num(_qargs[0]) if _qargs else 0
                    _cnt = _num(_qargs[1]) if len(_qargs) > 1 else 19
                    _names = _ranked[_off:_off + _cnt + 1]
                elif _qtype == "4":
                    # list of names (Friends filter): every known name, in rank order
                    _names = [n for n in _ranked if n.upper() in [a.upper() for a in _qargs]]
                    _total = len(_names)
                elif _qtype == "1":
                    # pivot: page of rows around the named player (Top Rank / Find Me / Details)
                    _cnt = _num(_qargs[1]) if len(_qargs) > 1 else 19
                    _up = {n.upper(): n for n in _ranked}
                    _nm = _up.get(_qargs[0].upper() if _qargs else "", None)
                    _ix = _ranked.index(_nm) if _nm else 0
                    _lo = max(0, _ix - _cnt // 2)
                    _names = _ranked[_lo:_lo + _cnt + 1]
                    if not _names and _qargs:
                        _names = _qargs[:1]
                elif _qtype == "2":
                    _names = _qargs if _qargs else [str(clt.username or "")]
                else:
                    _names = _qargs[:1]
                _rows = [_row_for(n) for n in _names]
                # Diagnostic (only while state/ladder_diag.flag exists): the post-match "my stats" query gets recognisable
                # score values (GEN 333 / SPY 555 / SEN 777) so the Game Statistics "Old Total" shows which column it reads.
                try:
                    if _qtype == "2" and (_sp.parent / "ladder_diag.flag").exists() and (_sp.parent / "ladder_diag.flag").read_text(encoding="utf-8-sig", errors="ignore").strip().lower() == "diag":
                        _dv = {"GEN_SCORE": "333", "SPY_SCORE": "555", "SEN_SCORE": "777"}
                        _rows = [[_dv.get(c, v) for c, v in zip(_cols, r)] for r in _rows]
                        log_line(f"[{now_ts()}] LADDER_MODULE DIAG my-stats values for {_qargs}: {_rows}", log_fp=log_fp)
                except Exception as _e:
                    log_line(f"[{now_ts()}] LADDER_MODULE diag error: {_e!r}", log_fp=log_fp)
                if not _rows and _qtype in ("1", "2"):
                    _rows = [_row_for(str(clt.username or ""))]
                res = gsm.GSMResponse(msg)
                res.header.property = gsm.PROPERTY.GS
                res.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
                res.dl = List(["1281", _items[1], ["38", ["1", [_req_id, str(_total), [[c, c] for c in _cols], _rows]]]])
                log_line(f"[{now_ts()}] LADDER_MODULE ladderquery id={_req_id} type={_qtype} args={_qargs} sort={_sortspec} -> total={_total} rows={len(_rows)} first={_rows[:1]}", log_fp=log_fp)
                return res
            except Exception as _e:
                log_line(f"[{now_ts()}] LADDER_MODULE ladder reply error: {_e!r}", log_fp=log_fp)
        if len(_items) >= 3:
            _code = str(_items[0])
            _fail = "0" if _code != "1025" else "1"
            res = gsm.GSMResponse(msg)
            res.header.property = gsm.PROPERTY.GS
            res.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
            res.dl = List([_code, _items[1], ["39", [_fail]]])
            log_line(f"[{now_ts()}] LADDER_MODULE code={_code} -> failure reply ['39',['{_fail}']]", log_fp=log_fp)
            return res
        res = gsm.GSMResponse(msg)
        res.header.property = gsm.PROPERTY.GS
        res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
        res.dl = List([])
        return res

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

        try:
            _first = msg.dl.lst[0] if (msg.dl is not None and msg.dl.lst) else None
            if isinstance(_first, (list, tuple)) or hasattr(_first, 'lst'):
                # [[3|4, id]] = the game's notice that a proxy module logged in (3) or failed (4): no reply.
                log_line(f"[{now_ts()}] ROUTER_WM PROXY_HANDLER module notice {req_subtype}: no reply sent", log_fp=log_fp)
                return None
        except Exception:
            pass

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
            f"[{now_ts()}] ROUTER_WM LOBBY_MSG subtype={lobby_subtype} from={getattr(clt, 'username', None)}@{clt.addr[0]}:{clt.addr[1]}",
            log_fp=log_fp,
        )

        if lobby_subtype == 21:
            # LOBBY_MSG.LOGIN — respond with GSSUCCESS, then push lobby list
            # CRITICAL: deepcopy header BEFORE any GSMResponse touches it,
            # because GSMResponse.__init__ mutates req.header in-place.
            orig_hdr = copy.deepcopy(msg.header)

            def _make_lobby_push_header():
                """Create a fresh response header (swapped S/R) from the original."""
                h = copy.deepcopy(orig_hdr)
                h.sender, h.receiver = h.receiver, h.sender
                return h

            login_res = gsm.LobbyMsgResponse(msg)
            # Replace the mutated shared header with a clean copy
            login_res.header = _make_lobby_push_header()
            login_res.header.property = gsm.PROPERTY.GS
            login_res.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG

            # Build GROUP_INFO push with lobby + any existing rooms
            from group import Lobby, Room as RoomCls
            lobby1 = Lobby(id=1, name="www.splintercellonline.net", master="", game_mode=0)
            lobby1.max_members = 16
            # r38: the lobby list is only sent at login, before this user's JOIN_LOBBY registers them; count them too
            try:
                _me = getattr(clt, 'username', None)
                with _lobby_state.lock:
                    _us = {(inf.get('username') or k) for k, inf in _lobby_state.sessions.items() if inf.get('source') in ('JOIN_LOBBY', 'LOBBYSERVERLOGIN')}
                if _me: _us.add(_me)
                lobby1.nb_members = len(_us)
            except Exception:
                lobby1.nb_members = lobby_player_count()
            _lc = os.environ.get("SCCT_LOBBY_CONFIG", "").strip()
            if _lc:
                try:
                    lobby1.config = int(_lc, 0)
                except ValueError:
                    log_line(f"[{now_ts()}] SCCT_LOBBY_CONFIG ignored (not a number): {_lc!r}", log_fp=log_fp)
            log_line(f"[{now_ts()}] ROUTER_WM LOGIN lobby entry: {lobby1.to_list()}", log_fp=log_fp)
            try:
                if conn is not None and getattr(clt, 'username', None):
                    _WM_CONNS[clt.username] = conn
                    _start_live_lobby_count(log_fp=log_fp)
            except Exception:
                pass

            # Check for existing rooms (file-backed cross-process)
            all_rooms = _lobby_state.get_rooms()
            if not all_rooms:
                all_rooms = _lobby_state._load_rooms_from_file()

            # --- Build lobby-only GROUP_INFO (14-field, is_rooms='0') ---
            # This is the WORKING path: the game parses and displays lobbies from this.
            group_info = gsm.GSMResponse(msg)
            group_info.header = _make_lobby_push_header()
            group_info.header.property = gsm.PROPERTY.GS
            group_info.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
            group_info.dl = List([
                str(53),  # LOBBY_MSG.GROUP_INFO
                ["1", str(0x100), ["0"], [lobby1.to_list()]]
            ])

            extras = [group_info]

            # --- Also send rooms GROUP_INFO with FULL 20-field format, is_rooms='1' ---
            # The game may have a SEPARATE parser for rooms (is_rooms='1') that expects
            # the full 20-field Room.to_list() format, not the 14-field lobby format.
            if all_rooms:
                room_gi = gsm.GSMResponse(msg)
                room_gi.header = _make_lobby_push_header()
                room_gi.header.property = gsm.PROPERTY.GS
                room_gi.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
                room_full_list = [r.to_list() for r in all_rooms]  # Full 20-field format
                room_gi.dl = List([
                    str(53),  # GROUP_INFO
                    ["1", str(0x100), ["1"], room_full_list]
                ])
                extras.append(room_gi)
                log_line(f"[{now_ts()}] ROUTER_WM LOGIN GROUP_INFO rooms payload: {room_full_list}", log_fp=log_fp)

                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY_MSG LOGIN -> GROUP_INFO with 1 lobby + "
                    f"{len(all_rooms)} room(s) (20-field format, is_rooms=1)",
                    log_fp=log_fp,
                )
            else:
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY_MSG LOGIN -> GROUP_INFO with 1 lobby (no rooms)",
                    log_fp=log_fp,
                )

            # Log extras for debugging
            for i, ex in enumerate(extras):
                ex_bytes = bytes(ex) if not isinstance(ex, (bytes, bytearray)) else ex
                b5_val = ex_bytes[5] if len(ex_bytes) > 5 else -1
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOGIN extra[{i}] len={len(ex_bytes)} b5=0x{b5_val:02x}",
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

        def _drop_stale_rooms_of(user):
            if os.environ.get("SCCT_DROP_STALE", "1").strip().lower() in ("0", "off", "false", "no") or not user:
                return
            for entry in _lobby_state.remove_rooms_by_master(user):
                r = entry["room"]
                _broadcast_removal(r.group_id, log_fp)
                log_line(f"[{now_ts()}] ROUTER_WM dropped stale room id={r.group_id} host={r.master}", log_fp=log_fp)

        if lobby_subtype == 23:
            # LOBBY_MSG.JOIN_LOBBY
            _sk23 = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
            if _sk23 in _LEFT_LOBBY:
                _LEFT_LOBBY.discard(_sk23)
                log_line(f"[{now_ts()}] ROUTER_WM JOIN_LOBBY on reused connection {_sk23} after lobby leave: dropping connection so the client reconnects", log_fp=log_fp)
                try:
                    if conn is not None:
                        threading.Timer(float(os.environ.get("SCCT_REJOIN_DROP_DELAY", "0.05")), lambda _c=conn: _c.shutdown(socket.SHUT_RDWR)).start()
                except Exception:
                    pass
                return None
            count = lobby_player_join()
            _lobby_state.clear_ctx(session_key or f"{clt.addr[0]}:{clt.addr[1]}")
            _drop_stale_rooms_of(clt.username)
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

            # Push existing rooms via GROUP_INFO (full 20-field format, is_rooms='1')
            existing_rooms = _lobby_state.get_rooms()
            if not existing_rooms:
                existing_rooms = _lobby_state._load_rooms_from_file()
            if existing_rooms:
                room_gi_frame = _build_lobby_msg_frame(
                    [str(53), ["1", str(0x100), ["1"],
                     [r.to_list() for r in existing_rooms]]],
                    b5=0x24,  # S->P for lobby server connection (Connection 2)
                )
                extras.append(room_gi_frame)
                log_line(
                    f"[{now_ts()}] ROUTER_WM JOIN_LOBBY GROUP_INFO rooms payload: "
                    f"{[r.to_list() for r in existing_rooms]}",
                    log_fp=log_fp,
                )
                log_line(
                    f"[{now_ts()}] ROUTER_WM pushing GROUP_INFO with {len(existing_rooms)} room(s) "
                    f"(20-field format, is_rooms=1) to {username}",
                    log_fp=log_fp,
                )

            if extras:
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY pushing {len(extras)} extras (members+rooms) to {username}",
                    log_fp=log_fp,
                )
                return (resp, extras)
            return resp

        if lobby_subtype == 106 and os.environ.get("SCCT_PLAYER_GROUP_GET", "1").strip().lower() not in ("0", "off", "false", "no"):
            # PLAYER_GROUP_GET: "Join Friend" asks which group (room) a player is in.
            # Reply payload (decoded from the game): [name, [id_a, id_b]] with the room id and its parent lobby id.
            try:
                _raw106 = msg.dl.lst[1]
                _items106 = list(_raw106.lst if hasattr(_raw106, "lst") else _raw106)
            except Exception:
                _items106 = []
            _who = str(_items106[0]) if _items106 else ""
            log_line(f"[{now_ts()}] ROUTER_WM PLAYER_GROUP_GET from {clt.username!r} for {_who!r} raw={_items106!r}", log_fp=log_fp)
            _hit = None
            for _r in _lobby_state.get_rooms():
                if str(_r.master).lower() == _who.lower():
                    _hit = _r
                    break
            if _hit is None:
                log_line(f"[{now_ts()}] ROUTER_WM PLAYER_GROUP_GET: {_who!r} is not hosting a room -> fail", log_fp=log_fp)
                return _lobby_fail(msg, 106, 1, _who)
            # Round 46: decoded from the game: the reply is a LIST of groups, one [key, id] pair each (the game calls its
            # handler once per pair and then once more with 0,0). The pair whose id matches a lobby the game already
            # knows is taken as the lobby; the other one becomes the room to join. Both are needed before it joins.
            _parent = str(getattr(_hit, "parent_id", 1))
            _lobby_key = os.environ.get("SCCT_PGG_LOBBY_KEY", "1")
            # Round 49: the extra pair of r47 made the game start the join too early (it joins as soon as one pair is a known
            # lobby and a second one is not), so the default is back to exactly two pairs: [lobby], [room]. SCCT_PGG_MODE=multi restores r47.
            # Round 47: the game's known-lobby list stores (group id, second id) and a lobby pair must match it exactly;
            # the second id is not certain (lobby_sv_id=1 or parent=0), so send both lobby candidates, room pair last.
            _entries = [[_lobby_key, _parent], ["0", _parent], [_parent, str(_hit.group_id)]]
            if os.environ.get("SCCT_PGG_MODE", "single").strip().lower() in ("single", "cycle"):
                _entries = [[_lobby_key, _parent], [_parent, str(_hit.group_id)]]
            if os.environ.get("SCCT_PGG_MODE", "single").strip().lower() == "cycle":
                # Round 50: both attempts failed with the same two pairs, and it is not certain which of the two numbers
                # in a pair the game reads first. Alternate: odd requests send [id, parent]-ordered pairs for the room.
                global _PGG_COUNT
                _PGG_COUNT = globals().get("_PGG_COUNT", 0) + 1
                if _PGG_COUNT % 2 == 0:
                    _entries = [[_lobby_key, _parent], [str(_hit.group_id), _parent]]
                log_line(f"[{now_ts()}] ROUTER_WM PLAYER_GROUP_GET variant {'B (room pair swapped)' if _PGG_COUNT % 2 == 0 else 'A (normal)'} request #{_PGG_COUNT}", log_fp=log_fp)
            log_line(f"[{now_ts()}] ROUTER_WM PLAYER_GROUP_GET: {_who!r} -> room {_hit.group_id} reply {_entries}", log_fp=log_fp)
            # Round 51: the game's PlayerGroupGet handler never calls the result decoder the other replies use, so it reads the
            # message as ['106', [name, pair, pair...]] directly. The typed ['38', ['106', ...]] reply made it read '106' as
            # the player name and the whole payload as a pair, which is why every attempt (r46-r50) failed.
            if os.environ.get("SCCT_PGG_WRAP", "raw").strip().lower() == "typed":
                return _lobby_reply(msg, 106, [_who] + _entries)
            return _lobby_raw_reply(msg, 106, [_who] + _entries)

        if lobby_subtype in (36, 40) and os.environ.get("SCCT_BAN_REPLY", "none").strip().lower() not in ("off", "false", "no", "old"):
            # PLAYER_BAN / PLAYER_UNBAN: the game sends PLAYER_UNBAN [room, player] with every invite. The generic
            # untyped reply used before is a stray reply that can upset the client's reply queue; answer with a typed
            # reply that echoes the request instead.
            try:
                _plb = _req_payload_strs(msg)
            except Exception:
                _plb = []
            if os.environ.get("SCCT_BAN_REPLY", "none").strip().lower() == "typed":
                log_line(f"[{now_ts()}] ROUTER_WM LOBBY_MSG {lobby_subtype} (ban/unban) from {clt.username!r}: {_plb} -> typed echo reply", log_fp=log_fp)
                return _lobby_reply(msg, lobby_subtype, list(_plb[:2]) if _plb else ["0"])
            # Round 45: default is NO reply (like 34/35/44/70). Every reply the client doesn't consume stays in its
            # queue and makes a later JOIN_LOBBY answer "Couldn't access lobby" (seen after invites in r43).
            log_line(f"[{now_ts()}] ROUTER_WM LOBBY_MSG {lobby_subtype} (ban/unban) from {clt.username!r}: {_plb} -> notification, no reply sent", log_fp=log_fp)
            return None

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

            if os.environ.get("SCCT_ACK42", "").strip().lower() in {"1", "ok", "true", "on"}:
                # proper S->P reply: LOBBY_MSG ['38', ['42']] (single header swap)
                ack = gsm.GSMResponse(msg)
                ack.header.property = gsm.PROPERTY.GS
                ack.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
                ack.dl = List([str(gsm.MESSAGE_TYPE.GSSUCCESS.value), [str(42)]])
                log_line(f"[{now_ts()}] ROUTER_WM SET_PLAYER_INFO ack=proper S->P", log_fp=log_fp)
            else:
              try:
                ack = gsm.LobbyMsgResponse(msg)
              except Exception:
                ack = gsm.GSMResponse(msg)
                ack.header.property = gsm.PROPERTY.GS
                ack.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                ack.dl = List([])

            # SCCT_PUSH_ROOMS_AFTER_42=1: the client sends SET_PLAYER_INFO last in its join
            # sequence, so rooms pushed earlier (inside the JOIN_LOBBY reply) may arrive before
            # its lobby UI can take them. Push the room list again now.
            if os.environ.get("SCCT_PUSH_ROOMS_AFTER_42", "").strip().lower() in {"1", "true", "yes", "on"}:
                late_rooms = _lobby_state.get_rooms()
                if late_rooms:
                    late_frame = _build_lobby_msg_frame(
                        [str(53), ["1", str(0x100), ["1"], [r.to_list() for r in late_rooms]]],
                        b5=0x24,
                    )
                    log_line(
                        f"[{now_ts()}] ROUTER_WM pushing {len(late_rooms)} room(s) to {username} after SET_PLAYER_INFO",
                        log_fp=log_fp,
                    )
                    frames = [late_frame]
                    # SCCT_SHOTGUN=1: also send alternative formats of the same data, each logged,
                    # to learn which (if any) makes the Games List / player list populate.
                    if os.environ.get("SCCT_SHOTGUN", "").strip().lower() in {"1", "true", "yes", "on"}:
                        rl = [r.to_list() for r in late_rooms]
                        variants = [
                            ("A GROUP_INFO flag0 is_rooms=0", [str(53), ["1", "0", ["0"], rl]]),
                            ("B GROUP_INFO flag0x1C0 is_rooms=1", [str(53), ["1", str(0x1C0), ["1"], rl]]),
                            ("C NEW_GROUP with parent prefix", [str(54), ["1"] + [rl[0]]]),
                        ]
                        try:
                            names = _lobby_state.get_session_usernames(exclude_key=sk) + [username]
                            variants.append(("D MEMBER_LIST(151) names", [str(151), ["1", list(names)]]))
                        except Exception as e:
                            log_line(f"[{now_ts()}] ROUTER_WM SHOTGUN D skipped: {e}", log_fp=log_fp)
                        for label, dl in variants:
                            try:
                                frames.append(_build_lobby_msg_frame(dl, b5=0x24))
                                log_line(f"[{now_ts()}] ROUTER_WM SHOTGUN sent {label} to {username}", log_fp=log_fp)
                            except Exception as e:
                                log_line(f"[{now_ts()}] ROUTER_WM SHOTGUN {label} build error: {e}", log_fp=log_fp)
                    return (ack, frames)
            return ack

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

            # Map CREATE_ROOM fields. Observed Chaos Theory PS2 payload (11 fields):
            #   [0] parent lobby id     '1'
            #   [1] host / room name    'ABC'
            #   [2] game title          'SPLINTERCELL3PS2US'
            #   [3] room_type           '5'   (5 = ROOM_UBI_CLIENTHOST in ubi-gs)
            #   [4] max_players         '4'   (2v2)
            #   [5] max_visitors        '0'
            #   [6] group_info blob     96 bytes, echoed back verbatim (never edited)
            #   [7] password            ''
            #   [8] numeric id          '1534654573' -> event_id
            #   [9] game_version        '1.0'
            #   [10] alt group info     b''
            def _sd_int(idx, default):
                try:
                    return int(sub_data[idx])
                except Exception:
                    return default

            def _sd_str(idx, default=""):
                try:
                    v = sub_data[idx]
                    return v if isinstance(v, str) else default
                except Exception:
                    return default

            def _sd_bin(idx):
                try:
                    v = sub_data[idx]
                    return bytes(v) if isinstance(v, (bytes, bytearray)) else b""
                except Exception:
                    return b""

            parent_id = _sd_int(0, 1)
            username = _sd_str(1) or username
            allowed_games = _sd_str(2)
            room_type = _sd_int(3, 1)
            max_players = _sd_int(4, 8)
            max_visitors = _sd_int(5, 0)
            info_blob = _sd_bin(6)
            room_password = _sd_str(7)
            event_id = _sd_int(8, 0)
            game_version = _sd_str(9)
            alt_info_blob = _sd_bin(10)

            blob_name = _room_name_from_blob(info_blob)
            if os.environ.get("SCCT_ROOM_NAME_FROM", "host").strip().lower() == "blob" and blob_name:
                room_name = blob_name
            else:
                room_name = username

            # Build Room object
            _drop_stale_rooms_of(username)
            rid = _lobby_state.next_room_id()
            _lobby_state.set_ctx(session_key or f"{clt.addr[0]}:{clt.addr[1]}", rid)
            room = Room(id=rid, name=room_name, master=username, game_mode=event_id)
            room.group_type = room_type
            room.parent_id = parent_id
            room.config = _default_room_config(room_password)
            room.allowed_games = allowed_games
            # Client-side filter (SC3_ON.ELF 0x387950) checks room field 9 ('games') against the
            # game-name whitelist it set at login; an empty 'games' makes it silently drop the room.
            if os.environ.get("SCCT_GAMES_FIELD", "1").strip().lower() not in ("0", "off", "false", "no"):
                room.games = allowed_games
            room.info = info_blob
            room.max_players = max_players
            room.max_visitors = max_visitors
            room.nb_players = 1
            # SC3_ON.ELF room observer (0x361310) does strcmp(room.game_version, <client build id>)
            # and silently drops the room on mismatch. The client's build id is the numeric string it
            # sends in CREATE_ROOM element [8]; element [9] ('1.0') is the gs_version. (Replay of the
            # game's own handler on a save state confirmed this.)  SCCT_GV_FROM=legacy restores old mapping.
            if os.environ.get("SCCT_GV_FROM", "request").strip().lower() == "legacy":
                room.game_version = game_version
            else:
                room.game_version = _sd_str(8) or game_version
                room.gs_version = game_version
            if os.environ.get("SCCT_GV_FORCE", "").strip():
                room.game_version = os.environ["SCCT_GV_FORCE"].strip()
            room.room_password = room_password   # not serialized by Room.to_list()
            room.alt_info = alt_info_blob        # not serialized by Room.to_list()
            # Set host IP from the creator's connection address
            creator_ip = clt.addr[0] if hasattr(clt, 'addr') and clt.addr else ""
            room.ip_addr = creator_ip
            room.alt_ip_addr = creator_ip
            _relay_room_at_create(room, creator_ip, log_fp=log_fp)
            _apply_room_overrides(room, log_fp=log_fp)

            sk = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
            # Store raw CREATE_ROOM frame and original sub_data for broadcasting
            relay_frame = None
            raw_frame = frame_bytes or b""
            if raw_frame and len(raw_frame) >= 6:
                relay_frame = bytearray(raw_frame)
                relay_frame[5] = 0x24  # S->P
                relay_frame = bytes(relay_frame)
            _lobby_state.create_room(
                room=room, owner_key=sk, raw_frame=relay_frame,
                create_sub_data=sub_data,  # Store original CREATE_ROOM fields
            )

            log_line(
                f"[{now_ts()}] ROUTER_WM LOBBY_MSG CREATE_ROOM host={username!r} map={blob_name!r} "
                f"group_name={room.group_name!r} room_id={rid} parent={room.parent_id} type={room.group_type} "
                f"max={room.max_players} visitors={room.max_visitors} event_id={room.event_id} "
                f"config=0x{int(room.config):x} info_len={len(info_blob)}",
                log_fp=log_fp,
            )
            log_line(f"[{now_ts()}] ROUTER_WM CREATE_ROOM room.to_list()={room.to_list()}", log_fp=log_fp)

            # --- Broadcast room via GROUP_INFO (14-field format) ---
            # Send a full GROUP_INFO with all current rooms to other players.
            # This uses the same format that works for lobbies.
            all_current_rooms = _lobby_state.get_rooms()
            if all_current_rooms:
                room_gi_broadcast = _build_lobby_msg_frame(
                    [str(53), ["1", str(0x100), ["1"],
                     [r.to_list() for r in all_current_rooms]]],
                    b5=0x24,  # S->P for lobby server connection (Connection 2)
                )
                log_line(
                    f"[{now_ts()}] ROUTER_WM CREATE_ROOM -> GROUP_INFO broadcast "
                    f"with {len(all_current_rooms)} room(s) (20-field format, is_rooms=1) "
                    f"len={len(room_gi_broadcast)}",
                    log_fp=log_fp,
                )
                log_line(
                    f"[{now_ts()}] ROUTER_WM CREATE_ROOM GROUP_INFO payload: "
                    f"{[r.to_list() for r in all_current_rooms]}",
                    log_fp=log_fp,
                )
                def _gi_for(ctx, _rooms=all_current_rooms):
                    rl = []
                    for _r in _rooms:
                        _l = list(_r.to_list())
                        if ctx:
                            _l[4] = str(ctx)
                        rl.append(_l)
                    return _build_lobby_msg_frame([str(53), ["1", str(0x100), ["1"], rl]], b5=0x24)
                _lobby_state.send_rooms_per_session(_gi_for, exclude_key=sk, log_fp=log_fp)

            # Also send NEW_GROUP(54). SCCT_NEW_GROUP: request (old default) | room | off
            ng_mode = os.environ.get("SCCT_NEW_GROUP", "request").strip().lower()
            if ng_mode == "off":
                log_line(f"[{now_ts()}] ROUTER_WM CREATE_ROOM -> NEW_GROUP(54) skipped (SCCT_NEW_GROUP=off)", log_fp=log_fp)
            else:
                ng_payload = room.to_list() if ng_mode == "room" else sub_data
                new_group_frame = _build_lobby_msg_frame(
                    [str(54), ng_payload],
                    b5=0x24,
                )
                log_line(
                    f"[{now_ts()}] ROUTER_WM CREATE_ROOM -> NEW_GROUP(54) broadcast mode={ng_mode} "
                    f"len={len(new_group_frame)} payload={ng_payload}",
                    log_fp=log_fp,
                )
                def _ng_for(ctx, _pl=ng_payload):
                    _p = list(_pl) if isinstance(_pl, list) else _pl
                    if ctx and isinstance(_p, list) and len(_p) > 4 and ng_mode == "room":
                        _p[4] = str(ctx)
                    return _build_lobby_msg_frame([str(54), _p], b5=0x24)
                _lobby_state.send_rooms_per_session(_ng_for, exclude_key=sk, log_fp=log_fp)
                # Round 24: the creator was excluded from NEW_GROUP, so its own room list never contained the room.
                # With the full ack it then auto-joins (24) a room it doesn't know, fails, and sends GROUP_LEAVE
                # 33 ms later ("Game creation failed"). Send the creator the room too, before the ack.
                if os.environ.get("SCCT_NEW_GROUP_TO_CREATOR", "").strip().lower() == "early":
                    try:
                        _sc = (_lobby_state.sessions.get(sk) or {}).get("conn")
                        if _sc is not None:
                            _sc.sendall(_ng_for(None))
                            log_line(f"[{now_ts()}] ROUTER_WM CREATE_ROOM -> NEW_GROUP(54) also sent to creator {sk}", log_fp=log_fp)
                        else:
                            log_line(f"[{now_ts()}] ROUTER_WM CREATE_ROOM creator conn not found for {sk}", log_fp=log_fp)
                    except Exception as _ec:
                        log_line(f"[{now_ts()}] ROUTER_WM CREATE_ROOM NEW_GROUP to creator failed: {_ec}", log_fp=log_fp)

            # TEST ONLY: SCCT_TEST_KILL_ROOM_AFTER=<seconds> removes every new room that many seconds after it is
            # created and broadcasts the removal (GROUP_REMOVE) -- lets us see whether clients drop a room they
            # never joined, without depending on what the host console sends.
            _kill_after = os.environ.get("SCCT_TEST_KILL_ROOM_AFTER", "").strip()
            if _kill_after:
                try:
                    _kill_s = float(_kill_after)
                    _kill_rid = str(rid)
                    def _kill_room(_rid=_kill_rid):
                        try:
                            for _r in list(_lobby_state.get_rooms()):
                                if str(_r.group_id) == _rid:
                                    _lobby_state.remove_rooms_by_master(_r.master)
                            log_line(f"[{now_ts()}] ROUTER_WM TEST_KILL_ROOM removing room {_rid}", log_fp=log_fp)
                            _broadcast_removal(_rid, log_fp)
                        except Exception:
                            log_line(f"[{now_ts()}] ROUTER_WM TEST_KILL_ROOM failed: {traceback.format_exc()}", log_fp=log_fp)
                    _kt = threading.Timer(_kill_s, _kill_room)
                    _kt.daemon = True
                    _kt.start()
                    log_line(f"[{now_ts()}] ROUTER_WM TEST_KILL_ROOM armed: room {_kill_rid} in {_kill_s}s", log_fp=log_fp)
                except Exception:
                    pass

            # SCCT_CREATE_ACK=roomid: reply S->P in the same shape JOIN_LOBBY uses
            # (LOBBY_MSG, ['38', ['12', [room_id]]]). Default keeps the old reply.
            _ca = os.environ.get("SCCT_CREATE_ACK", "legacy").strip().lower()
            if os.environ.get("SCCT_CREATE_FULL", "").strip().lower() in ("1", "on", "true", "yes"):
                _ca = "full"
            if _ca in ("roomid", "full"):
                ack = gsm.GSMResponse(msg)  # single header swap -> S->P
                ack.header.property = gsm.PROPERTY.GS
                ack.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
                if _ca == "full":
                    # Round 22: the game's CREATE_ROOM reply parser needs [roomId, roomName, lobbyId]; with only
                    # [roomId] the parse fails, the host never "joins" its own room and never sends GROUP_LEAVE.
                    ack.dl = List([str(gsm.MESSAGE_TYPE.GSSUCCESS.value), [str(12), [str(rid), str(room.group_name), str(room.parent_id or 1)]]])
                else:
                    ack.dl = List([str(gsm.MESSAGE_TYPE.GSSUCCESS.value), [str(12), [str(rid)]]])
                log_line(f"[{now_ts()}] ROUTER_WM CREATE_ROOM ack=roomid room_id={rid}", log_fp=log_fp)
                return ack

            # Respond with GSSUCCESS to the creator
            try:
                return gsm.LobbyMsgResponse(msg)
            except Exception:
                res = gsm.GSMResponse(msg)
                res.header.property = gsm.PROPERTY.GS
                res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                res.dl = List([])
                return res

        if lobby_subtype == 24:
            # LOBBY_MSG.JOIN_ROOM request: ['24', [room_id, password, flags, '0', gs_version]].
            # Client-side reply parser (SC3_ON.ELF 0x37c1e0) expects, on success (code 38):
            #   ['38', ['24', [<int>, <string<=128>]]]   (same envelope as the CREATE_ROOM ack)
            # SCCT_JOIN_REPLY: ip (default) | name | empty | off (leave unhandled, old behaviour)
            mode = os.environ.get("SCCT_JOIN_REPLY", "ip").strip().lower()
            try:
                sd = msg.dl.lst[1]
                sub = list(sd.lst) if hasattr(sd, "lst") else list(sd)
            except Exception:
                sub = []
            try:
                want = int(str(sub[0])) if sub else -1
            except Exception:
                want = -1
            target = None
            try:
                rooms_now = _lobby_state.get_rooms() or _lobby_state._load_rooms_from_file()
                for r in rooms_now:
                    if int(r.group_id) == want:
                        target = r
                        break
            except Exception as e:
                log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM lookup error: {e}", log_fp=log_fp)
            log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM request={sub} room_found={target is not None} mode={mode}", log_fp=log_fp)
            if target is None and want >= 0 and os.environ.get("SCCT_JOIN_FAIL_REPLY", "1").strip().lower() not in ("0", "off", "false", "no"):
                # Round 33: proper refusal (GROUP_NOT_EXIST) instead of the generic stray reply
                log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM room {want} not found: refusal reply [39,[24,[1,{want}]]]", log_fp=log_fp)
                return _lobby_fail(msg, 24, 1, want)
            if target is None and want >= 0 and os.environ.get("SCCT_STALE_JOIN_CLEAN", "0").strip().lower() not in ("0", "off", "false", "no"):
                # The client tried to join a room that no longer exists (it still lists it). After the generic failure
                # reply (the game then shows "The game is no longer available") tell THIS client to drop the room.
                _frames = _removal_frames(want) + [
                           _build_lobby_msg_frame([str(53), ["1", str(0x100), ["1"], [r.to_list() for r in _lobby_state.get_rooms()]]], b5=0x24)]
                def _clean_stale(_conn=conn, _fr=_frames, _w=want):
                    for _f in _fr:
                        try:
                            _conn.sendall(_f)
                        except Exception:
                            pass
                    log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM stale room {_w}: sent targeted GROUP_REMOVE + room list to {clt.username}", log_fp=log_fp)
                for _d in (0.5, 1.5, 3.0):
                    _t = threading.Timer(_d, _clean_stale)
                    _t.daemon = True
                    _t.start()
            if mode != "off" and target is not None:
                # Round 23: the host auto-joins its own room right after CREATE_ROOM (full ack). Answering it with its
                # own IP made the game abort ("Game creation failed", GROUP_LEAVE 33 ms later). SCCT_HOST_JOIN_REPLY
                # (empty | name | ip) chooses the reply for that case; default empty.
                try:
                    _sk24 = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
                    _host24 = (str(target.master).lower() == str(clt.username or "").lower()
                               or (_lobby_state.rooms.get(target.group_id) or {}).get("owner_key") == _sk24)
                except Exception:
                    _host24 = False
                _relay24 = _relay_for_joiner(str(clt.addr[0]), target, _host24)
                if _relay24:
                    _relay_notify({"op": "map", "joiner": str(clt.addr[0]), "host": _room_host_ip(target), "room": str(target.group_id), "user": str(clt.username or "")}, log_fp=log_fp)
                    log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM relay: joiner {clt.username}@{clt.addr[0]} dials {_relay24} for room {target.group_id} (host {_room_host_ip(target)})", log_fp=log_fp)
                if _host24 and os.environ.get("SCCT_NEW_GROUP_TO_CREATOR", "").strip().lower() in ("1", "on", "true", "yes", "late"):
                    # Round 25: the client only sets its current room from NEW_GROUP(54) AFTER it has processed the
                    # CREATE_ROOM reply (it queues a pending "created room" event there). The JOIN_ROOM reply is
                    # accepted only if that room is the current room, otherwise the client sends GROUP_LEAVE.
                    # The host's 24 arrives after it processed our 12 reply, so 54 sent now arrives in the right order.
                    try:
                        _sc24 = (_lobby_state.sessions.get(_sk24) or {}).get("conn")
                        if _sc24 is not None:
                            _sc24.sendall(_build_lobby_msg_frame([str(54), target.to_list()], b5=0x24))
                            log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM host: NEW_GROUP(54) sent to creator before join ack (room {target.group_id})", log_fp=log_fp)
                        else:
                            log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM host: creator conn not found for {_sk24}", log_fp=log_fp)
                    except Exception as _e54:
                        log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM host NEW_GROUP failed: {_e54}", log_fp=log_fp)
                if (not _host24) and os.environ.get("SCCT_JOIN_GROUPINFO", "").strip().lower() in ("1", "on", "true", "yes"):
                    # Round 27: the reference server (ubi-gs-go) answers JOIN_ROOM by first sending the joiner a
                    # room-level GROUP_INFO [roomId, flags, roomData, [], [members]] ("the game takes the room from this
                    # GROUP_INFO"), then the reply. After a leave/rejoin the client's list no longer has a usable entry
                    # for the room, which is the "game is no longer available" refusal.
                    try:
                        _fl = str(sub[2]) if len(sub) > 2 else "128"
                        _gi_room = _room_list_via_relay(target, _relay24) if _relay24 else target.to_list()
                        _gi = _build_lobby_msg_frame([str(53), [str(target.group_id), _fl, _gi_room, [], []]], b5=0x24)
                        conn.sendall(_gi)
                        log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM joiner {clt.username}: room-level GROUP_INFO(53) sent before ack (room {target.group_id} flags={_fl})", log_fp=log_fp)
                    except Exception as _egi:
                        log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM GROUP_INFO to joiner failed: {_egi}", log_fp=log_fp)
                if _host24:
                    mode = os.environ.get("SCCT_HOST_JOIN_REPLY", "ip").strip().lower()
                    log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM requester is host -> reply mode={mode}", log_fp=log_fp)
                if mode == "name":
                    extra = target.group_name
                elif mode == "empty":
                    extra = ""
                else:
                    extra = (_room_host_ip(target) if _host24 else target.ip_addr) or ""
                    # Hosted from a home PC: an outside joiner cannot reach a LAN-hosted room's private address, so hand
                    # it the server's public IP instead (the host's router must forward UDP 3658 and 10070-10080).
                    _pub24 = os.environ.get("SCCT_PUBLIC_IP", "").strip()
                    try:
                        if _pub24 and extra and is_private_addr(extra) and not is_private_addr(clt.addr[0]):
                            log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM outside joiner {clt.addr[0]}: host address {extra} -> public {_pub24}", log_fp=log_fp)
                            extra = _pub24
                    except Exception:
                        pass
                    if _relay24:
                        extra = _relay24
                ack = gsm.GSMResponse(msg)
                ack.header.property = gsm.PROPERTY.GS
                ack.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
                ack.dl = List([str(gsm.MESSAGE_TYPE.GSSUCCESS.value), [str(24), [str(target.group_id), str(extra)]]])
                log_line(f"[{now_ts()}] ROUTER_WM JOIN_ROOM ack room_id={target.group_id} extra={extra!r}", log_fp=log_fp)
                _friends_on_join_room(clt.username, target, log_fp=log_fp)
                return ack

        if lobby_subtype == 34:
            # Join completed: ['34', [room_id]]. Remember the room was really joined, and that this client is inside it.
            try:
                _a34 = msg.dl.lst[1].lst[0] if hasattr(msg.dl.lst[1], "lst") else msg.dl.lst[1][0]
                _lobby_state.__dict__.setdefault("played", set()).add(str(_a34))
                _lobby_state.set_ctx(session_key or f"{clt.addr[0]}:{clt.addr[1]}", _a34)
                log_line(f"[{now_ts()}] ROUTER_WM room {_a34} joined successfully by {clt.username}; marked as played", log_fp=log_fp)
            except Exception:
                pass

        if lobby_subtype in (17, 30, 45) and os.environ.get("SCCT_RANKED_FLOW", "1").strip().lower() not in ("0", "off", "false", "no"):
            # Round 66: ranked match flow (modelled on ubi-gs-go): START_MATCH(17) -> push MATCH_STARTED(62) to the room;
            # SUBMIT_MATCH(30) stores each player's result values; MATCH_FINISH(45) -> push FINAL_MATCH_RESULTS(71).
            import threading as _thr
            def _plain(x):
                x = x.lst if hasattr(x, "lst") else x
                if isinstance(x, (list, tuple)):
                    return [_plain(i) for i in x]
                if isinstance(x, (bytes, bytearray)):
                    return bytes(x).decode("latin-1")
                return str(x)
            try:
                _pl = _req_payload_strs(msg)
                _raw_args = _plain(msg.dl.lst[1]) if len(msg.dl.lst) > 1 else []
            except Exception:
                _pl, _raw_args = [], []
            _rid = _pl[0] if _pl else "0"
            _matches = _lobby_state.__dict__.setdefault("matches", {})
            def _room_targets(_room):
                with _lobby_state.lock:
                    _sess = list(_lobby_state.sessions.items())
                _in = [(k, v) for k, v in _sess if _lobby_state.get_ctx(k) == str(_room)]
                return _in if _in else _sess
            def _push_room(_room, _sub, _payload, _delay=0.3):
                def _go():
                    _fr = _build_lobby_msg_frame([str(_sub), _payload], b5=0x24)
                    for _k, _v in _room_targets(_room):
                        try:
                            _v["conn"].sendall(_fr)
                            log_line(f"[{now_ts()}] RANKED_FLOW push {_sub} -> {_v.get('username')} ({_k}): {_payload}", log_fp=log_fp)
                        except Exception as _e:
                            log_line(f"[{now_ts()}] RANKED_FLOW push {_sub} -> {_k} FAILED: {_e}", log_fp=log_fp)
                _thr.Timer(_delay, _go).start()
            log_line(f"[{now_ts()}] RANKED_FLOW lobby msg {lobby_subtype} from {clt.username!r}: args={_raw_args}", log_fp=log_fp)
            if lobby_subtype == 17:
                _mid = _lobby_state.__dict__.get("next_match_id", 1)
                _lobby_state.__dict__["next_match_id"] = _mid + 1
                _matches[_rid] = {"id": _mid, "order": [], "values": {}, "players": [str(v.get("username")) for _k, v in _room_targets(_rid)]}
                log_line(f"[{now_ts()}] RANKED_FLOW match {_mid} started in room {_rid}, players={_matches[_rid]['players']}", log_fp=log_fp)
                # Round 72: log every room field that could carry the game mode (Extraction / Sabotage / Neutralisation)
                try:
                    for _rm in _lobby_state.get_rooms():
                        if str(getattr(_rm, "group_id", "")) == str(_rid):
                            _inf = bytes(_rm.info or b"")
                            _matches[_rid]["modeinfo"] = _inf[:16].hex()
                            log_line(f"[{now_ts()}] RANKED_FLOW MODE_INFO room={_rid} config=0x{int(_rm.config):x} start_args={_raw_args} info_head={_inf[:16].hex()} info_ascii={_inf[:48]!r}", log_fp=log_fp)
                except Exception as _e:
                    log_line(f"[{now_ts()}] RANKED_FLOW MODE_INFO failed: {_e!r}", log_fp=log_fp)
                _push_room(_rid, 62, [str(_rid), str(_mid)])
                return _lobby_reply(msg, lobby_subtype, [_rid])
            if lobby_subtype == 30:
                # [match id, ?, [player, max index, count, mask, [values]], ...]
                _m = next((m for m in _matches.values() if str(m["id"]) == _rid), None)
                if _m is not None:
                    for _e in _raw_args[2:]:
                        if isinstance(_e, list) and len(_e) >= 5:
                            _nm = str(_e[0])
                            if _nm not in _m["values"]:
                                _m["order"].append(_nm)
                            _m["values"][_nm] = _e[4]
                    log_line(f"[{now_ts()}] RANKED_FLOW match {_m['id']} results stored: {_m['values']}", log_fp=log_fp)
                else:
                    log_line(f"[{now_ts()}] RANKED_FLOW SUBMIT_MATCH for unknown match {_rid}", log_fp=log_fp)
                return _lobby_reply(msg, lobby_subtype, [_rid])
            if lobby_subtype == 45:
                _m = _matches.pop(_rid, None)
                if _m is not None:
                    if _m["order"]:
                        # The game's results parser reads 14+ ints per player (scoGLClient::LobbyRcv_FinalMatchResults reads
                        # arr[0],arr[1],arr[6..8],arr[12],arr[13]); the 4 submitted values left it reading past the array. Pad to 22.
                        # SCCT_RANKED_DIAG=1 (default for now): put recognisable numbers in the unknown slots so the Game
                        # Statistics screen shows which slot feeds which column (spies use arr[6]/arr[7], mercenaries arr[12]/arr[13]).
                        _diag = os.environ.get("SCCT_RANKED_DIAG", "0").strip().lower() in ("1", "on", "true", "yes")
                        _WIN, _LOSS = int(os.environ.get("SCCT_RANKED_WIN", "50")), int(os.environ.get("SCCT_RANKED_LOSS", "25"))
                        _maxl = max(1, int(os.environ.get("SCCT_LADDER_MAX_LEVEL", "15")))
                        _rows = []; _plan = []
                        try:
                            import json as _json
                            _spath = pathlib.Path(__file__).resolve().parent.parent / "state" / "ladder_stats.json"
                            _sdb = _json.loads(_spath.read_text(encoding="utf-8-sig")) if _spath.exists() else {}
                        except Exception:
                            _spath, _sdb = None, {}
                        for _ix, _n in enumerate(_m["order"]):
                            _arr = [str(x) for x in (_m["values"][_n] if isinstance(_m["values"][_n], list) else [])][:22]
                            _arr += ["0"] * (22 - len(_arr))
                            try: _res = int(_arr[1])
                            except Exception: _res = 0
                            _delta = _WIN if _res > 0 else (-_LOSS if _res < 0 else 0)
                            _rk = "SPY" if _arr[0] == "0" else "SEN"
                            # the client's Old Total comes from the role column (SPY_/SEN_), so the floor is applied on that
                            _old = int(float((_sdb.get(_n) or {}).get(_rk + "_SCORE", 0) or 0))
                            _new = max(0, _old + _delta)
                            _plan.append((_n, _arr[0], _res, _delta, _arr[3]))
                            if _diag:
                                if _arr[0] == "0":
                                    _arr[6] = str(600 + _ix); _arr[7] = str(700 + _ix); _arr[8] = "1"
                                else:
                                    _arr[8] = "0"; _arr[12] = str(1200 + _ix); _arr[13] = str(1300 + _ix)
                            else:
                                # screen mapping found in round 67: with arr[8]!=0 the "Game Results" column shows arr[7]
                                _arr[7] = str(_new - _old); _arr[6] = str(_new); _arr[8] = "1"
                            _rows.append([_n, _arr])
                        # Round 72: which game mode was this? state/ranked_mode.txt (NEU / SAB / EXT) set by the operator; otherwise UNK
                        _MODES = {"NEU": "NEU", "NEUTRALISATION": "NEU", "NEUTRALIZATION": "NEU", "SAB": "SAB", "SABOTAGE": "SAB", "EXT": "EXT", "EXTRACTION": "EXT"}
                        # Round 78: automatic detection from the room info blob (first byte: 0x0d Extraction, 0x0e Sabotage,
                        # 0x0f Neutralisation; seen on Cinema and Station). A fixed NEU/SAB/EXT in ranked_mode.txt still overrides it;
                        # AUTO, an empty file or no file means "detect".
                        _AUTO = {0x0d: "EXT", 0x0e: "SAB", 0x0f: "NEU"}
                        try:
                            _mf = pathlib.Path(__file__).resolve().parent.parent / "state" / "ranked_mode.txt"
                            _mtxt = _mf.read_text(encoding="utf-8-sig").strip().upper() if _mf.exists() else ""
                        except Exception:
                            _mtxt = ""
                        _mkey = _MODES.get(_mtxt)
                        if _mkey is None:
                            try:
                                _mkey = _AUTO.get(int(str(_m.get("modeinfo", ""))[:2], 16), "UNK")
                            except Exception:
                                _mkey = "UNK"
                            log_line(f"[{now_ts()}] RANKED_FLOW mode auto-detected: {_mkey} (info_head={_m.get('modeinfo')!r}, file={_mtxt!r})", log_fp=log_fp)
                        _min_pl = int(os.environ.get("SCCT_RANKED_MIN_PLAYERS", "4"))
                        # Round 79: a match that ends within seconds (everyone backed out to the lobby) is not a real match.
                        _min_sec = int(os.environ.get("SCCT_RANKED_MIN_SECONDS", "30"))
                        try: _maxsec = max(int(p_[4]) for p_ in _plan) if _plan else 0
                        except Exception: _maxsec = 0
                        if _plan and _maxsec < _min_sec:
                            log_line(f"[{now_ts()}] RANKED_FLOW match {_m['id']} not recorded (too short: {_maxsec}s < {_min_sec}s)", log_fp=log_fp)
                            _plan = []
                        if len(_plan) >= _min_pl and any(p_[2] for p_ in _plan) and _spath is not None:
                            def _apply(_plan=_plan, _spath=_spath, _mid=_m["id"], _mkey=_mkey):
                                try:
                                    _d = _json.loads(_spath.read_text(encoding="utf-8-sig")) if _spath.exists() else {}
                                    for _n, _team, _res, _delta, _tm in _plan:
                                        _st = _d.setdefault(_n, {})
                                        _pre = "SPY" if _team == "0" else "SEN"
                                        try: _t = max(0, int(_tm))
                                        except Exception: _t = 0
                                        for _k in ("GEN", _pre):
                                            if _k != "GEN":
                                                _st[_k + "_SCORE"] = max(0, int(_st.get(_k + "_SCORE", 0) or 0) + _delta)
                                            _st[_k + "_GAMES"] = int(_st.get(_k + "_GAMES", 0) or 0) + 1
                                            _st[_k + "_WON"] = int(_st.get(_k + "_WON", 0) or 0) + (1 if _res > 0 else 0)
                                            _st[_k + "_TIME"] = int(_st.get(_k + "_TIME", 0) or 0) + _t
                                    for _n, _team, _res, _delta, _tm in _plan:   # separate per-mode ledger: <MODE>_SCORE/GAMES/WON/TIME
                                        _st = _d.setdefault(_n, {})
                                        try: _t = max(0, int(_tm))
                                        except Exception: _t = 0
                                        _st[_mkey + "_SCORE"] = max(0, int(_st.get(_mkey + "_SCORE", 0) or 0) + _delta)
                                        _st[_mkey + "_GAMES"] = int(_st.get(_mkey + "_GAMES", 0) or 0) + 1
                                        _st[_mkey + "_WON"] = int(_st.get(_mkey + "_WON", 0) or 0) + (1 if _res > 0 else 0)
                                        _st[_mkey + "_TIME"] = int(_st.get(_mkey + "_TIME", 0) or 0) + _t
                                    for _st in _d.values():  # overall = spy + mercenary totals
                                        _st["GEN_SCORE"] = int(_st.get("SPY_SCORE", 0) or 0) + int(_st.get("SEN_SCORE", 0) or 0)
                                    _spath.write_text(_json.dumps(_d, indent=1), encoding="utf-8")
                                    log_line(f"[{now_ts()}] RANKED_FLOW match {_mid} recorded in ladder_stats.json: {[(p_[0], p_[3]) for p_ in _plan]}", log_fp=log_fp)
                                except Exception as _e:
                                    log_line(f"[{now_ts()}] RANKED_FLOW recording failed: {_e!r}", log_fp=log_fp)
                            # Round 77: applied right away. The results screen's Old Total comes from the client's cached "my stats" (the last query it
                            # made, not the one right after the match), and the post-match query refreshes that cache for the NEXT match,
                            # so it must already return the new totals (the old 30 s delay made every Old Total one match behind).
                            _thr.Timer(float(os.environ.get("SCCT_RANKED_APPLY_DELAY", "0.1")), _apply).start()
                        else:
                            log_line(f"[{now_ts()}] RANKED_FLOW match {_m['id']} not recorded (players={len(_plan)}, min={_min_pl})", log_fp=log_fp)
                        _final = [str(_m["id"]), "38", _rows]
                    else:
                        _final = [str(_m["id"]), "39", ["0"]]
                    log_line(f"[{now_ts()}] RANKED_FLOW match {_m['id']} finished: {_final}", log_fp=log_fp)
                    _push_room(_rid, 71, _final)
                else:
                    log_line(f"[{now_ts()}] RANKED_FLOW MATCH_FINISH for room {_rid} with no running match", log_fp=log_fp)
                return _lobby_reply(msg, lobby_subtype, [_rid])

        if os.environ.get("SCCT_STRAY_FIX", "1").strip().lower() not in ("0", "off", "false", "no") and lobby_subtype in (15, 17, 31, 33, 34, 35, 44, 45, 70):
            # Round 32: the generic empty GSSUCCESS this server used to send for unhandled lobby messages is an
            # UNSOLICITED reply for 34/70 (the client expects none) and an untyped one for 15/31. The client queues
            # replies by subtype, so a stray one stays in its queue and breaks later replies (e.g. the next JOIN_LOBBY
            # answers "Couldn't Access Lobby"). 34/70: no reply. 15/31/33: typed reply [38,[subtype,[group id]]].
            try:
                _pl = _req_payload_strs(msg)
            except Exception:
                _pl = []
            if lobby_subtype in (34, 35, 44, 70):
                if lobby_subtype == 70:
                    try:
                        _a70 = _pl[0] if _pl else None
                        if _a70 is not None and str(_a70) in {str(r.group_id) for r in _lobby_state.get_rooms()}:
                            _lobby_state.__dict__.setdefault("played", set()).add(str(_a70))
                    except Exception:
                        pass
                log_line(f"[{now_ts()}] ROUTER_WM LOBBY_MSG {lobby_subtype}: notification, no reply sent", log_fp=log_fp)
                return None
            if lobby_subtype == 31 and os.environ.get("SCCT_ROOM_UPDATE", "1").strip().lower() not in ("0", "off", "false", "no"):
                # Round 34: host changed the room config (map, mode...). The new room info blob (flag 0x40) replaces the
                # stored one, and the room list is pushed to the other lobby sessions so the Games List shows it.
                try:
                    _raw = msg.dl.lst[1]
                    _items = list(_raw.lst if hasattr(_raw, "lst") else _raw)
                    _rid = str(_items[0]); _fl = int(str(_items[1]), 0)
                    _blob = next((x for x in _items[2:] if isinstance(x, (bytes, bytearray)) and len(x) >= 32), None)
                    if (_fl & 0x40) and _blob is not None:
                        _room = None
                        with _lobby_state.lock:
                            for _inf in _lobby_state.rooms.values():
                                if str(_inf["room"].group_id) == _rid:
                                    _room = _inf["room"]
                                    _room.info = bytes(_blob)
                                    if getattr(_room, "real_ip_addr", ""):
                                        _room.info = _rewrite_room_blob(_room.info, str(_room.ip_addr))  # keep the relay address after a map/settings change
                                    break
                            if _room is not None:
                                _lobby_state._save_rooms_to_file()
                        if _room is not None:
                            _sk31 = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
                            _all = _lobby_state.get_rooms()
                            _fr = _build_lobby_msg_frame([str(53), ["1", str(0x100), ["1"], [r.to_list() for r in _all]]], b5=0x24)
                            # Players already inside this room get the change from the host over their game connection. Pushing
                            # the lobby room list to them makes the client open a NEW game connection to the host (seen 2026-11-04:
                            # a second connect from another socket 2 s after each ROOM_UPDATE; through the relay the two
                            # connections collide and the joiner drops ~60 s later). SCCT_ROOM_UPDATE_TO_MEMBERS=1 restores the old push.
                            _skip31 = {_sk31}
                            if os.environ.get("SCCT_ROOM_UPDATE_TO_MEMBERS", "0").strip().lower() not in ("1", "on", "true", "yes"):
                                try:
                                    with _lobby_state.lock:
                                        _ctx31 = dict(_lobby_state.__dict__.get("ctx", {}))
                                    _skip31 |= {k for k, v in _ctx31.items() if str(v) == str(_rid)}
                                except Exception:
                                    pass
                            with _lobby_state.lock:
                                _targets31 = [(k, sess) for k, sess in _lobby_state.sessions.items() if k not in _skip31]
                            for _k31, _sess31 in _targets31:
                                try:
                                    _sess31["conn"].sendall(_fr)
                                    log_line(f"[{now_ts()}] LOBBY_BROADCAST(room_update) -> {_k31} len={len(_fr)}", log_fp=log_fp)
                                except Exception as _e31b:
                                    log_line(f"[{now_ts()}] LOBBY_BROADCAST(room_update) -> {_k31} FAILED: {_e31b}", log_fp=log_fp)
                            log_line(f"[{now_ts()}] ROUTER_WM ROOM_UPDATE room {_rid}: info replaced ({len(_blob)}B), room list pushed to {len(_targets31)} session(s), {len(_skip31) - 1} member(s) skipped", log_fp=log_fp)
                        else:
                            log_line(f"[{now_ts()}] ROUTER_WM ROOM_UPDATE room {_rid} not found", log_fp=log_fp)
                except Exception as _e31:
                    log_line(f"[{now_ts()}] ROUTER_WM ROOM_UPDATE failed: {_e31}", log_fp=log_fp)
            log_line(f"[{now_ts()}] ROUTER_WM LOBBY_MSG {lobby_subtype}: typed reply [{_pl[:1]}]", log_fp=log_fp)
            return _lobby_reply(msg, lobby_subtype, [_pl[0] if _pl else "0"])

        if lobby_subtype == 70:
            # In-match report from a joiner carries the room id; remember that this room was really played.
            try:
                _a70 = msg.dl.lst[1].lst[0] if hasattr(msg.dl.lst[1], "lst") else msg.dl.lst[1][0]
                if str(_a70) in {str(r.group_id) for r in _lobby_state.get_rooms()}:
                    _lobby_state.__dict__.setdefault("played", set()).add(str(_a70))
                    log_line(f"[{now_ts()}] ROUTER_WM room {_a70} marked as played (from {clt.username})", log_fp=log_fp)
            except Exception:
                pass

        if lobby_subtype == 8:
            # LOBBY_MSG.GROUP_LEAVE — player leaving a room/lobby
            sk = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
            if _relay_enabled() and not is_private_addr(str(clt.addr[0])):
                _relay_notify({"op": "unmap_any", "ip": str(clt.addr[0]), "why": "GROUP_LEAVE", "user": str(clt.username or "")}, log_fp=log_fp)
            _lobby_state.clear_ctx(sk)
            removed = _lobby_state.remove_room_by_owner(sk)
            for room_entry in removed:
                room = room_entry["room"]
                for remove_frame in _removal_frames(room.group_id):
                    _lobby_state.broadcast(remove_frame, log_fp=log_fp)
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY room removed (GROUP_LEAVE): id={room.group_id} name={room.group_name}",
                    log_fp=log_fp,
                )
            # Leaving a specific room: ['8', [room_id]]. Host leaving removes the room.
            try:
                leave_id = str(msg.dl.lst[1].lst[0]) if hasattr(msg.dl.lst[1], "lst") else str(msg.dl.lst[1][0])
            except Exception:
                leave_id = ""
            if leave_id:
                for r in list(_lobby_state.get_rooms()):
                    _sess_user = ""
                    try:
                        _sess_user = str((_lobby_state.sessions.get(sk) or {}).get("username") or "")
                    except Exception:
                        pass
                    _is_host = (
                        str(r.master).lower() in (str(clt.username or "").lower(), _sess_user.lower())
                        or (is_private_addr(str(clt.addr[0])) and str(getattr(r, "ip_addr", "")) == str(clt.addr[0]))
                        # (an address match only counts on a LAN: two players behind one NAT share a public IP)
                        or (_lobby_state.rooms.get(r.group_id) or {}).get("owner_key") == sk
                    )
                    log_line(f"[{now_ts()}] ROUTER_WM GROUP_LEAVE room={leave_id} master={r.master} room_ip={getattr(r,'ip_addr','')} sender={clt.username!r}/{_sess_user!r}@{clt.addr[0]} sk={sk} host_match={_is_host}", log_fp=log_fp)
                    _close_on_member = os.environ.get("SCCT_CLOSE_ON_MEMBER_LEAVE", "1").strip().lower() not in ("0", "off", "false", "no")
                    if str(r.group_id) == leave_id and (_is_host or (_close_on_member and leave_id in _lobby_state.__dict__.get("played", set()))):
                        _lobby_state.remove_rooms_by_master(r.master)
                        _broadcast_removal(r.group_id, log_fp)
                        log_line(f"[{now_ts()}] ROUTER_WM LOBBY room removed (host left): id={r.group_id}", log_fp=log_fp)
            # Round 21: tell the room's remaining members (the host) that this player left the room.
            # MEMBER_LEAVE(51) wire format in the game is [name, groupId]. Without it the host never learns the
            # joiner left, and a later rejoin by the same player is refused ("no longer available").
            if leave_id and os.environ.get("SCCT_ROOM_MEMBER_LEAVE", "").strip().lower() in ("1", "on", "true", "yes"):
                try:
                    _lv_user = str(clt.username or "")
                    try:
                        _lv_user = _lv_user or str((_lobby_state.sessions.get(sk) or {}).get("username") or "")
                    except Exception:
                        pass
                    _still = any(str(r.group_id) == leave_id for r in _lobby_state.get_rooms())
                    if _lv_user and _still:
                        _ml = _build_lobby_msg_frame([str(51), [_lv_user, leave_id]])
                        _lobby_state.broadcast(_ml, exclude_key=sk, log_fp=log_fp)
                        log_line(f"[{now_ts()}] ROUTER_WM ROOM MEMBER_LEAVE(51) sent: {_lv_user} left room {leave_id}", log_fp=log_fp)
                except Exception as _e51:
                    log_line(f"[{now_ts()}] ROUTER_WM ROOM MEMBER_LEAVE failed: {_e51}", log_fp=log_fp)
            if leave_id and leave_id in _lobby_state.__dict__.get("played", set()) \
                    and os.environ.get("SCCT_DROP_AFTER_LEAVE", "").strip():
                _lobby_state.__dict__.setdefault("recent_leavers", set()).add(sk)
            # Round 28: leaving the LOBBY (['8', ['1']]) from the Online menu. The client keeps its lobby-server
            # connection in its list and, when the lobby is selected again, sends JOIN_LOBBY(23) straight down the old
            # connection instead of JOIN_SERVER + a new connection; that 23 always fails ("Couldn't Access Lobby").
            # Close the lobby connection shortly after the leave reply so the client drops it and reconnects properly.
            if leave_id == "1" and os.environ.get("SCCT_CLOSE_ON_LOBBY_LEAVE", "").strip().lower() == "rejoin":
                # Round 30: keep the connection (client keeps its lobby list), but remember that this connection left
                # the lobby; if the client then sends JOIN_LOBBY(23) down it again, drop the connection.
                _LEFT_LOBBY.add(sk)
                log_line(f"[{now_ts()}] ROUTER_WM LOBBY LEAVE by {clt.username}: connection {sk} marked; will be dropped on next JOIN_LOBBY", log_fp=log_fp)
            if leave_id == "1" and os.environ.get("SCCT_CLOSE_ON_LOBBY_LEAVE", "").strip().lower() in ("1", "on", "true", "yes"):
                try:
                    _dl = float(os.environ.get("SCCT_CLOSE_ON_LOBBY_LEAVE_DELAY", "0.4"))
                    def _close_lobby_conn(_c=conn, _who=clt.username):
                        log_line(f"[{now_ts()}] ROUTER_WM LOBBY LEAVE: closing lobby connection of {_who}", log_fp=log_fp)
                        try:
                            _c.shutdown(socket.SHUT_RDWR)
                        except Exception:
                            pass
                    _ct = threading.Timer(_dl, _close_lobby_conn)
                    _ct.daemon = True
                    _ct.start()
                    # Round 29: the client wipes its lobby list when the lobby connection closes (screen then says
                    # "No Lobby available"). Re-push the lobby list on the Online-menu (WM) connection afterwards.
                    if os.environ.get("SCCT_REPUSH_LOBBYLIST", "1").strip().lower() not in ("0", "off", "false", "no"):
                        def _repush(_who=clt.username):
                            try:
                                _wc = _WM_CONNS.get(_who)
                                if _wc is None:
                                    log_line(f"[{now_ts()}] ROUTER_WM LOBBY LEAVE: no WM conn to re-push lobby list for {_who}", log_fp=log_fp)
                                    return
                                _wc.sendall(_lobby_list_frame())
                                log_line(f"[{now_ts()}] ROUTER_WM LOBBY LEAVE: lobby list re-pushed to {_who} on WM conn", log_fp=log_fp)
                            except Exception as _erp:
                                log_line(f"[{now_ts()}] ROUTER_WM lobby list re-push failed: {_erp}", log_fp=log_fp)
                        _rt = threading.Timer(_dl + float(os.environ.get("SCCT_REPUSH_DELAY", "0.6")), _repush)
                        _rt.daemon = True
                        _rt.start()
                    log_line(f"[{now_ts()}] ROUTER_WM LOBBY LEAVE by {clt.username}: closing its lobby connection in {_dl}s", log_fp=log_fp)
                except Exception as _ecl:
                    log_line(f"[{now_ts()}] ROUTER_WM LOBBY LEAVE close failed: {_ecl}", log_fp=log_fp)
            _r8 = os.environ.get("SCCT_R8", "flat").strip().lower()
            if _r8 not in ("off", "0", "false", "no"):
                _p8 = _req_payload_strs(msg)
                _lid8 = leave_id or (_p8[0] if _p8 else "0")
                _pl8 = [_lid8, ["1", str(0x100)]] if _r8 == "nested" else ([_lid8] if _r8 == "single" else [_lid8, "0"])
                log_line(f"[{now_ts()}] ROUTER_WM GROUP_LEAVE reply shape={_r8} payload={_pl8}", log_fp=log_fp)
                return _lobby_reply(msg, 8, _pl8)
            try:
                ack8 = gsm.LobbyMsgResponse(msg)
            except Exception:
                ack8 = gsm.GSMResponse(msg)
                ack8.header.property = gsm.PROPERTY.GS
                ack8.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                ack8.dl = List([])
            return ack8

        if lobby_subtype == 9:
            # Client returning to lobby after leaving a room: ['9', ['1', '256']].
            # Answer, then re-push the current room list so the Games List is populated.
            _r9 = os.environ.get("SCCT_R9", "flat").strip().lower()
            _p9 = _req_payload_strs(msg) or ["1", str(0x100)]
            if _r9 == "nested":
                ack9 = _lobby_reply(msg, 9, [_p9[0], list(_p9[:2])])
            elif _r9 == "echo":
                ack9 = _lobby_reply(msg, 9, list(_p9[:2]))
            elif _r9 == "flat":
                ack9 = _lobby_reply(msg, 9, [_p9[0], "0"])
            else:
                ack9 = gsm.GSMResponse(msg)
                ack9.header.property = gsm.PROPERTY.GS
                ack9.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                ack9.dl = List([])
            log_line(f"[{now_ts()}] ROUTER_WM GROUP_INFO_GET reply shape={_r9} req={_p9}", log_fp=log_fp)
            # WORKAROUND: the game keeps its own copy of a room it joined and ignores GROUP_REMOVE for it.
            # SCCT_DROP_AFTER_LEAVE=<seconds>: close this client's lobby connection that many seconds after it
            # returned to the lobby from a room it had joined, so the game rebuilds its lobby/list.
            _drop_s = os.environ.get("SCCT_DROP_AFTER_LEAVE", "").strip()
            _sk9 = session_key or f"{clt.addr[0]}:{clt.addr[1]}"
            if _drop_s and _sk9 in _lobby_state.__dict__.get("recent_leavers", set()):
                _lobby_state.__dict__["recent_leavers"].discard(_sk9)
                try:
                    _dsec = float(_drop_s)
                    def _drop_conn(_c=conn, _who=clt.username, _sk=_sk9):
                        log_line(f"[{now_ts()}] ROUTER_WM DROP_AFTER_LEAVE closing lobby connection of {_who} ({_sk})", log_fp=log_fp)
                        try:
                            _c.shutdown(socket.SHUT_RDWR)
                        except Exception:
                            pass
                        try:
                            _c.close()
                        except Exception:
                            pass
                    _dt = threading.Timer(_dsec, _drop_conn)
                    _dt.daemon = True
                    _dt.start()
                    log_line(f"[{now_ts()}] ROUTER_WM DROP_AFTER_LEAVE armed for {clt.username} in {_dsec}s", log_fp=log_fp)
                except Exception:
                    pass
            rooms9 = _lobby_state.get_rooms()
            log_line(f"[{now_ts()}] ROUTER_WM LOBBY_MSG 9 (return to lobby) pushing {len(rooms9)} room(s)", log_fp=log_fp)
            if (rooms9 or os.environ.get("SCCT_PUSH_EMPTY", "1").strip().lower() not in ("0", "off", "false", "no")) \
                    and os.environ.get("SCCT_PUSH_ON_9", "1").strip().lower() not in ("0", "off", "false", "no"):
                _f9 = os.environ.get("SCCT_9FORM", "old").strip().lower()
                if _f9 == "skip":
                    log_line(f"[{now_ts()}] ROUTER_WM GROUP_INFO_GET: nothing sent (SCCT_9FORM=skip)", log_fp=log_fp)
                    return None
                if _f9 == "ref":
                    # Round 31: reference-server shape [id, flags, data, [child rooms], [members]]
                    _gi9 = _build_lobby_msg_frame(
                        [str(53), [str(_p9[0]), str(_p9[1] if len(_p9) > 1 else 0x100), [], [r.to_list() for r in rooms9], []]], b5=0x24)
                else:
                    _gi9 = _build_lobby_msg_frame(
                        [str(53), ["1", str(0x100), ["1"], [r.to_list() for r in rooms9]]], b5=0x24)
                if _r9 == "none":
                    # Round 28: the reference server sends no reply to GROUP_INFO_GET; the GROUP_INFO is the answer.
                    conn.sendall(_gi9)
                    log_line(f"[{now_ts()}] ROUTER_WM GROUP_INFO_GET: no reply, GROUP_INFO pushed only", log_fp=log_fp)
                    return None
                return (ack9, [_gi9])
            return ack9

        if lobby_subtype == 109:
            # LOBBY_MSG subtype 109 — FIND_GAME / CHANGE_REQUESTED_LOBBIES
            # DL: ['109', ['SPLINTERCELL3PS2US']]
            # CRITICAL: This arrives on Connection 1 (P->R, b5=0x41), so responses
            # must use b5=0x14 (R->P) and be LOBBY_MSG type (not bare GSSUCCESS).
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
            if not all_rooms:
                all_rooms = _lobby_state._load_rooms_from_file()

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

            # Respond with GROUP_INFO containing rooms in 14-field lobby format.
            # CRITICAL: This arrives on Connection 1 (P->R, b5=0x41), so responses
            # must use R->P direction.
            orig_hdr_109 = copy.deepcopy(msg.header)

            def _make_109_header():
                h = copy.deepcopy(orig_hdr_109)
                h.sender, h.receiver = h.receiver, h.sender
                return h

            if existing_rooms:
                # --- Primary: GROUP_INFO(53) with full 20-field Room.to_list(), is_rooms='1' ---
                # The game's room parser likely expects the full 20 fields when is_rooms='1'.
                # This is the direct response to 109 (CHANGE_REQUESTED_LOBBIES).
                gi_resp = gsm.GSMResponse(msg)
                gi_resp.header = _make_109_header()
                gi_resp.header.property = gsm.PROPERTY.GS
                gi_resp.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
                room_list_20 = [r.to_list() for r in existing_rooms]  # Full 20-field
                gi_resp.dl = List([
                    str(53),  # GROUP_INFO
                    ["1", str(0x100), ["1"], room_list_20]
                ])

                extras = []

                # Fallback: GROUP_INFO with is_rooms='0' and 14-field format
                gi_14 = gsm.GSMResponse(msg)
                gi_14.header = _make_109_header()
                gi_14.header.property = gsm.PROPERTY.GS
                gi_14.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
                room_list_14 = [_room_as_lobby_format(r) for r in existing_rooms]
                gi_14.dl = List([
                    str(53),  # GROUP_INFO
                    ["1", str(0x100), ["0"], room_list_14]
                ])
                extras.append(gi_14)

                # GSSUCCESS ack as second fallback
                ack = gsm.GSMResponse(msg)
                ack.header = _make_109_header()
                ack.header.property = gsm.PROPERTY.GS
                ack.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
                ack.dl = List([str(gsm.MESSAGE_TYPE.GSSUCCESS.value), ["109"]])
                extras.append(ack)

                log_line(
                    f"[{now_ts()}] ROUTER_WM FIND_GAME -> GROUP_INFO with {len(existing_rooms)} room(s) "
                    f"(20-field primary is_rooms=1, 14-field fallback is_rooms=0)",
                    log_fp=log_fp,
                )
                for i, ex in enumerate([gi_resp] + extras):
                    ex_b = bytes(ex)
                    b5v = ex_b[5] if len(ex_b) > 5 else -1
                    log_line(
                        f"[{now_ts()}] ROUTER_WM FIND_GAME frame[{i}] len={len(ex_b)} b5=0x{b5v:02x}",
                        log_fp=log_fp,
                    )
                return (gi_resp, extras)

            # No rooms — just ack
            lobby_resp = gsm.GSMResponse(msg)
            lobby_resp.header = _make_109_header()
            lobby_resp.header.property = gsm.PROPERTY.GS
            lobby_resp.header.type = gsm.MESSAGE_TYPE.LOBBY_MSG
            lobby_resp.dl = List([str(gsm.MESSAGE_TYPE.GSSUCCESS.value), ["109"]])
            log_line(
                f"[{now_ts()}] ROUTER_WM FIND_GAME -> no rooms, sending GSSUCCESS ack only",
                log_fp=log_fp,
            )
            return lobby_resp

        # Handle other lobby subtypes
        log_line(
            f"[{now_ts()}] ROUTER_WM LOBBY_MSG UNHANDLED subtype={lobby_subtype} "
            f"DL={msg.dl.lst if msg.dl else None}",
            log_fp=log_fp,
        )
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

    # Round 52: 0xDE (222) is the game's RequestMOTD ("News" in the Online menu). The request carries the language ['en'];
    # the game reads GSSUCCESS [b'\xde', [ubi_motd, game_motd]] (each up to 512 chars). The text comes from state/motd.txt
    # (game message) and, optionally, state/motd_ubi.txt; the files are re-read on every request, so edits show up at once.
    if msg_type_val == 222 and os.environ.get("SCCT_MOTD", "1").strip().lower() not in ("0", "off", "false", "no"):
        try:
            _lang = str(msg.dl.lst[0]) if msg.dl is not None and len(msg.dl.lst) >= 1 else ""
        except Exception:
            _lang = ""
        _game_motd = _read_motd_file("motd.txt", _DEFAULT_MOTD, create=True)
        # The News screen shows BOTH texts one under the other (Ubisoft message first, game message second). By default only
        # the game message is sent (the Ubisoft one is empty); add state/motd_ubi.txt to fill the first block.
        _ubi_motd = _read_motd_file("motd_ubi.txt", "") or ""
        if os.environ.get("SCCT_MOTD_BOTH", "0").strip().lower() in ("1", "on", "true", "yes"):
            _ubi_motd = _ubi_motd or _game_motd
        log_line(f"[{now_ts()}] ROUTER_WM MOTD request lang={_lang!r} -> game={_game_motd[:60]!r}... ubi={_ubi_motd[:60]!r}...", log_fp=log_fp)
        res = gsm.GSMResponse(msg)
        res.header.property = gsm.PROPERTY.GS
        res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
        res.dl = List([(222).to_bytes(1, "little"), [_ubi_motd, _game_motd]])
        return res
    log_line(f"[{now_ts()}] ROUTER_WM unhandled msg type={msg_type_val} — sending empty GSSUCCESS", log_fp=log_fp)
    # Respond with a generic GSSUCCESS to avoid silent drops
    res = gsm.GSMResponse(msg)
    res.header.property = gsm.PROPERTY.GS
    res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
    res.dl = List([])
    return res


_DEFAULT_MOTD = ("Welcome to the Splinter Cell: Chaos Theory community server!\n"
                 "Edit state/motd.txt to change this message.")


def _read_motd_file(name, default, create=False):
    """Message-of-the-day text from state/<name> (re-read on every request). Max 500 characters, CRLF -> LF."""
    try:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
        path = os.path.normpath(os.path.join(base, name))
        if not os.path.exists(path):
            if create and default is not None:
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(default + "\n")
                except Exception:
                    pass
            return default
        raw = open(path, "rb").read()
        try:
            txt = raw.decode("utf-8-sig")
        except Exception:
            txt = raw.decode("latin-1")
        txt = txt.replace("\r\n", "\n").replace("\r", "\n").strip()
        return txt[:500] if txt else default
    except Exception:
        return default



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
    # Outside clients (SCCT_PUBLIC_IP set) must be told the public address, not the LAN one.
    _proxy_ip = public_ip_for(src_ip, str(args.proxy_ip))
    _ct34_host = public_ip_for(src_ip, str(args.ct34_host))
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
                        proxy_addr=(_proxy_ip, int(args.proxy_port)),
                        keyex2_mode=str(args.keyex2_mode),
                        post_ke2_push=str(args.post_ke2_push),
                        ct34_enable=bool(args.ct34_enable),
                        ct34_mode=str(args.ct34_mode),
                        ct34_base=str(args.ct34_base),
                        ct34_host=_ct34_host,
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
                    log_line(traceback.format_exc(), log_fp=log_fp)

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
        try:
            _friends_conn_closed(conn, log_fp)
        except Exception:
            pass
        if _joined_lobby:
            count = lobby_player_leave(f"{src_ip}:{src_port}")
            log_line(f"[{now_ts()}] ROUTER_WM {src_ip}:{src_port} left lobby, players_now={count}", log_fp=log_fp)
        # Clean up lobby session and broadcast room removal + member leave
        _sk = f"{src_ip}:{src_port}"
        removed_rooms = _lobby_state.unregister_session(_sk)
        # Broadcast MEMBER_LEAVE (51) for the departing player
        _left_username = getattr(_lobby_state, '_last_removed_username', None)
        if _left_username and _joined_lobby:
            member_leave_frame = _build_lobby_msg_frame(
                [str(51), [_left_username, "1"]],  # MEMBER_LEAVE: [name, groupId] (round 28: was reversed)
            )
            _lobby_state.broadcast(member_leave_frame, log_fp=log_fp)
            log_line(
                f"[{now_ts()}] ROUTER_WM MEMBER_LEAVE broadcast: {_left_username} left lobby",
                log_fp=log_fp,
            )
        if removed_rooms:
            for room_entry in removed_rooms:
                room = room_entry["room"]
                # Round 75: same treatment as a normal leave: removal repeated (0.8s / 2.5s) plus an authoritative room list,
                # so a client that is mid-screen-change does not keep a dead room after its host disconnected.
                _broadcast_removal(room.group_id, log_fp=log_fp)
                log_line(
                    f"[{now_ts()}] ROUTER_WM LOBBY room removed on disconnect: id={room.group_id} name={room.group_name}",
                    log_fp=log_fp,
                )
        try:
            conn.close()
        except Exception:
            pass
        if _relay_enabled() and removed_rooms and not is_private_addr(str(src_ip)):
            _relay_notify({"op": "unmap_host", "host": str(src_ip), "why": "disconnect"}, log_fp=log_fp)
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

    # Rooms are persisted to state/lobby_rooms.json so a second process can read them,
    # but after a restart nobody owns them any more: they show up as ghost rooms.
    if os.environ.get("SCCT_KEEP_ROOMS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        try:
            _rf = pathlib.Path(LOBBY_ROOMS_FILE)
            if _rf.exists() and _rf.read_text().strip() not in {"", "{}"}:
                _rf.write_text("{}")
                log_line(f"[{now_ts()}] ROUTER_WM cleared stale rooms from {_rf.name} (set SCCT_KEEP_ROOMS=1 to keep)", log_fp=log_fp)
        except Exception as e:
            log_line(f"[{now_ts()}] ROUTER_WM could not clear stale rooms: {e}", log_fp=log_fp)

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
            if args._boot_ke1_bytes is not None and os.environ.get('SCCT_LADDER_PREKE1','0').strip()=='1' if os.environ.get('SCCT_LADDER_MODULE','0').strip()=='1' else args._boot_ke1_bytes is not None:
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

















