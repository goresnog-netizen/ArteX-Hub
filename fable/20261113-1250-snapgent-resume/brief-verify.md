MISSION: Independently reproduce every acceptance claim for the Snapgen resume run and return a PASS/FAIL/PARTIAL verdict per claim with an evidence table. Read-only; never fix anything.

CONTEXT:
- Repo: C:\MRP\proyek 2026\snapgent-connector. Working tree has all changes UNCOMMITTED (branch main @ a68b4ee).
- Run ledger: C:\MRP\proyek 2026\snapgent-connector\fable\20261113-1250-snapgent-resume\ledger.md (15 rows; row 15 is the independent-verification row that you are producing the evidence for).
- Plan: C:\MRP\proyek 2026\snapgent-connector\fable\20261113-1250-snapgent-resume\plan.md.
- A live bridge currently owns ports 17613 (WebSocket) and 17614 (HTTP health) with PID 18672, started with $env:SNAPGENT_WORKSPACE='C:\MRP\proyek 2026\snapgent-connector'. Its logs are in C:\MRP\proyek 2026\snapgent-connector\bridge_run.log and bridge_run_err.log. The bridge auth secret is in config.json under bridge.auth.secret: READ IT LOCALLY to build the test header, but NEVER print the secret, never paste it into your report, and never write it to any artifact.
- Claims to verify (each must get PASS / FAIL / PARTIAL / UNVERIFIABLE + evidence pointer):
  C1 `python test_coordination.py` -> "82 passed, 0 failed", exit 0.
  C2 `python test_mesh.py` -> "26 passed, 0 failed", exit 0.
  C3 `python scripts/test_zed_mcp.py` -> OVERALL PASS (git 37 tools, shell 10, sandbox 10), exit 0.
  C4 `python scripts/test_coord_bridge.py` -> OVERALL PASS, exit 0 (needs the live bridge).
  C5 `python scripts/test_mesh_bridge.py` -> OVERALL PASS, exit 0 (needs the live bridge).
  C6 `python test_e2e.py` -> "E2E DONE", exit 0 (needs the live bridge; expects tools=103, close code 4001 unauthenticated, HTTP 401, and the git tool returning workspace root C:\MRP\proyek 2026\snapgent-connector).
  C7 `python test_bridge.py` -> "Ran 4 tests ... OK", exit 0. THIS ONE CONFLICTS WITH THE LIVE BRIDGE: it binds port 17614 and fails with WinError 10048 if the bridge is up. Do NOT kill PID 18672 without leaving the bridge restartable; instead run it with the bridge stopped ONLY if you can stop and then immediately restore the bridge (start it again with the same command and env, wait for "listening on ws://127.0.0.1:17613", then confirm ports 17613+17614 are listening and re-run C4/C5/C6 to prove the restore). If stopping/restarting is not safe in your environment, mark C7 UNVERIFIABLE with the exact port-conflict error and say so plainly.
  C8 `python -c "import coordination_hub"` exits 0 (the old NameError: name '_sliding' is not defined must be gone).
  C9 MemoryOptimizer budget behavior: re-check the four previously failing assertions (sliding "system prompt kept", append-stable "tail preserved", rolling-summary "summary block kept first", _enforce_budget "output inside default window") by reading C:\MRP\proyek 2026\snapgent-connector\coordination_hub.py and, if useful, by running targeted snippets that exercise those functions directly (write scratch scripts only under C:\Users\Brokenlink\AppData\Local\Temp\opencode\, never inside the repo).
  C10 Workspace resolution: verify that the workspace root resolves to C:\MRP\proyek 2026\snapgent-connector and NOT to a stale VS Code folder, for both bridge.py (_detect_workspace) and vscode_mcp.py (get_root). Read the code and, if the bridge is live, confirm from the live behavior (test_e2e.py T3) and/or a direct MCP stdio probe script kept OUTSIDE the repo (C:\Users\Brokenlink\AppData\Local\Temp\opencode\probe\ — note C:\Users\Brokenlink\AppData\Local\Temp\opencode\ contains an inspect.py that shadows the stdlib and breaks `import asyncio`, so run probes from C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\ instead).
  C11 shell_mcp.py Windows quoting: the shell actually executes through PowerShell on win32 and a quoted path containing spaces works (e.g. `python "C:\MRP\proyek 2026\snapgent-connector\scripts\test_zed_mcp.py"` must exit 0, and `dir "C:\MRP\proyek 2026\snapgent-connector"` must list the directory). Prove it through the MCP server, not by reading alone.
  C12 Extension packaging: `snapgent-extension/manifest.json` version == 3.10.0 and `dist\snapgentv3-3.10.0.zip` exists and contains the edited extension (read manifest.json, list the zip entries with Python's zipfile, and confirm main.js inside the zip contains the "Zed" target string).
  C13 `node --check snapgent-extension/core/main.js` exits 0.
  C14 Zed settings registration: C:\Users\Brokenlink\AppData\Roaming\Zed\settings.json contains 4 snapgent context servers (snapgent-git, snapgent-shell, snapgent-shell-sandboxed, snapgent-workspace) in agent.context_servers with SNAPGENT_WORKSPACE pointing at C:\MRP\proyek 2026\snapgent-connector. The file is JSONC (// comments, trailing commas) so parse it by stripping full-line comments and trailing commas.
  C15 docs/USAGE.md consistency: every number and path it states about servers, tool counts, test results, the Zed server list, CLI flags, the workspace-detection rules, and the release zip name must match the code and the measured results. Report each mismatch with the file:line.
  C16 No secret leakage: the bridge auth secret, tokens, or API keys must not appear in tracked-source outputs, logs committed to the repo, the packaged zip, or the run artifacts under fable\. Check without printing the secret itself (report only file:line + a redacted fingerprint such as the first 4 characters).

CONSTRAINTS:
- READ-ONLY on the repo: do not edit, create, or delete repo files. Scratch files only under C:\Users\Brokenlink\AppData\Local\Temp\ (use the snapprobe folder for anything importing asyncio). Do not git commit/push. Do not delete or move the instruction-lock directory.
- You MAY run the Python/Node test suites listed above and may run read-only inspection commands.
- If a suite needs the live bridge and the bridge is up, use it as-is.
- Report every failure with the exact command, exit code, and the literal error line. Never claim a PASS you did not observe.

DEFINITION OF DONE:
- All 16 claims have a verdict + evidence pointer (command, exit code, file:line, or quoted output).
- A written report exists at C:\MRP\proyek 2026\snapgent-connector\fable\20261113-1250-snapgent-resume\verify.md (this is the only file you may create in the repo).
- Any defect you find is reported with severity + file:line + a concrete failure scenario, but NOT fixed.

OUTPUT CONTRACT:
SUMMARY (max 6 lines) | EVIDENCE (table: claim id | verdict | command/file:line | observed result) | ARTIFACTS (paths) | RISKS | RECOMMENDED NEXT STEP

Base every statement on what you actually read, ran, or observed in this session. Label anything you could not check as UNVERIFIED.
