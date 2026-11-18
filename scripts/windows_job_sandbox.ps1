<#
.SYNOPSIS
Windows native sandbox using Job Objects for MCP server isolation.

.DESCRIPTION
Creates a Job Object with resource limits (memory, CPU, process count),
launches the target process via .NET Process.Start(), and assigns it to the job.
Provides process isolation on Windows without Docker.

.PARAMETER MemoryLimitMB
Maximum memory in MB for the job object (default: 512)

.PARAMETER CpuLimitPercent
CPU limit as percentage (default: 50) - advisory, enforced via priority class

.PARAMETER MaxProcesses
Maximum number of processes in the job (default: 50)

.PARAMETER RestrictNetwork
Whether to restrict network access (default: $true) - advisory, enforced by bridge

.PARAMETER RestrictFilesystem
Whether to restrict filesystem access to allowed paths (default: $true) - advisory, enforced by bridge

.PARAMETER AllowedPaths
Array of allowed filesystem paths

.PARAMETER DeniedPaths
Array of denied filesystem paths

.PARAMETER Command
Command to execute (after -- separator)

.EXAMPLE
.\windows_job_sandbox.ps1 -MemoryLimitMB 512 -CpuLimitPercent 50 -MaxProcesses 50 `
  -RestrictNetwork $true -RestrictFilesystem $true `
  -AllowedPaths "C:\workspace", "C:\temp" `
  -DeniedPaths "C:\Windows", "C:\Program Files" `
  -- python git_mcp.py
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$false)]
    [int]$MemoryLimitMB = 512,

    [Parameter(Mandatory=$false)]
    [int]$CpuLimitPercent = 50,

    [Parameter(Mandatory=$false)]
    [int]$MaxProcesses = 50,

    [Parameter(Mandatory=$false)]
    [switch]$RestrictNetwork,

    [Parameter(Mandatory=$false)]
    [switch]$RestrictFilesystem,

    [Parameter(Mandatory=$false)]
    [string[]]$AllowedPaths = @(),

    [Parameter(Mandatory=$false)]
    [string[]]$DeniedPaths = @(),

    [Parameter(Mandatory=$true, ValueFromRemainingArguments=$true)]
    [string[]]$Command
)

# Find the separator "--"
$sepIndex = -1
for ($i = 0; $i -lt $Command.Count; $i++) {
    if ($Command[$i] -eq '--') {
        $sepIndex = $i
        break
    }
}

if ($sepIndex -ge 0) {
    $targetCommand = $Command[($sepIndex+1)..$Command.Length]
} else {
    $targetCommand = $Command
}

if ($targetCommand.Count -eq 0) {
    Write-Error "No target command specified after --"
    exit 1
}

$targetExe = $targetCommand[0]
$targetArgs = $targetCommand[1..($targetCommand.Count-1)]

# ============================================================
# Load required .NET assemblies
# ============================================================

Add-Type -AssemblyName System.Core
Add-Type -AssemblyName System.Runtime

# ============================================================
# Define Win32 Job Object API via C#
# ============================================================

$csharpCode = @"
using System;
using System.Runtime.InteropServices;

