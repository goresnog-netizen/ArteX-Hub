# FABLE REVIEW BRIEF (attempt 3)

MISSION: Produce an adversarial read-only review of the uncommitted working tree in C:\MRP\proyek 2026\snapgent-connector and WRITE IT TO DISK as C:\MRP\proyek 2026\snapgent-connector\fable\20261113-1250-snapgent-resume\review.md

CONTEXT:
- Repo: C:\MRP\proyek 2026\snapgent-connector (git branch main, base commit a68b4ee). Work is uncommitted: modified tracked files plus new untracked files (coordination_hub.py, mesh.py, shell_mcp.py, git_mcp.py, sandbox_job.py, pytest.ini, scripts/, core/, docs/, zed/, test_coordination.py, test_e2e.py, test_mesh.py, test_coordination.js).
- Review scope = the full uncommitted change set: `git status --porcelain`, `git diff`, and every untracked source file listed above.
- A live bridge process runs on ws://127.0.0.1:17613 and http://127.0.0.1:17614. DO NOT start, stop, kill, or restart it or any other process.

CONSTRAINTS:
- READ-ONLY on repo source. You may create exactly ONE file: C:\MRP\proyek 2026\snapgent-connector\fable\20261113-1250-snapgent-resume\review.md. Do not modify anything else in the repo.
- Scratch scripts only under C:\Users\Brokenlink\AppData\Local\Temp\ (use the subfolder C:\Users\Brokenlink\AppData\Local\Temp\snapprobe for anything that imports asyncio — the parent Temp\opencode folder contains an inspect.py that shadows the stdlib and breaks `import asyncio`).
- NEVER print or copy the value of config.json bridge.auth.secret. You may report only whether it is present/absent.
- DO NOT call the compress tool, do NOT call acp_status, do NOT try to summarize your own context. Two previous reviewer attempts died doing exactly that. Write review.md FIRST (immediately after your analysis), then reply.
- Do not run the long test suites (test_e2e.py, scripts/test_coord_bridge.py, scripts/test_mesh_bridge.py need the live bridge). You MAY run `python test_coordination.py` and `python test_mesh.py` (fast, offline) and `python -m pytest test_vscode_mcp.py -q`.

DEFINITION OF DONE:
- review.md exists on disk at the exact path above.
- Every finding has: severity (CRITICAL/HIGH/MEDIUM/LOW), file path with line number, and a concrete failure scenario (what input or state triggers it, what the wrong outcome is).
- Findings that you verified as FALSE against the live code are listed in a separate "REJECTED CANDIDATES" section with the reason, so nobody re-checks them.
- No claim without a file read or a command you actually ran in this session.

OUTPUT CONTRACT (your final chat reply, max 40 lines):
SUMMARY (max 6 lines) | EVIDENCE (file:line per finding) | ARTIFACTS (paths) | RISKS | RECOMMENDED NEXT STEP

FOCUS AREAS (verify each yourself before reporting; report only what you can prove):
1. coordination_hub.py — MemoryOptimizer strategy functions and _enforce_budget arithmetic (is the result really within budget? is any message dropped or duplicated wrongly?), ProviderManager quota/failover logic, SharedMemory replay into role="system" messages.
2. mesh.py — _peer_notes injecting peer payload text into the agent system prompt; prompt/data boundary between task.system, peer notes and task.prompt.
3. bridge.py — _detect_workspace and _folder_from_ide_storage normalization of Windows paths (raw "C:\\..." vs "file:///C:/..." vs "/c:/..."); auth token handling.
4. vscode_mcp.py — get_root() resolution order, run_command/run_tests command construction on Windows, whether tools can escape the workspace root.
5. shell_mcp.py — _split_command tokenization, _is_safe_command allowlist logic, _shell_invocation quoting.
6. git_mcp.py — write/read classification of git subcommands.
7. sandbox_job.py — job object limits and escape possibilities.
8. scripts/register_zed_mcp.py — JSONC upsert idempotency and parser robustness on Zed's real settings.json shape.
9. snapgent-extension/core/main.js — knownTool gate vs bridge-advertised tool names; Zed target wiring.
10. Test suites: do the tests assert required behavior, or do they mirror the implementation? Name any test that would still pass if the feature were broken.

Base every statement on what you actually read, ran, or observed in this session. Label anything you could not check as UNVERIFIED.
