# Splinter Cell: Chaos Theory (PS2) – Online Server Revival- GROK and Claude helped with this

A community server that brings the online lobby of **Tom Clancy's Splinter Cell: Chaos Theory (PS2, US, SLUS-21137)** back to life. **Update (1-2 Nov 2026): the original, unpatched disc now connects** on a stock PS2, with no patched ISO. See [Original disc support](#original-disc-support-no-patched-iso) below.

It's built on [SPEARTECH/scops2_server_revive](https://github.com/SPEARTECH/scops2_server_revive), which in turn builds on [michal-kapala/ubi-gs](https://github.com/michal-kapala/ubi-gs).

This fork adds fixes for the lobby and room list so that real PS2 consoles and PCSX2 players can see each other, join rooms and play full matches. It also adds the online menu's **friend list, game invites and Join Friend**, and a working **ranking** (ranked 2v2 matches with points, levels and a live leaderboard).

## Status

> **Playing over the internet requires port forwarding on your router.** See [Port forwarding is required](#port-forwarding-is-required-for-internet-play). Playing on one LAN needs none.

Tested on a LAN with real PS2 consoles (via OPL, and with the original disc) and up to three PCSX2 clients (full 2v2 matches, including ranked ones with PCSX2). Working:

- **Original unpatched disc on a stock PS2** (fat and slim): login, account creation, lobby, creating and joining rooms, 1v1 matches against PCSX2 (see below)
- Login, lobby selection and lobby join
- Creating and joining rooms; rooms appear in other players' game lists
- Map and setting changes show up in the game list
- Leaving a room or the lobby and rejoining, before and after a match (no more "Couldn't Access Lobby")
- Stale rooms are removed; joining a removed room shows "game no longer available" without breaking the lobby
- Lobby player count shown on the Lobby Selection screen
- Full matches with a PS2 host and PCSX2 joiners (and the reverse)
- **Friend list:** add and remove friends, live online/offline icons, friends saved across restarts
- **Invites:** Invite Friend, "Invited" on the Online Menu, Game Invites, and joining from an invite
- **Join Friend:** join a friend's room straight from the friend list (see Friend Slots below)
- **News:** the Online menu's News screen shows your own message of the day (`state/motd.txt`)
- **Ranked 2v2 matches:** points, levels, results screen and the in-game Ranking screen (see Ranking below)
- **Live lobby player count:** the number on the Lobby Selection screen updates while you look at it
- **Dead rooms disappear:** when a host's connection drops, their room is removed from everyone's list within a few seconds

**Internet play (tested):** with the server on a home PC and a real PS2 on another network (and PCSX2 on the server's network), players joined each other's rooms in both directions, played unranked 1v1 matches to the end (Cinema and Station Sabotage), and joined through the Games List, the friend list and game invites. See "Port forwarding is required" below and "Hosting from a home PC".

Not tested or not supported yet: ranked matches over the internet and ranked 2v2 on the original disc (only unranked "Free" matches were played; unranked games record no stats), 4-player games over the internet, a PCSX2 joiner on a different network, Quick Join on the original disc, a PAL disc, full-room refusal, disconnect/quit penalties in ranked matches, ignore lists and friend-request approval.

## Port forwarding is required for internet play

**Nobody outside your home network can connect unless you forward ports on your router.** This is the single most common reason an internet test fails. Playing on one LAN needs no forwarding at all; everything below is only for players on other networks.

Do this on the router of **whoever runs the server** and on the router of **whoever hosts a room** (they can be different people):

| Who | Forward to | Protocol and ports |
|---|---|---|
| Server host | the server PC (give it a fixed LAN IP) | **TCP** 80, 443, 40000, 40005, 44001, 44002 |
| Server host | the server PC | **UDP** 53, 45000-45003 |
| Whoever hosts a room (a console or PCSX2) | that console or PCSX2's own LAN IP | **UDP** 3658 and 10070-10080 |

- Only one machine on a network can receive the game ports (UDP 3658, 10070-10080) at a time, so only one player per household can host. A player who only **joins** a room does not need to forward anything on their own router.
- Do not rely on UPnP, and do not use the router's DMZ (it exposes every port on the PC). Forward exactly the ports above.
- A console plugged straight into the modem (no router) needs no forwarding.
- The server PC's firewall must also allow those ports.
- Start the server with `run_public_test.bat` (set your public IP in it) so outside players are told your public address instead of your LAN address. See "Hosting from a home PC".
- Check it from a phone on mobile data (not Wi-Fi): open `http://<your public IP>/gsinit.php?dp=SPLINTERCELL3PS2US&user=T`. If you get a page of server addresses, TCP 80 works. The server log (`logs/gs_http_80.log`) should show the request from the phone's address.
- Remove the forwards when you finish testing; internet bots find open ports within hours.

## Original disc support (no patched ISO)

Until now a real PS2 needed a patched ISO that skipped the DNAS check. A real PS2 now goes online with the **original, unmodified US disc (SLUS-21137)**, from a physical disc, from an unpatched ISO through OPL, or on PCSX2.

**What was wrong.** The console reached the "Cannot access" / frozen screen right after the NAT check on UDP 45000: it hung on the **first odd-length SRP datagram** (12-byte header + message) from the NAT service. PCSX2 tolerates odd lengths, which is why the problem only showed on real hardware. That correlation is strong (every odd-length reply was followed by a hang, and every run with even lengths worked), but why the console reacts this way has not been verified.

**The fix** (`tools/nat2/udp_reply_server.py`, on by default):

- The NAT replies (`PORT_ID`, then `ADDRESS` per socket) are built so that the whole datagram, header included, is an **even length** (the logs show 44 or 46 bytes). For `PORT_ID` the ping-id string is re-drawn until the length is even (up to 64 tries). If an `ADDRESS` reply is still odd, the port is written with a leading `+`. Whether the game parses the `+` is unverified, but login works either way.
- The server prints `even-length datagrams: on` at startup. Set `SCCT_NAT_EVEN=0` to restore the old behaviour.
- Other NAT knobs kept for testing: `SCCT_NAT_PORTID_LIMIT`, `SCCT_NAT_ADDRESS_DELAY_MS`, `SCCT_NAT_NO_REPEAT_ACK`, `SCCT_NAT_NO_ADDRESS`, `SCCT_NAT_GAP_MS`.
- `tools/ubigs_router_server.py` also has an optional key-exchange variant, `SCCT_KE1_VARIANT` (default `0`, i.e. unchanged behaviour).

**Tested (1-2 Nov 2026, all on one LAN):** a fat PS2 using an unpatched ISO through OPL and using the physical disc, a PS2 slim with the physical disc, and PCSX2. This included new account creation, news, rooms visible and joinable, a 1v1 on Cinema, and a PS2 slim vs PCSX2 match on Museum (Neutralisation). Only unranked "Free" matches were played. Not tested: ranked matches, Friend List, Quick Join, two real consoles playing each other at the same time.

### Running with the original disc

1. Start the server with `run_nat_even.bat`. It sets `SCCT_NAT_EVEN=1` and calls `run_nat2_test.bat`, which sets `SCCT_DNAS_EXTERNAL=1`, `SCCT_KE1_VARIANT=0` and `SCCT_NAT_V2=1` and then starts the stack through `run_test_keep.bat`.
2. Wait about 15 seconds, then start the DNAS replacement **afterwards** (see "DNAS" below). Starting the server stops other listeners on port 443, so the replacement has to start second.
3. On the console set **manual DNS to the server's IP**.

**If you launch from OPL instead of the disc, you MUST enable OPL compatibility modes 2, 4 and 6 for the game. This applies to both a normal (original) ISO and the patched ISO.**

### LAN-only by default

DNS (53) and HTTP (80) now ignore any client that isn't on a private, loopback or link-local address (`is_lan_client()` in `tools/net_utils.py`, used by `tools/dns_override_forwarder.py` and `tools/gs_http_server.py`). Set `SCCT_ALLOW_PUBLIC=1` to allow outside clients, for example on a hosted server.

- The filter **drops silently**, so a quiet log doesn't prove nobody tried to connect.
- It does **not** cover the router (40000), lobby (40005) or ladder (44002) yet. Outside IPs were seen connecting to port 40000 on a test PC that was in the router's DMZ. Don't put the server PC in a DMZ; forward only the ports you need.

### Packet sniffers

`tools/sniff_ps2.py` with `run_sniff_ps2.bat` (fat PS2) and `run_sniff_ps2_slim.bat` (slim, `--ps2 192.168.1.82`) capture a console's traffic to the server. Edit the addresses for your network and run as Administrator.

## Friends, invites and Friend Slots

- **Add / remove friends** in the online menu's friend list. Adding a name that doesn't exist shows "<name> doesn't exist". Friends are stored in `state/friends.json`.
- **Online / offline icons** update live, with no restart.
- **Invite Friend:** the host presses X on an online friend. The friend sees "Invited" on the Online Menu and joins from **Game Invites** (or from the friend list). When they join, the host's "Cancel Invite" goes back to "Invite".
- **Join Friend:** a friend who is hosting a room is shown as joinable; press X on them in the friend list to join their room.

**Friend Slots:** a room created with **Friend Slots 3** has no public slots, so it is hidden from the Games List and can only be joined by friends the host has **invited**. Pressing Join Friend on such a room without an invite fails (the game leaves again about a second after joining). With **Friend Slots 0**, Join Friend works in both directions. **Tested over the internet with a 1v1:** Friend Slots **1** and **2** both leave the room visible in the Games List and joinable from the Games List and from the friend list (the two players were friends). Only **3** hides the room. The game writes the slot count into the room data (two bytes change with the setting), so the setting reaches the server. Whether slots 1 and 2 hold seats back for invited friends once a room is nearly full was not tested (it needs a 4-player game with a non-friend). This looks like the game's own design (reserved slots are for invited friends); it is not fully confirmed and may be explored further.
**Recommendation:** use Friend Slots 0 if players should join from the friend list, or send an invite first.

Set `SCCT_FRIENDS=0` to turn all friend features off.

## Ranking

Ranked matches need **4 players (2v2)**. The server records the result when the match ends and the game's own Game Statistics and Ranking screens show it.

- **Points:** a win is **+50**, a loss is **-25**, never below 0. Points are kept per role: a player has a **Spy** total and a **Mercenary** total, and the **Overall** total is Spy + Mercenary.
- **Levels:** everyone starts at level 1 with 0 points; the cap is level 15. Leaving level L takes `50 x L x (L + 10)` points (550 to leave level 1), a curve fitted to the original server's ranking screenshots. The level shown is always the Overall level. `SCCT_LADDER_PTS_PER_LEVEL=<n>` switches to a simple linear curve.
- **Revenge mode:** teams swap for round 2 and each round is scored on the role the player held in that round.
- **Per-mode tracking:** the server records points, games, wins and time separately for **Neutralisation, Sabotage and Extraction** (`NEU_`, `SAB_`, `EXT_` keys in `state/ladder_stats.json`). The game's Ranking screen has no per-mode data (every mode tab shows the same Overall numbers; only the Spy/Mercenary switch changes them), so run `python tools/ladder_modes_report.py` for per-mode leaderboards (also written to `state/ladder_modes.txt`).
- **Automatic mode detection:** the first byte of the room's info blob tells the mode (`0x0d` Extraction, `0x0e` Sabotage, `0x0f` Neutralisation). It does not depend on the map and was confirmed on three maps per mode. To force a mode, put `NEU`, `SAB` or `EXT` in `state/ranked_mode.txt`; `AUTO`, an empty file or no file means detect. The file is read after every match, so no restart is needed.
- **Not recorded:** a draw (all results 0), a match with fewer than 4 players, and a match that ends within 30 seconds (everyone backed out to the lobby).
- **How it runs:** the Ranking screen is served by a second instance of the same server on port 44002 (`SCCT_LADDER_MODULE=1`, started by `tools\start_online_capture_ext.ps1`). It reads `state/ladder_stats.json` on every query, so edits show up without a restart. Registered users in `state/users.json` are added at level 1 / 0 points.
- **Known client quirk:** the "Old Total" on the results screen comes from the game's own cached copy of each player's stats, which it refreshes at login and only sometimes after a match. It can show an old number for some players. The server's totals are always right; logging out and in refreshes the display.
- The in-game text "(the general ranking is updated twice an hour)" is fixed client text; this server updates the ranking immediately.

Optional ranking settings:

| Variable | Purpose |
|---|---|
| `SCCT_RANKED_FLOW=0` | turn match scoring off |
| `SCCT_RANKED_WIN` / `SCCT_RANKED_LOSS` | points for a win / loss (default 50 / 25) |
| `SCCT_RANKED_MIN_PLAYERS` | players needed to record a match (default 4) |
| `SCCT_RANKED_MIN_SECONDS` | shortest match that counts (default 30) |
| `SCCT_LADDER_MAX_LEVEL` | level cap (default 15) |
| `SCCT_LADDER_PTS_PER_LEVEL` | use a linear level curve with this many points per level |
| `SCCT_RANKED_APPLY_DELAY` | seconds between the match end and saving the points (default 0.1) |
| `SCCT_LOBBY_COUNT_LIVE=0` | turn the live lobby player count off |
| `SCCT_LOBBY_COUNT_POLL` | how often the count is checked, in seconds (default 2) |

## News (message of the day)

The Online menu's **News** screen shows a message served by this server. Edit `state/motd.txt` to change it (it is created with a short welcome on first use and re-read on every request, so no restart is needed). Optional `state/motd_ubi.txt` fills the screen's first block; set `SCCT_MOTD=0` to turn the feature off.

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
| NAT probe / UDP reply | 45000-45003 (UDP) |

Players join each other's rooms by **direct client-to-client connections**. The server only runs the lobby and the room list, so the player hosting a room must be reachable by the others.

### Ports from the game manual

The US PS2 manual lists these ports. The start scripts already listen on all of the UDP ports.

| Protocol | Ports | Used for |
|---|---|---|
| UDP | 3658, 10070-10080 | the game itself (room traffic between players) |
| TCP | 40000-42999, 6668 | connection to the Ubisoft server |
| UDP | 41006, 4400, 45000-45001 | connection check / NAT probe |

On a LAN, just allow them in the server PC's firewall. For play over the internet, the **host's** router must forward UDP 3658 and 10070-10080 to the host's console or PC, and the server's TCP ports must be reachable.

## Root cause of the lobby bugs (for developers)

The game matches replies to requests by LOBBY_MSG subtype. Any stray, untyped or unsolicited reply poisons its reply queue, and the next request fails. Typically this was the automatic JOIN_LOBBY after backing out of the games list, which showed "Couldn't Access Lobby". The server now follows the reference behaviour:

- **No reply** for notifications: 34 GAME_CONNECTED, 35 GAME_FINISH, 44 PLAYER_MATCH_STARTED, 70 PLAYER_MATCH_FINISHED, and 36/40 PLAYER_BAN/UNBAN (the game sends UNBAN with every invite, and answering it left a stray reply behind)
- **Typed success reply** `['38',[subtype,[id]]]` for 15, 17, 31, 33 and 45
- **Failure reply** `['39',[24,[1,room_id]]]` when joining a room that no longer exists
- Message 31 (room config update) replaces the stored room info and pushes the updated room list, so map changes show up
- The lobby connection is never closed when a client leaves the lobby

Friend-related details found in the game code:

- Invites travel as PEERPLAYER (91) from the sender, which the server forwards to the target as PEERMSG (90). A 16-byte payload with command 1 is an invite, command 3 cancels it. When the invited friend joins, the server sends the host a **1-byte** PEERMSG with command 2, which clears "Cancel Invite" (the game ignores a command-2 message of any other length).
- A friend hosting a room is reported with status 10 ("joinable") and the game id in the friend update.
- **Join Friend** asks the server PLAYER_GROUP_GET (106). The game's handler reads the reply as `['106', [name, [id,parent], ...]]` with **no** `['38', ...]` success wrapper, and needs one pair matching a known lobby followed by one pair for the room. A wrapped reply makes it read "106" as the player name and fail with "game no longer available".

See `docs/ONLINE_FIXES.md` for the complete write-up, client-side findings and test results.

## Requirements

- Windows 10/11 (or Linux with `run.sh`)
- Python **3.11+** with `pip install rsa dnslib` (the NAT service in `ubi-gs-new/` uses `typing.Self`)
- OpenSSL that still supports TLS 1.0 and 3DES (for the DNAS gate on port 443). The Windows start script searches common locations such as Git for Windows, MSYS2 and OpenSSL-Win64.
- A fixed LAN IP for the server (DHCP reservation recommended)
- Free ports 53, 80, 443, 40000, 40005, 44001, 44002 and 45000-45003

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

Optional friend settings (defaults work; change only when testing):

| Variable | Purpose |
|---|---|
| `SCCT_FRIENDS=0` | turn the friend features off |
| `SCCT_FRIEND_UNKNOWN_REASON` | failure code when adding an unknown name (default 14) |
| `SCCT_PEER_OFFLINE_REASON` | failure code when messaging an offline friend |
| `SCCT_FRIEND_JOINABLE` | show hosting friends as joinable |
| `SCCT_FRIEND_GAME` | game id used to mark friends as playing this game |
| `SCCT_PLAYER_GROUP_GET` | enable the Join Friend lookup |
| `SCCT_PGG_WRAP` | `raw` (default) or `typed` (old reply format) |
| `SCCT_INVITE_REFRESH` | refresh the host's friend list after an invite is taken |
| `SCCT_MOTD=0` | turn the News / message of the day off |
| `SCCT_BAN_REPLY` | how ban/unban notifications are answered (default: no reply) |

## Connecting clients

On every client, set the **DNS server manually to the server's IP**.

### Real PS2 (OPL)

- The **original disc or an unpatched ISO now works** (see "Original disc support"). A patched ISO still works too.
- In OPL, enable **compatibility modes 2, 4 and 6** for the game. This is required for **both a normal ISO and the patched ISO**, and it produces the identifier that lets PS2 and PCSX2 players match each other.
- In the PS2 network settings, use manual DNS pointing to the server.

### PCSX2

- Use **PCAP Bridged** so the emulated PS2 gets its own LAN address, with a different **PS2 Address** for every instance and the DNS set to the server's IP. Wi-Fi is unreliable (a real PS2 could not join PCSX2 rooms hosted over Wi-Fi), and two Wi-Fi machines may not be able to join each other (check the router for client isolation).
- If the server runs on the same PC as PCSX2, do **not** bridge PCSX2 to the adapter carrying the server's IP: the instances then never get past DNAS. What worked: a **second wired adapter** (a cheap USB-to-Ethernet dongle) plugged into the same router and used only for PCSX2. In Windows give the dongle a fixed IPv4 address in the same subnet (for example `192.168.1.250/24`) with **no gateway** and an interface metric of 500 (Advanced, untick automatic metric) so Windows doesn't route normal traffic through it. Removing IPv4 from the dongle entirely makes PCSX2 report "No network adaptor detected". With this setup a real PS2 can join rooms hosted by PCSX2 and the reverse.
- Sockets mode can join rooms but cannot host, because it reports `192.0.2.100` as its address.
- **Check your gateway for internet play.** Open **Settings -> Network & HDD -> Ethernet -> Intercept DHCP** and set **Gateway Address** to **your own router's address**, with **Auto** unticked. With Auto, PCSX2 copies the gateway from the Windows adapter it is bridged to. A USB-to-Ethernet adapter set up with no gateway (as described above) therefore gives the PS2 a blank gateway, and the game cannot send anything outside your own subnet. Play on the LAN keeps working, so the problem only shows when someone joins from another network: the joiner sees "The game is no longer available", and a packet capture shows their packets arriving while the PCSX2 console never replies.
  - **Find your own gateway with `ipconfig`:** open Command Prompt, run `ipconfig`, and read the **Default Gateway** line under your main adapter (the one connected to your router, usually Ethernet or Wi-Fi). Use that address. `192.168.1.1` and `192.168.0.1` are common, but yours may differ, so don't copy an address from this README. A bridged adapter that has no gateway shows a blank Default Gateway; use the router's address from your main adapter instead.
  - Restart the game in PCSX2 after changing it. The network settings are only read at startup.

## DNAS

Real PS2 hardware authenticates with Sony's DNAS before going online. This project does not patch the game; it answers the DNAS check itself:

1. **DNS:** the DNS forwarder points `*.dnas.playstation.org`, `*.ubisoft.com`, `*.gamespy.com`, `*.gamespy.net` and `*.ubi.com` at the server.
2. **HTTPS 443:** a DNAS replacement (`dnasrep.exe`) answers the console's request. Started with `-docroot gate -certdir certs -dns= -https :443 -default-region us -force-success=fallback`, it answers every title that has no captured packet with the gateway's `success.raw` (41 bytes). The console accepts it with no further checks that were visible in the logs.
3. The TLS certificates it loads (`gate1.{jp,eu,us}.dnas.playstation.org`) **expired on 2026-04-16** and the console still connected.

Earlier attempts with a self-signed certificate (error -610) and with the DNASrep chain but no stored reply (error -864; game ID `0d1eed8952a5dfa6`, query type `01180000`) are superseded by the `force-success` fallback above.

Notes:

- `dnasrep.exe` is **Windows only**, and it is **not in this repository**: its origin, author and licence are not confirmed. Related public projects that replay captured DNAS packets: [FogNo23/DNASrep](https://github.com/FogNo23/DNASrep) (AGPL-3.0) and forks such as [Sporesirius/DNASrep](https://github.com/Sporesirius/DNASrep). Whether those are the source of the binary used here is unknown.
- PCSX2 doesn't need DNAS.
- Details of the investigation are in `docs/ONLINE_FIXES.md`.

## Troubleshooting

- **"Couldn't Access Lobby" or "No Lobby available":** make sure `SCCT_CLOSE_ON_LOBBY_LEAVE=0` and that you are running the current `tools\ubigs_router_wm_server.py`.
- **Room not visible to others:** check the log for `CREATE_ROOM` and for the rooms push; confirm both clients have the same DNS and can ping each other. A room with Friend Slots 3 is hidden from the games list on purpose; invite the friend instead.
- **"Game is no longer available" when joining:** the host is unreachable from the joiner (client isolation, Sockets mode, firewall) or the room was already closed. From the friend list it can also mean the room has Friend Slots 3 and the friend wasn't invited.
- **Friend shows as offline:** the friend must be logged in to the same server and be in your friend list on both sides.
- **"The game is no longer available" only when the other player is on a different network:** on a PCSX2 host, check the gateway (see PCSX2 above, and run `ipconfig` to find yours). Also check that the host's router forwards UDP 3658 and 10070-10080 to the host console or PC.
- **Port in use:** the start script stops listeners it knows about; close other programs using ports 53, 80 or 443.
- **Logs:** everything is in `logs\`. `router_wm_40005.log` is the main one.

## Hosting from a home PC (tested)

Internet tests were run with the server on a home PC (behind a consumer router with the forwards above) and a real PS2 on another network. What worked: account creation, login, lobby and room list; joining rooms in both directions (PCSX2 hosting and the PS2 hosting); unranked 1v1 matches (Cinema and Station Sabotage) played to the end; joining through the friend list and game invites; Friend Slots 0, 1 and 2. Not yet tested: ranked matches (stats writes), 4-player games, a PCSX2 joiner on a different network, and a PAL disc.

Start the server with `run_public_test.bat` (copy it, put your public IP in `SCCT_PUBLIC_IP`). It sets:

| Variable | Purpose |
|---|---|
| `SCCT_ALLOW_PUBLIC=1` | let outside clients through the LAN-only filter |
| `SCCT_PUBLIC_IP=<your public IP>` | outside clients get this address (DNS answers, gsinit, router and lobby replies, and a LAN-hosted room's address) instead of the LAN one; DNS for outside clients is answer-only (everything else is refused) |
| `SCCT_NAT_PUBLIC_ADDR=1` | the NAT service reports the public address for LAN players, so rooms hosted on the LAN can be joined from outside; LAN players then reach each other through the router (NAT loopback), so leave it off for pure LAN sessions |

The port forwards are listed in "Port forwarding is required" near the top.

**PCSX2 players:** if your PCSX2 adapter has no gateway, a console on it can't send packets off the local network, and internet joins time out with "The game is no longer available" (see the PCSX2 note under Connecting clients).

## Hosting on the Internet

A small VPS (1 vCPU, 1–2 GB RAM) is enough for the Python services. Internet play between different homes has only had the limited test above. Because the DNAS replacement used here is Windows-only, a hosted setup needs a **Windows** server (for example Vultr's Windows plans) or a Linux build or Wine setup for the replacement, which has not been verified. Before opening it up:

- Set `SCCT_ALLOW_PUBLIC=1` (the LAN-only filter otherwise ignores outside consoles) and `SCCT_PUBLIC_IP` to the server's public IP; outside clients are then told that address (see "Hosting from a home PC").
- The NAT service reports the address it observes (or the public IP with `SCCT_NAT_PUBLIC_ADDR=1` for LAN players). A real PS2 behind a different network reaching a PCSX2 host worked in the home test; two consoles both behind home routers have not been tried.
- DNS for outside clients is answer-only: it answers the game's override hostnames and refuses everything else, so it is not an open resolver. Bots still probe ports 53 and 80 within hours.
- `state/users.json` stores passwords in plain text. Hash them (or at least keep the file private and out of git) before the server is public.
- Open only the ports listed under "Port forwarding is required". Don't use a router DMZ.
- Trim or rotate logs, since they contain players' IP addresses.
- Players who host rooms need UDP 3658 and 10070-10080 forwarded on their own router (see "Port forwarding is required").

## Repository layout

```
tools/ubigs_router_wm_server.py   lobby / wait-module server (main file)
tools/ubigs_router_server.py      router (port 40000), optional KE1 variant
tools/nat2/udp_reply_server.py    NAT service on UDP 45000+ (even-length replies)
tools/net_utils.py                LAN/outside client checks and public-IP selection (SCCT_PUBLIC_IP)
tools/dns_override_forwarder.py   DNS overrides (LAN-only by default)
tools/gs_http_server.py           gsinit HTTP server (LAN-only by default)
run_nat_even.bat                  start the stack for the original disc (NAT even-length)
run_nat2_test.bat                 sets SCCT_DNAS_EXTERNAL / SCCT_KE1_VARIANT / SCCT_NAT_V2
run_public_test.bat               start the stack for internet play (edit the public IP)
ubi-gs-new/                       newer ubi-gs snapshot used by the NAT service (MIT, see its LICENSE)
tools/ladder_modes_report.py      per-mode leaderboards from state/ladder_stats.json
tools/start_online_capture_ext.ps1  Windows start script for the whole stack
run_test_keep.bat / run_test.bat  recommended settings / base settings
run.sh                            Linux start script
docs/ONLINE_FIXES.md              full technical write-up
state/friends.json                saved friend lists (created at runtime; don't commit)
state/ladder_stats.json           ranking data (created at runtime; don't commit)
state/ranked_mode.txt             optional: force NEU / SAB / EXT, or AUTO
```

## Credits

- [SPEARTECH/scops2_server_revive](https://github.com/SPEARTECH/scops2_server_revive) – the base project
- [michal-kapala/ubi-gs](https://github.com/michal-kapala/ubi-gs) – Ubisoft GS server implementation
- [DigitalCyberSoft/ubi-gs-go](https://github.com/DigitalCyberSoft/ubi-gs-go) – reference server behaviour
- DNASrep and the DNAS-forever community – DNAS research
- The original-disc fix was found by analysing real-console traffic and logs

This project is for preservation of a game whose official servers are offline. It doesn't include any game files, ISOs or Sony code.
