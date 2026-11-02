from __future__ import annotations

import ipaddress
import os
import socket


def get_primary_lan_ip() -> str:
    """Best-effort: returns the IP used for default outbound route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't send packets; just asks OS what source IP would be used.
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    finally:
        s.close()


def is_lan_client(ip: str) -> bool:
    """True for loopback / private (RFC1918) / link-local sources.

    The DNS forwarder and HTTP server only need to talk to your own consoles and emulators. Without this check, anything
    port-forwarded to the PC lets the whole internet use the DNS forwarder (open resolver) and probe the HTTP server.
    Set SCCT_ALLOW_PUBLIC=1 to disable the check (e.g. if you host for friends over the internet on purpose).
    """
    if os.environ.get("SCCT_ALLOW_PUBLIC", "0") == "1":
        return True
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local
