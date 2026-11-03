@echo off
rem Same as run_nat2_test.bat (NAT v2 on), but the router also sends the type-6 UDPConnectID message.
rem Attempts use KE1 variants 15, 16, 17 in turn (then 0, 1, ... if you keep going).
rem Order: 1) this file, wait ~15 s   2) dnasrep_test\run_dnasrep_443_only.bat
rem Success = "dur_ms=" in logs\router_40000.log far below 15000.
>"%~dp0state\ke1_variant.txt" echo 15
set SCCT_DNAS_EXTERNAL=1
set SCCT_KE1_VARIANT=auto
set SCCT_NAT_V2=1
call "%~dp0run_test_keep.bat"
