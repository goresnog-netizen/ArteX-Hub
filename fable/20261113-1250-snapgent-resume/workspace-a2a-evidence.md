# Workspace + Zed A2A Evidence — 2026-11-15

Objectives (operator):

- m01654 — fix the extension showing "koneksi tidak tersambung" and make the extension show/use the REAL VS Code / Zed workspace for the selected target instead of the `snapgent-connector` repo.
- m01549 — one double-click of `start.bat` must wire the Zed path so Zed IDE agents can A2A-cooperate with the web-model agents.

All commands below were run in this workspace on Windows 10 Pro 19045 / Python 3.12.10 / Node (node --check).

## 1. Root cause of "koneksi tidak tersambung"

[FAKTA] `logs/bridge_debug.log` recorded a refused token request every ~5 seconds:

```
GET /auth-token HTTP/1.1" 403 188
```

[FAKTA] `bridge.py:2576-2629` (`auth_token_handler`) accepts only the pinned extension origin:

```python
allowed_origins = {f"chrome-extension://{expected_ext_id}", f"moz-extension://{expected_ext_id}"}
if not origin or origin not in allowed_origins:
    ... 403 json {"error":"Forbidden","reason":"extension_origin_mismatch", ...}
```

[FAKTA] The browser was still running the previous unpacked build, whose id differed from the pinned id, so every poll was refused.

[HASIL-UJI] Diagnostic `C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\diag_ext.py` -> `computed_ext_id == pinned_ext_id` -> `match: True`, id `hpeolgkpbdcicahifcgainojfilbgmnb` (manifest `key` field and `config.json` → `bridge.security.extensionId`).

[AKSI] `bridge.py:2612-2621` now answers a refused origin with a diagnosable body:

```json
{"error":"Forbidden","reason":"extension_origin_mismatch",
 "expected_origin":"chrome-extension://hpeolgkpbdcicahifcgainojfilbgmnb",
 "seen_origin":"...","hint":"Reload the unpacked extension (chrome://extensions -> Reload) ..."}
```

[AKSI] `snapgent-extension/background.js` keeps the failure in `authError` (`{reason, expected, seen, hint}`) and publishes it through `statusObj()`, so the panel shows the actual cause instead of a generic offline badge.

[AKSI] `start.bat` prints the pinned id and the reload instruction:

```
Extension id pinned: hpeolgkpbdcicahifcgainojfilbgmnb
If the panel shows "bridge offline", reload the extension once:
chrome://extensions -> ArteX-TNT -> Reload
```

Operator action still required once in the browser: `chrome://extensions` → ArteX-TNT → **Reload** (or remove and re-load unpacked from `C:\MRP\proyek 2026\snapgent-connector\snapgent-extension`).

## 2. Per-IDE workspace resolution

[AKSI] `bridge.py`:

| Line | Function | Behaviour |
|---|---|---|
| 835 | `_detect_workspace()` | env override → running IDE folder → bridge root |
| 856 | `_ide_workspace_root(ide)` | `vscode`/`code`/`code - insiders`/`vscodium`/`cursor` → `%APPDATA%\<Product>\User\globalStorage\storage.json`; `zed` → `_zed_workspace()` |
| 888 | `_zed_workspace()` | read-only SQLite on `%LOCALAPPDATA%\Zed\threads\threads.db`, `SELECT folder_paths FROM threads WHERE folder_paths IS NOT NULL AND folder_paths <> '' ORDER BY updated_at DESC LIMIT 20`, newest existing directory wins |
| 923 | `_server_workspace(server_id)` | the `vscode` and `zed` servers run against their own IDE folder; other servers share the global workspace |
| 1210 | `_server_status()` | emits `"workspace": str(getattr(server, "workspace", self.workspace))` per server |

[AKSI] Each MCP child is started with `SNAPGENT_WORKSPACE` and `SNAPGENT_WORKSPACE_ROOT` set to that server's own workspace (`bridge.py:538-539`).

