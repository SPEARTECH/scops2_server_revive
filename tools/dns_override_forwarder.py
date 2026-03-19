#!/usr/bin/env python3
"""DNS override + forwarder (UDP) for LAN/PCSX2 bridged mode.

Why:
- In PCSX2 pcap-bridged mode, the PS2 uses your LAN's DNS, not your Windows hosts file.
- This server lets you override a hostname (e.g. gsconnect.ubisoft.com) to point to
  your PC (or any IP), while forwarding all other queries to a real DNS resolver.

Usage (example):
  python tools/dns_override_forwarder.py \
    --listen 0.0.0.0:53 \
    --upstream 192.168.0.1:53 \
    --a gsconnect.ubisoft.com=192.168.0.213

Then set the PS2 network profile's DNS server to 192.168.0.213.

Notes:
- This only handles UDP DNS (enough for typical console game lookups).
- It is for redirecting game service hostnames you control/own.
"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
from dataclasses import dataclass
from typing import TextIO

from net_utils import get_primary_lan_ip

from dnslib import A, DNSHeader, DNSQuestion, DNSRecord, QTYPE, RR  # type: ignore


def log_line(msg: str, *, log_fp: TextIO | None = None) -> None:
    print(msg, flush=True)
    if log_fp is not None:
        log_fp.write(msg + "\n")
        log_fp.flush()


@dataclass(frozen=True)
class HostOverride:
    name: str  # lower, no trailing dot
    ip: str


@dataclass(frozen=True)
class SuffixOverride:
    suffix: str  # lower, no trailing dot (e.g. ubisoft.com)
    ip: str


def norm_name(name: str) -> str:
    name = name.strip().lower()
    if name.endswith("."):
        name = name[:-1]
    return name


def parse_addr(s: str) -> tuple[str, int]:
    if ":" not in s:
        raise ValueError("Address must be ip:port")
    host, port_s = s.rsplit(":", 1)
    return host, int(port_s, 10)


def parse_overrides(items: list[str]) -> list[HostOverride]:
    out: list[HostOverride] = []
    for it in items:
        if "=" not in it:
            raise ValueError(f"Override must be name=ip, got: {it}")
        name, ip = it.split("=", 1)
        ip = ip.strip()
        if ip.upper() == "AUTO":
            ip = get_primary_lan_ip()
        out.append(HostOverride(norm_name(name), ip))
    return out


def parse_suffix_overrides(items: list[str]) -> list[SuffixOverride]:
    out: list[SuffixOverride] = []
    for it in items:
        if "=" not in it:
            raise ValueError(f"Suffix override must be suffix=ip, got: {it}")
        suffix, ip = it.split("=", 1)
        ip = ip.strip()
        if ip.upper() == "AUTO":
            ip = get_primary_lan_ip()
        out.append(SuffixOverride(norm_name(suffix), ip))
    return out


def is_excluded(qname: str, excluded_suffixes: list[str]) -> bool:
    q = norm_name(qname)
    for suf in excluded_suffixes:
        s = norm_name(suf)
        if not s:
            continue
        if q == s or q.endswith("." + s):
            return True
    return False


def match_suffix(qname: str, suffixes: list[SuffixOverride]) -> SuffixOverride | None:
    # Longest-suffix match to avoid surprises if multiple suffixes are configured.
    best: SuffixOverride | None = None
    for s in suffixes:
        if qname == s.suffix or qname.endswith("." + s.suffix):
            if best is None or len(s.suffix) > len(best.suffix):
                best = s
    return best


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="DNS override + forwarder (UDP)")
    ap.add_argument("--listen", default="0.0.0.0:53", help="Listen ip:port (default 0.0.0.0:53)")
    ap.add_argument("--upstream", required=True, help="Upstream DNS ip:port (router or public DNS)")
    ap.add_argument(
        "--a",
        action="append",
        default=[],
        help="A-record override like gsconnect.ubisoft.com=192.168.0.213 (repeatable). Use ip=AUTO to detect your LAN IP.",
    )
    ap.add_argument(
        "--a-suffix",
        action="append",
        default=[],
        help="Suffix A-record override like ubisoft.com=192.168.0.213 to override any name ending in that suffix (repeatable).",
    )
    ap.add_argument(
        "--override-all-a",
        default="",
        help=(
            "DEBUG: if set to an IP (or AUTO), override ALL A queries to that IP (AAAA becomes NODATA) "
            "unless the name matches an excluded suffix. Useful to discover what hostnames the game uses."
        ),
    )
    ap.add_argument(
        "--exclude-suffix",
        action="append",
        default=[],
        help="Used with --override-all-a: do NOT override names ending in this suffix (repeatable), e.g. playstation.org",
    )
    ap.add_argument(
        "--nxdomain-suffix",
        action="append",
        default=[],
        help="Return immediate NXDOMAIN for names ending in this suffix (repeatable), e.g. dnas.playstation.org",
    )
    ap.add_argument("--ttl", type=int, default=60, help="TTL for overridden A records")
    ap.add_argument(
        "--log",
        action="store_true",
        help="Log each DNS query and whether it was overridden/forwarded",
    )
    ap.add_argument(
        "--log-file",
        default="",
        help="If set, append logs to this file (useful when run minimized/backgrounded)",
    )
    ap.add_argument(
        "--max-requests",
        type=int,
        default=0,
        help="If >0, exit after handling this many requests (useful for quick tests)",
    )
    args = ap.parse_args(argv)

    log_fp: TextIO | None = None
    if str(args.log_file).strip():
        try:
            log_fp = open(str(args.log_file).strip(), "a", encoding="utf-8")
        except OSError as e:
            print(f"ERROR: failed to open log file: {e}")
            return 2

    listen_host, listen_port = parse_addr(args.listen)
    upstream_host, upstream_port = parse_addr(args.upstream)
    overrides = parse_overrides(args.a)
    overrides_map = {o.name: o for o in overrides}
    suffix_overrides = parse_suffix_overrides(args.a_suffix)
    nxdomain_suffixes = [norm_name(s) for s in args.nxdomain_suffix]

    override_all_ip = str(args.override_all_a).strip()
    if override_all_ip:
        if override_all_ip.upper() == "AUTO":
            override_all_ip = get_primary_lan_ip()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((listen_host, listen_port))
        # Important on Windows: allows responsive Ctrl+C / stop flag checks.
        sock.settimeout(1.0)
    except OSError as e:
        log_line(f"ERROR: failed to bind {args.listen}: {e}", log_fp=log_fp)
        return 2

    upstream = (upstream_host, upstream_port)

    log_line("DNS override+forwarder listening", log_fp=log_fp)
    log_line(f"  listen:   {listen_host}:{listen_port}", log_fp=log_fp)
    log_line(f"  upstream: {upstream_host}:{upstream_port}", log_fp=log_fp)
    if overrides:
        log_line("  overrides:", log_fp=log_fp)
        for o in overrides:
            log_line(f"    A {o.name} -> {o.ip}  ttl={args.ttl}", log_fp=log_fp)
    else:
        log_line("  overrides: (none)", log_fp=log_fp)

    if suffix_overrides:
        log_line("  suffix overrides:", log_fp=log_fp)
        for s in sorted(suffix_overrides, key=lambda x: len(x.suffix), reverse=True):
            log_line(f"    A *.{s.suffix} -> {s.ip}  ttl={args.ttl}", log_fp=log_fp)

    if override_all_ip:
        log_line("  override-all:", log_fp=log_fp)
        log_line(f"    A * -> {override_all_ip}  ttl={args.ttl}", log_fp=log_fp)
        if args.exclude_suffix:
            log_line("    excluded suffixes:", log_fp=log_fp)
            for s in args.exclude_suffix:
                log_line(f"      *.{norm_name(s)}", log_fp=log_fp)

    if nxdomain_suffixes:
        log_line("  nxdomain suffixes (instant NXDOMAIN, no forwarding):", log_fp=log_fp)
        for s in nxdomain_suffixes:
            log_line(f"    *.{s} -> NXDOMAIN", log_fp=log_fp)

    if log_fp is not None:
        log_line(f"  log:      {str(args.log_file).strip()}", log_fp=log_fp)
    log_line("  ctrl+c to stop", log_fp=log_fp)

    stop = {"flag": False}

    def _on_sigint(_signum, _frame):
        stop["flag"] = True

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except Exception:
        pass
    # Best-effort on Windows terminals (Ctrl+Break)
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _on_sigint)  # type: ignore[attr-defined]
        except Exception:
            pass

    handled = 0

    while True:
        if stop["flag"]:
            log_line("\nStopping.", log_fp=log_fp)
            break
        try:
            data, client = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except KeyboardInterrupt:
            log_line("\nStopping.", log_fp=log_fp)
            break
        except OSError:
            continue

        try:
            req = DNSRecord.parse(data)
        except Exception:
            # Not DNS
            continue

        # Only handle standard queries with exactly one question; forward the rest.
        qname = None
        qtype = None
        if req.questions:
            q = req.questions[0]
            qname = norm_name(str(q.qname))
            qtype = QTYPE.get(q.qtype, str(q.qtype))

        if args.log:
            if qname and qtype:
                log_line(f"DNS {client[0]}:{client[1]}  {qtype} {qname}", log_fp=log_fp)
            else:
                log_line(f"DNS {client[0]}:{client[1]}  (unparsed question)", log_fp=log_fp)

        # Immediate NXDOMAIN for blackholed suffixes (before any override or forwarding).
        if qname and nxdomain_suffixes:
            _nx = norm_name(qname)
            for _suf in nxdomain_suffixes:
                if _nx == _suf or _nx.endswith("." + _suf):
                    nxreply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=1, ra=1, rcode=3), q=req.q)
                    sock.sendto(nxreply.pack(), client)
                    if args.log:
                        log_line(f"  -> NXDOMAIN (nxdomain-suffix=*.{_suf})", log_fp=log_fp)
                    handled += 1
                    break
            else:
                _nx = None  # no nxdomain match, fall through
            if _nx is not None:
                continue

        suffix_match = match_suffix(qname, suffix_overrides) if (qname and suffix_overrides) else None

        override_all = False
        if qname and override_all_ip and qtype in ("A", "AAAA"):
            if not is_excluded(qname, list(args.exclude_suffix)):
                override_all = True

        if qname and qtype in ("A", "AAAA") and (qname in overrides_map or suffix_match is not None or override_all):
            o = overrides_map.get(qname)
            if o is not None:
                ip = o.ip
            elif suffix_match is not None:
                ip = suffix_match.ip
            else:
                ip = override_all_ip
            reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=1, ra=1), q=req.q)
            if qtype == "A":
                reply.add_answer(RR(rname=req.q.qname, rtype=QTYPE.A, rclass=1, ttl=int(args.ttl), rdata=A(ip)))
                sock.sendto(reply.pack(), client)
                if args.log:
                    if o is not None:
                        src = "exact"
                    elif suffix_match is not None:
                        src = f"suffix=*.{suffix_match.suffix}"
                    else:
                        src = "all"
                    log_line(f"  -> OVERRIDE({src}) A {qname} = {ip}", log_fp=log_fp)
                handled += 1
                continue
            # For AAAA: return NOERROR with 0 answers (NODATA) so clients typically fall back to A.
            sock.sendto(reply.pack(), client)
            if args.log:
                if o is not None:
                    src = "exact"
                elif suffix_match is not None:
                    src = f"suffix=*.{suffix_match.suffix}"
                else:
                    src = "all"
                log_line(f"  -> OVERRIDE({src}) AAAA {qname} = (no data)", log_fp=log_fp)
            handled += 1
            continue

        # Forward to upstream and relay the response.
        try:
            sock_up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock_up.settimeout(2.0)
            sock_up.sendto(data, upstream)
            resp, _ = sock_up.recvfrom(4096)
            sock.sendto(resp, client)
            if args.log:
                log_line("  -> FORWARDED", log_fp=log_fp)
            handled += 1
        except Exception:
            # If upstream fails, return SERVFAIL
            try:
                fail = DNSRecord(DNSHeader(id=req.header.id, qr=1, ra=1, rcode=2), q=req.q)
                sock.sendto(fail.pack(), client)
                if args.log:
                    log_line("  -> SERVFAIL (upstream error)", log_fp=log_fp)
                handled += 1
            except Exception:
                pass
        finally:
            try:
                sock_up.close()
            except Exception:
                pass

        if args.max_requests and handled >= args.max_requests:
            log_line(f"\nStopping (handled {handled} requests).", log_fp=log_fp)
            break

    try:
        sock.close()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
