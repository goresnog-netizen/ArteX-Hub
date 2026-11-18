MISSION: Make `python test_coordination.py` and `python test_mesh.py` pass (exit 0, every check green) in C:\MRP\proyek 2026\snapgent-connector by fixing C:\MRP\proyek 2026\snapgent-connector\coordination_hub.py; touch mesh.py only if a test proves a mesh-side defect (read-only diagnosis first).

CONTEXT:
- Repo: C:\MRP\proyek 2026\snapgent-connector (git branch main, HEAD a68b4ee, dirty working tree = prior work, do NOT revert).
- Run id / evidence dir: C:\MRP\proyek 2026\snapgent-connector\fable\20261113-1250-snapgent-resume\ (write your evidence there, e.g. coordination-fix-evidence.md).
- Current blocker (measured): `import coordination_hub` -> NameError: name '_sliding' is not defined, raised at coordination_hub.py:506-508 because the class body builds `_STRATEGIES = {"sliding": _sliding, ...}` from @staticmethod functions defined LATER in the file.
- coordination_hub.py currently is a merge of two revisions: old ProviderManager (lines ~245-361), CoordinationHub facade (~442-498), new MemoryOptimizer (~506-635). Read the whole file before editing.
- AUTHORITATIVE CONTRACT = the test files themselves. Read them fully: C:\MRP\proyek 2026\snapgent-connector\test_coordination.py and C:\MRP\proyek 2026\snapgent-connector\test_mesh.py. Do not guess; make the code satisfy them exactly.
- Callers whose expectations must keep working (read them): bridge.py (~line 925-945 ProviderConfig construction, ~1090-1115 provider snapshot), mesh.py (~260-350, ~660-720, ~750-870 provider usage). Note: C:\MRP\proyek 2026\snapgent-connector\snapgent-app\mcp\vscode_mcp.py is a DEAD duplicate — never edit it.

