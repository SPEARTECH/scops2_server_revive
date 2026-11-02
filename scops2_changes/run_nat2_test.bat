@echo off
rem NAT v2 test (original disc). The UDP 45000 service follows the newer ubi-gs handshake:
rem   SYN -> SYN+ACK, NAT request -> PORT_ID (ping id + port), game ping -> ADDRESS.
rem KE1 variant 0 (baseline) is pinned so every attempt is a router connection.
rem Success = "dur_ms=" in logs\router_40000.log far below 15000, and "PING" lines in logs\udp_45000_reply.log.
rem Order: 1) this file, wait ~15 s   2) dnasrep_test\run_dnasrep_443_only.bat
set SCCT_DNAS_EXTERNAL=1
set SCCT_KE1_VARIANT=0
set SCCT_NAT_V2=1
call "%~dp0run_test_keep.bat"
