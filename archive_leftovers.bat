@echo off
rem Moves (does not delete) unneeded leftovers into _archive\. Safe to undo by moving files back.
cd /d "%~dp0"
set "A=%~dp0_archive"
set "LOG=%A%\archive_log.txt"
if not exist "%A%\root" mkdir "%A%\root"
if not exist "%A%\tools" mkdir "%A%\tools"
echo Archive run %date% %time% > "%LOG%"
for %%F in (run_test_b5.bat run_test_drop.bat run_test_kill.bat run_test_dnas.bat run_dnas_replacement.bat install_dnas_deps.bat dnas_step2.zip INSTALL.txt) do (
  if exist "%%F" (move "%%F" "%A%\root\" >> "%LOG%" 2>&1 & echo moved %%F >> "%LOG%")
)
if exist "Python-PS2-DNAS" (move "Python-PS2-DNAS" "%A%\root\" >> "%LOG%" 2>&1 & echo moved Python-PS2-DNAS >> "%LOG%")
for %%F in (backups.7z ubigs_router_wm_server.7z ubigs_router_wm_server.py.bak ubigs_router_wm_server.py.pre_claude ubigs_router_wm_server.py.pre_round6 ubigs_router_wm_server.py.pre_round6b ubigs_router_wm_server-1.py ubigs_router_wm_server-2.py start_online_capture_ext.ps1.pre_claude fix_indent.py) do (
  if exist "tools\%%F" (move "tools\%%F" "%A%\tools\" >> "%LOG%" 2>&1 & echo moved tools\%%F >> "%LOG%")
)
echo done >> "%LOG%"
