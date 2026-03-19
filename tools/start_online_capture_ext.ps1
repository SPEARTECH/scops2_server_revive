param(
  [string]$BindIP = '0.0.0.0',
  [string]$HostIP = '192.168.0.213',
  [string]$TlsCertCN = 'gate1.us.dnas.playstation.org',
  [string]$UpstreamDNS = '192.168.0.1:53',
  [ValidateSet('u32be','u32le','u16be','u16le','str','ack')]
  [string]$JoinWaitFormat = 'u32le',
  [ValidateSet('random','random-rawhdr','echo-client','echo-raw','echo-exact')]
  [string]$Keyex2Mode = 'random',
  [ValidateSet('random','random-rawhdr','echo-client','echo-raw','echo-exact')]
  [string]$WmKeyex2Mode = 'echo-client',
  [ValidateSet('off','login','joinwait','login+joinwait','ct-force','ct-replay','all')]
  [string]$PostKe2Push = 'off',
  [string]$PostKe2ReplayFiles = '',
  [ValidateSet('off','loginwait','loginwait+friends','lobbylogin','loginwait+friends+lobbylogin','ct-bootstrap','ct-replay','all')]
  [string]$WmPostKe2Push = 'off',
  [ValidateSet('diag','superset','current')]
  [string]$GsinitMode = 'diag',
  [ValidateSet('off','ct_ps2')]
  [string]$Ct34Profile = 'ct_ps2',
  [ValidateSet('off','light','full')]
  [string]$Scct12Extra = 'off',
  [ValidateSet('srp_nat','scct_ack','echo')]
  [string]$Udp45000ReplyMode = 'srp_nat',
  [switch]$UseFixedRsa,
  [switch]$UseExistingDnasCert,
  [switch]$TlsNoCert,
  [switch]$NoDnasOverride,
  [ValidateSet('openssl','probe')]
  [string]$DnasTlsBackend = 'openssl',
  [float]$LoginBootDelay = 0.0
)

$ErrorActionPreference = 'Stop'

function Get-ProcessInfo([int]$ProcessId) {
  try {
    return Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" |
      Select-Object ProcessId,Name,CommandLine
  } catch {
    return $null
  }
}

function Stop-UdpListenerSafe {
  param(
    [Parameter(Mandatory=$true)][int]$Port,
    [Parameter(Mandatory=$true)][string]$ExpectedCommandSubstring
  )

  $pids = Get-NetUDPEndpoint -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique

  foreach($procId in $pids) {
    $pi = Get-ProcessInfo -ProcessId $procId
    $cmd = ''
    if($pi -ne $null -and $pi.CommandLine) { $cmd = [string]$pi.CommandLine }
    if($cmd -and ($cmd -like "*$ExpectedCommandSubstring*")) {
      try {
        Stop-Process -Id $procId -Force -ErrorAction Stop
        Write-Host "  stopped UDP:$Port PID $procId" -ForegroundColor DarkGray
      } catch {
        Write-Host "  FAILED to stop UDP:$Port PID ${procId}: $($_.Exception.Message)" -ForegroundColor Yellow
      }
    } else {
      $name = ''
      if($pi -ne $null -and $pi.Name) { $name = [string]$pi.Name }
      # If Windows hides CommandLine/Path info (common when not elevated or due to policy),
      # we still want the capture stack to be restartable. These ports are very unlikely
      # to be legitimately used by other software in a typical environment.
      $expectedLooksLikeScript = ($ExpectedCommandSubstring -like "*.py")
      $looksLikePython = ($name -eq 'python.exe' -or $name -eq 'python' -or $name -eq 'pythonw.exe' -or $name -eq 'pythonw')
      if($expectedLooksLikeScript -and $looksLikePython) {
        try {
          Stop-Process -Id $procId -Force -ErrorAction Stop
          Write-Host "  stopped UDP:$Port PID $procId (python; details unavailable)" -ForegroundColor DarkGray
          continue
        } catch {
          Write-Host "  FAILED to stop UDP:$Port PID ${procId}: $($_.Exception.Message)" -ForegroundColor Yellow
          Write-Host "           If this is 'Access is denied', open an elevated PowerShell and run:" -ForegroundColor Yellow
          Write-Host "             Stop-Process -Id $procId -Force" -ForegroundColor Yellow
          Write-Host "           Then rerun tools/start_online_capture.ps1." -ForegroundColor Yellow
        }
      }

      Write-Host "  WARNING: UDP:$Port is owned by PID $procId ($name). Not stopping it automatically." -ForegroundColor Yellow
      if($cmd) { Write-Host "           cmd: $cmd" -ForegroundColor Yellow }
      throw "UDP port $Port is busy. Stop the owning process (or rerun this script elevated) and try again."
    }
  }
}

