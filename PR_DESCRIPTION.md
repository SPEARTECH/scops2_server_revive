# Lobby/room fixes, friends, and a working ranking for Chaos Theory PS2 (tested with real PS2 + PCSX2)

- Lobby leave/rejoin ("Couldn't Access Lobby"), stale rooms, map changes in the game list, join refusal for removed rooms and the lobby player count are fixed (stray/untyped replies were breaking the client's reply queue).
- Friend list, game invites, Join Friend and the News screen work.
- **New: ranking.** Ranked 2v2 matches score +50/-25 per role (Spy / Mercenary, Overall = sum), levels up to 15, Revenge supported, results and Ranking screens show the numbers. Points, games, wins and time are also tracked per mode (Neutralisation / Sabotage / Extraction), with the mode detected automatically from the room data; `tools/ladder_modes_report.py` prints per-mode leaderboards.
- **New:** the lobby player count updates live; a room whose host disconnects is removed from every player's list; matches that end within 30 seconds are not scored.
- Router (40000): wait-module address in the captured JOINWAIT reply is re-encoded with the real --wm-ip/--wm-port.
- Networking note for PCSX2 on the server PC (second wired adapter) in the README. Full details, env settings, findings and test results: docs/ONLINE_FIXES.md

Files: tools/ubigs_router_wm_server.py, tools/ubigs_router_server.py, tools/ladder_modes_report.py, tools/start_online_capture_ext.ps1 (starts the ranking module on 44002; set HostIP), run_test.bat, run_test_keep.bat, docs/, README.md.
Not included on purpose: logs, state/ (users, friends, ladder data), certs.
