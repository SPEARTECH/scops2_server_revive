@echo off
set "OUT=%~dp0hash_results.txt"
echo Hash run %date% %time% > "%OUT%"
powershell -NoProfile -Command "foreach($p in 'C:\Users\Dorian\Desktop\CT Patched ISO\CT_patched_online_universal.iso','C:\PS2 ISOS\DVD\old ps2 scct\OG Chaos theory\Tom Clancy''s Splinter Cell - Chaos Theory (USA) (En,Fr,Es).iso'){ if(Test-Path -LiteralPath $p){ $i=Get-Item -LiteralPath $p; $h=Get-FileHash -LiteralPath $p -Algorithm SHA256; $m=Get-FileHash -LiteralPath $p -Algorithm MD5; ('{0}|{1}|SHA256={2}|MD5={3}' -f $i.Name,$i.Length,$h.Hash,$m.Hash) | Add-Content '%OUT%' } else { ('MISSING '+$p) | Add-Content '%OUT%' } }"
echo done >> "%OUT%"
