@echo off
rem NAT v3 test with a longer gap between the 4 PORT_ID datagrams (real PS2 handled only 2 of 4 at 25 ms).
rem Order: 1) this file, wait ~15 s   2) dnasrep_test\run_dnasrep_443_only.bat (as administrator)
set SCCT_NAT_GAP_MS=150
call "%~dp0run_nat2_test.bat"
