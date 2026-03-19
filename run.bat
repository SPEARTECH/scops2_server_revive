@echo off
powershell -ExecutionPolicy Bypass -File "%~dp0tools\start_online_capture_ext.ps1" -JoinWaitFormat u32le -Keyex2Mode echo-client -WmKeyex2Mode echo-client -PostKe2Push off -WmPostKe2Push off -Ct34Profile ct_ps2 -UseFixedRsa
pause
