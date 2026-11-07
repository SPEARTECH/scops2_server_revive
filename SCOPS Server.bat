@echo off
title SCOPS Server launcher
cd /d "%~dp0"

rem The server needs administrator rights (and so does stopping it), so relaunch elevated if needed.
net session >nul 2>&1
if errorlevel 1 (
  echo Requesting administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo ==================================================
echo   SCOPS Server  -  server stack + DNAS in one go
echo ==================================================
echo.
echo   [I] Internet play  (run_public_test.bat, outside players allowed)
echo   [L] LAN only       (run_nat_even.bat)
echo.
choice /c IL /n /t 10 /d I /m "Choose I or L (Internet starts automatically in 10 s): "
if errorlevel 2 goto lan
goto internet

:internet
set "STACK=run_public_test.bat"
echo.
echo Mode: INTERNET. Make sure your router forwards are in place and the public IP in run_public_test.bat is current.
findstr /b /i /c:"set SCCT_PUBLIC_IP=" run_public_test.bat
goto start

:lan
set "STACK=run_nat_even.bat"
echo.
echo Mode: LAN only.

:start
if not exist "%STACK%" (
  echo ERROR: %STACK% not found in %CD%
  pause
  exit /b 1
)
if not exist "dnasrep_test\dnasrep.exe" (
  echo ERROR: dnasrep_test\dnasrep.exe not found. The DNAS answer cannot start.
  pause
  exit /b 1
)

echo.
echo Starting the cleanup watcher (closing this window stops the whole server).
if exist "%~dp0scops_stop.ps1" start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0scops_stop.ps1" -WatchParent
echo.
echo [1/3] Starting the server stack in its own window...
start "SCOPS stack" cmd /c call "%STACK%"

echo [2/3] Waiting for the lobby server on port 40005 (up to 60 s)...
powershell -NoProfile -Command "$t=0; while($t -lt 60){ if(Get-NetTCPConnection -State Listen -LocalPort 40005 -ErrorAction SilentlyContinue){exit 0}; Start-Sleep -Seconds 1; $t++}; exit 1"
if errorlevel 1 (
  echo WARNING: port 40005 did not come up in 60 s. Check the SCOPS stack window and logs\router_wm_40005.log.
  echo Starting DNAS anyway.
) else (
  echo Lobby server is up.
)
timeout /t 3 /nobreak >nul

echo [3/3] Starting DNAS on port 443 in its own window...
start "SCOPS DNAS" /d "%~dp0dnasrep_test" cmd /c run_dnasrep_443_only.bat

echo.
echo ==================================================
echo   SCOPS Server is running.
echo   Press any key here to STOP everything (server,
echo   all its Python processes and DNAS).
echo   Closing this window with the X does the same.
echo ==================================================
echo.
pause >nul
echo Stopping...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scops_stop.ps1"
timeout /t 3 >nul
