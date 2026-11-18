# snapgent-connector — usage

Enterprise MCP bridge that exposes Git + Shell tools to browser AI chats (ChatGPT, DeepSeek, Gemini, Claude, Grok, Kimi, Z.ai, Qwen, Meta AI) and to Zed, with HMAC auth, rate limiting, hot reload, request tracing, and a Windows-native sandbox that needs no Docker.

## Verified state

Every claim below was produced by a run in this workspace on Windows 10 Pro 19045 / Python 3.12.10.

| Check | Result |
| --- | --- |
| Bridge health `GET http://127.0.0.1:17614/health` | `healthy`, servers=4, active=4 |
| WS with Bearer token | `connected`, tools=103, mcp_alive=True |
| WS without token | closed `4001 Authentication required` |
| Browser WS auth (`?token=`) | `connected`, tools=103, token fetched via `/auth-token` |
| `GET /auth-token` foreign Origin | 403 Forbidden |
| Tool call through bridge (`git_workspace_info`) | ok=True, real JSON payload, root = active project |
| `GET /servers` no token / bad token | 401 / 403 |
| Rate limit burst (350 requests) | 300 ok, 50 limited 429 with Retry-After |
| Hot reload (config mtime bump) | `config_reload_triggered` → `config_reload_completed`, back to 4/4 |
| Sandbox script tests (4) | cmd echo, Python run, memory pressure, process limit |
| Unit tests `test_coordination.py` | 82 passed, 0 failed |
| Unit tests `test_mesh.py` | 26 passed, 0 failed |
| Unit tests `test_bridge.py` | 4 tests, OK |
| End-to-end `test_e2e.py` | E2E DONE (auth, tool call, `/servers`, workspace root) |
| Bridge suites `scripts\test_coord_bridge.py`, `scripts\test_mesh_bridge.py` | OVERALL PASS |
| Zed MCP over stdio (4 servers) | PASS, 2 responses each, notifications silent |

## 1. Start the bridge

```powershell
cd C:\MRP\proyek 2026\snapgent-connector
python bridge.py
```

Ports:

- WebSocket `ws://127.0.0.1:17613` — extension and Zed clients
- Health `http://127.0.0.1:17614` — `/health` (public), `/servers` and `/clients` (Bearer token)

The auth secret is generated on first run (32-byte hex) and persisted in `config.json` under `bridge.auth.secret`. Read it from there; it is never printed in full by the bridge (the startup line shows the first 8 characters only).

Run as a hidden background process on Windows:

```powershell
Start-Process python -ArgumentList "bridge.py" -WindowStyle Hidden `
  -RedirectStandardOutput bridge_run.log -RedirectStandardError bridge_run_err.log `
  -WorkingDirectory "C:\MRP\proyek 2026\snapgent-connector"
```

Note: quote the whole `bridge.py` path in `-ArgumentList` and Start-Process mangles it; pass it bare.

## 2. Servers it spawns

| id | command | sandbox in config.json |
| --- | --- | --- |
| vscode | `python vscode_mcp.py` | none (28 workspace tools) |
| git | `python git_mcp.py` | none (37 tools) |
| shell | `python shell_mcp.py` | `windows-job` (10 tools) |
| zed | `python vscode_mcp.py` | none |

Change the sandbox per server with `"sandbox": "none" | "windows-job"` in `config.json` → `mcpServers.<id>`. A config save triggers hot reload; servers restart within about a second.

### Workspace detection

`${workspace}` (used for `SNAPGENT_WORKSPACE`, sandbox `allowedPaths`, and the MCP cwd) is resolved in this order:

1. `SNAPGENT_WORKSPACE_ROOT`, then `SNAPGENT_WORKSPACE` — first value that is an existing directory wins.
2. The folder of the IDE that is actually running: `Cursor.exe`, `Code.exe`, `Code - Insiders.exe`, `VSCodium.exe` are matched through `tasklist`, and their `globalStorage/storage.json` is read (`windowsState.lastActiveWindow.folder`, `openedWindows[].folder`, `file://` URIs included).
3. The bridge repository root.