[AKSI] `snapgent-extension/core/main.js` generalises every VS Code-only branch:

- `WORKSPACE_TARGETS = ["vscode", "zed", "cursor"]` + `isWorkspaceTarget(id)` (main.js:102-103)
- workspace probe (list_dir on `.`), start-flow workspace gate, skills load gate, skills note text and the badge bar all key on `isWorkspaceTarget(selectedTargetId)`
- the badge Ready branch prints the resolved workspace path; `list_mcp_servers` appends ` - workspace: ${sv.workspace}`

[AKSI] `scripts/register_zed_mcp.py` gained `detect_workspace()` (same Zed thread database read) and registers a fifth server, `snapgent-a2a`, pointing at `zed_a2a_mcp.py` with `SNAPGENT_WORKSPACE`, `SNAPGENT_BRIDGE_URL=ws://127.0.0.1:17613` and `SNAPGENT_AGENT_NAME=zed`.

[AKSI] `start.bat` runs `scripts\register_zed_mcp.py` automatically (non-fatal: a missing Zed or settings file only prints a NOTE) and then `scripts\check_bridge_health.py --extension-id`.

[HASIL-UJI] Live bridge, `GET http://127.0.0.1:17614/health` -> `{"status":"healthy","version":"3.10.0","servers":4,"active_servers":4}`.

[HASIL-UJI] Live per-server workspace (`GET /servers` with the local bearer token) and the WS `connected` frame:

```
vscode -> C:\MRP\kumpulan MCP\Skills\No Detect\IndoHunt
zed    -> C:\MRP\kumpulan MCP\Skills\No Detect
git    -> C:\MRP\kumpulan MCP\Skills\No Detect\IndoHunt
shell  -> C:\MRP\kumpulan MCP\Skills\No Detect\IndoHunt
```

No server points at `C:\MRP\proyek 2026\snapgent-connector` any more.

## 3. Zed as a first-class A2A agent

[AKSI] `mesh.py`:

- `register_agent_inbox(agent)` gives an external agent (here `zed`) its own A2A inbox.
- `resolve_peer(peer)` returns the name unchanged when it is already a registered A2A agent, otherwise `provider:<name>`. Before this change a peer called `zed` was rewritten to `provider:zed` and silently dropped; `send_between_agents` and the peer-result routing now both go through `resolve_peer`.

[AKSI] `bridge.py` registers the inbox right after the mesh is created (`bridge.py:818`) and exposes two new websocket message kinds:

- `a2a_agents` -> `{"type":"a2a_agents","agents":[...],"inbox_depth":{...}}`
- `a2a_inbox` -> read or drain the inbox of one agent (`{"agent":"zed","drain":true}`)

[AKSI] `zed_a2a_mcp.py` (461 lines) is a new stdio MCP server that speaks the bridge websocket protocol and exposes ten tools to Zed:

`bridge_health`, `a2a_agents`, `a2a_inbox`, `a2a_send`, `coord_prepare`, `coord_stats`, `mesh_create_project`, `mesh_run_project`, `mesh_status`, `mesh_send_agent`.

[BUG-FIXED] `zed_a2a_mcp.py` `_send_recv` read only the first websocket frame, but the bridge sends a `connected` handshake first, so every call died with `id mismatch: expected N, got None`. It now loops until the frame id matches, ignoring handshake and broadcast frames, with a `time.monotonic()` deadline.

[BUG-FIXED] `_tool_bridge_health` used `with asyncio.timeout(3)` (a sync context manager) and raised `'Timeout' object does not support the context manager protocol`. It now uses `async with asyncio.timeout(3)`.

[HASIL-UJI] Live websocket against the running bridge (`C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\live_verify.py`, exit 0):

```
a2a_agents -> ['bridge','operator','provider:chatgpt','provider:deepseek','provider:gemini','provider:qwen','zed']
a2a_send   -> {"type":"a2a_ack","delivered":"zed","agents":["zed"]}
a2a_inbox  -> read=1 message, then drain=1
mesh_send_agent -> {"type":"mesh_agent_message","sent":{"ok":true,"delivered":["zed"]}}
mesh_status -> mesh_list
```

