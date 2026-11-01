# Lobby/room fixes for Chaos Theory PS2 (tested with real PS2 + PCSX2, 3 players)

- Fixes lobby leave/rejoin ("Couldn't Access Lobby"), stale rooms, map changes in the game list, game join refusal for removed rooms, and the lobby player count.
- Root cause: stray/untyped replies break the client's reply queue; notifications (34/35/44/70) now get no reply, 15/17/31/33/45 get typed replies, missing-room joins get a proper failure.
- Router (40000): wait-module address in the captured JOINWAIT reply is re-encoded with the real --wm-ip/--wm-port.
- Full details, env settings, test results, DNAS findings: docs/ONLINE_FIXES.md

Files: tools/ubigs_router_wm_server.py, tools/ubigs_router_server.py, run_test.bat, run_test_keep.bat, docs/.
Not included on purpose: logs, state/users.json, certs, local IPs (run.sh, ps1, gsinit).


Also adds persistent friend list support (add/remove, live online/offline icons), game invites (Invite Friend, Game Invites, Cancel Invite) and Join Friend from the friend list. Rooms with Friend Slots 3 can only be joined by invited friends (use Friend Slots 0 to join from the friend list); see docs/ONLINE_FIXES.md. Tested with 2 players (PCSX2 and a real PS2).
