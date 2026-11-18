import sys, os, asyncio, traceback

sys.path.insert(0, r"C:\MRP\proyek 2026\snapgent-connector")
os.chdir(r"C:\MRP\proyek 2026\snapgent-connector")

import bridge as b

print("Creating Bridge...")
br = b.Bridge()

# Get config manually
config = br.load_config()
print(f"Servers in config: {list(config.keys())}")

print("\nStarting servers one by one with timeout...")
for sid in list(config.keys()):
    print(f"\n--- Starting {sid} ---")
    server = b.MCPServer(sid, config[sid], br.sandbox_config, br.workspace)

    async def test_start():
        try:
            success = await asyncio.wait_for(server.start(), timeout=8)
            return success
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            return False

    result = asyncio.run(test_start())
    print(f"  alive={server.alive} ready={server.ready} error={server.error!r}")
    if server.process:
        print(f"  PID={server.process.pid}")
        # Clean up
        try:
            asyncio.run(server.stop())
        except:
            pass

print("\nDone.")
