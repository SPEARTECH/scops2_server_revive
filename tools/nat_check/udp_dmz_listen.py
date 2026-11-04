#!/usr/bin/env python3
"""Run this on the PC that is temporarily in the DMZ (or has the port forwards) in place of the PS2.

It listens on the SCCT game ports (UDP 3658 and 10070-10080), prints every datagram that arrives and echoes it back so
the sender (udp_dmz_send.py on the VPS) also sees the result.

Windows: run from an Administrator prompt, and let the firewall through first:
  netsh advfirewall firewall add rule name="SCCT DMZ test" dir=in action=allow protocol=UDP localport=3658,10070-10080
Remove the rule afterwards:
  netsh advfirewall firewall delete rule name="SCCT DMZ test"
"""
import socket, select, sys, time

PORTS = [3658] + list(range(10070, 10081))

socks = []
for p in PORTS:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", p))
        socks.append(s)
    except OSError as e:
        print(f"port {p}: cannot bind ({e}) - something else is using it")

print(f"listening on UDP {', '.join(str(s.getsockname()[1]) for s in socks)}  (ctrl+c to stop)")
print("waiting for packets from the VPS...")
try:
    while True:
        r, _, _ = select.select(socks, [], [], 1.0)
        for s in r:
            data, addr = s.recvfrom(2048)
            port = s.getsockname()[1]
            print(f"[{time.strftime('%H:%M:%S')}] GOT {len(data)}B on :{port} from {addr[0]}:{addr[1]}  {data[:60]!r}")
            try:
                s.sendto(b"ECHO " + data, addr)
            except OSError as e:
                print(f"   echo failed: {e}")
except KeyboardInterrupt:
    pass
