# WebSocket Migration Evidence — 2026-11-13

Objective (operator, m01282): remove the `websockets.server.WebSocketServerProtocol is deprecated`
DeprecationWarning printed by the running bridge at `bridge.py:43`, without breaking the bridge,
the WebSocket handshake, auth, origin checks, rate limiting, or the existing suites.

## Import block (bridge.py:41-53, after the change)

```python
try:
    import websockets
    from websockets.asyncio.server import ServerConnection, serve as websockets_serve
except ImportError:
    try:
        import websockets
        from websockets.legacy.server import WebSocketServerProtocol as ServerConnection

        websockets_serve = websockets.serve
    except ImportError:
        websockets = None
        ServerConnection = None
        websockets_serve = None
```

The legacy branch is reached only when `websockets.asyncio.server` is missing (websockets <= 12),
where the legacy module is not deprecated. On websockets 13+ the legacy module is never imported,
so no deprecation warning can be emitted.

## Changed lines in bridge.py (current line numbers, verified by grep after the edit)

| Line | Change |
|------|--------|
| 41-53 | Import block rewritten to the two-branch form above (`ServerConnection`, `websockets_serve`) |
| 780 | `self.clients: Set[WebSocketServerProtocol]` -> `Set[ServerConnection]` |
| 781 | `self.client_info: Dict[WebSocketServerProtocol, ...]` -> `Dict[ServerConnection, ...]` |
| 1170 | `_coord_prepare(self, websocket: ServerConnection, request: Dict[str, Any])` |
| 1220 | `_a2a_send(self, websocket: ServerConnection, ...)` |
| 1292 | `_mesh_create_project(self, websocket: ServerConnection, ...)` |
| 1332 | `_mesh_run_project(self, websocket: ServerConnection, ...)` |
| 1493 | `_mesh_park_open_tasks(self, websocket: ServerConnection, ...)` |
| 1519 | `_mesh_task_result(self, websocket: ServerConnection, ...)` |
| 1552 | `_mesh_status(self, websocket: ServerConnection, ...)` |
| 1586 | `_mesh_send_agent(self, websocket: ServerConnection, ...)` |
| 1634 | `_send_client(self, client: ServerConnection, ...)` |
| 1642 | `handle_client(self, websocket: ServerConnection, _path: Optional[str] = None)` — the optional second parameter is kept so the legacy two-argument handler call still works; the asyncio implementation calls the handler with one argument |
| 1764 | `_handle_request(self, websocket: ServerConnection, ...)` |
| 1867 | `_call_tool(self, websocket: ServerConnection, ...)` |
| 2066 | `_change_server(self, websocket: ServerConnection, ...)` |
| 2381 | Runtime guard `if websockets is None:` -> `if websockets_serve is None:` (same RuntimeError text) |
| 2393 | `await websockets.serve(...)` -> `await websockets_serve(...)` |

`grep WebSocketServerProtocol bridge.py` now matches only line 47, inside the legacy fallback
import. Handshake, Origin/Authorization header reading, token query fallback, rate limiting,
`except websockets.exceptions.ConnectionClosed` and `BRIDGE_VERSION = "3.10.0"` are untouched.

## Verification

### Implementing agent (fable-coder, session ses_e2b215cdfffeawOCO12fmtvUyD)

| Command | Exit | Observed |
|---------|------|----------|
| `python -W error::DeprecationWarning -c "import bridge"` | 0 | no warning raised |
| `python -c "import bridge, websockets; print(bridge.ServerConnection.__name__)"` | 0 | `ServerConnection` |
| `python -m py_compile bridge.py` | 0 | ok |
| `grep "WebSocketServerProtocol" bridge.py` | 1 | only line 47 (legacy fallback) |
| `python -m pytest test_coordination.py test_mesh.py test_security_fixes.py -q` | 0 | `20 passed in 1.42s` |

### Lead, independent re-run against the live bridge

