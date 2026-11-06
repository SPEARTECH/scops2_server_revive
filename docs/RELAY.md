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

## Findings 2026-11-04 (first relayed joins)
- Joins through the relay work in both directions (COOLDOR into SNK's room and SNK into COOLDOR's room, `subtype=34`).
- The joiner uses one socket for a short probe (34 B / 66 B exchange), then another for the room session (58 B connect,
  ~1 KB room download, 18-byte keepalives every 200 ms). Packets are `[u16 LE length][payload …0xbd]`.
- Each ROOM_UPDATE (31) pushed to a player inside the room made that client open ANOTHER game connection to the host from a
  third socket; through the relay both arrive from `<relay>:45000`, replies were split between them and the joiner dropped
  ~60 s later. The server no longer pushes the room list to the room's own members (`SCCT_ROOM_UPDATE_TO_MEMBERS=1` to revert).

## Findings 2026-11-04 (evening)
- Multi-hole mode works: the console's 3658 socket follows an unsolicited PORT_ID (it replaces the ping target), so the
  service steers it on demand between 45001/45002 and each joiner link gets its own source port. Three matches in a row
  with map/mode changes and team swaps played through the relay.
- The console stops reacting after ~11 unsolicited PORT_IDs (SRP receive window 536 B); steers are therefore on demand
  only (stale needed hole), at least 30 s apart, and the count is logged (`HOLE steer #N`).
- The client reuses local ports across roles (probe port later used for the game link): an endpoint that was never
  established is re-allocated when it reappears after idling.
- "Revenge" rematch: the host's game sends GROUP_LEAVE for the room right after starting; the relay mapping must not be
  dropped on lobby leaves (only on the host's disconnect / idle expiry, `SCCT_RELAY_UNMAP_ON_LEAVE=1` restores the old behaviour).

## 2026-11-05 15:09 - neither joiner could join (hole sharing / re-allocation)

- COOLDOR's room link (10073) exchanged 3 packets on hole 45001, idled 7 s (normal in the lobby), and on resuming was moved to 45002 by the "re-evaluate a never-established endpoint" rule (established = 20 packets). The host then saw an unknown peer from VPS:45002 and COOLDOR timed out after ~27 s.
- DORDOR's join request (62736) was put on 45001 too (slot 0 "free" because COOLDOR's links were idle and not "established"), so the host saw it as COOLDOR's peer and both joiners' replies were misrouted. DORDOR gave up after 5 s.
- Fix: `pick_slot` now gives every joiner endpoint (ip, port) its own hole for the whole session (mirrors a direct path, where the host keys the peer by (ip, port) and copes with port reuse itself); no sharing, no re-allocation. Only when all holes are taken does a new endpoint take over the longest-idle one. Pre-open raised to 6 holes so two joiners' probe+room+game links are all open at join time.

## 2026-11-05 19:14 - joins needed 3-4 tries, nobody loaded into the match (holes die, steering budget spent)

- The host NAT (38.27.30.232) keeps a hole only while the HOST sends through it. Its 3658 socket pings exactly one hole - the last PORT_ID target - every 25 s; a hole it stopped pinging is gone after roughly 60 s (alive at 32 s, dead at ~67 s and beyond). The relay inbound 2-byte keepalives do NOT refresh it (holes pre-opened at 19:09 were all dead by 19:14 despite keepalives every 45 s). Only links the host answers stay open, through the host own game traffic.
- Pre-opening 6 holes therefore burned 6 of the ~10 steers per login for nothing: every join landed on a dead hole, the dead-hole rule steered (one steer, then up to 25 s for the console ping) while the join probe gives up after 5 s - hence 3-4 attempts per joiner. By the time the match started the budget was at 10/10: DORDOR game link (62773) sat on dead hole 45005 for 70 s with no steer left and both joiners were dropped.
- New rule (`pick_hole_port` / `hole_steering`): the hole the console is pinging right now is the READY hole. A new joiner endpoint takes it (host answers at once); the console is then steered to the next unassigned hole, which becomes the new spare within 25 s. A joiner second link right behind its join probe shares the probe hole (the host copes, as in every earlier 2-player session) so the spare stays free for the next joiner; the probe port, when the client reuses it for another link later, gets its own hole again. Every endpoint keeps its hole for good. Budget per session: 1 (login) + 1 per join + 1 per game link.
- Consequence for players: joiners should come in one at a time, ~30 s apart (the spare needs one ping cycle after the previous join). Two joins within the same 25 s put the second one on a hole that is still being opened, and the join probe times out before the ping arrives (retry works).

## 2026-11-05 19:44 - two 3-player matches OK, DORDOR dropped on the 2nd return-to-lobby (steer budget)

- Ready/spare steering worked: every join and every new game link landed on the hole the console was already pinging and got an instant reply. Two full 3-player matches played.
- DORDOR-SLIM sits behind Windows ICS (symmetric NAT): every one of its links gets a FRESH external port (62786/62787/62781/62790/62793/62794/62795...). Each is a new peer to the host, so each needs its own hole and one steer. Over two matches that reached steer #10 at 19:53:52.
- The old cap HOLE_BUDGET=10 then refused to steer. DORDOR 62795 (2nd return to lobby) landed on hole 45011 "needs a steer", the console was never told to ping it, the router never opened it, in=122 out=0, dropped at 19:57:54. COOLDOR (its own NAT) survived.
- The host console ACKed our PORT_IDs over SRP right up to segment 0x12 (18 reliable segments) with no stall - the "stops reacting around 11" of 2026-11-04 was UNSERIALIZED steers overflowing the 536 B window, not a hard limit. With steers serialized (one per confirmed ping / HOLE_MIN_S apart) the console digests many more. Fix: HOLE_BUDGET 10 -> 40 (SCCT_RELAY_HOLE_BUDGET).
