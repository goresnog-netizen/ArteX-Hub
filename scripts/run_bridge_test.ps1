# Run bridge.py and capture full output
$logFile = "C:\MRP\proyek 2026\snapgent-connector\bridge_full.log"

$proc = Start-Process -FilePath "python" -ArgumentList "bridge.py" -WorkingDirectory "C:\MRP\proyek 2026\snapgent-connector" -PassThru -NoNewWindow

Start-Sleep -Seconds 5

Write-Host "Bridge process state:" -ForegroundColor Cyan
Write-Host "  PID: $($proc.Id)"
Write-Host "  HasExited: $($proc.HasExited)"
if ($proc.HasExited) { Write-Host "  ExitCode: $($proc.ExitCode)" }

Write-Host "`n=== Full Bridge Log ===" -ForegroundColor Yellow
Get-Content $logFile 2>$null

Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
