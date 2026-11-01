# Splinter Cell: Chaos Theory (PS2) – lobby / room fixes for `ubigs_router_wm_server.py` (v6)

Changes to the `scops2_server_revive` router (SPEARTECH, built on michal-kapala's `ubi-gs`).
This replaces the earlier packages (v2/v3); it contains everything from them plus the fixes found since.

Test setup: LAN, server on a Windows PC (192.168.1.113), game `SPLINTERCELL3PS2US`, game patched for online.
Clients: PCSX2 "ABC", a real PS2 via OPL "DORTEST", and a second PCSX2 "LINEBACKER" (a different PC).
3-player sessions (PS2 host, two PCSX2 joiners) and PCSX2-hosted sessions were played start to finish,
including repeated leave/rejoin of rooms and of the lobby.

Contents
- `tools/ubigs_router_wm_server.py` – full modified server (CRLF, as in the original)
- `ubigs_router_wm_server.diff` – diff against the unmodified original (CR stripped)
- `run_test_keep.bat` – recommended settings (calls `run_test.bat`, which calls the stock `run.bat`)
- `run_test.bat` – base settings
- `optional_dnas_replacement/` – DNAS replacement experiment (see DNAS section); not needed for the lobby fixes

## IMPORTANT for PS2 players using OPL
Real PS2 consoles launching the game through OPL need **compatibility modes 2, 4 and 6** enabled for the game.
This produces the proper identifier so the console matches (is listed with / can join) PCSX2 players.
(Found earlier in this project; without it the PS2 and PCSX2 clients did not see each other correctly.)

## Status
Working (tested): rooms created by either side appear in the other's Games List; join/play/finish; map and
setting changes made in the room show up in the list; leaving a room and leaving/re-entering the lobby any number
of times, before and after a match, no longer gives "Couldn't Access Lobby" / "No Lobby available"; stale rooms
disappear; asking to join a room that no longer exists shows "game no longer available" without breaking the lobby.

Friends (tested with PCSX2 + a real PS2): friend list, Invite Friend / Game Invites and Join Friend work.
**Friend Slots note:** a room created with Friend Slots 3 has no public slots, so it is hidden from the games list and
players can only get in by being **invited** by the host (Invite Friend, then accept from Game Invites or the friend list).
Pressing Join Friend on a friend hosting a Friend Slots 3 room without an invite fails: the join is accepted by the
server but the game leaves again about a second later. With Friend Slots 0 Join Friend works in both directions.
This looks like the game's own design (reserved slots are for invited friends); it is not fully confirmed and may be
explored further. Recommendation: use Friend Slots 0 if players should join from the friend list, or invite first.

Untested / known limitations
- Full rooms: the server does not track per-room players and never refuses with code 7 (the client computes the 2/4 shown).
- Never tested with 4 real players, nor with unusual leave orders in a 4-player room.
- Pushes MATCH_STARTED (after 17) and GAME_STARTED (56, after 33) are not sent; not needed so far.
- Unknown message type 75 still gets the generic reply once (harmless so far).
- Sockets-mode PCSX2 can join but cannot host (see network notes).

## Root cause of the lobby breakage (the key finding)
The client matches replies to requests by LOBBY_MSG subtype (GetNewCurMessage / CMsgQueue). Any stray, untyped or
unsolicited reply poisons that queue, and the NEXT request fails – typically the automatic JOIN_LOBBY (23) the client
sends ~1 s after backing out of the games list, which then shows "Couldn't Access Lobby". The old server answered
unhandled subtypes with a generic reply, so after a room had been played the lobby could not be re-entered.

Fix (block before the subtype-70 handler, `SCCT_STRAY_FIX`, default on), matching the reference ubi-gs-go server:
- No reply at all for notifications: 34 GAME_CONNECTED, 35 GAME_FINISH, 44 PLAYER_MATCH_STARTED,
  70 PLAYER_MATCH_FINISHED (70 still marks the room as played).
- Typed success reply `['38',[subtype,[id]]]` for 15 START_GAME, 17 START_MATCH, 31 GROUP_CONFIG_UPDATE_RES,
  33 GAME_READY, 45 MATCH_FINISH.
- 31 with flag 0x40 carries the 96-byte room info blob (contains the map name): the server replaces the room's
  info and pushes `[53,["1","256",["1"],[rooms]]]` to other clients so map changes show in the list
  (`SCCT_ROOM_UPDATE`, default on).
- JOIN_ROOM (24) for a missing/removed room returns the failure `['39',[24,[1,room_id]]]` (reason 1 = group does
  not exist) instead of nothing/generic (`SCCT_JOIN_FAIL_REPLY`, default on).
- GROUP_INFO_GET (9) answers only with a GROUP_INFO (53) push in the reference form `[53,[id,flags,[],[rooms],[]]]`
  (`SCCT_9FORM=ref`, `SCCT_R9=none`); 8 uses `SCCT_R8=single`.
- Do NOT close the lobby TCP connection when a client leaves the lobby: the client clears its lobby list on close
  ("No Lobby available"), and dropping it on the next 23 gives "Connection to Ubi.com was lost".
  (`SCCT_CLOSE_ON_LOBBY_LEAVE=0`).

## Earlier fixes (from v2, still included)
1. CREATE_ROOM: `game_version` = request[8] (client build id), not request[9] ("1.0") – otherwise the client drops every room.
2. Room list `games` field set to the allowed game name (`SCCT_GAMES_FIELD`).
3. JOIN_ROOM reply `['38',['24',[room_id,host_ip]]]` (`SCCT_JOIN_REPLY`, `SCCT_HOST_JOIN_REPLY=ip`).
4. 8 and 9 need flat typed replies/pushes (see client parsers below).
5. Stale rooms removed on the same user's new room / re-login, on host GROUP_LEAVE, and when a joiner leaves
   (GROUP_REMOVE 55 repeated at ~0.8 s and ~2.5 s).
6. Log lines show `from=user@ip:port`.

## Lobby player count (r37/r38, confirmed in-game)
The number shown after the lobby name on Lobby Selection came from a join/leave counter that drifted (showed 4 with
3 players: the client opens several connections / re-logins per user). Now it is the number of distinct usernames
with a live JOIN_LOBBY / LOBBYSERVERLOGIN session in `_lobby_state.sessions`, recomputed on login and on
disconnect (`_unique_lobby_users`). Used for the lobby entry in the LOGIN (21) reply (`nb_members`).
r38: the lobby list is only sent in the LOGIN reply, before the user's own JOIN_LOBBY registers them, so the logging-in
user is counted explicitly. Verified with 3 clients: Lobby Selection shows (1), (2), (3) for the first, second and
third login. The game only receives the count at login, so each player sees the number as of their own login
(a live push to others was not added; it risks the stray-message problem above).

## Recommended settings (`run_test_keep.bat`)
```
SCCT_CREATE_FULL=1  SCCT_CLOSE_ON_MEMBER_LEAVE=0  SCCT_ROOM_MEMBER_LEAVE=1  SCCT_HOST_JOIN_REPLY=ip
SCCT_NEW_GROUP_TO_CREATOR=1  SCCT_JOIN_GROUPINFO=1  SCCT_R8=single  SCCT_CLOSE_ON_LOBBY_LEAVE=0
SCCT_R9=none  SCCT_9FORM=ref
```
(`SCCT_STRAY_FIX`, `SCCT_ROOM_UPDATE`, `SCCT_JOIN_FAIL_REPLY` default on.)

## DNAS (real PS2 hardware)
- PS2 on OPL with the patched ISO gets through DNAS (OPL compatibility modes 2, 4, 6 as noted above).
- The ORIGINAL unpatched disc on a real PS2 fails: with our self-signed cert (openssl s_server on 443) the console answers
  `fatal bad_certificate` (TLS 1.0, SSLv2-format ClientHello offering 3DES/RC4/DES) -> on-screen "Network Error (-610)".
- Replacing the 443 server with Python-PS2-DNAS (DNASrep certificates, TLS 1.0 / 3DES, see `optional_dnas_replacement/`):
  the console accepts the certificate chain (`Handshake OK TLS_DHE_RSA_WITH_3DES_EDE_CBC_SHA`) and POSTs `/us-gw/v2.5_i-connect`.
  There is no stored reply for this game: `MISSING us-gw/packets/0d1eed8952a5dfa6_01180000` -> error.raw -> "DNAS Error (-864)".
  Game id `0d1eed8952a5dfa6`, query type `01180000`. A captured packet (from the DNAS-forever collection) would be needed
  in `dnas/gateways/us-gw/packets/` to let unpatched discs authenticate; none is in the 205/121/65 packets bundled for us/eu/gai.
- Patched build vs original `SC3_ON.ELF` (US, SLUS-21137): the running patched image differs in 276 bytes / 13 places:
  server host string replaced with `noname`, two functions at 0x35e540 and 0x3a63f0 start with `li v0,1; jr ra` (always-true stubs),
  a conditional branch at 0x112798 made unconditional, plus code caves at 0x2b5ea0 and 0x4a3498. Original uses
  `gsconnect.ubisoft.com`, which our DNS already redirects, so the address patch is not needed; only DNAS blocks unpatched discs.
  (The patched image was taken from a savestate of a running game, not from the ISO file itself; which callers the stubs
  belong to was not disassembled.)

## Network notes
- Game rooms are joined by direct client-to-client connections (nothing shows in the server's UDP logs); the server
  only brokers the lobby.
- PCSX2: **PCAP Bridged on a wired Ethernet adapter** is the reliable choice. Over Wi-Fi it is flaky; two Wi-Fi
  machines failed to join each other (AP/client isolation suspected; check router setting, ping between PCs).
  Set the emulated PS2's DNS manually to the server IP. If the server runs on the same PC as PCSX2, bridge
  PCSX2 to a different adapter than the one with the server's IP.
- PCSX2 Sockets mode: reports `192.0.2.100` as its address. Such a client can log in and JOIN rooms (worked in
  testing) but cannot HOST, since other consoles get an unroutable address.
- PS2 via OPL: compatibility modes 2, 4, 6 (see above).
- **Ports (from the US PS2 manual):** UDP 3658 and 10070-10080 are used by the game itself (room traffic between players);
  TCP 40000-42999 and 6668 are used for the connection to the Ubisoft server; UDP 41006, 4400 and 45000-45001 are used for
  the connection check/NAT probe. The stock start script (`run.sh` / `start_online_capture_ext.ps1`) already listens on all
  of these UDP ports, plus TCP 40000 (router), 40005 (wait module), 44001/44002 (persistent proxy), 80/443 and 6667/6668.
  On a LAN just allow them in the server PC's firewall. For play over the internet, the **host's** router must forward
  UDP 3658 and 10070-10080 to the host's console/PC (a room advertises UDP 3658), and the server's TCP ports must be reachable.

## Things that did not work
- Closing the lobby connection on leave (+ repush of the lobby list), or dropping it on rejoin.
- Other GROUP_INFO (9) reply shapes alone; repeated GROUP_REMOVE spam; re-parenting room lists (`SCCT_CTX_PARENT`);
  targeted clean-ups to a console joining a dead room (`SCCT_STALE_JOIN_CLEAN`).

## Client-side findings (SC3_ON.ELF, US PS2 build)
- LOBBY_MSG dispatcher `0x370380`; NEW_GROUP(54) -> `0x382fc0` -> room parser `0x37b670` (20 fields) -> filter
  `0x387950` -> observer `0x364b00` -> `0x361310` (strcmp of `game_version`).
- GROUP_REMOVE(55) -> `0x385520` -> `0x37ce70`. Reply parsers: 24 `0x37c1e0`, 9 `0x37b910`, 8 `0x37f360`.
- Replies: `['38',[subtype,[payload]]]`; failures `['39',[subtype,[reason,echo]]]`; pushes `[subtype,[payload]]`.
- The client auto-sends 109 + 8 when backing out of the games list, then 23 on the lobby connection ~1 s later.
- Room list entry (20 fields): type, name, id, lobby_sv_id, parent_id, config, level, master, allowed_games, games,
  info blob, event_id, max_players, nb_players, max_visitors, nb_visitors, game_version, gs_version, ip, alt_ip.

## Test flow
1. Start `run_test_keep.bat`; clients log in (`LOBBYSERVERLOGIN` should show each real LAN IP).
2. Host a room, join from another client, play, quit to the room, leave room, leave lobby, re-enter lobby (repeat).
3. Log lines to look for: `notification, no reply sent`, `ROOM_UPDATE room N: info replaced`, `players_now`.

No game files, ISOs or personal logs are included.


## News / Message of the day
The Online menu's **News** screen asks the server for the message of the day (message 0xDE, with the language). The server answers
with the text in `state/motd.txt` (created with a short welcome message on the first request). Edit the file with any text
editor; it is re-read on every request, so no restart is needed. Line breaks are kept and the text is cut at 500 characters.
The screen can show two blocks, a Ubisoft message and a game message: the game message comes from `state/motd.txt`, and an optional
`state/motd_ubi.txt` fills the first block. If that file is missing the first block is empty (the text then starts a little lower
on the screen). `SCCT_MOTD=0` turns this off; `SCCT_MOTD_BOTH=1` sends the same text in both blocks.

## Friend list, invites and Join Friend (v6)

Friends persist in `state/friends.json` and update live:
- Add/remove friends in-game (registered users only). Unknown names show "<name> doesn't exist" (failure reason 14; override with `SCCT_FRIEND_UNKNOWN_REASON`).
- Friends show the online icon when logged in and the plain icon when logged out, updating without a restart.
- **Invite Friend / Game Invites:** the host opens the friend list and presses X on an online friend. The friend sees "Invited" on the Online Menu and joins from Game Invites (or the friend list). When the friend joins, the host's "Cancel Invite" returns to "Invite" (the server sends the 1-byte "invite taken" message the game expects).
- **Join Friend:** a friend hosting a room shows as joinable; pressing X on them in the friend list joins that room (server answers the game's PLAYER_GROUP_GET, message 106, with the lobby and room ids).
- **Friend Slots:** rooms created with Friend Slots 3 have no public slots, so they are hidden from the games list. Joining one by Join Friend without an invite fails (the game leaves again about a second after joining); joining it through an invite works. Use Friend Slots 0 if players should join from the friend list, or invite first.
- Lobby ban/unban notifications (36/40) are no longer answered; the old generic reply left a stray message in the game's queue and later caused "Couldn't access lobby" after invites.
- Tested with 2 players (PCSX2 and real PS2). Set `SCCT_FRIENDS=0` to disable.
- Optional environment variables: `SCCT_FRIEND_UNKNOWN_REASON`, `SCCT_PEER_OFFLINE_REASON`, `SCCT_FRIEND_JOINABLE`, `SCCT_FRIEND_GAME`, `SCCT_PLAYER_GROUP_GET`, `SCCT_PGG_WRAP` (`raw` default, `typed` = old reply), `SCCT_PGG_MODE`, `SCCT_PGG_LOBBY_KEY`, `SCCT_BAN_REPLY`, `SCCT_INVITE_REFRESH`.
- Not implemented: ignore lists, friend-request approval.


## Ranking, ranked matches and per-mode tracking (v7)

**Ranking screen.** The game asks the proxy module on port 44002 for `ladderquery`. A second instance of `ubigs_router_wm_server.py` with `SCCT_LADDER_MODULE=1` answers it (the old text-protocol stub made the game hang up with "No CHALLENGE received"; `$UseOldProxyWm = $true` in the start script goes back to it). Columns the client requests: `GEN_/SPY_/SEN_` + `LEVEL, SCORE, GAMES, WON, WINRATIO, TIME`, `ALIAS`, `GLOBAL_RANK`/`QUERY_RANK`, `NB_DISC`. There are no per-mode columns: the Neutralisation/Sabotage/Extraction tabs show the same Overall data and only the Spy/Mercenary switch changes the numbers. Data lives in `state/ladder_stats.json` and is read on every query. `GEN_SCORE` is always `SPY_SCORE + SEN_SCORE`.

**Ranked match flow (lobby port 40005).** `START_MATCH` (17) -> `MATCH_STARTED` (62) pushed to the four players. Each client sends `SUBMIT_MATCH` (30) with per-player values `[team (0 spy / 1 merc), result (1 win / -1 loss / 0 draw), present, seconds]`; the last submission wins (the game briefly submits inverted or draw values before the final one). `MATCH_FINISH` (45) -> `FINAL_MATCH_RESULTS` (71). The result rows are 22 ints per player: `arr[6]` = new total, `arr[7]` = points gained, `arr[8]` = 1 (the client reads `arr[0], arr[1], arr[6..8], arr[12], arr[13]`; the 4 submitted values must be padded to 22 or it reads past the array).

**Scoring.** +50 win, -25 loss on the role score the player held in that round (`SPY_` if team 0, else `SEN_`), floor 0. Games, wins and time are kept per role and overall. Level curve: leaving level L needs `50*L*(L+10)` points, cap 15. Not recorded: all-zero results (draw), fewer than 4 players, matches shorter than `SCCT_RANKED_MIN_SECONDS` (30).

**Results screen.** "Old Total" is the client's cached copy of its own stats (spy or mercenary column), refreshed at login (ladderquery ids 1-4) and sometimes after a match; "New Total" is Old + Results, computed by the client. The server data is correct even when the cached Old Total on screen is stale.

**Game mode detection.** The first byte of the 96-byte room info blob: `0x0d` Extraction, `0x0e` Sabotage, `0x0f` Neutralisation, independent of the map (Neutralisation on Cinema, Station, Schermerhorn; Sabotage on Station, Mount Hospital, Museum; Extraction on Station, Deftech Bellew, Warehouse). The blob is logged at match start as `MODE_INFO`. A per-mode ledger `<MODE>_SCORE/GAMES/WON/TIME` is kept next to the role totals; `tools/ladder_modes_report.py` prints it. `state/ranked_mode.txt` (`NEU`/`SAB`/`EXT`) overrides detection, `AUTO`/empty/missing detects; an unknown byte records `UNK`.

**Live lobby count.** A watcher thread (started at first login) polls every 2 s and pushes a fresh lobby list frame over each player's lobby connection whenever the count of players in the lobby changes, so the number on the Lobby Selection screen updates without re-entering. `SCCT_LOBBY_COUNT_LIVE=0` disables it.

**Dead rooms.** When a host's connection drops, the server sends three removal frames plus an authoritative room-list push (`_broadcast_removal`) so the room disappears from every client's list.

**Networking note (PS2 joining PCSX2 hosts).** Bridging PCSX2 to the server PC's own Ethernet adapter breaks DNAS and server reach; Wi-Fi bridging stops a real PS2 from joining. A second wired adapter (USB-to-Ethernet) on the same router, used only by PCSX2 (fixed IP in the LAN subnet, no gateway, interface metric 500, a unique "PS2 Address" per instance, DNS = server) works for all combinations.

**Environment variables:** `SCCT_LADDER_MODULE`, `SCCT_LADDER_REPLY`, `SCCT_LADDER_MAX_LEVEL`, `SCCT_LADDER_PTS_PER_LEVEL`, `SCCT_LADDER_PREKE1`, `SCCT_RANKED_FLOW`, `SCCT_RANKED_WIN`, `SCCT_RANKED_LOSS`, `SCCT_RANKED_MIN_PLAYERS`, `SCCT_RANKED_MIN_SECONDS`, `SCCT_RANKED_APPLY_DELAY`, `SCCT_RANKED_DIAG`, `SCCT_LOBBY_COUNT_LIVE`, `SCCT_LOBBY_COUNT_POLL`.

**Test results.** Ranked 2v2 on a real PS2 plus three PCSX2 instances, all three modes on three maps each, both team sides, Revenge (teams swap, scored per round), floors at 0, spy+merc=overall, live lobby count in both directions, dead-room removal after a host drops.
