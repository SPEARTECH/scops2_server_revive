@echo off
rem DNAS test for Splinter Cell: Chaos Theory (original disc). STOP the normal server first (it uses ports 53 and 443).
cd /d "%~dp0"
echo Starting dnasrep (DNS 53 + DNAS 443). Set the PS2 DNS to 192.168.1.113. Close this window to stop.
powershell -NoProfile -Command "& .\dnasrep.exe -docroot gate -certdir certs -redirect-ip 192.168.1.113 -default-region us -force-success=fallback -upstream 1.1.1.1:53 2>&1 | Tee-Object dnasrep_test.log"
pause
