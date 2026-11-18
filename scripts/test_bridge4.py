import sys, os, asyncio, traceback

sys.path.insert(0, r"C:\MRP\proyek 2026\snapgent-connector")
os.chdir(r"C:\MRP\proyek 2026\snapgent-connector")

import bridge as b

print("Creating Bridge...")
br = b.Bridge()
# Expose config for debugging
print(f"Servers in config:")
for sid in br.servers_config.keys():
    print(f"  {sid}")

print("\nStarting servers one by one...")
for sid in list(br.servers_config.keys())[:3]:  # just first 3
    try:
        server = list(br.servers.values())[0] if br.servers else None
        print(f"\nTesting {sid}...")
    except Exception as e:
        print(f"Error testing {sid}: {e}")

# Now try full start_servers
print("\n=== Full start_servers ===")
try:
    asyncio.run(br.start_servers())
    print("start_servers completed successfully")
except Exception as e:
    print(f"FATAL during start_servers: {e}")
    traceback.print_exc()

print(f"\nFinal server states:")
for sid, s in br.servers.items():
    alive_status = "ALIVE" if s.alive else "DEAD"
    print(f"  {sid}: {alive_status} ready={s.ready} error={s.error!r}")