Zed exposes no `globalStorage/storage.json`, so its folder is read from `%LOCALAPPDATA%\Zed\threads\threads.db` (newest non-empty `folder_paths` column, opened read-only), through `bridge.py:_zed_workspace`. When that is unavailable, Zed users set `SNAPGENT_WORKSPACE` (or run `scripts\register_zed_mcp.py --root <project>`). `vscode_mcp.py` uses the same order and additionally accepts the workspace root that the MCP client passes in `roots/list`. Check what resolved with `python scripts\check_bridge_health.py` (it prints both the VS Code and the Zed root).

## 3. Load the browser extension

The extension is MV3 and works in Edge and Chrome.

1. `edge://extensions` (or `chrome://extensions`).
2. Enable **Developer mode**.
3. **Load unpacked** → select `C:\MRP\proyek 2026\snapgent-connector\snapgent-extension`.

The extension lives in the background service worker; there is no toolbar button. It injects the tool panel into supported AI chat pages and persists sessions in IndexedDB, so a reload does not lose the connection token.

Reload after every code change: `chrome://extensions` (or `edge://extensions`) → **Reload** on the ArteX-TNT entry. The extension ID is **pinned** by the `"key"` field in `snapgent-extension/manifest.json` and is `hpeolgkpbdcicahifcgainojfilbgmnb`. It survives reloads, browser restarts, new chat sessions, and new profiles; it changes only when that `key` is deleted or replaced. The bridge's `/auth-token` endpoint answers **403** to any other origin, which surfaces in the panel as an auth error and in `logs\bridge_debug.log` as `GET /auth-token HTTP/1.1" 403`. `python scripts\check_bridge_health.py` prints the pinned ID and the last refused origin.

Prebuilt package: `dist\snapgentv3-3.11.0.zip` (26 files, 290 KB) for machines without the source folder. Rebuild with `python scripts\pack_ext.py`.

### Local-only privacy mode

The **LOCAL-ONLY** toggle in the MCP popover (`snapgent-extension/core/main.js:3213`) decides what crosses into the web-model page.

- OFF (default): unchanged legacy behaviour — full prompt and full tool results are typed into the provider page.
- ON: the full text and tool result never reach the provider page. The extension builds an opaque padded encrypted token in `snapgent-extension/core/privacy.js` (`buildEnvelopeAsync`) — a fixed-length AES-GCM-256 ciphertext (padding to `TARGET_LEN`) of the metadata only, so the provider cannot recover even the byte/line/digest counts or the payload length. The envelope contains only `{_privacy, schema, token}`; `raw` is `null`. Only that opaque JSON is typed into the page and sent as a normal turn; the real payload travels over the WebSocket to the bridge and is handed to the A2A inbox of `zed` via `a2a_send`.
- Failure of the bridge call aborts the turn with a visible banner instead of falling back to typing the raw text (no silent leak on error).
- `node snapgent-extension/test-privacy.js` guards the contract: 12 asserts covering the opaque token (constant-ish length, no metadata fields, no raw payload leak), the encrypted-token path, and the fallback.

## 4. Register the MCP servers in Zed

`git_mcp.py` and `shell_mcp.py` already speak JSON-RPC over stdio, so Zed only needs its `context_servers` entries filled in.

One-shot registration (backs up first, idempotent, prints `SKIP` if already registered):

```powershell
python scripts\register_zed_mcp.py
python scripts\register_zed_mcp.py --root "C:\path\to\your-project"
python scripts\register_zed_mcp.py --dry-run
```

`--root` sets the workspace handed to every registered server through `SNAPGENT_WORKSPACE`; `--settings` points at a different `settings.json`; `--dry-run` prints the result without writing.

Verify:

```powershell
python scripts\verify_zed_settings.py
python scripts\test_zed_mcp.py
```

