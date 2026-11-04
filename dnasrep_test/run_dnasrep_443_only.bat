@echo off
rem DNAS gate only (port 443). DNS is left to the normal server, which already points dnas.playstation.org at this PC.
cd /d "%~dp0"
powershell -NoProfile -Command "& .\dnasrep.exe -docroot gate -certdir certs -dns= -https :443 -default-region us -force-success=fallback 2>&1 | Tee-Object dnasrep_443.log"
pause
