@echo off
rem Original-disc test: all 4 PORT_IDs answered, every datagram padded to an EVEN length
set SCCT_NAT_EVEN=1
call "%~dp0run_nat2_test.bat"
