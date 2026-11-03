@echo off
rem NAT v3 slow-timing test: limit 1 PORT_ID, 400 ms before ADDRESS, repeat pings ignored.
rem Order: 1) this file, wait ~15 s   2) dnasrep_test\run_dnasrep_443_only.bat (as administrator)
set SCCT_NAT_PORTID_LIMIT=1
set SCCT_NAT_ADDRESS_DELAY_MS=400
set SCCT_NAT_NO_REPEAT_ACK=1
call "%~dp0run_nat2_test.bat"