| Command | Exit | Observed |
|---------|------|----------|
| `python -W error::DeprecationWarning -c "import bridge, websockets; print('IMPORT_OK', bridge.ServerConnection.__name__, websockets.__version__)"` | 0 | `IMPORT_OK ServerConnection 17.2` |
| `python test_bridge.py` (bridge stopped, port 17614 free) | 0 | `Ran 4 tests in 2.212s` / `OK` |
| `python test_e2e.py` (bridge live) | 0 | `E2E DONE` — T1 `type: connected | tools: 103 | mcp_alive: True`; T2 close `4001`; T3 git root = repo, `is_git_repository: true`; T4 `HTTP 401`; T5 servers `vscode/git/shell/zed` |
| `python scripts/test_coord_bridge.py` | 0 | `OVERALL: PASS` (10 PASS lines) |
| `python scripts/test_mesh_bridge.py` | 0 | `OVERALL: PASS` (13 PASS lines) |
| `GET http://127.0.0.1:17614/health` | 0 | 200 `{'status': 'healthy', 'version': '3.10.0', 'servers': 4, 'active_servers': 4}` |

Differential proof that the warning is gone at runtime: the pre-fix bridge (background shell
`sh_1d457b6ad001Ne5er5zdo1ympg`, PID 19344) wrote
`bridge.py:43: DeprecationWarning: websockets.server.WebSocketServerProtocol is deprecated` into its
captured stderr file. The restarted bridge (background shell `sh_1d4e6876300143M6xq1B7lC0Rq`,
same command, same redirection) has an empty capture file (0 lines) while serving all four MCP
servers and passing the three live suites above.

Durable-capture confirmation (the shell capture file of that run was deleted together with the
dead shell, so the bridge was relaunched with permanent redirection):
`python -u bridge.py > bridge_live.out 2> bridge_live.log`, background shell
`sh_1d4f8d0cb001jAXXHl4ZfGsXN5`. `bridge_live.log` (stderr) and `bridge_live.out` (stdout) are both
0 lines after startup and after the full suite run below — the DeprecationWarning used to be the
very first line on stderr. Bridge logging itself goes to `logs/bridge_debug.log`
(`bridge.py:91 LOG_PATH`, `bridge.py:122 logging.basicConfig(filename=...)`), which is why the
process streams are empty: only warnings and tracebacks ever reach stderr.

### Stability after the first migrated instance died

[FAKTA] The first migrated instance (background shell `sh_1d4e6876300143M6xq1B7lC0Rq`) served all
three live suites and then stopped answering `/health` (WinError 10061) with the shell reporting
`Shell.NotFoundError`. Its capture file was deleted with the dead shell, so no traceback survived.

[FAKTA] No code path exits silently: `bridge.py:2433-2447` installs SIGINT/SIGTERM handlers that
set `stop_event`, and the `finally` block always logs `bridge stopped` — no such line exists in
`logs/bridge_debug.log` for either live instance. `Select-String` over the whole log for
ERROR/CRITICAL/Traceback/FATAL returns nothing after 2026-11-13 03:28. The bridge log shows the
migrated instance starting at 21:34:01 ("ready 103 tools available (4 MCP server(s))"), the suites
running, and then no shutdown record.

[INFERENSI] The death was the shell tool losing its shell handle (`Shell.NotFoundError`) and
reaping the process tree, not a code regression in the migration: the process was terminated
without running its shutdown handler, no exception was recorded, and no exception path in
`bridge.py` terminates the process without logging.

[HASIL-UJI] Relaunched instance against the identical code: startup -> 4 MCP servers up, 103 tools
ready (log 21:53:59-21:54:00); `/health` -> 200 `{'status': 'healthy', 'uptime_seconds': 244,
'servers': 4, 'active_servers': 4, 'requests_errors': 0, 'servers_failed': 0}`; `test_e2e.py` ->
E2E DONE (103 tools, 4 servers); `scripts/test_coord_bridge.py` -> OVERALL: PASS; 
`scripts/test_mesh_bridge.py` -> OVERALL: PASS; stderr and stdout captures still 0 lines after
the suites. `test_bridge.py` (4/4 OK) had already been run against this exact code before the
first migrated instance was started.

## Independent review

No independent review agent verdict exists for this change. The change was executed by
fable-coder and re-verified by the lead (lead does not edit specialist code); the adversarial
review gate remains unavailable because every reviewer attempt in this run failed on provider
errors (see `ledger.md` row 15). Residual risk: the legacy fallback branch (websockets <= 12) was
not exercised, because the machine runs websockets 17.2.