function Test-IsAdmin {
  $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if(-not (Test-IsAdmin)) {
  Write-Host 'ERROR: Run this script in an elevated PowerShell (Run as Administrator).' -ForegroundColor Red
  exit 2
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root '.venv\Scripts\python.exe'
if(-not (Test-Path $python)) {
  $python = 'python'
}

Write-Host "Using Python: $python" -ForegroundColor DarkGray
try {
  & $python -c "import rsa" | Out-Null
} catch {
  Write-Host 'ERROR: Python dependency check failed (missing module: rsa).' -ForegroundColor Red
  Write-Host '       Install it in the interpreter this script uses, e.g.:' -ForegroundColor Red
  if($python -like '*\.venv\Scripts\python.exe') {
    Write-Host "         $python -m pip install rsa" -ForegroundColor Red
  } else {
    Write-Host "         python -m pip install rsa" -ForegroundColor Red
  }
  exit 2
}

# Find an OpenSSL build that still supports TLSv1 + 3DES (needed for PS2-era DNAS).
$openssl = $null
$opensslCandidates = @(
  'C:\cygwin64\bin\openssl.exe',
  'C:\msys64\ucrt64\bin\openssl.exe',
  'C:\msys64\usr\bin\openssl.exe',
  'C:\Program Files\FreeCAD 0.21\bin\openssl.exe',
  'C:\Program Files\Git\mingw64\bin\openssl.exe'
)

foreach($cand in $opensslCandidates) {
  if(Test-Path $cand) { $openssl = $cand; break }
}

if(-not $openssl) {
  Write-Host 'ERROR: Could not find openssl.exe (needed for legacy TLS on 443).' -ForegroundColor Red
  Write-Host '       Install Cygwin/MSYS2 OpenSSL or adjust tools/start_online_capture.ps1.' -ForegroundColor Red
  exit 2
}

Write-Host "Using OpenSSL: $openssl" -ForegroundColor DarkGray

# Pick which gsinit file to serve.
$gsinitFile = 'gsinit.php'
if($GsinitMode -eq 'diag') { $gsinitFile = 'gsinit_diag_localweb.php' }
elseif($GsinitMode -eq 'superset') { $gsinitFile = 'gsinit_superset.php' }

New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root 'captures\tcp') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root 'tools\certs') | Out-Null

$dnasKey = 'tools/certs/dnas.key'
$dnasCrt = 'tools/certs/dnas.crt'
if($UseExistingDnasCert) {
  if((-not (Test-Path $dnasCrt)) -or (-not (Test-Path $dnasKey))) {
    Write-Host "ERROR: -UseExistingDnasCert was set, but missing $dnasCrt or $dnasKey" -ForegroundColor Red
    Write-Host "       Generate once with tools/gen_dnas_cert.ps1 or run without -UseExistingDnasCert." -ForegroundColor Red
    exit 2
  }
  Write-Host "Reusing existing DNAS cert/key: $dnasCrt / $dnasKey" -ForegroundColor Cyan
} else {
  Write-Host "Generating PS2-friendly TLS cert for host '$TlsCertCN' (SHA1, valid from year 2000)..." -ForegroundColor Cyan

  # We intentionally backdate notBefore to avoid "not yet valid" failures on PS2-era clocks.
  $caDir = Join-Path $root 'tools\certs\ca'
  if(Test-Path $caDir) {
    Remove-Item -Recurse -Force $caDir
  }
  if(Test-Path $dnasCrt) { Remove-Item -Force $dnasCrt }
  if(Test-Path $dnasKey) { Remove-Item -Force $dnasKey }
  New-Item -ItemType Directory -Force -Path $caDir, (Join-Path $caDir 'newcerts') | Out-Null
  if(-not (Test-Path (Join-Path $caDir 'index.txt'))) { Set-Content -Path (Join-Path $caDir 'index.txt') -Value '' -NoNewline }
  if(-not (Test-Path (Join-Path $caDir 'serial'))) { Set-Content -Path (Join-Path $caDir 'serial') -Value '01' -NoNewline }

  $cnfPath = Join-Path $caDir 'openssl.cnf'
  $dirFwd = (Resolve-Path $caDir).Path -replace '\\','/'
  @"
[ req ]
default_bits = 1024
prompt = no
distinguished_name = req_distinguished_name
req_extensions = v3_req

[ req_distinguished_name ]
CN = $TlsCertCN

[ v3_req ]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[ alt_names ]
DNS.1 = $TlsCertCN

[ ca ]
default_ca = CA_default

[ CA_default ]
dir = $dirFwd
database = $dirFwd/index.txt
new_certs_dir = $dirFwd/newcerts
serial = $dirFwd/serial
default_md = sha1
policy = policy_any
x509_extensions = v3_req

[ policy_any ]
commonName = supplied
"@ | Set-Content -Path $cnfPath

  $csrPath = Join-Path $caDir 'dnas.csr'
  & $openssl genrsa -out $dnasKey 1024 | Out-Null
  & $openssl req -new -key $dnasKey -out $csrPath -config $cnfPath | Out-Null
  & $openssl ca -selfsign -batch -config $cnfPath -extensions v3_req -in $csrPath -out $dnasCrt -keyfile $dnasKey -startdate 20000101000000Z -enddate 20500101000000Z -notext | Out-Null
}

