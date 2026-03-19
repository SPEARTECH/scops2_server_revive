#!/usr/bin/env python3
"""Common helpers for running Ubisoft GS (ubi-gs) protocol services.

These wrappers exist so we can:
- bind on LAN (0.0.0.0) and configurable ports
- log decoded GSM messages and raw rx/tx blobs
- reuse the `ubi-gs/ubi-gs-master` protocol implementation

"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import pathlib
import sys
from dataclasses import dataclass
from typing import TextIO


UBI_GS_ROOT = pathlib.Path(__file__).resolve().parents[1] / "ubi-gs" / "ubi-gs-master"


def ensure_ubigs_importable() -> None:
    if str(UBI_GS_ROOT) not in sys.path:
        sys.path.insert(0, str(UBI_GS_ROOT))


def now_ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def file_ts() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def log_line(msg: str, *, log_fp: TextIO | None = None) -> None:
    print(msg, flush=True)
    if log_fp is not None:
        log_fp.write(msg + "\n")
        log_fp.flush()


def hexdump(b: bytes, width: int = 16, max_len: int = 256) -> str:
    b = b[:max_len]
    lines: list[str] = []
    for off in range(0, len(b), width):
        chunk = b[off : off + width]
        hex_part = " ".join(f"{x:02x}" for x in chunk)
        ascii_part = "".join(chr(x) if 32 <= x <= 126 else "." for x in chunk)
        lines.append(f"{off:04x}  {hex_part:<{width*3}}  {ascii_part}")
    if len(b) >= max_len:
        lines.append(f"??? (truncated to {max_len} bytes)")
    return "\n".join(lines)


def split_gsm_frames(data: bytes) -> list[bytes]:
    """Split a TCP receive buffer into one or more GS frames.

    GS frames start with a 24-bit big-endian length.
    """

    frames: list[bytes] = []
    off = 0
    while off + 3 <= len(data):
        size = (data[off] << 16) | (data[off + 1] << 8) | data[off + 2]
        if size <= 0:
            break
        if off + size > len(data):
            break
        frames.append(data[off : off + size])
        off += size
    if not frames:
        return [data]
    if off < len(data):
        frames.append(data[off:])
    return frames


def pop_gsm_frames(buf: bytearray, *, max_frame_size: int = 1024 * 1024) -> list[bytes]:
    """Pop complete GS frames from a TCP stream buffer.

    GS frames start with a 24-bit big-endian length.
    Leaves any incomplete trailing bytes in `buf` for the next recv.
    """

    frames: list[bytes] = []
    while True:
        if len(buf) < 3:
            break
        size = (buf[0] << 16) | (buf[1] << 8) | buf[2]
        # Sanity checks: minimum header is 6 bytes.
        if size < 6:
            # Desync or garbage; drop one byte and retry.
            del buf[0:1]
            continue
        if size > max_frame_size:
            # Prevent unbounded growth on desync.
            buf.clear()
            break
        if len(buf) < size:
            break
        frames.append(bytes(buf[:size]))
        del buf[:size]
    return frames


def safe_slug(s: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in s)


def save_blob(out_dir: str | os.PathLike[str] | None, name: str, blob: bytes) -> None:
    if not out_dir:
        return
    p = pathlib.Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    try:
        (p / name).write_bytes(blob)
    except OSError:
        return


def sha16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


@dataclass
class ClientState:
    addr: tuple[str, int]
    username: str | None = None
    game_pubkey: object | None = None
    game_bf_key: bytes | None = None
    sv_pubkey: object | None = None
    sv_privkey: object | None = None
    sv_bf_key: bytes | None = None
    last_bf_key: bytes | None = None


def decode_gsm_message(gsm, frame: bytes, *, keys: list[bytes | None]):
    """Decode a GS frame, trying candidate BF keys in order.

    Some CT flows appear to encrypt different requests using different negotiated
    keys. We try a few likely keys and return the first that successfully parses.
    """

    last_exc: Exception | None = None
    hdr = None
    try:
        hdr = gsm.GSMessageHeader(frame[: gsm.GSMSG_HEADER_SIZE])
    except Exception:
        hdr = None

    seen: set[bytes] = set()
    for key in keys:
        if key is None:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            return gsm.Message(frame, key), key
        except Exception as e:
            last_exc = e
            # Some CT packets appear to use a list framing variant that the upstream
            # gsm.Message parser (List.from_buf default) rejects. For encrypted frames,
            # try decrypting and parsing with outer=False before giving up.
            if hdr is not None and hdr.property == gsm.PROPERTY.GS_ENCRYPT:
                try:
                    import blowfish
                    from data import List

                    dec = blowfish.Cipher(key).decrypt(frame[gsm.GSMSG_HEADER_SIZE : hdr.size])
                    alt = List.from_buf(bytearray(dec), False)

                    class _AltParsedMessage:
                        def __init__(self, header, dl):
                            self.header = header
                            self.dl = dl

                        def __repr__(self):
                            _tn = self.header.type.name if hasattr(self.header.type, 'name') else f"0x{self.header.type:02X}"
                            return (
                                f"<{_tn}\t{self.header.property.name}\t"
                                f"{self.header.sender.name}->{self.header.receiver.name}\t"
                                f"{self.header.size}B>\n{self.dl}"
                            )

                    return _AltParsedMessage(hdr, alt), key
                except Exception:
                    pass
            continue

    # Fallback: some CT packets decode to a valid header but use payload forms
    # that the upstream List parser rejects. For encrypted control-flow packets,
    # keep processing via a header-only message instead of dropping the frame.
    try:
        if hdr is not None and hdr.property == gsm.PROPERTY.GS_ENCRYPT:
            class _HeaderOnlyMessage:
                def __init__(self, header):
                    self.header = header
                    self.dl = None

                def __repr__(self):
                    _tn = self.header.type.name if hasattr(self.header.type, 'name') else f"0x{self.header.type:02X}"
                    return (
                        f"<{_tn}\t{self.header.property.name}\t"
                        f"{self.header.sender.name}->{self.header.receiver.name}\t"
                        f"{self.header.size}B>\n<unparsed encrypted payload>"
                    )

            fallback_key = next((k for k in keys if k is not None), b"")
            return _HeaderOnlyMessage(hdr), fallback_key
    except Exception:
        pass

    if last_exc is not None:
        raise last_exc
    raise ValueError("No keys provided")