[HASIL-UJI] Real stdio MCP round-trip against the live bridge (`C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\zed_mcp_live.py`, exit 0): initialize -> `serverInfo name=zed-a2a-mcp 1.0.0`; `tools/list` -> 10 tools; `bridge_health` -> ok=true, status=healthy, version 3.10.0; `a2a_agents` -> includes `zed`; `a2a_send` -> ok=true, agents=["zed"]; `a2a_inbox` -> messages then drain; `mesh_status` -> ok. No token or secret appeared in any payload.

## 4. Regression coverage

[AKSI] New suite `test_workspace_a2a.py` (stdlib only, no live bridge) covers:

- `_ide_workspace_root("vscode")` and `_ide_workspace_root("zed")` return an existing directory that is NOT inside the `snapgent-connector` repo
- `_server_workspace("zed")` equals the Zed IDE root, `_server_workspace("git")` equals the global workspace
- `_server_status()` exposes a `workspace` field per server
- `ProjectMesh.resolve_peer` maps `zed` -> `zed`, `chatgpt` -> `provider:chatgpt`, `""` -> `""`
- `register_agent_inbox` is idempotent
- the 403 body carries `reason`, `expected_origin`, `seen_origin`, `hint`; the 200 body carries the keys `token` and `enabled` (values are never printed)

[HASIL-UJI] `python test_workspace_a2a.py` -> `RESULT: 6 passed, 2 skipped, 0 failed`, exit 0. The two skips are the auth-handler checks: they need the health port free and the live bridge was holding `127.0.0.1:17614` (`[Errno 10048] ... only one usage of each socket address ... is normally permitted`).

[AKSI] `test_zed_a2a_mcp.py` — the "no bridge" case now points the health URL at a closed port (`http://127.0.0.1:1/health`) and restores the previous environment value in a `finally` block, so it no longer depends on the live bridge being stopped.

[AKSI] `test_oep_e2e.py` — the encrypted tool call now uses `git_workspace_info` (unique to the git server, takes no arguments) instead of reading `bridge.py` by relative path. The vscode server is now rooted at the real IDE folder, where `bridge.py` does not exist; the test follows the workspace instead of the old repo layout. The wire-frame plaintext audit is unchanged.

[HASIL-UJI] `python test_oep_e2e.py` -> `connected: tools=103 / oep established / envelope length: 153 bytes / result envelope length: 273 bytes / decrypted result: 110 bytes of workspace info, ok=True / PASS`, exit 0.

## 5. Suite runs

[HASIL-UJI] `python C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\run_suites.py --out ...\suites_round3.json` -> 14 PASS / 1 FAIL (exit 1), the single failure being `test_oep_e2e.py` described above.

[HASIL-UJI] After the two repairs the same runner reports the round recorded in `suites_round4.json` (see the "Final suite run" section of this file, written after the last full run).

## 6. Residual risks

- The extension must be reloaded once in the browser after this change; until then the panel keeps showing the refused-origin state.
- `_zed_workspace()` reads Zed's thread database read-only. If Zed rotates or moves that database, the zed server falls back to the global workspace until a new thread is opened.
- The Zed inbox lives in memory in the bridge process; a bridge restart clears undrained messages.

## 7. V3.11.0 privacy mode - final suite run

[HASIL-UJI] All node checks green: `node --check snapgent-extension/core/main.js`, `snapgent-extension/background.js`, `snapgent-extension/core/privacy.js` - exit 0 each.

[HASIL-UJI] `node snapgent-extension/test-privacy.js` -> `All tests passed` (24 asserts, exit 0). The guard covers the envelope shape (schema 1, FNV-1a 64-bit digest as 16 hex chars, `bytes`/`lines` counts, `images` as a count only, `raw: null`), the fact that no raw payload or file path fragment is present in the envelope, digest determinism, and the literal `async function submitAndGetBase(text, images)` signature of `submitAndGetBase`.

