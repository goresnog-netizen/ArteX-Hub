import sys, os, traceback

sys.path.insert(0, r"C:\MRP\proyek 2026\snapgent-connector")
os.chdir(r"C:\MRP\proyek 2026\snapgent-connector")

# Monkey-patch to capture full traceback
original_excepthook = sys.excepthook


def debug_excepthook(exc_type, exc_value, exc_tb):
    print(f"\n=== CRASH ===", file=sys.stderr)
    print(f"Type: {exc_type.__name__}", file=sys.stderr)
    print(f"Value: {exc_value}", file=sys.stderr)
    traceback.print_exception(exc_type, exc_value, exc_tb, limit=None, chain=True)
    print("=== END CRASH ===\n", file=sys.stderr)
    original_excepthook(exc_type, exc_value, exc_tb)


sys.excepthook = debug_excepthook

try:
    import bridge
except Exception as e:
    print(f"Import error: {e}")
    traceback.print_exc()
    sys.exit(1)

print("Starting bridge...")
try:
    # Just test server startup, don't run full event loop
    b = bridge.Bridge()
    print(f"Workspace: {b.workspace}")
    print(f"Servers config: {list(b.servers_config.keys())}")
    print("Starting servers...")
    result = asyncio.run(b.start_servers())
    print(f"Servers started: {result}")
    for sid, s in b.servers.items():
        print(
            f"  {sid}: alive={s.alive} ready={s.ready} tools={len(s.tools)} error={s.error}"
        )
except Exception as e:
    print(f"\nError during start_servers:")
    traceback.print_exc()
