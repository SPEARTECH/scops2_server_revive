#!/usr/bin/env python3
"""Run this on the VPS (or any machine outside the host's network) while udp_dmz_listen.py runs on the DMZ PC.

  python tools/nat_check/udp_dmz_send.py 38.27.30.232

Sends a tagged datagram to UDP 3658 and 10070-10080 at the target and waits 3 s for the echo on each.
'reply' = the port is reachable from the internet (DMZ / forward works). 'no reply' = blocked somewhere in front of the PC.
"""
import socket, sys, time

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(2)
target = sys.argv[1]
PORTS = [3658] + list(range(10070, 10081))
ok = 0
for p in PORTS:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3.0)
    tag = f"SCCT-DMZ-TEST port={p} t={int(time.time())}".encode()
    try:
        s.sendto(tag, (target, p))
        data, addr = s.recvfrom(2048)
        print(f"  :{p:<6} reply from {addr[0]}:{addr[1]}  {data[:40]!r}")
        ok += 1
    except socket.timeout:
        print(f"  :{p:<6} no reply")
    except OSError as e:
        print(f"  :{p:<6} error: {e}")
    finally:
        s.close()
print(f"\n{ok}/{len(PORTS)} ports reachable at {target}")
if ok == 0:
    print("Nothing got through: double NAT / CGNAT, DMZ pointing at the wrong LAN IP, or the PC firewall. "
          "Compare the router's WAN IP with the address above.")
elif ok < len(PORTS):
    print("Partial: some ports blocked - check the forward/DMZ rule covers 3658 and 10070-10080.")
else:
    print("All ports reachable: inbound is fine; the join failure is not router filtering.")
