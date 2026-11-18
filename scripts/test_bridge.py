import subprocess
import sys
import os

# Set up paths
os.chdir(r"C:\MRP\proyek 2026\snapgent-connector")

# Run bridge.py for 5 seconds
proc = subprocess.Popen(
    [sys.executable, "bridge.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
)

import time

time.sleep(5)

if proc.poll() is None:
    print("Bridge is running (PID:", proc.pid, ")")
    proc.terminate()
else:
    print(f"Bridge exited with code {proc.returncode}")

# Get all output
stdout, _ = proc.communicate(timeout=2)
print("\n=== Bridge Output ===")
print(stdout[-3000:] if len(stdout) > 3000 else stdout)
