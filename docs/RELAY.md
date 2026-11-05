# UDP relay for internet play (`SCCT_RELAY=1`)

## Why
Game traffic between players is console-to-console UDP (host:3658). A host behind a port-restricted NAT or an ISP that
filters unsolicited inbound traffic cannot be reached directly, and port forwarding / DMZ does not help when the ISP is
the one dropping the packets (confirmed on 38.27.x: DMZ passes LAN-hairpin traffic but nothing from the internet).

The console keeps one hole open by itself: after login it pings the GSNAT service (UDP 45000) from its 3658 socket every
~25 s, so packets from `<server>:45000` always reach `<console>:3658`. The relay uses that hole.

## How it works
1. `ubigs_router_wm_server.py` advertises every room created by an **outside (public-IP) host** with the relay address
   (`SCCT_RELAY_IP`, default `SCCT_PUBLIC_IP`) in fields 18/19 and in both address slots of the 96-byte info blob, from
   CREATE_ROOM on (the game dials the address from its room list, not from the JOIN_ROOM reply - test of 2026-11-04
   15:38: the reply alone produced no traffic to the relay). The real host address is kept in `room.real_ip_addr`. On
   JOIN_ROOM by anyone but the host, the relay is told `joiner -> real host` over loopback UDP (`SCCT_RELAY_CTRL_PORT`,
   default 45099); the host's own JOIN_ROOM ack still carries its real address. Rooms created by LAN clients stay direct.
2. `tools/nat2/udp_reply_server.py --relay-ports 3658,10070-10080` listens on the game ports. A joiner datagram is sent
   to `host:3658` **from 45000**; whatever the host sends back to 45000 from its game socket (not a 4-byte NAT ping, not
   its SRP session on 10070) is sent to the joiner from the relay port the joiner used.
3. GROUP_LEAVE / disconnect clear the mapping; idle flows expire after `SCCT_RELAY_IDLE_S` (600 s).
   `state/relay_map.json` shows the live table; log lines start with `RELAY` in `logs/udp_45000_reply.log` and
   `logs/router_wm_40005.log`.

## Limits (v1)
- One relayed joiner per host: the host sees every relayed peer as `<relay>:45000`. A port-restricted host NAT only
  accepts that one source, so a second outside joiner needs either a cone NAT on the host (then one relay source port per
  joiner) or the game's other presolved sockets (10071-10073) - to be tested.
- All relayed game traffic crosses the server (PS2-era bandwidth, a few KB/s per player).

## Enable
`run_public_test.bat` sets `SCCT_RELAY=1`; `start_online_capture_ext.ps1` then starts the NAT responder with the relay
ports instead of the `udp_log_server` and forces `SCCT_NAT_V2=1`. `nat_type_probe.py` in `tools/nat_check/` tells you
whether a player's NAT is cone or port-restricted.

## Firewall (VPS)
The relay ports must be open on the server's own firewall: `open_ports.ps1` now adds UDP 3658 and 10070-10080 (and ICMP
echo, which the game uses for its ping display). Test of 2026-11-04 16:03: PCSX2 dialed `<relay>:3658` from its 10072
socket (34-byte opener, 18-byte retries every 200 ms for 5 s) and Windows dropped every packet because the rule was missing.

## Same NAT
Two players behind the same public IP joining each other with the relay on is not supported yet (v1): the room list can only
carry one address and the relay cannot tell the two consoles apart. Join from different networks, or turn the relay off for
same-LAN sessions.
