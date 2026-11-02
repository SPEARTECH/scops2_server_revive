# Stops everything SCOPS Server.bat started: the server's Python processes, dnasrep and their windows.
# -WatchParent : wait until the process that started this script (the SCOPS Server.bat window) exits, then clean up.
param([switch]$WatchParent)
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log  = Join-Path $root 'logs\scops_stop.log'

if ($WatchParent) {
  $me = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
  $parentId = $me.ParentProcessId
  if ($parentId) { Wait-Process -Id $parentId }
  Start-Sleep -Seconds 1
}

$pyPattern = 'tools[\\/](nat2[\\/])?(dnas_probe_server|dns_override_forwarder|gs_http_server|tcp_log_server|ubigs_\w+|udp_log_server|udp_reply_server)\.py'
$winPattern = 'run_public_test\.bat|run_nat_even\.bat|run_nat2_test\.bat|run_dnasrep_443_only\.bat'
$killed = @()

# windows by title (start "SCOPS stack" / start "SCOPS DNAS"), with everything under them
Get-Process | Where-Object { $_.MainWindowTitle -match '^(SCOPS stack|SCOPS DNAS)' } | ForEach-Object {
  taskkill /T /F /PID $_.Id | Out-Null; $killed += "window '$($_.MainWindowTitle)' $($_.Id)"
}
# the PowerShell that hosts dnasrep (Tee-Object) and any leftover run_*.bat shells
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match 'dnasrep\.exe' -and $_.CommandLine -notmatch 'scops_stop' } | ForEach-Object {
  taskkill /T /F /PID $_.ProcessId | Out-Null; $killed += "powershell $($_.ProcessId) (dnasrep)"
}
# the stack and DNAS windows (and everything under them)
Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -match $winPattern } | ForEach-Object {
  taskkill /T /F /PID $_.ProcessId | Out-Null; $killed += "cmd $($_.ProcessId)"
}
# the server's Python processes (they were started minimized and are not children of any window)
Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match $pyPattern } | ForEach-Object {
  $short = ($_.CommandLine -replace '^.*(tools[\\/][^ "]+).*$', '$1')
  taskkill /T /F /PID $_.ProcessId | Out-Null; $killed += "python $($_.ProcessId) $short"
}
# DNAS
Get-CimInstance Win32_Process -Filter "Name='dnasrep.exe'" | ForEach-Object {
  taskkill /T /F /PID $_.ProcessId | Out-Null; $killed += "dnasrep $($_.ProcessId)"
}

try {
  New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
  Add-Content -Path $log -Value ("{0}  stopped {1} process(es): {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $killed.Count, ($killed -join '; '))
} catch {}
if (-not $WatchParent) { Write-Host ("Stopped {0} process(es)." -f $killed.Count) }
