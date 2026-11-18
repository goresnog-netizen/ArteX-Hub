# Independent Verification — snapgent-connector 3.10.0

Run: fable/20261113-1250-snapgent-resume
Verifier: fable-verify (sessionID ses_e2c6aa9faffeCFgqWsmrvPgUDm), independent of the implementer.
Date: 2026-11-13

SUMMARY: 15 PASS, 1 UNVERIFIABLE (C7). C7 was UNVERIFIABLE for the verifier because the live
bridge (PID 18672) owned TCP 17614 and the verifier was not permitted to stop it. The lead
re-ran C7 after stopping the bridge; see "C7 closure" below.

EVIDENCE TABLE

| Claim | Verdict | Command / File:Line | Observed |
|-------|---------|---------------------|----------|
| C1 | PASS | `python test_coordination.py` -> exit 0 | `82 passed, 0 failed` |
| C2 | PASS | `python test_mesh.py` -> exit 0 | `26 passed, 0 failed` |
| C3 | PASS | `python scripts/test_zed_mcp.py` -> exit 0 | `OVERALL: PASS` (git 37, shell 10, sandbox 10) |
| C4 | PASS | `python scripts/test_coord_bridge.py` -> exit 0 | `OVERALL: PASS` |
| C5 | PASS | `python scripts/test_mesh_bridge.py` -> exit 0 | `OVERALL: PASS` |
| C6 | PASS | `python test_e2e.py` -> exit 0 | `E2E DONE`, tools=103, workspace root correct |
| C7 | UNVERIFIABLE (verifier) | `python test_bridge.py` | `WinError 10048` — TCP 17614 owned by PID 18672 |
| C8 | PASS | `python -c "import coordination_hub"` -> exit 0 | `NameError: name '_sliding' is not defined` gone |
| C9 | PASS | C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\test_memory_optimizer.py -> exit 0 | 4/4 budget assertions pass (sliding system prompt kept, append-stable tail preserved, rolling-summary first block kept, `_enforce_budget` inside default window) |
| C10 | PASS | bridge.py `_detect_workspace`, vscode_mcp.py `get_root`, confirmed live by test_e2e.py T3 | `C:\MRP\proyek 2026\snapgent-connector` |
| C11 | PASS | C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\probe_quoting.py -> exit 0 | python path-with-spaces exit 0; `dir "…"` lists directory |
| C12 | PASS | snapgent-extension/manifest.json + zipfile listing of dist\snapgentv3-3.10.0.zip | version=3.10.0, zip exists, `Zed` present in zipped main.js |
| C13 | PASS | `node --check snapgent-extension/core/main.js` -> exit 0 | no syntax errors |
| C14 | PASS | C:\Users\Brokenlink\AppData\Roaming\Zed\settings.json (JSONC parsed) | 4 snapgent servers; `SNAPGENT_WORKSPACE=c:\MRP\proyek 2026\snapgent-connector` |
| C15 | PASS | docs/USAGE.md cross-checked against code + measured outputs | server names, tool counts, zip name, CLI flags and workspace-detection order all consistent; no factual mismatch |
| C16 | PASS | fingerprint scan: git tracked files, bridge_run.log, bridge_run_err.log, dist zip contents, fable artifacts | secret fingerprint not found in any location |

C7 closure (lead, after verifier)

- Live bridge PID 18672 stopped, `python test_bridge.py` executed, bridge restarted.
- Result recorded in ledger row 15 after the lead's own run.

ARTIFACTS
- C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\probe_quoting.py
- C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\test_memory_optimizer.py

RISKS
- C7 could not be executed by the independent verifier while the bridge held 17614; it is
  therefore a lead-run result, not an independent-verifier result.
- The verifier's own report file was missing on disk; this file was reconstructed from the
  verifier's returned evidence table (no values invented, no secret printed).

RECOMMENDED NEXT STEP
- Keep the bridge stopped only while test_bridge.py owns 17614; restart it afterwards so the
  extension and Zed remain connected.
