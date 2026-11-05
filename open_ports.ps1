# Run this script as Administrator to open all required ports for the SCOPS server
Write-Host "Adding Windows Firewall rules for SCOPS server..." -ForegroundColor Cyan

New-NetFirewallRule -DisplayName "SCOPS DNS UDP 53" -Direction Inbound -Protocol UDP -LocalPort 53 -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "SCOPS TCP Ports" -Direction Inbound -Protocol TCP -LocalPort 80,443,40000,40005,44001,44002 -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "SCOPS UDP NAT" -Direction Inbound -Protocol UDP -LocalPort 45000-45003 -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "SCOPS UDP 40000" -Direction Inbound -Protocol UDP -LocalPort 40000 -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "SCOPS UDP relay game ports" -Direction Inbound -Protocol UDP -LocalPort 3658,10070-10080 -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "SCOPS ICMP echo" -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -Action Allow -ErrorAction SilentlyContinue

Write-Host "Firewall rules added." -ForegroundColor Green
Write-Host ""
Write-Host "Verifying rules:" -ForegroundColor Cyan
Get-NetFirewallRule -DisplayName "SCOPS*" | Select-Object DisplayName, Enabled, Direction | Format-Table -AutoSize