[HASIL-UJI] Six privacy defects fixed and re-verified in the live files:
1. `main.js` local-only branch no longer reads the undefined `msg`; it reads `A.toolName` and derives ok from `A.toolSettle.phase !== "err"`.
2. The provider call is `P.typeAndSend(compactEnvelope, null)` - `typeText` does not exist in any provider module (grep: 21 hits for `typeAndSend(text, images)`, 0 for `typeText`), and every provider guards `images && images.length`, so `null` images is safe.
3. The envelope is now a real turn (typed and sent), so the provider receives a request instead of timing out after ~60 s.
4. `background.js` a2a_send sends `type: "a2a_send"` and lets `send()` assign its own `id`; the bridge dispatches on `request.get("type")`.
5. The SHA-256 wording at `main.js:123` and the popup footer `main.js:3180` now reads FNV-1a 64-bit. `core/oep.js` still uses HKDF-SHA256, which is a different mechanism and was left untouched.
6. When the envelope or the bridge call fails, the turn aborts with `A.stop = true` and a banner instead of falling through to the raw `P.typeAndSend(text, images)` path.

[HASIL-UJI] `background.js` `handleBridgeMessage` gained the four A2A branches (`a2a_ack`, `a2a_agents`, `a2a_inbox`, `a2a_error`) that mirror the bridge emit shapes, so the extension's pending promise resolves for every A2A reply.

[HASIL-UJI] Suites on the live bridge (WS 17613, health 17614): `pytest test_bridge.py test_oep_e2e.py test_e2e.py -q` -> 4 passed; `python test_e2e.py` -> E2E DONE exit 0 with tools=103 and servers vscode/git/shell/zed alive; `python test_oep_e2e.py` -> PASS exit 0 (envelope round-trip); `python test_workspace_a2a.py` -> 6 passed, 2 skipped, 0 failed - the two skips are `auth_token_403`/`auth_token_200`, which cannot bind the health port while this same bridge holds it (WinError 10048), so they stay UNVERIFIED here rather than counted as PASS; `scripts/test_coord_bridge.py`, `scripts/test_mesh_bridge.py`, `scripts/test_zed_mcp.py` -> `OVERALL: PASS` exit 0 each.

[HASIL-UJI] Bridge version and packaging: `bridge.py:105` `BRIDGE_VERSION = "3.11.0"`, `snapgent-extension/manifest.json` version 3.11.0 with the `"key"` field kept, `python scripts/pack_ext.py` -> `OK dist\snapgentv3-3.11.0.zip 26 files 290 KB`, `/health` -> 200 healthy version 3.11.0 servers 4 active 4.

## 8. Post-commit verification round (fresh process, bridge live)

[HASIL-UJI] Local commit: `git log --oneline -3` -> `a3739f3 feat: V3.11.0 local-only privacy mode, Zed A2A agent and per-IDE workspace`, parent `a117013`. `git status --short` -> empty (working tree clean). `git log --oneline origin/main -1` -> `a68b4ee`; `git rev-list --count origin/main..HEAD` -> 3. Nothing was pushed.

[HASIL-UJI] `python scripts/pack_ext.py` -> `OK  dist\snapgentv3-3.11.0.zip  26 files  290 KB` (297311 bytes on disk). `dist/` stays out of the commit.

[HASIL-UJI] ZIP content audit with `C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\zipcheck.py` -> 16/16 checks PASS, exit 0: `background.js` carries `type: "a2a_send"` and no `kind: "a2a_send"`; the `a2a_ack`, `a2a_agents`, `a2a_inbox`, `a2a_error` branches are present; `core/main.js` contains `FNV-1a 64-bit digest` and no `SHA-256` wording, contains `typeAndSend(compactEnvelope` and no `P.typeText`, contains `A.toolName` and `A.toolSettle.phase`, and exposes the `setLocalOnly:` setter; `core/privacy.js` contains `fnv1a64`; `manifest.json` version is `3.11.0` and the `key` field is present. 64-hex secret scan over every non-binary ZIP entry -> 0 hits.