Five servers are registered: `snapgent-git`, `snapgent-shell`, `snapgent-shell-sandboxed` (the last one runs through the Job Object sandbox via `scripts\shell-mcp-job.json`), `snapgent-workspace` (the workspace/file tools from `vscode_mcp.py`), and `snapgent-a2a` (`zed_a2a_mcp.py`, which speaks the bridge WebSocket protocol and exposes `bridge_health`, `a2a_agents`, `a2a_inbox`, `a2a_send`, `coord_prepare`, `coord_stats`, `mesh_create_project`, `mesh_run_project`, `mesh_status`, `mesh_send_agent`). They appear in the top-level `agent.context_servers` and inside each agent profile, each with `SNAPGENT_WORKSPACE` pointing at the chosen root. Restart Zed after registration so the new context servers are picked up.

The A2A channel is how web-model agents and IDE agents talk: `a2a_send` delivers to a registered inbox (`zed` is registered by the bridge on startup, every provider agent as `provider:<name>`), and `a2a_inbox` reads the result back. Peer findings are wrapped in untrusted markers and capped at 600 characters by `mesh.py:_sanitize_payload` before they reach a system prompt.

Undo: restore `C:\Users\Brokenlink\AppData\Roaming\Zed\settings.json` from the newest `settings.json.bak-<epoch>` file beside it.

## 5. Sandbox model (Windows, no Docker)

