from __future__ import annotations

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