[HASIL-UJI] Bridge health while the suites ran: `GET http://127.0.0.1:17614/health` -> 200 `{"status":"healthy","version":"3.11.0","servers":4,"active_servers":4,"clients":0,"requests_errors":0,"tools_errors":0,"servers_failed":0,"clients_connected":5}`. Bridge PID 3204 plus four MCP children (vscode_mcp, git_mcp, shell_mcp, sandbox_job).

[HASIL-UJI] `python -m pytest test_coordination.py test_mesh.py test_vscode_mcp.py test_zed_a2a_mcp.py test_ws0_hardening.py test_oep_inprocess.py test_setup_config.py test_security_fixes.py -q` -> 51 passed, exit 0. `python -m pytest test_bridge.py -q` -> 4 passed, exit 0 (run with `ZS_HEALTH_PORT=17699` so the test's own health server does not collide with the live bridge on 17614).

[HASIL-UJI] `python test_workspace_a2a.py` -> 8 passed, 0 skipped, 0 failed, exit 0 with `ZS_HEALTH_PORT=17699`. This closes the gap left open in section 7: the same file run against port 17614 gives 6 passed / 2 skipped because the live bridge already holds that port, so the two auth-endpoint assertions (`auth_token_403`, `auth_token_200`) were unproven there. On the alternate health port both now execute and PASS: the wrong Origin yields 403 with `reason=extension_origin_mismatch`, `expected_origin=chrome-extension://<id>`, `seen_origin=chrome-extension://wrongid` and a hint, and the matching Origin yields 200 with `token` and `enabled` keys.

[HASIL-UJI] `python test_e2e.py` -> E2E DONE exit 0 (WS connected, tools=103, unauthenticated WS closed with 4001, git tool call returned the workspace JSON, `/servers` 401 without token and 200 with token listing vscode/git/shell/zed all alive). `python test_oep_e2e.py` -> PASS exit 0 (session established, envelope 153 bytes, result envelope 273 bytes, decrypted workspace info 110 bytes, ok=True). `scripts/test_coord_bridge.py`, `scripts/test_mesh_bridge.py`, `scripts/test_zed_mcp.py` -> `OVERALL: PASS` exit 0 each.

[HASIL-UJI] `node --check` on `snapgent-extension/core/main.js`, `snapgent-extension/background.js`, `snapgent-extension/core/privacy.js` -> exit 0 each. `node snapgent-extension/test-privacy.js` -> `All tests passed`, exit 0 (envelope shape, no raw payload, no path fragment such as `/etc/passwd`, `/root/.ssh/id_rsa`, `/home/user/.env`, digest determinism, `fnv1a64('')=cbf29ce484222321`, the literal `async function submitAndGetBase(text, images)` signature, and the mode flag reference).

[FAKTA] Provider-side payload mode: with LOCAL-ONLY ON the page receives only the envelope JSON `{_privacy, schema, tool, ok, bytes, lines, digest, images, raw: null, routed_to}` - the full tool result is routed to the local bridge A2A inbox of recipient `zed`. With LOCAL-ONLY OFF (default) the previous behavior is unchanged. The digest is FNV-1a 64-bit, not SHA-256.

[FAKTA] Extension identity: the Chrome extension id is derived from the `key` field in `snapgent-extension/manifest.json`, so the id is stable across reloads, new chats and a new Chrome profile. It only changes if that `key` field is removed or replaced. The bridge pins the same id in `config.json` (`bridge.security.extensionId`) and rejects any other Origin on `/auth-token`, which is what produced the 403 loop in the browser until the unpacked extension was reloaded.

[INFERENSI] The remaining residual risks are unchanged from section 6: the Zed inbox is in-memory only, Zed's thread database read can fail if Zed rotates it, and the review gate of the FABLE run stays PARTIAL because the reviewer provider failed three times and the fallback timed out, so the code review evidence is the lead's own line-level reading rather than an independent adversarial pass.


## 9. Total padding + encryption mode (operator m05661 "gas padding dan enkripsi total")

[FAKTA] Operator authorized hiding even metadata/size from the provider page. Implemented as a constant-length, opaque, encrypted token.

[AKSI] `snapgent-extension/core/privacy.js` rewritten: `TARGET_LEN = 512`, FNV-1a 64-bit digest, AES-GCM-256 via Web Crypto, PKCS7-style pad to fixed length, `buildEnvelope` (sync random token) + `buildEnvelopeAsync` (encrypted token) + `encryptMeta` + `fnv1a64` + `toHex`. The envelope now contains ONLY `{_privacy, schema, token}` — no `bytes`, no `lines`, no `digest`, no `images`, no `raw`, no `tool`, no `ok`.

[AKSI] `snapgent-extension/core/main.js` local-only branch calls `buildEnvelopeAsync` with `_zsEncKey`; the bridge payload changed from `{envelope, tool, ok}` to `{text, tool, ok, images}` (the full payload routes to the A2A inbox of `zed`); `compactEnvelope = JSON.stringify(envelope)` (the previous 600-char slice is gone). Comment at `main.js:124` and the popup footer wording updated to "opaque padded encrypted token".

[AKSI] `snapgent-extension/test-privacy.js` rewritten for the new contract (12 asserts). A trailing `PY` heredoc artifact caused `ReferenceError: PY is not defined at ... test-privacy.js:133:1`; removed, then all 12 pass.

[HASIL-UJI] `node --check` on `snapgent-extension/core/main.js`, `snapgent-extension/core/privacy.js`, `snapgent-extension/background.js` -> exit 0 each.

[HASIL-UJI] `node snapgent-extension/test-privacy.js` -> `passed: 12 failed: 0`, exit 0. Asserts: module exports, `fnv1a64` empty vector, no plaintext metadata fields, token varies between calls, padded constant-ish length, raw payload/path segments not leaked, images not leaked, `envelopeFor` alias, `encryptMeta` null without key, `encryptMeta` base64 ciphertext, `buildEnvelopeAsync` encrypted, `buildEnvelopeAsync` fallback without key.

[HASIL-UJI] `python C:\Users\Brokenlink\AppData\Local\Temp\snapprobe\run_suites.py --out ...\suite_results.json` -> 15/15 PASS exit 0 (test_coordination, test_mesh, test_security_fixes, test_vscode_mcp, test_setup_config, test_oep_inprocess, test_oep_e2e, test_ws0_hardening, test_zed_a2a_mcp, test_workspace_a2a, test_bridge, test_e2e, scripts/test_zed_mcp, scripts/test_coord_bridge, scripts/test_mesh_bridge).

[HASIL-UJI] `GET http://127.0.0.1:17614/health` -> 200 `{"status":"healthy","version":"3.11.0","servers":4,"active_servers":4,...}`. `bridge.py:105` `BRIDGE_VERSION = "3.11.0"` (no bump needed).

[HASIL-UJI] `docs/USAGE.md` "### Local-only privacy mode" section updated (opaque token wording, 12-assert contract); `CHANGELOG.md` [V3.11.0] Privacy bullet and the test-privacy.js Verified bullet both rewritten to the opaque-token contract.

[INFERENSI] Provider page receives only a base64 ciphertext token of fixed padded length — no size leak, no metadata, no raw text. Full payload travels over the WS bridge to the A2A inbox of `zed`. Default OFF keeps legacy byte-for-byte behavior.

[PENDING] Secret scan over staged files, `scripts/pack_ext.py` (dist ZIP 3.11.0), one local commit (no push), and the operator-side reload of the unpacked extension in Edge + restart of Zed remain outstanding.