`windows-job` is implemented in `sandbox_job.py` with Win32 Job Objects through ctypes. It reads a JSON config, creates the job with `CreateJobObjectW`, applies `JOB_OBJECT_LIMIT_ACTIVE_PROCESS`, `JOB_OBJECT_LIMIT_JOB_MEMORY`, `JOB_OBJECT_LIMIT_PROCESS_MEMORY`, `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, `JOB_OBJECT_LIMIT_PRIORITY_CLASS`, plus UI restrictions, then creates the child suspended (`CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT`), assigns it to the job with `AssignProcessToJobObject`, and resumes it. Closing the sandbox handle kills the whole process tree.

Its config file looks like:

```json
{"memoryLimitMB":512,"maxProcesses":20,"exe":"python","args":["shell_mcp.py"],"cwd":"C:\\MRP\\proyek 2026\\snapgent-connector"}
```

All diagnostics from `sandbox_job.py` go to stderr, never stdout, so a sandboxed stdio MCP server keeps a clean JSON-RPC stream.

**Measured limit behaviour on this build (Windows 10 19045):** the memory limit is advisory — a 100 MB allocation under a 50 MB job limit completed instead of being killed, and a 3-process job ran 5 children. Treat the sandbox as blast-radius containment (kill-on-close, process accounting, priority) and not as a hard memory or process barrier. Network and filesystem restrictions listed in the profile are likewise enforced by the bridge's allowlists, not by the job object itself.

## 6. Coordination hub over WebSocket

`coordination_hub.py` adds four provider profiles (chatgpt, deepseek, gemini, qwen) with quota accounting, health tracking, per-provider memory strategies, and an A2A message bus. The bridge registers providers from `config.json` → `providers` on every config load, so a hot reload re-applies them.

Client message types (newline-delimited JSON, same channel as tool calls):

- `{"type":"coord_stats","id":N}` → `coord_stats` with providers, memory profiles, agents.
- `{"type":"coord_prepare","id":N,"provider":"qwen","messages":[…]}` → `coord_prepared` with the trimmed message list, chosen provider, `switched`, `strategy`, `tokens_before`, `tokens_after`, `compressed`.
- `{"type":"a2a_send","id":N,"sender":"x","messageType":"ping","payload":{...}}` → `a2a_ack` with `agents` (who received it) and the `message` dict.

Memory strategies: `sliding` (chatgpt), `append-stable` (deepseek), `hierarchical` (gemini), `rolling-summary` (qwen). Every strategy output is passed through `_enforce_budget`, so the optimized list can never exceed the window; a single oversized message is truncated with `...[truncated to fit window]`.

Quota rule: when every provider is out of quota, `coord_prepare` returns `coord_error` with the provider table rather than silently overshooting. Pass `"allowOverQuota": true` to use an exhausted but healthy provider.

```json
{"type":"coord_prepare","id":9,"provider":"qwen","messages":[...],"allowOverQuota":true}
```

## 7. Test suite

Run from `C:\MRP\proyek 2026\snapgent-connector` with the bridge already up.

| File | What it proves |
| --- | --- |
| `test_bridge.py` | bridge unit tests (4): config, auth, rate limit, tracing |
| `test_e2e.py` | WS auth, tool call, `/servers` 401 + 200 against the live bridge |
| `test_coordination.py` | quota, health, memory strategies, A2A routing (82 assertions) |
| `test_mesh.py` | project mesh DAG, broker, A2A handoff (26 assertions) |
| `scripts\test_coord_bridge.py` | coord + a2a message types over the live WS |
| `scripts\test_mesh_bridge.py` | mesh create/run/park/answer/handoff over the live WS |
| `scripts\test_sandbox.ps1` | Job Object sandbox: echo, Python, memory, process limit |
| `scripts\test_zed_mcp.py` | all four MCP servers driven as stdio JSON-RPC clients |
| `scripts\verify_zed_settings.py` | Zed settings.json parses and carries the servers |

```
python test_bridge.py
python test_e2e.py
python test_coordination.py
python test_mesh.py
python scripts\test_coord_bridge.py
python scripts\test_mesh_bridge.py
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test_sandbox.ps1
python scripts\test_zed_mcp.py
```

`test_bridge.py` binds the health port, so run it while the bridge is stopped; every other script expects the bridge up on `127.0.0.1:17613` / `:17614`.

## 8. Security posture (loopback-only, browser-based providers)

- **Binding:** WS `127.0.0.1:17613`, health `127.0.0.1:17614`. No external network access; the bridge never listens on 0.0.0.0.
- **WS auth:** secret accepted via `Authorization: Bearer`, `?token=` query, or `Sec-WebSocket-Protocol`. Unauthenticated sockets closed with `4001 Authentication required`.
- **Token endpoint:** `GET /auth-token` returns the local shared secret only to MV3 extension origins (`chrome-extension://`, `moz-extension://`). No CORS header, so a normal web page cannot read it; foreign Origin → 403; rate-limited.
- **Rate limit:** 300 req/min + burst 50; max message 50 MB; oversized messages rejected.
- **Origin allowlist:** WS origin checked against configured provider origins (inert on websockets 17.2's ServerConnection — do not harden to reject or extension sockets break).
- **Sandbox:** shell tools run under Windows Job Objects (memory + process limits); no Docker required. The job object does not set `JOB_OBJECT_LIMIT_BREAKAWAY_OK`, so a child process cannot break out of it.
- **Untrusted content:** peer findings and replayed shared memory are wrapped in `<<<PEER_FINDINGS_UNTRUSTED>>>` delimiters before they enter an agent prompt, control characters are stripped, peer payloads are capped at 600 characters, and any delimiter smuggled inside the payload is neutralised.
- **Secrets:** token is a local bridge/extension shared secret only — never a provider API key. Providers are browser UI tabs; no `api_key` stored in config.
- **Dorking/hacking surface:** /health and /metrics are public (status only); /servers, /clients, /auth-token require auth or origin check. No external exposure; nothing leaves the machine.

## 9. Project mesh (A2A, no API keys)

Provider di repo ini adalah **web UI**, bukan endpoint berbayar. Tidak ada
`api_key` di `config.json` dan tidak ada yang boleh masuk ke sana. Worker setiap
task karena itu adalah **tab browser asli**, bukan panggilan HTTP.

Alurnya: bridge membuat project + task (DAG), memilih provider per task, lalu
saat satu task siap ia *parkir* di `ScriptedGateway`. Watcher `bridge.py`
mengirim frame `mesh_task` ke extension, background mengantrikannya, tab
provider di-*raise*, worker menjalankan satu turn lewat `submitAndGetBase` +
`waitForResponse`, lalu mengirim `mesh_task_result`. Task berikutnya yang
bergantung padanya menerima hasil itu di prompt-nya sebagai "Findings from your
peer agents".

Dua aturan yang membuat multi-provider pada SATU project benar-benar jalan:

1. **Seriale, bukan paralel.** Satu worker browser hanya bisa mengetik ke tab
   aktif satu window, jadi extension menjalankan satu task pada satu waktu
   (`background.js` `meshQueue` + `drainMeshQueue`) dan watcher bridge hanya
   mendorong satu parked task per siklus. DAG tetap boleh punya task paralel;
   front end mengeksekusinya berurutan dan mengambil yang berikutnya saat satu
   selesai. Ini yang membuat project satu provider tunggal maupun rantai
   empat provider berperilaku sama.
2. **Tab di-raise, bukan ditolak.** `meshRaise` melakukan
   `chrome.tabs.update({active:true})` + `chrome.windows.update({focused:true})`
   lalu menunggu `zs-visibility` yang melaporkan `document.hidden === false`
   sebelum mengirim `zs-mesh-task`. Raise itulah yang menghidupkan worker yang
   parkir di `core\main.js:332` `waitVisible()` (resolve pada
   `visibilitychange`), jadi tidak ada perubahan logika di dalam tab. Window
   boleh saling tumpang tindih dan boleh di-minimize selama tidak ditumpuk
   total — yang aktif di window-nya selalu punya layout.

Yang tetap gagal cepat dengan pesan jelas: tidak ada tab provider sama sekali,
atau tab-nya `discarded` (suspended Chrome).

Pesan WS untuk mesh (dua arah):

| arah        | type                  | isi                                                              |
| ----------- | --------------------- | ---------------------------------------------------------------- |
| ke bridge   | `mesh_create_project` | title, goal, tasks[] (id, title, prompt, dependsOn, preferProvider, notifyPeers) |
| ke bridge   | `mesh_run_project`    | projectId, maxConcurrent, maxAttempts                             |
| ke bridge   | `mesh_task_result`    | threadId, text                                                    |
| ke bridge   | `mesh_status`         | projectId                                                         |
| ke bridge   | `mesh_send_agent`     | sender, recipient, note                                           |
| dari bridge | `mesh_run_started`    | projectId, alreadyRunning                                         |
| dari bridge | `mesh_task`           | task{threadId, projectId, provider, system, prompt} (push satu per siklus) |
| dari bridge | `mesh_parked`         | parked[] = call yang menunggu jawaban                             |
| dari bridge | `mesh_run_finished`   | projectId, summary                                                |

Timeout harus sejajar di dua sisi: worker (`MESH_ANSWER_TIMEOUT`, 15 menit) dan
broker (`ScriptedGateway` timeout, 900 detik). Satu turn chat nyata butuh menit;
nilai lama 120 detik membunuh task saat tab masih menjawab.

State project persis di `state\projects.json` (tulis atomik), jadi run
dilanjutkan setelah bridge restart.

## 10. Layout

```
bridge.py                 WebSocket bridge: auth, rate limit, tracing, health, hot reload
config.json               servers, bridge settings, sandbox profiles, provider profiles
coordination_hub.py       ProviderManager, MemoryOptimizer, A2AProtocol, CoordinationHub
git_mcp.py                37 git tools over stdio JSON-RPC
shell_mcp.py              10 shell/tmux tools, allowlist + dangerous-pattern blocking
vscode_mcp.py             28 workspace/file/run tools over stdio JSON-RPC (IDE-neutral root)
sandbox_job.py            Win32 Job Object launcher (JSON config arg)
mesh.py                   project mesh: DAG tasks, cross-provider cooperation, broker

test_bridge.py            bridge unit tests
test_e2e.py               live-bridge end-to-end test
test_coordination.py      coordination hub unit tests
test_mesh.py              project mesh unit tests
scripts\                  sandbox tests, Zed registration/verification, stdio MCP tests
snapgent-extension\       MV3 extension (background service worker + injected UI)
core\                     recipe-engine.js, coordination-hub.js
zed\                      context-servers-snippet.json
dist\                     prebuilt extension zip
state\                    shared memory, mesh projects
logs\                     bridge_security.log, bridge_debug.log
fable\                    run plans, ledgers, evidence
```
