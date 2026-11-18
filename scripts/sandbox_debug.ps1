[CmdletBinding()]
param(
    [string]$MemoryLimitMB = "512",
    [string]$CpuLimitPercent = "50",
    [string]$MaxProcesses = "50",
    [switch]$RestrictNetwork,
    [switch]$RestrictFilesystem,
    [string[]]$AllowedPaths = @(),
    [string[]]$DeniedPaths = @(),
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$TargetArgs
)

Write-Host "[Sandbox] Args: MemoryLimitMB=$MemoryLimitMB MaxProcesses=$MaxProcesses"
Write-Host "[Sandbox] TargetArgs count: $($TargetArgs.Count)"
foreach ($a in $TargetArgs) { Write-Host "  > [$a]" }

# Build command line for dotnet/process
if ($TargetArgs.Count -eq 0) {
    Write-Error "No target command specified"
    exit 1
}
$exe = $TargetArgs[0]
$exeArgs = $TargetArgs[1..($TargetArgs.Count-1)] -join " "
Write-Host "[Sandbox] Target: $exe $exeArgs"
exit 0
