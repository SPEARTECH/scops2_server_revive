#!/usr/bin/env python3
"""Ubisoft GS "Router" service (TCP).

This is the service the Chaos Theory PS2 client hits first after `/gsinit.php`.
Your captures show the initial 284-byte message is a GS `KEY_EXCHANGE` (0xDB).

We implement just enough to progress:
- KEY_EXCHANGE (req_id 1 and 2)
- LOGIN (always success)
- NEWUSERREQUEST (always success)
- JOINWAITMODULE (redirect to Router WM)
- STILLALIVE (ignored)

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

from ubigs_userdb import UserDB


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
                log_line(f"[{now_ts()}] ROUTER fixed-rsa loaded from {p}", log_fp=log_fp)
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
            log_line(f"[{now_ts()}] ROUTER fixed-rsa generated at {p}", log_fp=log_fp)
            return (pub, priv)
        finally:
            os.close(fd)

    # Final fallback.
    pair = _read_pair()
    log_line(f"[{now_ts()}] ROUTER fixed-rsa loaded (fallback) from {p}", log_fp=log_fp)
    return pair


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
    """
    Build PROXY_HANDLER(0xCC) response payload variants.

    Payload shape mirrors `gsm.ProxyHandlerResponse` and adds byte-scalar
    experiments for client parser probing.
    """
    result = "38"  # MESSAGE_TYPE.GSSUCCESS.value as string

    if mode == "a":
        module_info = [base, str(num_a), str(num_b), [[str(proxy_id), host, str(port)]]]
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


def _extract_login_fields(msg) -> tuple[str | None, str | None, str | None]:
    """Best-effort parse: returns (username, password, dp)."""
    try:
        if msg.dl is None or not getattr(msg.dl, "lst", None):
            return (None, None, None)
        lst = msg.dl.lst
        # Observed CT PS2 LOGIN payload shape:
        #   [username, password, dp, b'\x01']
        username = lst[0] if len(lst) >= 1 and isinstance(lst[0], str) else None
        password = lst[1] if len(lst) >= 2 and isinstance(lst[1], str) else None
        dp = lst[2] if len(lst) >= 3 and isinstance(lst[2], str) else None
        return (username, password, dp)
    except Exception:
        return (None, None, None)


def _extract_newuser_fields(msg) -> tuple[str | None, str | None, str | None]:
    """Best-effort parse: returns (username, password, dp)."""
    try:
        if msg.dl is None or not getattr(msg.dl, "lst", None):
            return (None, None, None)
        lst = msg.dl.lst
        # Observed CT PS2 NEWUSERREQUEST payload shape:
        #   [dp, username, password, ...]
        # (LOGIN has [username, password, dp, ...] — note different order)
        dp = lst[0] if len(lst) >= 1 and isinstance(lst[0], str) else None
        username = lst[1] if len(lst) >= 2 and isinstance(lst[1], str) else None
        password = lst[2] if len(lst) >= 3 and isinstance(lst[2], str) else None
        return (username, password, dp)
    except Exception:
        return (None, None, None)



def _build_joinwait_success(msg, *, wait_module: tuple[str, int], joinwait_format: str,
                            ct34_base: str | None = None, ct34_id: int | None = None):
    """Build GSSUCCESS(JOINWAITMODULE) response.

    FUN_003b3400 expects 3 fields in the sublist:
      field 0: session token (string, passed back in LOGINWAITMODULE request)
      field 1: module name (string, e.g. "persistantdata")
      field 2: module ID (short, e.g. proxy_id)
    When ct34_base is provided, use this 3-field format.
    """
    import gsm
    import utils
    from data import List

    res = gsm.GSMResponse(msg)
    res.header = copy.deepcopy(msg.header)
    res.header.property = gsm.PROPERTY.GS
    res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
    msg_id_str = str(gsm.MESSAGE_TYPE.JOINWAITMODULE.value)  # "93"
    result_str = str(gsm.MESSAGE_TYPE.GSSUCCESS.value)       # "38"

    ip, port = wait_module
    if ct34_base:
        # FUN_003b3000 GSSUCCESS: child[1]->field[0] = "93" → inner_type 0x5D.
        # FUN_003b3400 receives child[1] as dl_root, reads:
        #   field[0] = "93" (session token / inner type)
        #   field[1] = module_name (e.g. "persistantdata")
        #   field[2] = module_id (e.g. "1")
        res.dl = List([result_str,
                       [msg_id_str, str(ct34_base), str(ct34_id or 1)]])
    elif joinwait_format == "ack":
        res.dl = List([result_str, [msg_id_str]])
    elif joinwait_format == "u32le":
        res.dl = List([result_str, [msg_id_str, ip, utils.write_u32(port)]])
    elif joinwait_format == "str":
        res.dl = List([result_str, [msg_id_str, ip, str(port)]])
    elif joinwait_format == "u16le":
        res.dl = List([result_str, [msg_id_str, ip, utils.write_u16(port)]])
    elif joinwait_format == "u16be":
        res.dl = List([result_str, [msg_id_str, ip, utils.write_u16_be(port)]])
    elif joinwait_format == "u32be":
        res.dl = List([result_str, [msg_id_str, ip, utils.write_u32_be(port)]])
    else:
        raise ValueError(f"Unknown joinwait-format: {joinwait_format}")

    return res

def _build_boot_ke1(*, fixed_rsa: tuple) -> bytes:
    """Build a server-first KEY_EXCHANGE(1) frame from the fixed RSA public key.

    Sends the server's RSA public key to the client, which is the trigger for the
    Login flow: the game encrypts its Blowfish key with this pubkey in KE2, then
    sends encrypted LOGIN.  Must be followed by setting clt.sv_pubkey/sv_privkey.
    """
    ensure_ubigs_importable()
    import gsm
    import pkc
    from data import List

    pub_key, _priv_key = fixed_rsa
    pub_key_obj = pkc.RsaPublicKey.from_pubkey(pub_key)
    buf = bytes(pub_key_obj)

    dl = List(['1', ['1', str(len(buf)), buf]])

    # Fake a P->R received header (b5=0x41) so GSMResponse swaps to R->P (b5=0x14, server->client).
    fake_hdr_bytes = bytes([0, 0, 6, 0, gsm.MESSAGE_TYPE.KEY_EXCHANGE.value, 0x41])

    class _FakeMsg:
        pass

    fake_msg = _FakeMsg()
    fake_msg.header = gsm.GSMessageHeader(fake_hdr_bytes)
    fake_msg.dl = None

    res = gsm.GSMResponse(fake_msg)
    res.header.type = gsm.MESSAGE_TYPE.KEY_EXCHANGE
    res.dl = dl
    return bytes(res)


def _build_joinwait_from_scratch(*, wait_module: tuple[str, int], joinwait_format: str) -> object:
    """Build a GSSUCCESS(JOINWAITMODULE) boot frame without a received message.

    Uses a synthetic R->P header so that GSMResponse swaps it to P->R (b5=0x41),
    matching the direction the game expects in a server-pushed JOINWAITMODULE.
    """
    import gsm
    # Fake header: size=6, property=GS(0), type=JOINWAITMODULE(93), sender=R(1), receiver=P(4) → b5=0x14.
    # GSMResponse swaps sender/receiver → result has sender=P(4), receiver=R(1) → b5=0x41.
    fake_hdr_bytes = bytes([0, 0, 6, 0, gsm.MESSAGE_TYPE.JOINWAITMODULE.value, 0x14])

    class _FakeMsg:
        pass

    fake_msg = _FakeMsg()
    fake_msg.header = gsm.GSMessageHeader(fake_hdr_bytes)
    fake_msg.dl = None
    return _build_joinwait_success(fake_msg, wait_module=wait_module, joinwait_format=joinwait_format)

def _frame_with_b5(frame: bytes, b5: int) -> bytes:
    """Return a copy of `frame` with header byte 5 rewritten."""
    if not frame or len(frame) < 6:
        return frame
    out = bytearray(frame)
    out[5] = b5 & 0xFF
    return bytes(out)

def handle_message(
    clt: ClientState,
    msg,
    *,
    frame_bytes: bytes | None,
    wait_module: tuple[str, int],
    keyex2_mode: str,
    post_ke2_push: str,
    post_ke2_replay_frames: list[bytes] | None,
    joinwait_format: str,
    ct34_enable: bool,
    ct34_mode: str,
    ct34_base: str,
    ct34_host: str,
    ct34_port: int,
    ct34_id: int,
    ct34_num_a: int,
    ct34_num_b: int,
    userdb: UserDB | None,
    enforce_userdb: bool,
    fixed_rsa: tuple | None,
    log_fp: TextIO | None,
):
    # Late imports after sys.path tweak.
    import gsm
    import pkc
    from data import List

    res = None

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
            # Client provides its RSA public key.
            try:
                pub_buf = bytes(msg.dl.lst[1][2])
                pub = pkc.RsaPublicKey.from_buf(pub_buf)
                clt.game_pubkey = pub.to_pubkey()
                log_line(
                    f"[{now_ts()}] ROUTER KEY_EXCHANGE(1) client pubkey bits={pub.bits} n_bits={clt.game_pubkey.n.bit_length()} e={clt.game_pubkey.e}",
                    log_fp=log_fp,
                )
            except Exception:
                # If parsing fails, leave it None and still try to reply.
                clt.game_pubkey = None
                log_line(f"[{now_ts()}] ROUTER KEY_EXCHANGE(1) client pubkey parse FAILED", log_fp=log_fp)
            if fixed_rsa is not None:
                pub_key, priv_key = fixed_rsa
            else:
                pub_key, priv_key = pkc.keygen()
            clt.sv_pubkey = pub_key
            clt.sv_privkey = priv_key
            res = gsm.KeyExchangeResponse(msg, clt)
        elif req_id == "2":
            # Client provides a blowfish key encrypted to our RSA pubkey.
            if clt.sv_privkey is None:
                raise RuntimeError("KEY_EXCHANGE(2) before KEY_EXCHANGE(1)")
            try:
                enc_bf_key = bytes(msg.dl.lst[1][2])
                clt.game_bf_key = pkc.decrypt(enc_bf_key, clt.sv_privkey)
                log_line(
                    f"[{now_ts()}] ROUTER KEY_EXCHANGE(2) decrypted client BF key len={len(clt.game_bf_key)}",
                    log_fp=log_fp,
                )
            except Exception:
                clt.game_bf_key = None
                log_line(f"[{now_ts()}] ROUTER KEY_EXCHANGE(2) decrypt FAILED", log_fp=log_fp)

            # Response behavior differs across titles/flows. CT PS2 appears to be picky in some
            # paths; offer a compatibility mode that echoes the client's negotiated BF key back.
            mode = str(keyex2_mode or "random").strip().lower()
            if mode in {"echo-exact", "echo_exact"} and frame_bytes:
                # Strictest reflection mode: send the exact KEY_EXCHANGE(2) bytes back.
                clt.sv_bf_key = clt.game_bf_key
                log_line(f"[{now_ts()}] ROUTER KEY_EXCHANGE(2) mode=echo-exact", log_fp=log_fp)
                res = bytes(frame_bytes)
            elif mode in {"echo-raw", "echo_raw"}:
                # Strict reflection mode: return the exact encrypted blob the client sent.
                # Some titles appear to expect byte-for-byte reflection here.
                enc_key = bytes(msg.dl.lst[1][2])
                clt.sv_bf_key = clt.game_bf_key
                res = gsm.GSMResponse(msg)
                res.header = copy.deepcopy(msg.header)
                res.dl = List(["2", ["1", str(len(enc_key)), enc_key]])
                log_line(f"[{now_ts()}] ROUTER KEY_EXCHANGE(2) mode=echo-raw", log_fp=log_fp)
            elif mode in {"echo", "echo-client", "echo_client"} and clt.game_bf_key and clt.game_pubkey:
                # Use a single shared key for both directions.
                clt.sv_bf_key = clt.game_bf_key
                enc_key = pkc.encrypt(clt.sv_bf_key, clt.game_pubkey)
                res = gsm.GSMResponse(msg)
                res.header = copy.deepcopy(msg.header)
                res.dl = List(["2", ["1", str(len(enc_key)), enc_key]])
                log_line(f"[{now_ts()}] ROUTER KEY_EXCHANGE(2) mode=echo-client", log_fp=log_fp)
            else:
                # Default ubi-gs behavior: generate a fresh server->client key.
                res = gsm.KeyExchangeResponse(msg, clt)

            push_mode = str(post_ke2_push or "off").strip().lower()
            if push_mode not in {"off", "none", "0", "false"}:
                import utils
                extras = []

                def _typed_login_response():
                    r = gsm.GSMResponse(msg)
                    r.header = copy.deepcopy(msg.header)
                    r.header.property = gsm.PROPERTY.GS
                    r.header.type = gsm.MESSAGE_TYPE.LOGIN
                    # Keep this minimal and unwrapped: type-only success hint.
                    r.dl = List([])
                    return r

                if push_mode in {"login", "login+joinwait", "all"}:
                    extras.append(_typed_login_response())

                if push_mode in {"joinwait", "login+joinwait", "all"}:
                    r = gsm.GSMResponse(msg)
                    r.header = copy.deepcopy(msg.header)
                    r.header.property = gsm.PROPERTY.GS
                    r.header.type = gsm.MESSAGE_TYPE.JOINWAITMODULE
                    ip, port = wait_module
                    fmt = str(joinwait_format or "u32le").strip().lower()
                    if fmt == "ack":
                        r.dl = List([])
                    elif fmt == "str":
                        r.dl = List([[ip, str(port)]])
                    elif fmt == "u16le":
                        r.dl = List([[ip, utils.write_u16(port)]])
                    elif fmt == "u16be":
                        r.dl = List([[ip, utils.write_u16_be(port)]])
                    elif fmt == "u32be":
                        r.dl = List([[ip, utils.write_u32_be(port)]])
                    else:
                        r.dl = List([[ip, utils.write_u32(port)]])
                    extras.append(r)
                # Aggressive CT bootstrap: emit create-account style success markers
                # immediately after KE2 so clients that never send LOGIN/JOINWAIT can
                # still advance UI state.
                if push_mode in {"ct-force", "all"}:
                    newuser_ack = gsm.GSMResponse(msg)
                    newuser_ack.header = copy.deepcopy(msg.header)
                    newuser_ack.header.property = gsm.PROPERTY.GS
                    newuser_ack.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                    newuser_ack.dl = List([gsm.MESSAGE_TYPE.NEWUSERREQUEST.value.to_bytes(1, "little")])
                    extras.append(newuser_ack)

                    login_typed = _typed_login_response()
                    extras.append(login_typed)

                    joinwait_ack = _build_joinwait_success(
                        msg,
                        wait_module=wait_module,
                        joinwait_format=joinwait_format,
                    )
                    extras.append(joinwait_ack)

                if push_mode in {"ct-replay", "all"} and post_ke2_replay_frames:
                    extras.extend(post_ke2_replay_frames)
                log_line(
                    f"[{now_ts()}] ROUTER KEY_EXCHANGE(2) post-push mode={push_mode} count={len(extras)}",
                    log_fp=log_fp,
                )
                return (res, extras)
        else:
            log_line(f"[{now_ts()}] ROUTER unhandled KEY_EXCHANGE req_id={req_id}")
            return None

    elif msg.header.type == gsm.MESSAGE_TYPE.PROXY_HANDLER and ct34_enable:
        # Experimental response path for CT parser work.
        res = gsm.GSMResponse(msg)
        res.header = copy.deepcopy(msg.header)
        res.header.property = gsm.PROPERTY.GS
        res.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
        res.dl = List(
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
        log_line(
            f"[{now_ts()}] ROUTER PROXY_HANDLER ct34 mode={ct34_mode} base={ct34_base} id={ct34_id} host={ct34_host}:{ct34_port}",
            log_fp=log_fp,
        )

    elif msg.header.type == gsm.MESSAGE_TYPE.LOGIN:
        username, password, dp = _extract_login_fields(msg)
        if username:
            clt.username = username
        login_encrypted_unparsed = (
            msg.header.property == gsm.PROPERTY.GS_ENCRYPT and msg.dl is None
        )

        # --- Authentication check ---
        # Reload user DB from disk so we pick up changes from other processes (e.g. WM MODIFYUSER)
        if userdb is not None:
            try:
                userdb.load()
            except Exception:
                pass
        login_ok = True
        if userdb is not None and not login_encrypted_unparsed:
            if username is None:
                log_line(f"[{now_ts()}] ROUTER LOGIN REJECT: no username parsed", log_fp=log_fp)
                login_ok = False
            elif userdb.get_user(username) is None:
                log_line(f"[{now_ts()}] ROUTER LOGIN REJECT: user={username!r} not found", log_fp=log_fp)
                login_ok = False
            elif password is not None and not userdb.check_password(username=username, password=password):
                log_line(f"[{now_ts()}] ROUTER LOGIN REJECT: wrong password for user={username!r}", log_fp=log_fp)
                login_ok = False
            else:
                log_line(f"[{now_ts()}] ROUTER LOGIN OK: user={username!r}", log_fp=log_fp)

        if not login_ok:
            fail_res = gsm.GSMResponse(msg)
            fail_res.header = copy.deepcopy(msg.header)
            fail_res.header.property = gsm.PROPERTY.GS
            fail_res.header.type = gsm.MESSAGE_TYPE.GSFAIL
            fail_res.dl = List([b'\x66'])  # GSFAIL for LOGIN
            fail_res._close_after = True   # Game has no GSFAIL handler; close so it sees disconnect
            return fail_res
        # --- V5 approach: +0x149 ELF bypass means GSSUCCESS dispatch is always active.
        # Send ct34 (proxy info) + PROXY_HANDLER data + GSSUCCESS triggers in LOGIN extras
        # so the game can process them immediately (no need to wait for WM).
        # V22: Build LOGIN extras dynamically instead of hardcoded hex.
        # The old hardcoded frames had invalid DLs that the GSSUCCESS dispatcher
        # couldn't recognise, causing "unknown error" on screen.
        login_extras = []

        # --- V7: ct-bootstrap on router with b5=0x41 (matching NEWUSERREQUEST format) ---
        # V6 proved WM session is useless (game FINs without reading KE2 response).
        # "unknown error" is cosmetic — caused by NEWUSERREQUEST GSSUCCESS(0x01) being
        # dispatched through the +0x149-bypassed GSSUCCESS dispatcher (not ct-bootstrap).
        # Key insight: NEWUSERREQUEST pushes use b5=0x41 for ALL frames and the game
        # processes them. Our ct-bootstrap used b5=0x14 which may be filtered/ignored.
        # Fix: use b5=0x41 for all ct-bootstrap frames to match NEWUSERREQUEST format.
        if ct34_enable:
            # V9 FIX: Use b5=0x14 (R→P) so the game actually PROCESSES these frames.
            # V7/V8 used b5=0x41 (P→R) — game IGNORES all frames not addressed to it.
            # Game identifies as P on router connection, so receiver must be P (=4).
            # b5=0x14 means sender=R(1), receiver=P(4) → game processes.
            # NEWUSERREQUEST extras stay b5=0x41 (intentional no-ops to keep conn alive).
            def _mk_ph_14(dl):
                """PROXY_HANDLER (0xCC) with b5=0x14 (R->P, addressed to game)."""
                r = gsm.GSMResponse(msg)
                r.header = copy.deepcopy(msg.header)
                r.header.property = gsm.PROPERTY.GS
                r.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
                r.header.sender = gsm.SENDER_RECEIVER.R
                r.header.receiver = gsm.SENDER_RECEIVER.P
                r.dl = dl
                return r

            def _mk_gs_14(sub):
                """GSSUCCESS (0x26) with b5=0x14 (R->P, addressed to game)."""
                r = gsm.GSMResponse(msg)
                r.header = copy.deepcopy(msg.header)
                r.header.property = gsm.PROPERTY.GS
                r.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                r.header.sender = gsm.SENDER_RECEIVER.R
                r.header.receiver = gsm.SENDER_RECEIVER.P
                r.dl = List([sub])
                return r

            # ct34 PROXY_HANDLER (proxy info for 44001)
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
            # PROXY_HANDLER data frames (stored by game, looked up by GSSUCCESS handlers)
            # V8: Binary keys matching handler integer lookups (b'\xC8'=200, etc.)
            # V9: b5=0x14 so game actually dispatches these through 0xCC handler
            r_ph_c8 = _mk_ph_14(List([b'\xC8', ['persistantdata', '0', '0']]))
            r_ph_c9 = _mk_ph_14(List([b'\xC9', ['persistantdata', '0', '0']]))
            r_ph_lwm = _mk_ph_14(List([b'\x4D', [b'\x4D', ['1']]]))
            # GSSUCCESS triggers — b5=0x14 so game dispatches via +0x149 bypassed dispatcher
            r_gs_c8 = _mk_gs_14(b'\xC8')
            r_gs_c9 = _mk_gs_14(b'\xC9')
            r_gs_lwm = _mk_gs_14(b'\x4D')
            # LOGINFRIENDS b5=0x14 (was 0x41, game needs to process this too)
            push_loginfriends_14 = bytes.fromhex("000006004e14")

            # Order: ct34 → PROXY_HANDLER data (stored) → GSSUCCESS triggers (dispatch)
            login_extras.append(ct34_r)
            login_extras.append(r_ph_c8)
            login_extras.append(r_ph_c9)
            login_extras.append(r_ph_lwm)
            login_extras.append(r_gs_c8)
            login_extras.append(r_gs_c9)
            login_extras.append(push_loginfriends_14)
            login_extras.append(r_gs_lwm)
            log_line(f"[{now_ts()}] ROUTER LOGIN V9: appended ct34+bootstrap proxy={ct34_host}:{ct34_port} b5=0x14 BINARY keys", log_fp=log_fp)

        log_line(f"[{now_ts()}] ROUTER LOGIN V22: total extras={len(login_extras)}", log_fp=log_fp)
        # V22: Build a proper GSSUCCESS(LOGIN) primary response.
        login_res = gsm.GSMResponse(msg)
        login_res.header = copy.deepcopy(msg.header)
        login_res.header.property = gsm.PROPERTY.GS
        login_res.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
        login_res.dl = List([b'\x66'])  # 0x66 = LOGIN type → dispatcher recognises it
        return (login_res, login_extras)

    elif msg.header.type == gsm.MESSAGE_TYPE.JOINWAITMODULE:
        raise RuntimeError("JOINWAITMODULE should be handled by handle_joinwaitmodule()")

    elif msg.header.type == gsm.MESSAGE_TYPE.NEWUSERREQUEST:
        # Account creation request.
        # Reload user DB from disk so we see accounts created/deleted by other processes
        if userdb is not None:
            try:
                userdb.load()
            except Exception:
                pass
        username, password, dp = _extract_newuser_fields(msg) if userdb is not None else (None, None, None)

        # If user already exists, skip creation but let the flow proceed.
        # The game has no GSFAIL handler for NEWUSERREQUEST (it hangs).
        # Instead, let it through to LOGIN where password gets validated.
        if userdb is not None and username:
            if userdb.get_user(username) is not None:
                log_line(f"[{now_ts()}] ROUTER NEWUSERREQUEST SKIP: user={username!r} already exists, proceeding to LOGIN", log_fp=log_fp)
            elif password is not None:
                # New user — create the account
                userdb.upsert_user(username=username, password=password, dp=dp)
                try:
                    userdb.save()
                except Exception:
                    pass
                log_line(f"[{now_ts()}] ROUTER NEWUSERREQUEST OK: created user={username!r}", log_fp=log_fp)
        # Many GS APIs respond with a GS `GSSUCCESS` whose payload begins with
        # the original request's message-id as a single byte.
        res = gsm.GSMResponse(msg)
        res.header = copy.deepcopy(msg.header)
        res.header.property = gsm.PROPERTY.GS
        # NNN-run observation: game keeps router connection alive (and sends LOGIN)
        # only when the NEWUSERREQUEST response frames use type=PROXY_HANDLER (0xCC).
        # With GSSUCCESS (0x26) the game treats account-creation as complete and
        # disconnects immediately without ever sending LOGIN.
        res.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
        # NNN-run: NEWUSERREQUEST response frames all had b5=0x41 (P→R, addressed to
        # server). Game ignores server-addressed frames, so these are no-ops from the
        # game's perspective. With b5=0x14 (R→P) the game dispatches these small
        # PROXY_HANDLER frames through its ct34 handler — corrupting proxy state before
        # the real ct34 in the LOGIN response arrives.
        res.header.sender = gsm.SENDER_RECEIVER.P
        res.header.receiver = gsm.SENDER_RECEIVER.R
        msg_id = gsm.MESSAGE_TYPE.NEWUSERREQUEST.value
        res.dl = List([msg_id.to_bytes(1, "little")])
        # Hardcoded NEWUSERREQUEST extras from 17:59 run (game connected to WM).
        # These are the EXACT bytes that worked — dynamically constructed DL
        # with corrected format poisoned the game's state machine.
        extras = [
            bytes.fromhex("00000c00cc41eb8be88a8c8c"),  # push0: PROXY_HANDLER (LOGIN msg_id)
            bytes.fromhex("00002600cc41eb8bd3abafae8a8ca0a0ada28ce3a4a9a1a6d4bab4a039a1a9fde1f5b69ea7a7"),  # push1: JWM-as-PROXY_HANDLER (WM addr, old DL)
            bytes.fromhex("00000c002641eb8b8f8a8c8c"),  # push2: GSSUCCESS prelude
            bytes.fromhex("000006002641"),               # push3: bare GSSUCCESS
            bytes.fromhex("00004400cc41fab3fde1fef9a4acb9d6cae5e8939a858c90e7f5d1d98388bef1fba1f29f8a8ae3edc1f3829989c2ebeba79686cff098ff969f9bbbf099d6de8c898f9e9b"),  # push4: ct34 PROXY_HANDLER
        ]
        return (res, extras)
    elif msg.header.type == gsm.MESSAGE_TYPE.LOBBY_MSG:
        # Game sends LOBBY_MSG.LOGIN ['21', ['SPLINTERCELL3PS2US']] after
        # completing the LOGIN state machine.  Respond with LobbyMsgResponse
        # which wraps the subtype in GSSUCCESS: ["38", ["21"]].
        try:
            res = gsm.LobbyMsgResponse(msg)
            # b5=0x14 (R→P) so the game processes it.
            res.header.sender = gsm.SENDER_RECEIVER.R
            res.header.receiver = gsm.SENDER_RECEIVER.P
            log_line(
                f"[{now_ts()}] ROUTER LOBBY_MSG subtype={msg.dl.lst[0] if msg.dl else '?'} -> GSSUCCESS",
                log_fp=log_fp,
            )
            return res
        except Exception as e:
            log_line(f"[{now_ts()}] ROUTER LOBBY_MSG handler error: {e}", log_fp=log_fp)
            return None

    else:
        _tname = msg.header.type.name if hasattr(msg.header.type, 'name') else f"0x{msg.header.type:02X}"
        log_line(f"[{now_ts()}] ROUTER unhandled msg type: {_tname}")
        return None

    return res


def handle_joinwaitmodule(msg, *, wait_module: tuple[str, int], joinwait_format: str,
                          ct34_enable: bool = False, ct34_mode: str = "a",
                          ct34_base: str = "persistantdata", ct34_host: str = "192.168.0.213",
                          ct34_port: int = 44001, ct34_id: int = 1,
                          ct34_num_a: int = 0, ct34_num_b: int = 0,
                          log_fp: TextIO | None = None):
    import gsm
    import copy
    from data import List

    if msg.header.type != gsm.MESSAGE_TYPE.JOINWAITMODULE:
        raise TypeError(f"handle_joinwaitmodule called with {msg.header.type}")

    # Hardcoded JWM response from 18:01:23 run (game connected to WM).
    # Dynamic _build_joinwait_success produces different-sized output now
    # (40 bytes vs 38 bytes) which the game can't parse correctly.
    primary = bytes.fromhex("000026002641eb8bd3abafae8a8ca0a0ada28ce3a4a9a1a6d4bab4a039a1a9fde1f5b69ea7a7")
    push0 = bytes.fromhex("00000c002641eb8bd38a8c8c")

    jwm_extras = [
        push0,              # push0: bare GSSUCCESS b5=0x41
    ]

    # --- V9: ct-bootstrap on JWM with b5=0x14 (R→P, addressed to game) ---
    # V7/V8 used b5=0x41 (P→R) — game ignored ALL extras.
    # V9: b5=0x14 so game processes 0xCC (stores data) and 0x26 (dispatches handlers).
    if ct34_enable:
        def _mk_ph_14(dl):
            """PROXY_HANDLER (0xCC) with b5=0x14 (R->P, addressed to game)."""
            r = gsm.GSMResponse(msg)
            r.header = copy.deepcopy(msg.header)
            r.header.property = gsm.PROPERTY.GS
            r.header.type = gsm.MESSAGE_TYPE.PROXY_HANDLER
            r.header.sender = gsm.SENDER_RECEIVER.R
            r.header.receiver = gsm.SENDER_RECEIVER.P
            r.dl = dl
            return r

        def _mk_gs_14(sub):
            """GSSUCCESS (0x26) with b5=0x14 (R->P, addressed to game)."""
            r = gsm.GSMResponse(msg)
            r.header = copy.deepcopy(msg.header)
            r.header.property = gsm.PROPERTY.GS
            r.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
            r.header.sender = gsm.SENDER_RECEIVER.R
            r.header.receiver = gsm.SENDER_RECEIVER.P
            r.dl = List([sub])
            return r

        # ct34 PROXY_HANDLER (proxy info for 44001)
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
        # PROXY_HANDLER data frames — binary keys, b5=0x14
        r_ph_c8 = _mk_ph_14(List([b'\xC8', ['persistantdata', '0', '0']]))
        r_ph_c9 = _mk_ph_14(List([b'\xC9', ['persistantdata', '0', '0']]))
        r_ph_lwm = _mk_ph_14(List([b'\x4D', [b'\x4D', ['1']]]))
        # GSSUCCESS triggers — b5=0x14
        r_gs_c8 = _mk_gs_14(b'\xC8')
        r_gs_c9 = _mk_gs_14(b'\xC9')
        r_gs_lwm = _mk_gs_14(b'\x4D')
        # LOGINFRIENDS b5=0x14
        push_loginfriends_14 = bytes.fromhex("000006004e14")

        # Order: ct34 → PROXY_HANDLER data (stored) → GSSUCCESS triggers (dispatch)
        jwm_extras.append(ct34_r)
        jwm_extras.append(r_ph_c8)
        jwm_extras.append(r_ph_c9)
        jwm_extras.append(r_ph_lwm)
        jwm_extras.append(r_gs_c8)
        jwm_extras.append(r_gs_c9)
        jwm_extras.append(push_loginfriends_14)
        jwm_extras.append(r_gs_lwm)
        log_line(f"[{now_ts()}] ROUTER JWM V9: appended ct34+bootstrap proxy={ct34_host}:{ct34_port} b5=0x14 BINARY keys", log_fp=log_fp)

    log_line(
        f"[{now_ts()}] ROUTER JOINWAIT reply hardcoded format={joinwait_format} extras={len(jwm_extras)} (V7)",
        log_fp=log_fp,
    )
    return (primary, jwm_extras)

def client_thread(conn: socket.socket, addr: tuple[str, int], args: argparse.Namespace, *, log_fp: TextIO | None):
    ensure_ubigs_importable()
    import gsm

    clt = ClientState(addr=addr)
    conn.settimeout(float(args.idle_timeout))

    src_ip, src_port = addr
    local_ip, local_port = conn.getsockname()
    log_line(f"\n[{now_ts()}] ROUTER {src_ip}:{src_port} -> {local_ip}:{local_port} CONNECT", log_fp=log_fp)

    # Login-flow boot prelude: if the client sends nothing within login_boot_delay seconds,
    # send a server-first KE1 (RSA public key) to trigger the Login flow.
    # The real Ubisoft server sends its RSA public key first; the game then replies with
    # KE1 (its own pubkey) + KE2 (BF key encrypted with server pubkey) + LOGIN.
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
                log_line(f"[{now_ts()}] ROUTER TX[boot_ke1] len={len(boot_bytes)} sha256={sha16(boot_bytes)}", log_fp=log_fp)
                log_line(hexdump(boot_bytes, max_len=int(args.dump_max)), log_fp=log_fp)
                conn.sendall(boot_bytes)
            except Exception as e:
                log_line(f"[{now_ts()}] ROUTER boot_ke1 error: {e}", log_fp=log_fp)

    stream_buf = bytearray()
    t_conn_start = time.time()
    t_ke2_tx: float | None = None
    close_reason = "peer_eof"
    stats = {
        "rx_reads": 0,
        "frames": 0,
        "decode_err": 0,
        "keyex1": 0,
        "keyex2": 0,
        "newuser": 0,
        "login": 0,
        "joinwait": 0,
        "gs_encrypt": 0,
        "post_ke2_app_frames": 0,
    }
    last_msg_name = "none"
    fallback_postpush_sent = False
    _force_close = False

    try:
        while not _force_close:
            try:
                data = conn.recv(8192)
            except socket.timeout:
                continue
            except Exception as e:
                close_reason = f"recv_error:{type(e).__name__}"
                break
            if not data:
                close_reason = "peer_eof"
                break
            stats["rx_reads"] += 1

            rx_name = f"{file_ts()}_{safe_slug(src_ip)}_{src_port}_to_{safe_slug(local_ip)}_{local_port}_rx.bin"
            save_blob(args.save_rx_dir, rx_name, data)
            log_line(f"[{now_ts()}] ROUTER RX len={len(data)} sha256={sha16(data)}", log_fp=log_fp)
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
                    log_line(f"[{now_ts()}] ROUTER decode error: {e}", log_fp=log_fp)
                    stats["decode_err"] += 1
                    continue

                stats["frames"] += 1
                last_msg_name = msg.header.type.name if hasattr(msg.header.type, 'name') else f"0x{msg.header.type:02X}"
                if msg.header.property == gsm.PROPERTY.GS_ENCRYPT:
                    clt.last_bf_key = used_key
                    stats["gs_encrypt"] += 1

                log_line(str(msg).rstrip(), log_fp=log_fp)
                if (
                    msg.header.property == gsm.PROPERTY.GS_ENCRYPT
                    and msg.header.type == gsm.MESSAGE_TYPE.LOGIN
                    and msg.dl is None
                ):
                    # For CT: capture decrypt variants when upstream parser cannot parse LOGIN payload.
                    try:
                        import blowfish
                        from data import List

                        cands = [
                            ("last_bf", clt.last_bf_key),
                            ("game_bf", clt.game_bf_key),
                            ("sv_bf", clt.sv_bf_key),
                            ("zero", b""),
                        ]
                        seen_keys = set()
                        for name, k in cands:
                            if k is None:
                                continue
                            if k in seen_keys:
                                continue
                            seen_keys.add(k)
                            try:
                                dec = blowfish.Cipher(k).decrypt(frame[gsm.GSMSG_HEADER_SIZE : msg.header.size])
                                log_line(
                                    f"[{now_ts()}] ROUTER LOGIN forensic key={name} dec_len={len(dec)} sha256={sha16(dec)}",
                                    log_fp=log_fp,
                                )
                                log_line(hexdump(dec, max_len=96), log_fp=log_fp)
                                try:
                                    dl_outer = List.from_buf(bytearray(dec))
                                    log_line(f"[{now_ts()}] ROUTER LOGIN forensic key={name} parse outer=True -> {dl_outer}", log_fp=log_fp)
                                except Exception as pe1:
                                    log_line(f"[{now_ts()}] ROUTER LOGIN forensic key={name} parse outer=True failed: {pe1}", log_fp=log_fp)
                                try:
                                    dl_inner = List.from_buf(bytearray(dec), False)
                                    log_line(f"[{now_ts()}] ROUTER LOGIN forensic key={name} parse outer=False -> {dl_inner}", log_fp=log_fp)
                                except Exception as pe2:
                                    log_line(f"[{now_ts()}] ROUTER LOGIN forensic key={name} parse outer=False failed: {pe2}", log_fp=log_fp)
                            except Exception as de:
                                log_line(f"[{now_ts()}] ROUTER LOGIN forensic key={name} decrypt failed: {de}", log_fp=log_fp)
                    except Exception as fe:
                        log_line(f"[{now_ts()}] ROUTER LOGIN forensic unavailable: {fe}", log_fp=log_fp)
                if msg.header.type == gsm.MESSAGE_TYPE.KEY_EXCHANGE:
                    try:
                        req_id = str(msg.dl.lst[0]) if msg.dl is not None else ""
                    except Exception:
                        req_id = ""
                    if req_id == "1":
                        stats["keyex1"] += 1
                    elif req_id == "2":
                        stats["keyex2"] += 1
                elif msg.header.type == gsm.MESSAGE_TYPE.NEWUSERREQUEST:
                    stats["newuser"] += 1
                elif msg.header.type == gsm.MESSAGE_TYPE.LOGIN:
                    stats["login"] += 1
                elif msg.header.type == gsm.MESSAGE_TYPE.JOINWAITMODULE:
                    stats["joinwait"] += 1
                elif msg.header.type == gsm.MESSAGE_TYPE.LOBBY_MSG:
                    stats["lobby_msg"] = stats.get("lobby_msg", 0) + 1

                if t_ke2_tx is not None and msg.header.type not in {
                    gsm.MESSAGE_TYPE.KEY_EXCHANGE,
                    gsm.MESSAGE_TYPE.STILLALIVE,
                }:
                    stats["post_ke2_app_frames"] += 1
                # Capture incoming type before handlers may mutate msg/header.
                rx_mtype_obj = msg.header.type
                rx_mtype_name = getattr(rx_mtype_obj, "name", str(rx_mtype_obj))
                try:
                    if hasattr(rx_mtype_obj, "value"):
                        rv = rx_mtype_obj.value
                        if isinstance(rv, (bytes, bytearray)):
                            rx_mtype_val = int.from_bytes(rv, "little")
                        else:
                            rx_mtype_val = int(rv)
                    else:
                        rx_mtype_val = int(rx_mtype_obj)
                except Exception:
                    rx_mtype_val = -1

                res = None
                extras = []
                try:
                    if msg.header.type == gsm.MESSAGE_TYPE.JOINWAITMODULE:
                        handled = handle_joinwaitmodule(
                            msg,
                            wait_module=(args.wm_ip, int(args.wm_port)),
                            joinwait_format=str(args.joinwait_format),
                            ct34_enable=bool(args.ct34_enable),
                            ct34_mode=str(args.ct34_mode),
                            ct34_base=str(args.ct34_base),
                            ct34_host=str(args.ct34_host),
                            ct34_port=int(args.ct34_port),
                            ct34_id=int(args.ct34_id),
                            ct34_num_a=int(args.ct34_num_a),
                            ct34_num_b=int(args.ct34_num_b),
                            log_fp=log_fp,
                        )
                        if isinstance(handled, tuple):
                            res, extras = handled
                        else:
                            res = handled
                    else:
                        handled = handle_message(
                            clt,
                            msg,
                            frame_bytes=bytes(frame),
                            wait_module=(args.wm_ip, int(args.wm_port)),
                            keyex2_mode=str(args.keyex2_mode),
                            post_ke2_push=str(args.post_ke2_push),
                            post_ke2_replay_frames=args._post_ke2_replay_frames,
                            joinwait_format=str(args.joinwait_format),
                            ct34_enable=bool(args.ct34_enable),
                            ct34_mode=str(args.ct34_mode),
                            ct34_base=str(args.ct34_base),
                            ct34_host=str(args.ct34_host),
                            ct34_port=int(args.ct34_port),
                            ct34_id=int(args.ct34_id),
                            ct34_num_a=int(args.ct34_num_a),
                            ct34_num_b=int(args.ct34_num_b),
                            userdb=args._userdb,
                            enforce_userdb=bool(args.enforce_userdb),
                            fixed_rsa=args._fixed_rsa,
                            log_fp=log_fp,
                        )
                        if isinstance(handled, tuple):
                            res, extras = handled
                        else:
                            res = handled
                except Exception as e:
                    log_line(f"[{now_ts()}] ROUTER handler error: {e}", log_fp=log_fp)
                    continue

                if res is None:
                    continue

                def _send_response(rsp, tag: str):
                    out = bytes(rsp) if isinstance(rsp, (bytes, bytearray)) else bytes(rsp)
                    tx_name = f"{file_ts()}_{safe_slug(local_ip)}_{local_port}_to_{safe_slug(src_ip)}_{src_port}_{tag}.bin"
                    save_blob(args.save_tx_dir, tx_name, out)
                    log_line(f"[{now_ts()}] ROUTER TX[{tag}] len={len(out)} sha256={sha16(out)}", log_fp=log_fp)
                    log_line(hexdump(out, max_len=int(args.dump_max)), log_fp=log_fp)
                    conn.sendall(out)

                _send_response(res, "tx")
                # If handler flagged _close_after (e.g. GSFAIL for bad login),
                # close the connection so the game sees a disconnect instead of
                # hanging forever (game has no GSFAIL handler on router).
                if getattr(res, '_close_after', False):
                    close_reason = "auth_reject"
                    log_line(f"[{now_ts()}] ROUTER closing connection after GSFAIL", log_fp=log_fp)
                    _force_close = True
                    break  # break inner for-loop; while-loop exits via _force_close
                if msg.header.type == gsm.MESSAGE_TYPE.KEY_EXCHANGE:
                    try:
                        req_id = str(msg.dl.lst[0]) if msg.dl is not None else ""
                    except Exception:
                        req_id = ""
                    if req_id == "2":
                        t_ke2_tx = time.time()
                # Robust fallback: if this connection never reached KEY_EXCHANGE(2),
                # allow post-push profiles to trigger once when encrypted/login traffic appears.
                mtype_name = rx_mtype_name
                mtype_val = rx_mtype_val
                # 0x5D is JOINWAITMODULE in this title's router flow.
                fallback_trigger = (mtype_name == "JOINWAITMODULE") or (mtype_val == 0x5D)
                if stats["keyex2"] == 0:
                    log_line(
                        f"[{now_ts()}] ROUTER frame type name={mtype_name} val=0x{mtype_val:02x}",
                        log_fp=log_fp,
                    )
                if stats["keyex2"] == 0 and (mtype_name in {"LOGIN", "JOINWAITMODULE"} or mtype_val in {0x5D, 0x66}):
                    log_line(
                        f"[{now_ts()}] ROUTER fallback gate keyex2={stats['keyex2']} sent={fallback_postpush_sent} msg={mtype_name} val=0x{mtype_val:02x}",
                        log_fp=log_fp,
                    )
                if stats["keyex2"] == 0 and (not fallback_postpush_sent) and fallback_trigger:
                    try:
                        from data import List
                        fallback_mode = str(args.post_ke2_push or "off").strip().lower()
                        fallback_extras = []

                        if fallback_mode in {"login", "login+joinwait", "all"}:
                            r_login = gsm.GSMResponse(msg)
                            r_login.header = copy.deepcopy(msg.header)
                            r_login.header.property = gsm.PROPERTY.GS
                            r_login.header.type = gsm.MESSAGE_TYPE.LOGIN
                            r_login.dl = List([])
                            fallback_extras.append(r_login)

                        if fallback_mode in {"ct-force"}:
                            # Aggressive CT shim: use the exact historical prelude bytes that
                            # produced deeper movement in earlier captures.
                            fallback_extras.append(bytes.fromhex("00000c002641eb8b8f8a8c8c"))
                            fallback_extras.append(bytes.fromhex("000006002641"))

                        if fallback_mode in {"joinwait", "login+joinwait", "ct-force", "all"}:
                            fallback_extras.append(
                                _build_joinwait_success(
                                    msg,
                                    wait_module=(args.wm_ip, int(args.wm_port)),
                                    joinwait_format=str(args.joinwait_format),
                                )
                            )

                        if fallback_mode in {"all"}:
                            newuser_ack = gsm.GSMResponse(msg)
                            newuser_ack.header = copy.deepcopy(msg.header)
                            newuser_ack.header.property = gsm.PROPERTY.GS
                            newuser_ack.header.type = gsm.MESSAGE_TYPE.GSSUCCESS
                            newuser_ack.dl = List([gsm.MESSAGE_TYPE.NEWUSERREQUEST.value.to_bytes(1, "little")])
                            fallback_extras.insert(0, newuser_ack)

                        log_line(
                            f"[{now_ts()}] ROUTER fallback check mode={fallback_mode} count={len(fallback_extras)} keyex2={stats['keyex2']} msg={mtype_name} val=0x{mtype_val:02x}",
                            log_fp=log_fp,
                        )
                        if fallback_extras:
                            log_line(
                                f"[{now_ts()}] ROUTER fallback post-push mode={fallback_mode} count={len(fallback_extras)}",
                                log_fp=log_fp,
                            )
                            for i, extra in enumerate(fallback_extras):
                                _send_response(extra, f"fallback{i}")
                            fallback_postpush_sent = True
                    except Exception as fe:
                        log_line(f"[{now_ts()}] ROUTER fallback error: {fe}", log_fp=log_fp)
                for i, extra in enumerate(extras):
                    _send_response(extra, f"push{i}")

    finally:
        try:
            conn.close()
        except Exception:
            pass
        dur_ms = int((time.time() - t_conn_start) * 1000)
        post_ke2_ms = -1 if t_ke2_tx is None else int((time.time() - t_ke2_tx) * 1000)
        log_line(
            (
                f"[{now_ts()}] ROUTER SUMMARY reads={stats['rx_reads']} frames={stats['frames']} "
                f"keyex1={stats['keyex1']} keyex2={stats['keyex2']} newuser={stats['newuser']} "
                f"login={stats['login']} joinwait={stats['joinwait']} gs_encrypt={stats['gs_encrypt']} "
                f"post_ke2_app={stats['post_ke2_app_frames']} decode_err={stats['decode_err']} "
                f"last_msg={last_msg_name} close={close_reason} dur_ms={dur_ms} post_ke2_ms={post_ke2_ms}"
            ),
            log_fp=log_fp,
        )
        log_line(f"[{now_ts()}] ROUTER {src_ip}:{src_port} DISCONNECT", log_fp=log_fp)
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ubisoft GS router service")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=40000)
    ap.add_argument("--wm-ip", default="192.168.0.213", help="Router wait-module IP to return")
    ap.add_argument("--wm-port", type=int, default=40005, help="Router wait-module port to return")
    ap.add_argument(
        "--joinwait-format",
        default="u32le",
        choices=["u32le", "u32be", "u16le", "u16be", "str", "ack"],
        help="Encoding for JOINWAITMODULE success payload",
    )
    ap.add_argument(
        "--keyex2-mode",
        default="random",
        choices=["random", "echo-client", "echo-raw", "echo-exact"],
        help=(
            "How to respond to KEY_EXCHANGE(2): 'random' generates a fresh server->client BF key; "
            "'echo-client' re-encrypts the client-provided BF key back to the client (single shared key). 'echo-raw' mirrors encrypted-key payload; 'echo-exact' mirrors full frame bytes."
        ),
    )
    ap.add_argument(
        "--post-ke2-push",
        default="off",
        choices=["off", "login", "joinwait", "login+joinwait", "ct-force", "ct-replay", "all"],
        help="Optional extra GS messages sent immediately after KEY_EXCHANGE(2) response.",
    )
    ap.add_argument(
        "--post-ke2-replay-files",
        default="",
        help="Optional ';' or ',' separated list of raw frame files to send when post-ke2-push includes ct-replay.",
)
    ap.add_argument("--idle-timeout", type=float, default=2.0)
    ap.add_argument("--dump-max", type=int, default=256)
    ap.add_argument("--save-rx-dir", default="captures/tcp/router_rx")
    ap.add_argument("--save-tx-dir", default="captures/tcp/router_tx")
    ap.add_argument("--log-file", default="logs/router_40000.log")
    ap.add_argument("--user-db", default="state/users.json", help="Path to JSON user DB for NEWUSERREQUEST")
    ap.add_argument(
        "--fixed-rsa-key-file",
        default="",
        help="Optional JSON path to persist/reuse a fixed RSA keypair across GS/WM modules.",
    )
    ap.add_argument(
        "--enforce-userdb",
        action="store_true",
        help="If set, log mismatched passwords on LOGIN (still replies success for now).",
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
                    help="If > 0, send a server-first JOINWAITMODULE after this many seconds of silence on connect (Login flow fix).")
    args = ap.parse_args(argv)

    if str(args.ct34_profile).strip().lower() == "ct_ps2":
        args.ct34_enable = True
        args.ct34_mode = "a"
        args.ct34_base = "persistantdata"
        args.ct34_host = str(args.wm_ip)
        args.ct34_id = 1
        args.ct34_num_a = 0
        args.ct34_num_b = 0

    # Load user DB once at startup; updates are saved on NEWUSERREQUEST.
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
            log_line(f"[{now_ts()}] ROUTER fixed-rsa error: {e}", log_fp=log_fp)
            args._fixed_rsa = None

    replay_spec = str(args.post_ke2_replay_files or "").strip()
    if replay_spec:
        parts = [p.strip() for p in replay_spec.replace(";", ",").split(",") if p.strip()]
        for p in parts:
            try:
                args._post_ke2_replay_frames.append(pathlib.Path(p).read_bytes())
            except Exception as e:
                log_line(f"[{now_ts()}] ROUTER post-ke2 replay read error: {p} ({e})", log_fp=log_fp)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, int(args.port)))
    sock.listen(16)

    log_line("Ubisoft GS Router listening", log_fp=log_fp)
    log_line("  build-marker: router_fallback_gate_v2", log_fp=log_fp)
    log_line(f"  bind: {args.bind}:{args.port}", log_fp=log_fp)
    log_line(f"  wm:   {args.wm_ip}:{args.wm_port}", log_fp=log_fp)
    log_line(f"  joinwait-format: {args.joinwait_format}", log_fp=log_fp)
    log_line(f"  keyex2-mode: {args.keyex2_mode}", log_fp=log_fp)
    log_line(f"  post-ke2-push: {args.post_ke2_push}", log_fp=log_fp)
    log_line(f"  post-ke2-replay-files: {len(args._post_ke2_replay_frames)} frame(s)", log_fp=log_fp)
    log_line(f"  fixed-rsa-key-file: {args.fixed_rsa_key_file or 'off'}", log_fp=log_fp)
    log_line(
        f"  ct34: profile={args.ct34_profile} enabled={args.ct34_enable} mode={args.ct34_mode} base={args.ct34_base} proxy={args.ct34_host}:{args.ct34_port}",
        log_fp=log_fp,
    )

    try:
        while True:
            conn, addr = sock.accept()
            th = threading.Thread(target=client_thread, args=(conn, addr, args), kwargs={"log_fp": log_fp}, daemon=True)
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





























