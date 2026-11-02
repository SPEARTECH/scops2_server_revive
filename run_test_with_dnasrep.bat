@echo off
rem Normal server (all services) but WITHOUT the old openssl 443 gate: dnasrep answers DNAS instead.
rem Order: 1) this file, wait ~15 s   2) dnasrep_test\run_dnasrep_443_only.bat
set SCCT_DNAS_EXTERNAL=1
call "%~dp0run_test_keep.bat"
