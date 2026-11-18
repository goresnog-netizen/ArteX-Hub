# Plan — Resume snapgent-connector (interrupted session recovery)

Run ID: 20261113-1250-snapgent-resume
Project: C:\MRP\proyek 2026\snapgent-connector (git branch main, HEAD a68b4ee)
Source of truth for interrupted state: OpenCode DB session ses_e3bb61e3dffe6DUQ2xqpRqjR4w (last seq 19548, idle 19557 @ 2026-11-13 12:21:34)

Serves MISI goals #1 (gratis + auto-failover), #2 (hemat token + konteks tahan lama), #3 (kerja otonom terbukti), #4 (gerbang data internal), #5 (laptop sehat), #7 (bahasa Indonesia, bukti menyertai klaim).

## Current blockers (measured 2026-11-13 12:47)
- `import coordination_hub` -> NameError: name '_sliding' is not defined (coordination_hub.py:506-508, class-body forward reference to @staticmethod defined later).
- test_coordination.py / test_mesh.py both abort at that import (exit 1).
- File is a merge of two revisions: old ProviderManager (245-361) + new MemoryOptimizer (506-635) + CoordinationHub facade (442-498) defined BEFORE MemoryOptimizer.

## Milestones
M1. Rewrite coordination_hub.py: ProviderConfig fields, ProviderManager API/attribute-access entries, MemoryOptimizer lazy strategy lookup + _enforce_budget + profiles/presets. Gate: `python test_coordination.py` exit 0 AND `python test_mesh.py` exit 0.
M2. Tool layer hardening: vscode_mcp.py run_command/run_tests/run_project_script, shell_mcp.py _is_safe_command whitelist, snapgent-extension/core/main.js knownTool gate vs bridge-advertised tool names. Gate: scripts/test_zed_mcp.py + scripts/test_coord_bridge.py + scripts/test_mesh_bridge.py green.
M3. Bridge + E2E: test_bridge.py (free port 17614 first), test_e2e.py (bridge alive). Gate: 4/4 + E2E DONE.
M4. Repackage extension zip via scripts/pack_ext.py; update docs/USAGE.md + CHANGELOG if version changed.
M5. Independent verification pass (fable-verify) over all 7 suites + diff review.

## Verification commands (all must end exit 0)
python test_coordination.py
python test_mesh.py
python test_bridge.py
python test_e2e.py
python scripts/test_mesh_bridge.py
python scripts/test_coord_bridge.py
python scripts/test_zed_mcp.py

## Rollback
All work is uncommitted, in-repo, reversible. No push/publish/deploy (MISI #4). Git baseline: a68b4ee on main, uncommitted working tree preserved.
