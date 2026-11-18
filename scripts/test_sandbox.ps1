# Test script for windows_job_sandbox.ps1
$params = @{
    MemoryLimitMB = 256
    CpuLimitPercent = 50
    MaxProcesses = 10
    RestrictNetwork = $true
    RestrictFilesystem = $true
    AllowedPaths = @("C:\MRP\proyek 2026\snapgent-connector", "C:\Windows\System32")
    DeniedPaths = @()
}

Write-Host "=== Test 1: Basic command ===" -ForegroundColor Cyan
& "C:\MRP\proyek 2026\snapgent-connector\scripts\windows_job_sandbox.ps1" @params -- "C:\Windows\System32\cmd.exe" "/c" "echo Hello from sandbox"

Write-Host "`n=== Test 2: Python execution ===" -ForegroundColor Cyan
$params2 = $params.Clone()
$params2.MemoryLimitMB = 512
$params2.MaxProcesses = 20
# Write Python code to temp file to avoid argument parsing issues
$pyFile2 = [System.IO.Path]::GetTempFileName() + ".py"
'import sys; print("Python works:", sys.version); print("PID:", __import__("os").getpid())' | Out-File -FilePath $pyFile2 -Encoding UTF8
& "C:\MRP\proyek 2026\snapgent-connector\scripts\windows_job_sandbox.ps1" @params2 -- "C:\Users\Brokenlink\AppData\Local\Programs\Python\Python314\python.exe" $pyFile2
Remove-Item -Force $pyFile2 -ErrorAction SilentlyContinue

Write-Host "`n=== Test 3: Memory pressure test (should be limited) ===" -ForegroundColor Cyan
$params3 = @{
    MemoryLimitMB = 50
    CpuLimitPercent = 50
    MaxProcesses = 5
    RestrictNetwork = $true
    RestrictFilesystem = $true
    AllowedPaths = @("C:\MRP\proyek 2026\snapgent-connector", "C:\Windows\System32")
    DeniedPaths = @()
}
$pyFile3 = [System.IO.Path]::GetTempFileName() + ".py"
# Use bytearray to force actual memory commitment
"import sys; data = bytearray(100 * 1024 * 1024); print('Allocated 100MB')" | Out-File -FilePath $pyFile3 -Encoding UTF8
& "C:\MRP\proyek 2026\snapgent-connector\scripts\windows_job_sandbox.ps1" @params3 -- "C:\Users\Brokenlink\AppData\Local\Programs\Python\Python314\python.exe" $pyFile3
Remove-Item -Force $pyFile3 -ErrorAction SilentlyContinue

Write-Host "`n=== Test 4: Process limit test (max 3 processes) ===" -ForegroundColor Cyan
$params4 = @{
    MemoryLimitMB = 512
    CpuLimitPercent = 50
    MaxProcesses = 3
    RestrictNetwork = $true
    RestrictFilesystem = $true
    AllowedPaths = @("C:\MRP\proyek 2026\snapgent-connector", "C:\Windows\System32")
    DeniedPaths = @()
}
$pyFile4 = [System.IO.Path]::GetTempFileName() + ".py"
"import subprocess, time; procs = [subprocess.Popen(['cmd', '/c', 'ping', '-n', '10', '127.0.0.1']) for _ in range(5)]; [p.wait() for p in procs]; print('Done')" | Out-File -FilePath $pyFile4 -Encoding UTF8
& "C:\MRP\proyek 2026\snapgent-connector\scripts\windows_job_sandbox.ps1" @params4 -- "C:\Users\Brokenlink\AppData\Local\Programs\Python\Python314\python.exe" $pyFile4
Remove-Item -Force $pyFile4 -ErrorAction SilentlyContinue