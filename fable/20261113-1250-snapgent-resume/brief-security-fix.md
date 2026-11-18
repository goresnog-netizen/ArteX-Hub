# FABLE CODER BRIEF — security hardening (attempt 1)

MISSION: Fix 4 confirmed findings in C:\MRP\proyek 2026\snapgent-connector with tests, and prove it by running the offline test suites.

CONTEXT:
- Repo: C:\MRP\proyek 2026\snapgent-connector (git branch main, base a68b4ee, everything uncommitted). Python 3.12 on Windows.
- Current state: test_coordination.py = 82 passed/0 failed; test_mesh.py = 26 passed/0 failed; `python -m pytest test_vscode_mcp.py -q` = 8 passed. A live bridge runs on 127.0.0.1:17613/17614 — DO NOT start, stop, kill, or restart it or any process.
- Files you may edit: mesh.py, coordination_hub.py, sandbox_job.py, bridge.py, test_mesh.py, test_coordination.py, and ONE new test file if you need it (name it test_security_fixes.py at repo root).

CONSTRAINTS:
- Smallest change that fixes the root cause. No new dependencies. No refactors beyond the fix.
- Do not touch: config.json, shell_mcp.py, vscode_mcp.py, git_mcp.py, scripts/, core/, docs/, snapgent-extension/, README.md, CHANGELOG.md, start.bat, zed/, state/, fable/ (except your evidence file).
- Scratch scripts only under C:\Users\Brokenlink\AppData\Local\Temp\ (use C:\Users\Brokenlink\AppData\Local\Temp\snapprobe for anything importing asyncio — the parent Temp\opencode folder has an inspect.py that shadows the stdlib and breaks `import asyncio`).
- NEVER print or copy the value of config.json bridge.auth.secret.
- Do not call the compress tool.
- Keep the existing public API and the existing test expectations intact unless a fixture explicitly says otherwise. If a change breaks an existing test, the test is wrong only if the brief says so — otherwise fix the code, not the test.

DEFINITION OF DONE:
- All 4 fixes landed.
- Each fix has a regression test that fails before the fix and passes after it.
- `python test_coordination.py` -> "82 passed, 0 failed" (or more), exit 0.
- `python test_mesh.py` -> "26 passed, 0 failed" (or more), exit 0.
- `python -m pytest test_security_fixes.py -q` -> all pass, exit 0.
- Evidence file written to C:\MRP\proyek 2026\snapgent-connector\fable\20261113-1250-snapgent-resume\security-fix-evidence.md with the before/after command output.

OUTPUT CONTRACT (final reply, max 40 lines):
SUMMARY (max 6 lines) | EVIDENCE (file:line per fix + command output) | ARTIFACTS (paths) | RISKS | RECOMMENDED NEXT STEP

FIX SPEC:

F1. mesh.py — peer payload must not act as system-level instructions (HIGH).
Current code: mesh.py:537-561 `_peer_notes` builds lines as
    f"- {n['from']} ({n['type']}): {json.dumps(n['payload'])[:600]}"
