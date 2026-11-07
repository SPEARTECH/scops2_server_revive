@echo off
rem NAT v3 test: answer only the first 3 of the 4 NAT requests with a PORT_ID.
rem Order: 1) this file, wait ~15 s   2) dnasrep_test\run_dnasrep_443_only.bat (as administrator)
set SCCT_NAT_PORTID_LIMIT=3
call "%~dp0run_nat2_test.bat"
