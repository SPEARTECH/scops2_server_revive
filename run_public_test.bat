@echo off
rem Hosted test from the home PC: outside consoles are told the public IP, LAN consoles keep the LAN IP.
rem 1) forward the ports on the router (not DMZ)  2) run this  3) wait ~15 s, then dnasrep_test\run_dnasrep_443_only.bat
rem Edit SCCT_PUBLIC_IP if your public IP changes.
rem SCCT_NAT_PUBLIC_ADDR=1: the NAT service reports the PUBLIC address for LAN players too, so rooms hosted here can be
rem joined from outside. LAN-only play then goes through the router (NAT loopback); remove it for pure LAN sessions.
set SCCT_ALLOW_PUBLIC=1
set SCCT_PUBLIC_IP=38.49.212.120
set SCCT_NAT_PUBLIC_ADDR=1
rem SCCT_RELAY=1: outside joiners behind a different NAT than the host are routed through this server (UDP relay on
rem 3658/10070-10080 -> host via 45000). Needs no port forwarding on the host's side. SCCT_RELAY_IP defaults to SCCT_PUBLIC_IP.
set SCCT_RELAY=1
rem Hex-dump the first 24 datagrams per direction of each relayed flow (to learn the peer protocol for multi-joiner routing).
set SCCT_RELAY_DUMP=300
rem Multi-hole relay: every console's 3658 socket is steered to ping 45001 and 45002 alternately (an unsolicited PORT_ID
rem replaces the ping target, test 2026-11-04 17:08), so the host keeps two NAT holes and the relay can give each joiner
rem link its own source port (room link via 45001, game link via 45002) - what the host sees natively.
set SCCT_RELAY_HOLES=45001,45002
rem a steering PORT_ID is sent only when a needed hole saw no host traffic for this long (min 8 s apart)
set SCCT_RELAY_HOLE_STALE_S=12
call "%~dp0run_nat_even.bat"