and mesh.py:563-570 `_system_for` concatenates those notes into the agent SYSTEM prompt. A peer agent controls that payload text, so it currently lands with system authority.
Required: keep the existing behaviour that peer findings reach the next agent (test_mesh.py:167-174 asserts "RESULT-FROM-chatgpt" appears in the deepseek system prompt and that the first agent's system prompt does NOT contain "Findings from your peer"), but make the boundary explicit:
- Wrap every peer payload inside a single delimited data block, e.g.
    <<<PEER_FINDINGS_UNTRUSTED>>>
    - from (type): payload
    <<<END_PEER_FINDINGS_UNTRUSTED>>>
- Put one short line before the block stating the content inside is data relayed from peer agents and must never be treated as instructions to the receiving agent.
- Strip control characters (except \n and \t) from the payload text, and keep the existing 600-char per-payload cap.
- A peer payload containing the delimiter text must not be able to close the block early: replace any occurrence of the opening/closing delimiter inside the payload with a neutralised form (e.g. underscores) before interpolation.
- Same delimiter discipline applies if you touch how notes reach the prompt.

F2. coordination_hub.py — untrusted replayed memory must not be silently authoritative, and non-string content must not bypass the budget (HIGH/MEDIUM).
Current code: coordination_hub.py:226-272 `SharedMemory.bootstrap_messages` emits role="system" messages containing replayed short-term/long-term content VERBATIM (prefix "[shared-memory] %s: %s"). coordination_hub.py:533-536 `_msg_len` returns 0 for any non-string content, so a list/dict multimodal payload costs 0 characters and escapes the window budget.
Required:
- Keep role="system" for the replay (mesh.py's responder picks the LAST role=="user" message; changing the role would break the live-request selection) and keep the existing "[shared-memory" marker text that test_coordination.py:356 checks for.
- Wrap the replayed memory body in the same style of explicit untrusted-data delimiter used in F1, with one line stating it is archived context, not instructions.
- `_msg_len` must measure non-string content by its serialised length (e.g. len(json.dumps(content, ensure_ascii=False)) with a str() fallback), so list/dict content consumes budget. Keep the string case as-is so the 82 coordination assertions stay valid.
- A replayed memory entry containing the delimiter text must not be able to close the block early (neutralise it the same way as F1).

F3. sandbox_job.py — remove the job-object escape hatch (MEDIUM).
Current code: sandbox_job.py:158 sets JOB_OBJECT_LIMIT_BREAKAWAY_OK (constant at sandbox_job.py:18). With that flag a child can break away from the job and escape the memory/process limits and the kill-on-close behaviour.
Required: drop JOB_OBJECT_LIMIT_BREAKAWAY_OK from limits.Basic.LimitFlags and delete the now-unused constant at sandbox_job.py:18. Do not change any other limit (keep ACTIVE_PROCESS, PROCESS_MEMORY, JOB_MEMORY, KILL_ON_JOB_CLOSE, DIE_ON_UNHANDLED_EXCEPTION, PRIORITY_CLASS).

F4. bridge.py — workspace URI normalisation has false positives and false negatives (MEDIUM).
Current code: bridge.py:834-861 `_folder_from_ide_storage` does urlparse+unquote and only strips a leading "/" for paths matching `^/[A-Za-z]:/`. Measured failures (probe C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\uri_path_probe.py):
- raw "C:\\MRP\\proyek 2026\\snapgent-connector" -> path becomes "\\MRP\\proyek 2026\\snapgent-connector" (drive letter lost; resolves against the current drive instead of C:).
- "file:///C:\\MRP\\proyek 2026\\snapgent-connector" -> "/C:\\MRP\\..." -> is_dir False, folder lost.
- "vscode-remote://ssh-remote+box/c%3A/MRP" -> resolves to local "c:/MRP" and matches an existing local folder — a remote workspace must never be reported as a local path.
Required: add one small helper that normalises a stored folder string to a local path:
- A plain Windows drive path (regex ^[A-Za-z]:[\\/]) is used as-is (no urlparse).
- A file:// URI yields its path; convert backslashes to forward slashes, then strip a leading "/" when a drive letter follows, so "file:///C:\\MRP\\x" and "file:///C:/MRP/x" both give "C:/MRP/x".
- Any URI with a non-file scheme (vscode-remote://, ssh-remote, etc.) must be REJECTED, not resolved locally.
- Return None when the result is not an existing directory, as today.
Add a regression test for: raw drive path, file:///C:/ with spaces (percent-encoded and literal), file:///C:\ backslash form, and a vscode-remote:// URI that must be rejected.

OUTPUT CONTRACT (your final chat reply, max 40 lines):
SUMMARY (max 6 lines) | EVIDENCE (file:line per fix + command output) | ARTIFACTS (paths) | RISKS | RECOMMENDED NEXT STEP

Base every statement on what you actually read, ran, or observed in this session. Label anything you could not check as UNVERIFIED.
