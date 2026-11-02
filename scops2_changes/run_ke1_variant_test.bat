@echo off
rem KE1 variant search for the ORIGINAL disc (use with dnasrep running as usual).
rem Order: 1) this file, wait ~15 s   2) dnasrep_test\run_dnasrep_443_only.bat
rem Every connection to the router gets the next KE1 reply variant (0..17, then wraps).
rem No server restart needed between tries. Results: logs\router_40000.log
rem   KE1_VARIANT n/17: ...                       = variant sent
rem   KE1_VARIANT RESULT: client SENT KE2 ...     = the client accepted it
rem NOTE: while this is on, every connection is treated as a router connection,
rem so the second (WM) leg of a real login will not work. Test only.
rem start at variant 15 (type-6 UDPConnectID variants 15,16,17; counter then wraps to 0)
>"%~dp0state\ke1_variant.txt" echo 15
set SCCT_DNAS_EXTERNAL=1
set SCCT_KE1_VARIANT=auto
call "%~dp0run_test_keep.bat"
