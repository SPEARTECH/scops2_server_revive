@echo off
rem NAT test: the 4 NATServer entries in gsinit use ports 45000..45003, and a NAT reply
rem listener runs on each. KE1 variant 0 (baseline) is pinned so every attempt is a router
rem connection; check logs\router_40000.log "dur_ms=" (was ~15070 before) and
rem logs\udp_45000_reply.log for which NAT ports the client talks to.
rem Order: 1) this file, wait ~15 s   2) dnasrep_test\run_dnasrep_443_only.bat
set SCCT_DNAS_EXTERNAL=1
set SCCT_KE1_VARIANT=0
set SCCT_GSINIT_FILE=gsinit_nat4.php
set SCCT_NAT_PORTS=45000,45001,45002,45003
call "%~dp0run_test_keep.bat"
