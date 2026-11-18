import sys, os, asyncio, traceback

sys.path.insert(0, r"C:\MRP\proyek 2026\snapgent-connector")
os.chdir(r"C:\MRP\proyek 2026\snapgent-connector")

import bridge as b

print("Creating Bridge...")
br = b.Bridge()
print(f"Workspace: {br.workspace}")
print(f"Servers in config: {list(br._config.get('mcpServers', {}).keys())}")

print("\nStarting servers...")
try:
    asyncio.run(br.start_servers())
except Exception as e:
    print(f"FATAL: {e}")
    traceback.print_exc()

print(f"\nServer states:")
for sid, s in br.servers.items():
    print(
        f"  {sid}: alive={s.alive} ready={s.ready} error={s.error!r} tools={len(s.tools)}"
    )
    if not s.alive and s.process is None:
        # Check if _build_command produced anything
        cmd, args, env = s._build_command()
        print(f"       command: {cmd}")
        print(f"       args: {args[:5]}...")

print(f"\nCatalog: {br._catalog[:3]}...")