$ports = @(80,443,40000,40005,44000,44001,44002,45000,6667,6668,27900,27901,28910,29900,29901,29920)

Write-Host 'Stopping existing listeners on key ports...' -ForegroundColor Cyan
$manualUdpPorts = @(3658,4400,41006) + (10070..10080)
$ubiUdpReplyPorts = @(45000,45001)
$gamespyUdpEchoPorts = @(27900,27901,28910,29900,29901,29920)
$udpPorts = @(53,19341) + $manualUdpPorts
foreach($p in $ubiUdpReplyPorts) { $udpPorts += $p }
foreach($p in $gamespyUdpEchoPorts) { $udpPorts += $p }

foreach($udpPort in $udpPorts) {
  try {
    if($udpPort -eq 53) { Stop-UdpListenerSafe -Port 53 -ExpectedCommandSubstring 'dns_override_forwarder.py' }
    elseif(($ubiUdpReplyPorts -contains $udpPort) -or ($gamespyUdpEchoPorts -contains $udpPort)) { Stop-UdpListenerSafe -Port $udpPort -ExpectedCommandSubstring 'udp_reply_server.py' }
    else { Stop-UdpListenerSafe -Port $udpPort -ExpectedCommandSubstring 'udp_log_server.py' }
  } catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 2
  }
}

$pids = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $ports -contains $_.LocalPort } |
  Select-Object -ExpandProperty OwningProcess -Unique