public static class SandboxApi {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetInformationJobObject(IntPtr hJob, uint JobObjectInformationClass, IntPtr lpJobObjectInformation, uint cbJobObjectInformationLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool TerminateJobObject(IntPtr hJob, uint uExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr hObject);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    public const uint JOB_OBJECT_LIMIT_WORKINGSET = 0x00000001;
    public const uint JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002;
    public const uint JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004;
    public const uint JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008;
    public const uint JOB_OBJECT_LIMIT_AFFINITY = 0x00000010;
    public const uint JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x00000020;
    public const uint JOB_OBJECT_LIMIT_PRESERVE_JOB_TIME = 0x00000040;
    public const uint JOB_OBJECT_LIMIT_SCHEDULING_CLASS = 0x00000080;
    public const uint JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100;
    public const uint JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200;
    public const uint JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400;
    public const uint JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800;
    public const uint JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000;
    public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    public const uint JOB_OBJECT_LIMIT_SUBSET_AFFINITY = 0x00004000;
    public const uint JOB_OBJECT_LIMIT_JOB_MEMORY_HIGH = 0x00008000;
    public const uint JOB_OBJECT_LIMIT_JOB_MEMORY_LOW = 0x00010000;

    public const uint JobObjectExtendedLimitInformation = 9;
    public const uint JobObjectBasicUIRestrictions = 4;

    public const uint BELOW_NORMAL_PRIORITY_CLASS = 0x00004000;
    public const uint INFINITE = 0xFFFFFFFF;
}
"@

Add-Type -TypeDefinition $csharpCode -Language CSharp -ReferencedAssemblies @("System.dll", "System.Core.dll")

if (-not ("SandboxApi" -as [Type])) {
    Write-Error "Failed to compile SandboxApi"
    exit 1
}

# ============================================================
# Create Job Object with limits
# ============================================================

$jobHandle = [SandboxApi]::CreateJobObject([IntPtr]::Zero, $null)
if ($jobHandle -eq [IntPtr]::Zero) {
    $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    Write-Error "[Sandbox] Failed to create Job Object: $err"
    exit 1
}

Write-Host "[Sandbox] Job Object created: $jobHandle" -ForegroundColor Green

# Configure extended limits
$limits = New-Object SandboxApi+JOBOBJECT_EXTENDED_LIMIT_INFORMATION
$limits.BasicLimitInformation.LimitFlags = [SandboxApi]::JOB_OBJECT_LIMIT_ACTIVE_PROCESS `
    -bor [SandboxApi]::JOB_OBJECT_LIMIT_JOB_MEMORY `
    -bor [SandboxApi]::JOB_OBJECT_LIMIT_PROCESS_MEMORY `
    -bor [SandboxApi]::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE `
    -bor [SandboxApi]::JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION `
    -bor [SandboxApi]::JOB_OBJECT_LIMIT_BREAKAWAY_OK `
    -bor [SandboxApi]::JOB_OBJECT_LIMIT_PRIORITY_CLASS

$limits.BasicLimitInformation.ActiveProcessLimit = [uint32]$MaxProcesses
$limits.JobMemoryLimit = [UIntPtr]::new([uint64]($MemoryLimitMB * 1024 * 1024))
$limits.ProcessMemoryLimit = [UIntPtr]::new([uint64]($MemoryLimitMB * 1024 * 1024))
$limits.BasicLimitInformation.PriorityClass = [SandboxApi]::BELOW_NORMAL_PRIORITY_CLASS

$limitsSize = [System.Runtime.InteropServices.Marshal]::SizeOf($limits)
$limitsPtr = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($limitsSize)
[System.Runtime.InteropServices.Marshal]::StructureToPtr($limits, $limitsPtr, $true)

$result = [SandboxApi]::SetInformationJobObject(
    $jobHandle,
    [SandboxApi]::JobObjectExtendedLimitInformation,
    $limitsPtr,
    [uint32]$limitsSize
)

[System.Runtime.InteropServices.Marshal]::FreeHGlobal($limitsPtr)

if (-not $result) {
    $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    Write-Error "[Sandbox] Failed to set Job Object limits: $err"
    [SandboxApi]::CloseHandle($jobHandle)
    exit 1
}

# Basic UI restrictions (no window creation, no hooks, no messages to outside)
$uiRestrictions = [uint32]0x1 -bor [uint32]0x2 -bor [uint32]0x4 -bor [uint32]0x8 -bor [uint32]0x10 -bor [uint32]0x20
$uiPtr = [System.Runtime.InteropServices.Marshal]::AllocHGlobal(4)
[System.Runtime.InteropServices.Marshal]::WriteInt32($uiPtr, $uiRestrictions)
[SandboxApi]::SetInformationJobObject(
    $jobHandle,
    [SandboxApi]::JobObjectBasicUIRestrictions,
    $uiPtr,
    4
) | Out-Null
[System.Runtime.InteropServices.Marshal]::FreeHGlobal($uiPtr)

Write-Host "[Sandbox] Job Object configured: MemoryLimit=${MemoryLimitMB}MB, MaxProcesses=${MaxProcesses}, Priority=BelowNormal" -ForegroundColor Green

# ============================================================
# Launch process via .NET Process.Start(), then assign to Job Object
# For long-running MCP servers, this works well. For very short commands,
# there's a race condition - we handle it by checking if process exited.
# ============================================================

Write-Host "[Sandbox] Launching: $targetExe $($targetArgs -join ' ')" -ForegroundColor Cyan

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $targetExe
$psi.Arguments = $targetArgs -join ' '
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.WorkingDirectory = [System.IO.Path]::GetDirectoryName($targetExe)

try {
    $process = [System.Diagnostics.Process]::Start($psi)
    if (-not $process) {
        Write-Error "[Sandbox] Process.Start returned null"
        [SandboxApi]::CloseHandle($jobHandle)
        exit 1
    }
} catch {
    Write-Error "[Sandbox] Process.Start failed: $($_.Exception.Message)"
    [SandboxApi]::CloseHandle($jobHandle)
    exit 1
}

Write-Host "[Sandbox] Process started: PID=$($process.Id)" -ForegroundColor Green

# Assign process to Job Object (with retry for race condition on fast-exiting processes)
$maxRetries = 5
$retryDelayMs = 50
$assigned = $false

for ($i = 0; $i -lt $maxRetries; $i++) {
    if ($process.HasExited) {
        Write-Warning "[Sandbox] Process already exited (code: $($process.ExitCode)), skipping assignment"
        $assigned = $true
        break
    }
    $assignResult = [SandboxApi]::AssignProcessToJobObject($jobHandle, $process.Handle)
    if ($assignResult) {
        $assigned = $true
        Write-Host "[Sandbox] Process assigned to Job Object successfully" -ForegroundColor Green
        break
    }
    $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    if ($err -eq 5) {  # ERROR_ACCESS_DENIED - process may be exiting
        Start-Sleep -Milliseconds $retryDelayMs
        continue
    }
    Write-Error "[Sandbox] AssignProcessToJobObject failed: $err"
    $process.Kill()
    [SandboxApi]::CloseHandle($jobHandle)
    exit 1
}

if (-not $assigned) {
    Write-Error "[Sandbox] Failed to assign process to Job Object after $maxRetries retries"
    $process.Kill()
    [SandboxApi]::CloseHandle($jobHandle)
    exit 1
}

# ============================================================
# Wait for process completion, capture output
# ============================================================

$output = $process.StandardOutput.ReadToEnd()
$errorOutput = $process.StandardError.ReadToEnd()

$process.WaitForExit()
$exitCode = $process.ExitCode

if ($output) { Write-Host "[Sandbox] STDOUT:`n$output" }
if ($errorOutput) { Write-Host "[Sandbox] STDERR:`n$errorOutput" -ForegroundColor Yellow }

Write-Host "[Sandbox] Process exited with code: $exitCode" -ForegroundColor Cyan

# ============================================================
# Cleanup
# ============================================================

# Job Object will auto-terminate all processes on close (KILL_ON_JOB_CLOSE)
[SandboxApi]::CloseHandle($jobHandle)

exit $exitCode