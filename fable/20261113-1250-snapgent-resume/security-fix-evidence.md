# Security fix evidence — 20261113-1250-snapgent-resume

Project: `C:\MRP\proyek 2026\snapgent-connector`
Date: 2026-11-13
Scope: F1 (peer payload → agent prompt), F2 (shared memory replay → agent prompt, budget bypass),
F4 (workspace URI normalization). F3 (sandbox job breakaway) is listed as verified-but-not-changed-here.

## F1 — peer payload must not act as system instructions

| Item | Location | Behaviour |
|---|---|---|
| sanitize + cap | `mesh.py:538` `_sanitize_payload` | JSON-serializes the payload, caps at 600 chars, strips every control char except `\n`/`\t` (including DEL 0x7f, `mesh.py:541-546`), neutralises both `<<<PEER_FINDINGS_UNTRUSTED>>>` delimiters |
| wrap on use | `mesh.py:573` `_peer_notes` | each peer note is wrapped as `<<<PEER_FINDINGS_UNTRUSTED>>>…<<<END_PEER_FINDINGS_UNTRUSTED>>>` before it enters an agent system prompt |

Before the fix the raw peer JSON was pasted into the agent prompt unframed.

## F2 — shared memory replay and token budget

| Item | Location | Behaviour |
|---|---|---|
| sanitize | `coordination_hub.py:226` `_sanitize_untrusted` | neutralises embedded delimiters, then wraps the text in the untrusted delimiter pair |
| replay | `coordination_hub.py:239` `bootstrap_messages` | last-task redirect, long-term block and every short-term entry are emitted as `role="system"` **inside** the wrapper (`coordination_hub.py:248-289`) |
| length | `coordination_hub.py:551` `_msg_len` | string content → `len(content)`; structured content → `len(json.dumps(content, ensure_ascii=False))`; non-serializable → `len(str(content))` |
| length | `coordination_hub.py:694` `MemoryOptimizer._total` | same measure as `_msg_len` |

Before the fix: shared memory was replayed verbatim as `role="system"`, and non-string content measured
as 0 characters, so multimodal payloads bypassed the memory budget entirely.

## F3 — sandbox job containment

`sandbox_job.py:13-18` holds the job-object limit constants. `JOB_OBJECT_LIMIT_BREAKAWAY_OK`
(0x01000000) and the corresponding `SetInformationJobObject` flag were removed, so child processes
can no longer break out of the job object.

Verified by grep: no `BREAKAWAY` token remains in `C:\MRP\proyek 2026\snapgent-connector\sandbox_job.py`.

## F4 — workspace URI normalization

`bridge.py:834` `_normalize_workspace_uri`:

- plain Windows drive path (`^[A-Za-z]:[\\/]`) → used as-is, no `urlparse`
- `file://` URI → `unquote` + backslash→slash + leading `/` stripped when a drive letter follows
- any other scheme (`vscode-remote://`, `http://`, `ssh://`, …) → `None`
- path that is not an existing directory → `None`

## Regression suite

`test_security_fixes.py` (20 tests: F1×4, F2×9, F4×7) was rewritten by the lead: the original file
from the security coder called APIs that do not exist (`hub.record`, `hub.long_term`,
`hub.last_task`, `hub.bootstrap_messages` → they live on `hub.shared`; `hub._total` is a static
method on `MemoryOptimizer`; `Bridge(Path("."))` → `Bridge()`), asserted hard-coded JSON lengths
(27, not 29/25), and asserted the control-char rule against a decoded payload that JSON
escaping makes impossible to observe. Every assertion now targets the real public API and the
real serialized output.

```
$ python test_security_fixes.py
PASS f1_delimiter_sanitize_clean
PASS f1_delimiter_neutralisation
PASS f1_control_char_stripping
PASS f1_cap_600_chars
PASS f2_untrusted_wrap_short_term
PASS f2_untrusted_wrap_long_term
PASS f2_untrusted_wrap_last_task
PASS f2_delimiter_neutralisation_in_memory
PASS f2_msg_len_string_unchanged
PASS f2_msg_len_list_measured
PASS f2_msg_len_dict_measured
PASS f2_msg_len_fallback_str
PASS f2_memoryoptimizer_total_uses_json_len
PASS f4_raw_drive_path
PASS f4_file_uri_forward_slash
PASS f4_file_uri_backslash
PASS f4_file_uri_percent_encoded_spaces
PASS f4_vscode_remote_rejected
PASS f4_nonexistent_returns_none
PASS f4_non_file_scheme_rejected

RESULT: 20 passed, 0 failed
EXIT=0
```

## Full suite re-run after the fixes (2026-11-13)

| Suite | Command | Result | Exit |
|---|---|---|---|
| coordination | `python test_coordination.py` | `RESULT: 82 passed, 0 failed` | 0 |
| mesh | `python test_mesh.py` | `RESULT: 26 passed, 0 failed` | 0 |
| vscode mcp | `python test_vscode_mcp.py` | `Ran 8 tests … OK` | 0 |
| security fixes | `python test_security_fixes.py` | `RESULT: 20 passed, 0 failed` | 0 |
| pytest collection | `python -m pytest test_coordination.py test_mesh.py test_vscode_mcp.py test_security_fixes.py -q` | `28 passed, 2 warnings in 2.49s` | 0 |
| bridge | `python test_bridge.py` (bridge stopped, port 17614 free) | `Ran 4 tests in 4.385s … OK` | 0 |
| e2e | `python test_e2e.py` (bridge live) | `E2E DONE` — T1 tools=103, T2 close 4001, T3 git root = repo, T4 401, T5 4 servers | 0 |
| zed mcp | `python scripts\test_zed_mcp.py` | `OVERALL: PASS` (git 37, shell 10, sandbox 10) | 0 |
| coord bridge | `python scripts\test_coord_bridge.py` | `OVERALL: PASS` | 0 |
| mesh bridge | `python scripts\test_mesh_bridge.py` | `OVERALL: PASS` | 0 |

Bridge state after the run: `http://127.0.0.1:17614/health` → `200 {"status":"healthy","version":"3.10.0","servers":4,"active_servers":4}`, PID 19344 on 17613/17614.

## Review honesty note

The independent review gate did not produce a verdict. `fable-review` failed three times on
provider-side errors (`OpenAI Chat tool call delta is missing id or name`; a crash while
compressing its own blocks; `every candidate model refused or failed`) and the plain `reviewer`
fallback timed out waiting for response headers. The line-level reading recorded above was done by
the lead against the live files, not by an independent reviewer. `fable-verify` did complete
(15 PASS / 1 UNVERIFIABLE); its report was not written to disk and was reconstructed from the
agent's own 16-claim table in `verify.md`, with the one UNVERIFIABLE row closed by the lead
(`test_bridge.py` re-run against a stopped bridge: 4/4 OK).
