# Splinter Cell: Chaos Theory (PS2) – Online Server Revival- GROK and Claude helped with this

A community server that brings the online lobby of **Tom Clancy's Splinter Cell: Chaos Theory (PS2, US, SLUS-21137)** back to life. It's built on [SPEARTECH/scops2_server_revive](https://github.com/SPEARTECH/scops2_server_revive), which in turn builds on [michal-kapala/ubi-gs](https://github.com/michal-kapala/ubi-gs).

This fork adds fixes for the lobby and room list so that real PS2 consoles and PCSX2 players can see each other, join rooms and play full matches.

## Status

Tested on a LAN with a real PS2 (via OPL) and two PCSX2 clients (3 players). Working:

- Login, lobby selection and lobby join
- Creating and joining rooms; rooms appear in other players' game lists
- Map and setting changes show up in the game list
- Leaving a room or the lobby and rejoining, before and after a match (no more "Couldn't Access Lobby")
- Stale rooms are removed; joining a removed room shows "game no longer available" without breaking the lobby
- Lobby player count shown on the Lobby Selection screen
- Full matches with a PS2 host and PCSX2 joiners (and the reverse)

Not tested or not supported yet: 4 players at once, full-room refusal, and Internet play (everything above was tested on one LAN).

## How it works (short version)

The game client talks to several Ubisoft "GS" services (router, lobby, persistent data) plus Sony's DNAS authentication. This project replaces them:

| Service | Port |
|---|---|
| DNS override (redirects Ubisoft/DNAS names to this server) | 53 (UDP/TCP) |
| HTTP (gsinit config) | 80 |
| TLS "DNAS gate" | 443 |
| Router | 40000 |
| Lobby / wait module | 40005 |
| Persistent data proxies | 44001, 44002 |
| NAT probe / UDP reply | 45000 (UDP) |

Players join each other's rooms by **direct client-to-client connections**. The server only runs the lobby and the room list, so the player hosting a room must be reachable by the others.

## Root cause of the lobby bugs (for developers)

The game matches replies to requests by LOBBY_MSG subtype. Any stray, untyped or unsolicited reply poisons its reply queue, and the next request fails. Typically this was the automatic JOIN_LOBBY after backing out of the games list, which showed "Couldn't Access Lobby". The server now follows the reference behaviour:

- **No reply** for notifications: 34 GAME_CONNECTED, 35 GAME_FINISH, 44 PLAYER_MATCH_STARTED, 70 PLAYER_MATCH_FINISHED
- **Typed success reply** `['38',[subtype,[id]]]` for 15, 17, 31, 33 and 45
- **Failure reply** `['39',[24,[1,room_id]]]` when joining a room that no longer exists
- Message 31 (room config update) replaces the stored room info and pushes the updated room list, so map changes show up
- The lobby connection is never closed when a client leaves the lobby

See `docs/ONLINE_FIXES.md` for the complete write-up, client-side findings and test results.

## Requirements

- Windows 10/11 (or Linux with `run.sh`)
- Python 3.9+ with `pip install rsa dnslib`
- OpenSSL that still supports TLS 1.0 and 3DES (for the DNAS gate on port 443). The Windows start script searches common locations such as Git for Windows, MSYS2 and OpenSSL-Win64.
- A fixed LAN IP for the server (DHCP reservation recommended)
- Free ports 53, 80, 443, 40000, 40005, 44001, 44002 and 45000

## Quick start (Windows)

1. Clone this repository.
2. Set your server's LAN IP: edit the `HostIP` default near the top of `tools\start_online_capture_ext.ps1` (and `HOST_IP` in `run.sh` on Linux).
3. Run `run_test_keep.bat`. It sets the recommended options and then starts the whole stack.
4. Check that `logs\router_wm_40005.log` is created and that the server windows stay open.

Recommended settings (already in `run_test_keep.bat`):

```bat
set SCCT_CREATE_FULL=1
set SCCT_CLOSE_ON_MEMBER_LEAVE=0
set SCCT_ROOM_MEMBER_LEAVE=1
set SCCT_HOST_JOIN_REPLY=ip
set SCCT_NEW_GROUP_TO_CREATOR=1
set SCCT_JOIN_GROUPINFO=1
set SCCT_R8=single
set SCCT_CLOSE_ON_LOBBY_LEAVE=0
set SCCT_R9=none
set SCCT_9FORM=ref
```

`SCCT_STRAY_FIX`, `SCCT_ROOM_UPDATE` and `SCCT_JOIN_FAIL_REPLY` are on by default.

## Connecting clients

On every client, set the **DNS server manually to the server's IP**.

### Real PS2 (OPL)

- Use the patched ISO (see "DNAS" below).
- In OPL, enable **compatibility modes 2, 4 and 6** for the game. This produces the identifier that lets PS2 and PCSX2 players match each other.
- In the PS2 network settings, use manual DNS pointing to the server.

### PCSX2

- Use **PCAP Bridged** on a **wired Ethernet** adapter so the emulated PS2 gets its own LAN address. Wi-Fi is unreliable, and two Wi-Fi machines may not be able to join each other (check the router for client isolation).
- If the server runs on the same PC as PCSX2, bridge PCSX2 to a different adapter than the one carrying the server's IP.
- Sockets mode can join rooms but cannot host, because it reports `192.0.2.100` as its address.

## DNAS

Real PS2 hardware authenticates with Sony's DNAS before going online. The original, unpatched disc fails against this server (error -610 with a self-signed certificate). With the DNASrep certificate chain the console accepts the TLS handshake, but there is no stored reply for this game, so it ends in error -864:

- Game ID `0d1eed8952a5dfa6`, query type `01180000`
- A captured packet would be needed to allow unpatched discs

So real consoles currently need a **patched ISO** that skips the DNAS check. PCSX2 doesn't need this. Details of the investigation are in `docs/ONLINE_FIXES.md`.

## Troubleshooting

- **"Couldn't Access Lobby" or "No Lobby available":** make sure `SCCT_CLOSE_ON_LOBBY_LEAVE=0` and that you are running the current `tools\ubigs_router_wm_server.py`.
- **Room not visible to others:** check the log for `CREATE_ROOM` and for the rooms push; confirm both clients have the same DNS and can ping each other.
- **"Game is no longer available" when joining:** the host is unreachable from the joiner (client isolation, Sockets mode, firewall) or the room was already closed.
- **Port in use:** the start script stops listeners it knows about; close other programs using ports 53, 80 or 443.
- **Logs:** everything is in `logs\`. `router_wm_40005.log` is the main one.

## Hosting on the Internet

Any small Linux VPS (1 vCPU, 1–2 GB RAM) is enough. Atlanta, Dallas or Chicago are good central locations for North America. Before opening it up:

- Restrict the DNS forwarder so it only answers the game's hostnames (an open resolver can be abused).
- Open only the ports listed above.
- Trim or rotate logs, since they contain players' IP addresses.

## Repository layout

```
tools/ubigs_router_wm_server.py   lobby / wait-module server (main file)
tools/ubigs_router_server.py      router (port 40000)
tools/start_online_capture_ext.ps1  Windows start script for the whole stack
run_test_keep.bat / run_test.bat  recommended settings / base settings
run.sh                            Linux start script
docs/ONLINE_FIXES.md              full technical write-up
```

## Credits

- [SPEARTECH/scops2_server_revive](https://github.com/SPEARTECH/scops2_server_revive) – the base project
- [michal-kapala/ubi-gs](https://github.com/michal-kapala/ubi-gs) – Ubisoft GS server implementation
- [DigitalCyberSoft/ubi-gs-go](https://github.com/DigitalCyberSoft/ubi-gs-go) – reference server behaviour
- DNASrep and the DNAS-forever community – DNAS research

This project is for preservation of a game whose official servers are offline. It doesn't include any game files, ISOs or Sony code.
