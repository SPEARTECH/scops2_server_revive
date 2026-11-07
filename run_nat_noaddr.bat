@echo off
rem NAT v3 test: answer only the first NAT request with a PORT_ID, and never send the ADDRESS.
rem Order: 1) this file, wait ~15 s   2) dnasrep_test\run_dnasrep_443_only.bat (as administrator)
set SCCT_NAT_PORTID_LIMIT=1
set SCCT_NAT_NO_ADDRESS=1
call "%~dp0run_nat2_test.bat"