REQUIRED API SURFACE (from prior-session analysis of the tests + callers; verify each against the test files yourself):
1. `ProviderConfig` dataclass must accept: endpoint, api_key (default ""), models, requests_per_minute, tokens_per_minute, max_concurrent, transform_request, transform_response, PLUS auth, headers, timeout, max_retries, priority (priority default 1). bridge.py:930 constructs it with auth=, headers=, timeout=, max_retries=, priority=; tests construct `ProviderConfig(endpoint=..., priority=3)` with no api_key.
2. `ProviderManager` must provide: register_provider(name, cfg) and alias register(name, cfg); list_providers(); unregister(name); get_best_provider(allow_over_quota=False) -> name, raising AllProvidersExhausted when nothing usable; good_provider(); begin_request(name); end_request(name, ok=True, tokens_used=0, latency_ms=0) (mesh.py calls it positionally as end_request(provider, True, tokens, latency_ms)); report_health(name, ok=True, latency_ms=0) (tests call it both positionally report_health("a", False, 100) and by keyword report_health("gemini", ok=False, latency_ms=900)); has_quota(name, estimated_tokens=1000); remaining_quota(name); is_healthy(name); report_usage(name, requests=0, tokens=0); reset_failure_count(name); stats().
3. Health statuses: healthy / degraded / down. 1 failure -> degraded and STILL usable (is_healthy True); 3 consecutive failures -> down and NOT usable; a success -> healthy and resets consecutive_failures to 0. Existing callers in bridge.py/mesh.py use status values; keep "up"/"down" compatibility where old code compares (bridge.py prints health status, mesh.py checks is_healthy).
4. usage/health entries must support BOTH attribute access and item access, because tests do `pm.usage["a"].window_start = time.time() - 61`, `pm.usage["a"].requests = 5`, `pm.usage["a"].tokens == 42`, `pm.usage["a"].errors == 0`, `pm.health["a"].status == "degraded"`, `pm.health["a"].consecutive_failures == 0`. Implement a small hybrid container class (e.g. _Record) with __getattr__/__setattr__/__getitem__/__setitem__/to_dict(); stats() must return plain JSON-serializable dicts (json.dumps must not fail).
5. Quota window: 60 seconds. After >60s since the window start, requests/tokens counters reset (test sets window_start back by 61s and expects requests == 0 afterwards). `remaining_quota(name)` returns a dict of remaining request/token/concurrent headroom (bridge.py:1106 prints it).
6. `MemoryOptimizer`: strategy lookup by NAME lazily (no forward references in the class body), each strategy callable as fn(messages, budget) (staticmethod or plain function, NOT bound with self), plus a real `_enforce_budget(messages, budget)` helper that guarantees the result fits the budget. `optimize(provider, messages, reserve=512)` returns dict with keys messages, strategy, tokens_before, tokens_after, compressed. `register_profile(provider, strategy, window)` plus presets: chatgpt -> sliding / 128000, deepseek -> append-stable, gemini -> hierarchical, qwen -> rolling-summary, default -> append-stable / 32000. The hierarchical digest marker must be lowercase "[compressed" (test checks it). hub.stats() must expose memory_profiles containing at least chatgpt and qwen, plus providers, a2a_messages, shared_memory.
7. `A2AProtocol` + `SharedMemory` + `CoordinationHub` keep working: A2AMessage(type, sender, payload, recipient=None, id, timestamp, reply_to) + to_dict; register_agent/unregister_agent; send(...) returns delivered names; broadcast skips the sender; a failing handler is skipped, not fatal; history capped at 500. SharedMemory keeps DEFAULT_SHORT_LIMIT=40, DEFAULT_LONG_LIMIT=20, state_path, load/persist/record/_promote/set_long_term/get_short_term/get_long_term, bootstrap_messages replaying prior short-term as role="system" with content "[shared-memory] %s: %s", prepare = bootstrap_messages(task) + messages then record(task, messages), stats keys tasks/last_task/short_messages/long_summaries/state_path. hub.prepare_request(provider, messages, allow_over_quota=False, task=None) returns provider/requested/switched/task/messages/strategy/tokens_before/tokens_after/compressed/shared_memory.

CONSTRAINTS:
- Python 3.12 on Windows. No new dependencies. No network calls. No Docker/CI. Edit only inside C:\MRP\proyek 2026\snapgent-connector.
- Do NOT touch: bridge.py, vscode_mcp.py, shell_mcp.py, git_mcp.py, sandbox_job.py, anything under snapgent-extension/, scripts/, docs/, or the dead snapgent-app/mcp/vscode_mcp.py. Another agent owns those files in parallel.
- Do NOT commit, push, publish, or delete files. Reversible work only.
- Smallest correct diff; match the file's existing style (type hints, docstrings only where useful).
- Do not weaken or edit the test files to make them pass. Do not add skips. Do not stub functions to return fake success.

DEFINITION OF DONE (evidence required):
- `cd C:\MRP\proyek 2026\snapgent-connector; python -c "import coordination_hub; print('IMPORT OK')"` -> exit 0, prints IMPORT OK.
- `python test_coordination.py` -> exit 0 and its own summary shows every check passed (capture the tail of output).
- `python test_mesh.py` -> exit 0, all checks passed (capture tail).
- `python -m py_compile coordination_hub.py` -> exit 0.
- If you had to change mesh.py, state exactly why, with the failing assertion before and after.

OUTPUT CONTRACT:
SUMMARY (max 6 lines) | EVIDENCE (each command + exit code + the observed output line) | ARTIFACTS (paths) | RISKS | RECOMMENDED NEXT STEP
Write the same content into C:\MRP\proyek 2026\snapgent-connector\fable\20261113-1250-snapgent-resume\coordination-fix-evidence.md and return only the summary + path.

Base every statement on what you actually read, ran, or observed in this session. Label anything you could not check as UNVERIFIED.
