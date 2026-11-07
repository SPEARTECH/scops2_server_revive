#!/usr/bin/env python3
"""Health-check a relay log window. Usage: python3 relay_health.py logs/udp_45000_reply.log [HH:MM] [HH:MM]
Prints, per host that was relayed: steer count, holes used, dead links (joiner sent but host never replied),
budget/ceiling hits, and room (map/rematch) count. A healthy session: steers well under 11, no dead links,
one stable hole per joiner socket reused across rooms."""
import sys, re, collections
path = sys.argv[1]
lo = sys.argv[2] if len(sys.argv) > 2 else "00:00"
hi = sys.argv[3] if len(sys.argv) > 3 else "99:99"
def t(l):
    m = re.match(r"\[([0-9-]+) ([0-9:]+)", l)
    return m.group(2) if m else ""
steers = collections.Counter(); ceiling = collections.Counter()
holes = collections.defaultdict(set); rooms = set()
j2h = collections.Counter(); h2j = collections.Counter()
waiting = []
for l in open(path, errors="replace"):
    ts = t(l)
    if not (lo <= ts <= hi): continue
    m = re.search(r"HOLE steer #\d+: ([0-9.]+) socket", l)
    if m: steers[m.group(1)] += 1
    if "HOLE budget:" in l or "stops reacting" in l or "near the steer budget" in l:
        m = re.search(r"budget[^:]*: ([0-9.]+)|: ([0-9.]+) (?:is near|has received)", l)
        if m: ceiling[(m.group(1) or m.group(2))] += 1
    m = re.search(r"RELAY host ([0-9.]+): joiner link ([0-9.:]+) <.*hole port (\d+)", l)
    if m: holes[m.group(1)].add((m.group(2), m.group(3)))
    m = re.search(r"map joiner .* room (\d+)", l)
    if m: rooms.add(m.group(1))
    m = re.search(r"RELAY J->H ([0-9.:]+) -> ([0-9.]+):", l)
    if m: j2h[(m.group(2), m.group(1))] += 1
    m = re.search(r"RELAY H->J ([0-9.]+):\d+ -> ([0-9.:]+)", l)
    if m: h2j[(m.group(1), m.group(2))] += 1
    if "waiting for the host's ping" in l or "needs a steer" in l:
        waiting.append(ts + " " + l.split("joiner link ",1)[-1].strip()[:70])
hosts = set(steers) | {h for h, _ in holes.items()} | {k[0] for k in j2h}
print(f"window {lo}-{hi}   rooms (maps/rematches): {len(rooms)}   total steers: {sum(steers.values())}")
for h in sorted(hosts):
    dead = [j for (hh, j), n in j2h.items() if hh == h and n >= 10 and h2j.get((h, j), 0) == 0]
    print(f"  host {h}: steers={steers.get(h,0)} ceiling_hits={ceiling.get(h,0)} "
          f"holes={len(holes.get(h,set()))} dead_links={len(dead)}")
    for d in dead: print(f"      DEAD (joiner sent, host never replied): {d}")
if waiting:
    print(f"  {len(waiting)} 'waiting/needs-a-steer' events (first few):")
    for w in waiting[:5]: print("     ", w)
