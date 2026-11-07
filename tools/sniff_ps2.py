#!/usr/bin/env python3
"""Logs every IP packet received from/to the PS2 (Windows raw socket, needs Administrator).

Shows what the console really sends after our NAT replies: pings from UDP 3658/10071-10073, ICMP errors, anything on
other ports. Usage: python sniff_ps2.py [--ps2 192.168.1.218] [--bind 192.168.1.113] [--log logs/ps2_sniff.log]
"""
import argparse, datetime, pathlib, socket, struct, sys

def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

def parse(pkt: bytes):
    """Returns a one-line description of an IPv4 packet, plus (src, dst)."""
    if len(pkt) < 20 or pkt[0] >> 4 != 4:
        return None, None, None
    ihl = (pkt[0] & 0x0F) * 4
    proto = pkt[9]
    src = socket.inet_ntoa(pkt[12:16]); dst = socket.inet_ntoa(pkt[16:20])
    body = pkt[ihl:]
    if proto == 17 and len(body) >= 8:
        sp, dp, ln = struct.unpack("!HHH", body[:6])
        data = body[8:]
        return f"UDP {src}:{sp} -> {dst}:{dp} len={len(data)} {data[:20].hex()}", src, dst
    if proto == 6 and len(body) >= 20:
        sp, dp = struct.unpack("!HH", body[:4]); fl = body[13]
        names = "".join(n for b, n in ((2, "S"), (16, "A"), (1, "F"), (4, "R"), (8, "P")) if fl & b)
        return f"TCP {src}:{sp} -> {dst}:{dp} flags={names} len={len(body) - ((body[12] >> 4) * 4)}", src, dst
    if proto == 1 and len(body) >= 8:
        extra = ""
        if body[0] == 3 and len(body) >= 36:   # unreachable: quoted original datagram
            qi = body[8:]; qihl = (qi[0] & 0x0F) * 4
            if len(qi) >= qihl + 4:
                qs, qd = struct.unpack("!HH", qi[qihl:qihl + 4]); extra = f" about udp/tcp {qs}->{qd}"
        return f"ICMP {src} -> {dst} type={body[0]} code={body[1]}{extra}", src, dst
    return f"IP proto={proto} {src} -> {dst} len={len(pkt)}", src, dst

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ps2", default="192.168.1.218")
    ap.add_argument("--bind", default="192.168.1.113")
    ap.add_argument("--log", default="logs/ps2_sniff.log")
    ap.add_argument("--skip-tcp-data", action="store_true", help="hide TCP segments with payload (keeps SYN/FIN/RST)")
    a = ap.parse_args()
    p = pathlib.Path(a.log); p.parent.mkdir(parents=True, exist_ok=True)
    fp = open(p, "a", encoding="utf-8")
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
    s.bind((a.bind, 0))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
    s.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)
    msg = f"[{ts()}] sniffing {a.ps2} on {a.bind}"
    print(msg); fp.write(msg + "\n"); fp.flush()
    while True:
        pkt = s.recvfrom(65535)[0]
        desc, src, dst = parse(pkt)
        if desc is None or a.ps2 not in (src, dst):
            continue
        if a.skip_tcp_data and desc.startswith("TCP") and not desc.endswith("len=0"):
            continue
        line = f"[{ts()}] {desc}"
        print(line); fp.write(line + "\n"); fp.flush()

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
    except OSError as e:
        print(f"ERROR: {e}. Run as Administrator and check --bind is the PC's LAN address.", file=sys.stderr); sys.exit(2)
