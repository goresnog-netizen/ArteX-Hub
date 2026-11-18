# Evidence — coordination layer fixes (coordination_hub.py, mesh.py)

Run id: 20261113-1250-snapgent-resume
Project: C:\MRP\proyek 2026\snapgent-connector
Date: 2026-11-13

## Context

The interrupted session left `coordination_hub.py` unimportable:

```
$ python -c "import coordination_hub"
NameError: name '_sliding' is not defined
  File "C:\MRP\proyek 2026\snapgent-connector\coordination_hub.py", line 506, in <module>
```

`MemoryOptimizer._STRATEGIES` was a class-body dict referencing `@staticmethod`
objects that were defined *after* the class body executed.

## Changes (all in C:\MRP\proyek 2026\snapgent-connector\coordination_hub.py)

1. `ProviderConfig` — added `priority: int = 1`, `auth: str = ""`, `headers: Dict[str, str] = field(default_factory=dict)`,
   `timeout: int = 60`, `max_retries: int = 3`; `api_key` now defaults to `""`.
   Reason: `bridge.py` constructs `ProviderConfig(...)` with all of those keys.

2. `ProviderManager` — new surface required by `bridge.py` / `mesh.py` / `test_coordination.py`:
   `register`, `list_providers`, `unregister`, `get_best_provider(allow_over_quota=False, est_tokens=0)`,
   `begin_request(name)`, `end_request(name, ok=True, tokens_used=0, latency_ms=0)`,
   `report_health(name, ok=True, latency_ms=0)` (positional and keyword forms both accepted),
   `has_quota(name, est_tokens=1000)`, `remaining_quota(name)`, `is_healthy(name)`,
   `report_usage`, `reset_failure_count`.
   Usage/health records are `_Record` hybrids supporting attribute *and* item access
   (`pm.usage["a"].tokens`, `pm.health["a"].status`) while staying JSON-serializable for `stats()`.

3. `get_best_provider(est_tokens=...)` — quota filtering now uses `has_quota(name, est_tokens=est_tokens)`.
   Reason: `mesh.py:767` calls
   `self.hub.providers.get_best_provider(est_tokens=len(task.prompt) // 4, allow_over_quota=allow_over_quota)`;
   `has_quota` default `est_tokens=0` so a token check never blocks a provider that has request headroom.

4. `MemoryOptimizer` strategies rewritten (module level functions, resolved by name through
   `_STRATEGIES` after definition — no forward references):
   - `_sliding` (coordination_hub.py:533) pins the system head at index 0 and keeps the newest messages that fit.
   - `_append_stable` (coordination_hub.py:556) keeps system + first body message verbatim, digests the middle
     (`[compressed N messages]`), keeps the newest tail.
   - `_hierarchical` (coordination_hub.py:589) keeps head + digest + tail; the head message is no longer duplicated in the tail.
   - `_rolling_summary` (coordination_hub.py:622) keeps the existing summary block verbatim at index 0,
     then a fresh digest of the dropped messages, then the recent tail.
   - `_msg_len` helper (coordination_hub.py:533) centralizes the content-length unit.

5. `MemoryOptimizer._enforce_budget` (coordination_hub.py:690) rewritten:
   drops middle messages (largest first, head pair and tail protected), then truncates the largest
   remaining message with the suffix `...[truncated to fit window]` **while reserving the suffix length**,
   so the result always satisfies `tokens_after <= window - reserve`.
   Previous code appended the suffix after the binary search, which pushed the result over budget.

6. `SharedMemory` — `SHARED_MEMORY_PATH = os.path.join("state", "shared_memory.json")`,
   `bootstrap_messages()` replays short-term history as `role="system"` with content
   `"[shared-memory] %s: %s"`, `prepare()` = bootstrap + new messages then records.
   `stats()` exposes `tasks`, `last_task`, `short_messages`, `long_summaries`, `state_path`.

7. `A2AProtocol` — `A2AMessage` dataclass (`type`, `sender`, `payload`, `recipient`, `id`, `timestamp`, `reply_to`)
   with `to_dict()`, agent registry, broadcast that skips the sender, failing handlers skipped instead of fatal,
   history capped at 500, `route(msg)`.

8. `CoordinationHub.stats()` now exposes `providers`, `memory_profiles`, `a2a_messages`, `shared_memory`.

## Verification (commands run in this session)

```
$ python -c "import coordination_hub"
exit 0

$ python test_coordination.py
RESULT: 82 passed, 0 failed
exit 0

$ python test_mesh.py
RESULT: 26 passed, 0 failed
exit 0
```

Before the strategy rewrite the same suites reported `78 passed, 4 failed` (exit 1) and
`TypeError: ProviderManager.get_best_provider() got an unexpected keyword argument 'est_tokens'` (exit 1).