foreach($procId in $pids) {
  try {
    Stop-Process -Id $procId -Force -ErrorAction Stop
    Write-Host "  stopped PID $procId" -ForegroundColor DarkGray
  } catch {
    Write-Host "  FAILED to stop PID ${procId}: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}

Start-Sleep -Milliseconds 250

Write-Host 'Starting capture stack...' -ForegroundColor Cyan

# DNS override forwarder (UDP/53)
$dnsArgs = @(
  'tools/dns_override_forwarder.py',
  '--listen', "$BindIP`:53",
  '--upstream', $UpstreamDNS,
  '--a-suffix', "ubisoft.com=$HostIP",
  '--a-suffix', "ubi.com=$HostIP",
  '--a-suffix', "gamespy.com=$HostIP",
  '--a-suffix', "gamespy.net=$HostIP",
  '--a', "noname=$HostIP"
)
if(-not $NoDnasOverride) {
  $dnsArgs += @('--a-suffix', "dnas.playstation.org=$HostIP")
} # else: forward DNAS to upstream (returns NXDOMAIN since Sony servers are gone — no TLS attempt)
$dnsArgs += @(
  '--log',
  '--log-file', 'logs/dns_53.log'
)
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList $dnsArgs | Out-Null

# HTTP 80
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList @(
  'tools/gs_http_server.py',
  '--bind', $BindIP,
  '--port', '80',
  '--gsinit-file', $gsinitFile,
  '--web-root', 'webroot',
  '--allow-any',
  '--log-file', 'logs/gs_http_80.log'
) | Out-Null

# TLS on 443 (DNAS gate): either OpenSSL s_server or local Python DNAS probe backend.
if($DnasTlsBackend -eq 'probe') {
  if($TlsNoCert) {
    Write-Host 'WARNING: -TlsNoCert is ignored when -DnasTlsBackend probe is used.' -ForegroundColor Yellow
  }
  Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList @(
    'tools/dnas_probe_server.py',
    '--bind', $BindIP,
    '--port', '443',
    '--cert', $dnasCrt,
    '--key', $dnasKey,
    '--log-file', 'logs/tls_443.log',
    '--out-dir', 'logs/dnas_probe',
    '--fallback-raw', 'DNASrep-master/DNASrep-master/www/dnas/us-gw/error.raw',
    '--packet-dir', 'DNASrep-master/DNASrep-master/www/dnas/us-gw/packets'
  ) -RedirectStandardError 'logs/tls_443_err.log' | Out-Null
} else {
  # Default mode serves cert/key. Optional -TlsNoCert tries anonymous DH (no certificate)
  # to isolate client-side certificate validation failures.
  $tlsArgs = @(
    's_server',
    '-accept', "$BindIP`:443"
  )
  if($TlsNoCert) {
    $tlsArgs += @(
      '-nocert',
      '-cipher', 'ADH:@SECLEVEL=0'
    )
  } else {
    $tlsArgs += @(
      '-cert', $dnasCrt,
      '-key', $dnasKey,
      '-cipher', 'DES-CBC3-SHA:@SECLEVEL=0'
    )
  }
  $tlsArgs += @(
    '-bugs',
    '-legacy_renegotiation',
    '-ign_eof',
    '-state',
    '-tlsextdebug',
    '-msg'
  )
  Start-Process -WindowStyle Minimized -FilePath $openssl -WorkingDirectory $root -ArgumentList $tlsArgs -RedirectStandardOutput 'logs/tls_443.log' -RedirectStandardError 'logs/tls_443_err.log' | Out-Null
}
# TCP discovery logger (other ports)
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList @(
  'tools/tcp_log_server.py',
  '--bind', $BindIP,
  '--ports', '44000,45000,6667,6668,27900,27901,28910,29900,29901,29920',
  '--max-bytes', '1024',
  '--idle-timeout', '2',
  '--out-dir', 'captures/tcp',
  '--log-file', 'logs/tcp_ports.log'
) | Out-Null

# DNAS / general UDP discovery (DNAS has shown up as UDP/19341 in prior captures)
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList @(
  'tools/udp_log_server.py',
  '--bind', $BindIP,
  '--ports', '19341',
  '--max-bytes', '512',
  '--log-file', 'logs/udp_19341.log'
) | Out-Null

# UDP ports mentioned in the CT manual (game + ubi.com connectivity/NAT)
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList @(
  'tools/udp_log_server.py',
  '--bind', $BindIP,
  '--ports', ($manualUdpPorts -join ','),
  '--max-bytes', '512',
  '--log-file', 'logs/udp_manual_ports.log'
) | Out-Null

# UDP reply service for ubi.com connectivity/NAT (client probes UDP/45000)
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList @(
  'tools/udp_reply_server.py',
  '--bind', $BindIP,
  '--ports', ($ubiUdpReplyPorts -join ','),
  '--max-bytes', '512',
  '--reply-mode', $Udp45000ReplyMode,
  '--scct12-extra', $Scct12Extra,
  '--log-file', 'logs/udp_45000_reply.log'
) | Out-Null

# GameSpy-style UDP discovery/echo (helps reveal or satisfy additional probes)
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList @(
  'tools/udp_reply_server.py',
  '--bind', $BindIP,
  '--ports', ($gamespyUdpEchoPorts -join ','),
  '--max-bytes', '512',
  '--reply-mode', 'echo',
  '--log-file', 'logs/udp_gamespy_echo.log'
) | Out-Null

# Ubisoft GS Router on 40000
$routerArgs = @(
  'tools/ubigs_router_server.py',
  '--bind', $BindIP,
  '--port', '40000',
  '--wm-ip', $HostIP,
  '--wm-port', '40005',
  '--joinwait-format', $JoinWaitFormat,
  '--keyex2-mode', $Keyex2Mode,
  '--post-ke2-push', $PostKe2Push,
  '--log-file', 'logs/router_40000.log',
  '--save-rx-dir', 'captures/tcp/router_rx',
  '--save-tx-dir', 'captures/tcp/router_tx',
  '--ct34-profile', $Ct34Profile
)
if($LoginBootDelay -gt 0.0) {
  $routerArgs += @('--login-boot-delay', $LoginBootDelay.ToString())
}
if([string]::IsNullOrWhiteSpace($PostKe2ReplayFiles) -eq $false) {
  $routerArgs += @('--post-ke2-replay-files', $PostKe2ReplayFiles)
}
if($UseFixedRsa) {
  $routerArgs += @('--fixed-rsa-key-file', 'state/shared_router_rsa.json')
}
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList $routerArgs | Out-Null

# Ubisoft GS Router Wait Module on 40005
$routerWmArgs = @(
  'tools/ubigs_router_wm_server.py',
  '--bind', $BindIP,
  '--port', '40005',
  '--proxy-ip', $HostIP,
  '--proxy-port', '44002',
  '--keyex2-mode', $WmKeyex2Mode,
  '--post-ke2-push', $WmPostKe2Push,
  '--log-file', 'logs/router_wm_40005.log',
  '--ct34-profile', $Ct34Profile,
  '--user-db', 'state/users.json'
)
if($LoginBootDelay -gt 0.0) {
  $routerWmArgs += @('--login-boot-delay', $LoginBootDelay.ToString())
}
if([string]::IsNullOrWhiteSpace($PostKe2ReplayFiles) -eq $false) {
  $routerWmArgs += @('--post-ke2-replay-files', $PostKe2ReplayFiles)
}
if($UseFixedRsa) {
  $routerWmArgs += @('--fixed-rsa-key-file', 'state/shared_router_rsa.json')
}
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList $routerWmArgs | Out-Null

# Ubisoft GS Persistent Proxy on 44001
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList @(
  'tools/ubigs_pers_proxy_server.py',
  '--bind', $BindIP,
  '--port', '44001',
  '--wm-ip', $HostIP,
  '--wm-port', '44002',
  '--log-file', 'logs/pers_proxy_44001.log'
) | Out-Null

# Ubisoft GS Persistent Proxy Wait Module on 44002
Start-Process -WindowStyle Minimized -FilePath $python -WorkingDirectory $root -ArgumentList @(
  'tools/ubigs_pers_proxy_wm_server.py',
  '--bind', $BindIP,
  '--port', '44002',
  '--log-file', 'logs/pers_proxy_wm_44002.log'
) | Out-Null

Write-Host ''
Write-Host 'Capture stack started.' -ForegroundColor Green
Write-Host "  HTTP log:   logs/gs_http_80.log" -ForegroundColor Green
Write-Host "  DNS log:    logs/dns_53.log" -ForegroundColor Green
Write-Host "  TCP log:    logs/tcp_ports.log" -ForegroundColor Green
Write-Host "  TLS log:    logs/tls_443.log" -ForegroundColor Green
Write-Host "  TLS err:    logs/tls_443_err.log" -ForegroundColor Green
Write-Host "  UDP log:    logs/udp_19341.log" -ForegroundColor Green
Write-Host "  UDP echo:   logs/udp_gamespy_echo.log" -ForegroundColor Green
Write-Host "  Router log: logs/router_40000.log" -ForegroundColor Green
Write-Host "  Router WM:  logs/router_wm_40005.log" -ForegroundColor Green
Write-Host "  keyex2 mode (router): $Keyex2Mode" -ForegroundColor Green
Write-Host "  keyex2 mode (wm): $WmKeyex2Mode" -ForegroundColor Green
Write-Host "  post-ke2 push (router): $PostKe2Push" -ForegroundColor Green
Write-Host "  post-ke2 push (wm): $WmPostKe2Push" -ForegroundColor Green
Write-Host "  fixed rsa (shared GS/WM): $UseFixedRsa" -ForegroundColor Green
Write-Host "  reuse DNAS cert/key: $UseExistingDnasCert" -ForegroundColor Green
Write-Host "  dnas dns override disabled: $NoDnasOverride" -ForegroundColor Green
Write-Host "  tls backend: $DnasTlsBackend" -ForegroundColor Green
Write-Host "  tls no-cert mode: $TlsNoCert" -ForegroundColor Green
Write-Host "  tls cert cn: $TlsCertCN" -ForegroundColor Green
Write-Host "  ct34 profile: $Ct34Profile" -ForegroundColor Green
Write-Host "  scct12 extra: $Scct12Extra" -ForegroundColor Green
Write-Host "  udp 45000 mode: $Udp45000ReplyMode" -ForegroundColor Green
Write-Host "  Proxy log:  logs/pers_proxy_44001.log" -ForegroundColor Green
Write-Host "  Proxy WM:   logs/pers_proxy_wm_44002.log" -ForegroundColor Green
Write-Host ''
Write-Host 'Now cold-boot the game and click Login/Create once.' -ForegroundColor Cyan
Write-Host 'Then check logs/tcp_ports.log for TLS SNI or any new connects.' -ForegroundColor Cyan

























