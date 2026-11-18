#!/usr/bin/env python3
"""Windows native sandbox using Job Objects — pure Python via ctypes."""

import ctypes
import json
import os
import sys
from pathlib import Path

# ─── Win32 constants ────────────────────────────────────────────────────────
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x00000020
BELOW_NORMAL_PRIORITY = 0x00004000
JobObjectExtendedLimitInformation = 9
JobObjectBasicUIRestrictions = 4
UI_RESTRICTIONS = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x20
INFINITE = 0xFFFFFFFF


def die(msg):
    print(f"[sandbox] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def escape_arg(arg):
    """Escape a single argument for CreateProcessW cmdline."""
    s = str(arg)
    needs_quote = " " in s or "\t" in s or '"' in s or s == ""
    if not needs_quote:
        return s
    parts = ['"']
    i = 0
    while i < len(s):
        if s[i] == "\\":
            cnt = 0
            j = i
            while j < len(s) and s[j] == "\\":
                cnt += 1
                j += 1
            if j < len(s) and s[j] == '"':
                parts.append("\\" * (2 * cnt + 1))
            else:
                parts.append("\\" * cnt)
            i = j
        elif s[i] == '"':
            parts.append('\\"')
            i += 1
        else:
            parts.append(s[i])
            i += 1
    parts.append('"')
    return "".join(parts)


def main():
    # Read config from JSON file
    if len(sys.argv) < 2:
        die("Usage: sandbox_job.py <config.json>")
    cfg_path = Path(sys.argv[1])
    if not cfg_path.exists():
        die(f"Config not found: {cfg_path}")
    cfg = json.loads(cfg_path.read_text())

    memory_mb = cfg.get("memoryLimitMB", 512)
    max_procs = cfg.get("maxProcesses", 50)
    exe_raw = cfg["exe"]
    exe_args = list(cfg.get("args", []))
    cwd = cfg.get("cwd") or os.getcwd()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # ─── Structs ────────────────────────────────────────────────────────────
    class JOBOBJECT_BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBEXT(ctypes.Structure):
        _fields_ = [
            ("Basic", JOBOBJECT_BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("lpReserved", ctypes.c_void_p),
            ("lpDesktop", ctypes.c_wchar_p),
            ("lpTitle", ctypes.c_wchar_p),
            ("dwX", ctypes.c_uint32),
            ("dwY", ctypes.c_uint32),
            ("dwXSize", ctypes.c_uint32),
            ("dwYSize", ctypes.c_uint32),
            ("dwXCountChars", ctypes.c_uint32),
            ("dwYCountChars", ctypes.c_uint32),
            ("dwFillAttribute", ctypes.c_uint32),
            ("dwFlags", ctypes.c_uint32),
            ("wShowWindow", ctypes.c_uint16),
            ("cbReserved2", ctypes.c_uint16),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", ctypes.c_void_p),
            ("hStdOutput", ctypes.c_void_p),
            ("hStdError", ctypes.c_void_p),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", ctypes.c_void_p),
            ("hThread", ctypes.c_void_p),
            ("dwProcessId", ctypes.c_uint32),
            ("dwThreadId", ctypes.c_uint32),
        ]

    SIZEOF_JOBOBJECT_EXTENDED = ctypes.sizeof(JOBEXT)

    # ─── Create Job Object ──────────────────────────────────────────────────
    hJob = kernel32.CreateJobObjectW(None, None)
    if not hJob:
        die(f"CreateJobObject failed (err={ctypes.get_last_error()})")
    print(f"[sandbox] Job created: handle={hJob:#x}", file=sys.stderr, flush=True)

    limits = JOBEXT()
    limits.Basic.LimitFlags = (
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        | JOB_OBJECT_LIMIT_PRIORITY_CLASS
    )
    limits.Basic.ActiveProcessLimit = max_procs
    mem_bytes = memory_mb * 1024 * 1024
    limits.ProcessMemoryLimit = mem_bytes
    limits.JobMemoryLimit = mem_bytes
    limits.Basic.PriorityClass = BELOW_NORMAL_PRIORITY

    ok = kernel32.SetInformationJobObject(
        hJob,
        JobObjectExtendedLimitInformation,
        ctypes.byref(limits),
        SIZEOF_JOBOBJECT_EXTENDED,
    )
    if not ok:
        err = ctypes.get_last_error()
        kernel32.CloseHandle(hJob)
        die(f"SetInformationJobObject failed (err={err})")
    print(
        f"[sandbox] Limits: mem={memory_mb}MB procs={max_procs}",
        file=sys.stderr,
        flush=True,
    )

    # UI restrictions
    ui_buf = (ctypes.c_uint32)(UI_RESTRICTIONS)
    kernel32.SetInformationJobObject(
        hJob, JobObjectBasicUIRestrictions, ctypes.byref(ui_buf), 4
    )

    # ─── Resolve exe ────────────────────────────────────────────────────────
    import shutil as _shutil

    exe_found = _shutil.which(exe_raw)
    if not exe_found:
        if os.path.isabs(exe_raw) and os.path.exists(exe_raw):
            exe_found = exe_raw
        else:
            die(f"Exe not found: {exe_raw!r} (searched PATH)")
    exe_full = exe_found

    # Build mutable command line (UTF-16LE)
    cmd_parts = [escape_arg(exe_full)]
    for a in exe_args:
        cmd_parts.append(escape_arg(a))
    cmdline_str = " ".join(cmd_parts)
    cmdline_buf = ctypes.create_unicode_buffer(cmdline_str)

    si = STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()

    # ─── CreateProcess (suspended) ─────────────────────────────────────────
    ok = kernel32.CreateProcessW(
        None,  # lpApplicationName
        cmdline_buf,  # lpCommandLine (mutable)
        None,
        None,
        True,  # inherit handles
        CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,  # flags
        None,  # env (inherit)
        cwd,  # cwd (LPWSTR)
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not ok:
        err = ctypes.get_last_error()
        kernel32.CloseHandle(hJob)
        die(f"CreateProcess failed (err={err})")
    pid = pi.dwProcessId
    print(f"[sandbox] Process started: PID={pid}", file=sys.stderr, flush=True)

    # ─── Assign to Job ──────────────────────────────────────────────────────
    ok = kernel32.AssignProcessToJobObject(hJob, pi.hProcess)
    if not ok:
        err = ctypes.get_last_error()
        kernel32.TerminateProcess(pi.hProcess, 1)
        kernel32.CloseHandle(hJob)
        die(f"AssignProcessToJobObject failed (err={err})")
    print("[sandbox] Assigned to Job Object OK", file=sys.stderr, flush=True)

    # ─── Resume & wait ─────────────────────────────────────────────────────
    kernel32.ResumeThread(pi.hThread)
    kernel32.CloseHandle(pi.hThread)

    kernel32.WaitForSingleObject(pi.hProcess, INFINITE)
    exit_code = ctypes.c_uint32()
    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
    print(f"[sandbox] Exited: code={exit_code.value}", file=sys.stderr, flush=True)

    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(hJob)
    sys.exit(exit_code.value)


if __name__ == "__main__":
    main()
