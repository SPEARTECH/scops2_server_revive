@echo off
rem Packet sniffer for the PS2 slim (192.168.1.82). RUN AS ADMINISTRATOR. Logs to logs\ps2slim_sniff.log
rem Uses .venv if present (like the server), otherwise the system python. Needs no extra packages.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" tools\sniff_ps2.py --ps2 192.168.1.82 --bind 192.168.1.113 --log logs\ps2slim_sniff.log --skip-tcp-data
pause
