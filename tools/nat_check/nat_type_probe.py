#!/usr/bin/env python3
"""NAT-type probe for the SCCT relay design.

Question it answers: after a console behind a home router/ISP has sent one packet from port 3658 to the VPS on 45000
(the game's own NAT ping, repeated every 25 s), which VPS source ports can reach that console on 3658?

  only 45000 replies arrive          -> port-restricted NAT: the relay must send to the host from 45000 only
  45000 AND the other ports arrive   -> cone NAT: the relay may use a different source port per joiner

VPS (stop the SCOPS stack first, it owns 45000):
    python tools\nat_check\nat_type_probe.py --server

PC on the player's LAN (admin prompt; allow the replies through the PC firewall first):
    netsh advfirewall firewall add rule name="SCCT NAT probe" dir=in action=allow protocol=UDP localport=3658
    python tools\nat_check\nat_type_probe.py --client 38.49.212.120
    netsh advfirewall firewall delete rule name="SCCT NAT probe"
"""
import socket, select, sys, time

MAIN_PORT = 45000
EXTRA_PORTS = [45010, 45011, 3658]   # 3658 = also check "same port as the game socket" as a source
CLIENT_PORT = 3658


def server() -> None:
    socks = {}
    for p in [MAIN_PORT] + EXTRA_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", p))
            socks[p] = s
        except OSError as e:
            print(f"cannot bind {p}: {e}  (stop the SCOPS stack first)")
            return
    print(f"NAT probe server: main port {MAIN_PORT}, extra reply ports {EXTRA_PORTS}  (ctrl+c to stop)")
    main = socks[MAIN_PORT]
    while True:
        r, _, _ = select.select([main], [], [], 1.0)
        if not r:
            continue
        data, addr = main.recvfrom(2048)
        print(f"[{time.strftime('%H:%M:%S')}] probe from {addr[0]}:{addr[1]}  {data[:40]!r}")
        seen = f"{addr[0]}:{addr[1]}".encode()
        # reply from the main port first, then from each extra port; each tagged with its source port
        for p in [MAIN_PORT] + EXTRA_PORTS:
            try:
                socks[p].sendto(b"NATPROBE from=%d seen=%s" % (p, seen), addr)
            except OSError as e:
                print(f"   send from {p} failed: {e}")
        print(f"   replied from {[MAIN_PORT] + EXTRA_PORTS}")


def client(target: str) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", CLIENT_PORT))
    except OSError as e:
        print(f"cannot bind local port {CLIENT_PORT}: {e}  (is the PS2 traffic or another tool using it on this PC?)")
        return
    results = {}
    for attempt in range(3):
        s.sendto(b"NATPROBE attempt=%d" % attempt, (target, MAIN_PORT))
        deadline = time.time() + 3.0
        while time.time() < deadline:
            r, _, _ = select.select([s], [], [], max(0.0, deadline - time.time()))
            if not r:
                break
            data, addr = s.recvfrom(2048)
            results.setdefault(addr[1], set()).add(data.decode(errors="replace"))
        time.sleep(0.5)
    print(f"\nprobe to {target}:{MAIN_PORT} from local port {CLIENT_PORT}")
    seen = None
    for p in [MAIN_PORT] + EXTRA_PORTS:
        ok = p in results
        print(f"  reply from VPS port {p:<6} {'ARRIVED' if ok else 'blocked'}")
        if ok and seen is None:
            for msg in results[p]:
                if "seen=" in msg:
                    seen = msg.split("seen=", 1)[1]
    if seen:
        print(f"\nVPS saw this PC as {seen}  ->  source port {'PRESERVED' if seen.endswith(f':{CLIENT_PORT}') else 'REWRITTEN (symmetric-style NAT)'}")
    if MAIN_PORT not in results:
        print("\nNo reply even from 45000: outbound UDP blocked, VPS not running the server, or wrong address.")
    elif all(p in results for p in EXTRA_PORTS):
        print("\nVerdict: CONE NAT. Once the game has pinged 45000, any VPS source port can reach it -> relay can use one source port per joiner.")
    else:
        print("\nVerdict: PORT-RESTRICTED NAT (or firewall). Only the port the console talked to can reach it -> relay must send from 45000.")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--server":
        server()
    elif len(sys.argv) >= 3 and sys.argv[1] == "--client":
        client(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)
