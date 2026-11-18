# AUDIT.md â€” Bukti Gap Keamanan (M0)

**Proyek:** snapgent-connector
**Tanggal:** 14-11-26
**Metode:** Pembacaan kode penuh `bridge.py`, `shell_mcp.py`, `sandbox_job.py`, `git_mcp.py`, `vscode_mcp.py` (sebagian terpotong di input limit â€” ditandai).
**Status:** M0 selesai. Plan PLAN.md perlu revisi karena ada temuan baru (G13â€“G15).

---

## Ringkasan: dari 12 gap, jadi 15

G1â€“G12 dari PLAN.md dikonfirmasi oleh kode. Tiga temuan baru (G13â€“G15) ditemukan saat audit dan **lebih parah** dari yang sudah ada.

| # | Temuan | File:line (indikatif) | ATT&CK | Severity | Status |
|---|---|---|---|---|---|
| **G13** | WS `add_server` / `remove_server` mengizinkan klien terautentikasi menambah MCP server dengan `command` arbitrer â†’ **RCE langsung** tanpa perlu edit file | `bridge.py::_change_server` | T1059, T1546 | **CRITICAL** | **BARU** |
| **G14** | WS `auth_token` message type mengembalikan secret bridge ke klien yang sudah terhubung â€” dan origin `chrome-extension://` apa pun lolos WS origin check | `bridge.py::handle_client`, `_handle_request` | T1528, T1552 | **CRITICAL** | **BARU** |
| **G15** | Origin check WS menerima *semua* prefix `chrome-extension://` / `moz-extension://` â€” bukan hanya extension Snapgent | `bridge.py::handle_client` | T1528 | **HIGH** | **BARU** |
| G1 | `git status; calc` lolos karena cek hanya token pertama | `shell_mcp.py::_is_safe_command` | T1059 | CRITICAL | CONFIRMED |
| G2 | `DANGEROUS_COMMANDS` substring â€” bypass dengan spasi ganda / `${IFS}` | `shell_mcp.py` | T1485 | HIGH | CONFIRMED |
| G3 | `curl/wget/scp/ssh/base64/openssl/docker/kubectl` ada di `SAFE_COMMANDS` | `shell_mcp.py` | T1105, T1048, T1567 | HIGH | CONFIRMED |
| G4 | Hot reload `config.json` mtime-based, tanpa signature | `bridge.py::watch_config` | T1546 | HIGH | CONFIRMED |
| G5 | `/auth-token` HTTP hanya cek prefix origin | `bridge.py::auth_token_handler` | T1528 | HIGH | CONFIRMED (diperluas oleh G15) |
| G6 | Secret plaintext di `config.json`; token via `?token=` | `bridge.py`, `config.json` | T1552.001 | HIGH | CONFIRMED |
| G7 | Child mewarisi `os.environ` penuh | `shell_mcp.py::run_command` | T1552.001 | HIGH | CONFIRMED |
| G8 | Job Object tanpa low-integrity token; limit advisory | `sandbox_job.py` | T1611 | MEDIUM | CONFIRMED |
| G9 | Binary tidak ditandatangani | rilis | T1553.002 | MEDIUM | CONFIRMED |
| G10 | Log plain-text, tidak hash-chained | `bridge.py` logging | T1070.002 | MEDIUM | CONFIRMED |
| G11 | Sanitasi peer-findings delimiter-only | `mesh.py`, `coordination_hub.py` | AML.T0051 | MEDIUM | CONFIRMED |
| G12 | Rate limit per `client_ip` = `127.0.0.1` untuk semua â†’ praktis global | `bridge.py::RateLimiter` | T1499 | LOW | CONFIRMED |

---

## G13 â€” RCE via WS `add_server` (CRITICAL, baru)

**Kode:** `bridge.py`, handler `_change_server`.

```python
servers[server_id] = {
    "command": command,   # dari klien, tanpa allowlist
    "args": args,
    "env": env,
    "sandbox": sandbox,
}
temp_path.replace(CONFIG_PATH)
await self.stop_servers()
await self.start_servers()   # <- spawn command baru
```

**Proteksi yang ada:** hanya menolak id `roblox` dan `vscode`. Id lain (`git`, `shell`, `zed`) bisa **ditimpa** dengan `command` apa saja.

**Attack chain:**
1. Attacker mendapat token bridge (via G5/G14/G15, atau ekstensi yang sudah terpasang).
2. Kirim: `{"type":"add_server","server_id":"shell","command":"C:\\Windows\\System32\\calc.exe","args":[]}`
3. Bridge menulis `config.json`, restart server, spawn `calc.exe` â€” **langsung, sebagai user**.

**Bahkan tanpa token:** jika attacker bisa menulis `config.json` (G4), hot reload menjalankan hal yang sama. Tapi G13 lebih buruk karena lewat jaringan loopback dan hanya butuh token (yang bocor via G14).

**Bukti PoC (belum dieksekusi, hanya rencana):**
```powershell
# Setelah dapat token
$ws = New-Object System.Net.WebSockets.ClientWebSocket
$ws.ConnectAsync([Uri]"ws://127.0.0.1:17613?token=$token", ...)
Send '{"type":"add_server","server_id":"shell","command":"calc.exe","args":[]}'
```

**Fix (PLAN.md WS-4 diperluas):**
1. Hapus message type `add_server` / `remove_server` dari WS. Pengelolaan server hanya lewat CLI lokal (`scripts/register_*`) yang tidak menerima input dari jaringan.
2. Jika tetap butuh remote management, wajib: pairing ulang + allowlist direktori command + signature config.

---

## G14 â€” Secret leakage via WS `auth_token` (CRITICAL, baru)

**Kode:** `bridge.py`, handler `_handle_request`:

```python
elif kind == "auth_token":
    await self._send_client(websocket, {
        "type": "auth_token", "id": request_id,
        "token": self.auth.get_auth_header_value(),
    })
```

**Masalah:** ini mengembalikan secret **penuh** ke klien. Klien itu bisa saja:
- Extension Snapgent (sah) â€” tapi mengapa perlu? Extension sudah punya token saat connect.
- Extension lain yang lolos origin check (G15).
- Proses lokal yang menebak token via `/auth-token` (G5).

**Efek:** token yang sudah dipakai untuk connect bisa dipakai untuk request HTTP `/servers`, `/clients`, dan WS reconnect dari proses lain.

**Fix:** hapus message type `auth_token` sepenuhnya dari WS. Tidak ada alasan sah untuk mengambil secret via channel yang sudah terautentikasi.

---

## G15 â€” Origin check WS terlalu longgar (HIGH, baru)

**Kode:** `bridge.py::handle_client`:

```python
is_extension = origin.startswith(("chrome-extension://", "moz-extension://"))
is_allowed = origin in self.allowed_origins
if not (is_extension or is_allowed):
    ... close
```

**Masalah:** **ekstensi apa pun** yang terpasang di browser yang sama (password manager, adblock, extension jahat dari Chrome Web Store) punya origin `chrome-extension://<id>` dan lolos. Ini bukan bug teoretis â€” Chrome Web Store penuh ekstensi dengan izin luas.

**Fix:**
1. Pin extension ID persis (dari `manifest.json` `key` yang stabil).
2. Atau: pairing code first-run (lihat PLAN WS-3).
3. Jangan pernah mempercayai prefix origin untuk otorisasi.

---

## Konfirmasi gap lama (ringkas)

### G1 â€” Command injection
`_is_safe_command` hanya cek `parts[0]`. Eksekusi via `_shell_invocation` â†’ `[shell, "-c", command]`. Jadi `git status; calc.exe` â†’ `parts[0]=="git"` lolos â†’ shell menjalankan keduanya.

### G2 â€” Bypass deny-list
`DANGEROUS_COMMANDS` dicek dengan `if dangerous in cmd_lower`. `rm  -rf /` (spasi ganda) tidak cocok substring `"rm -rf /"` â†’ lolos.

### G3 â€” Network tools di allowlist
`SAFE_COMMANDS` berisi `curl`, `wget`, `scp`, `ssh`, `base64`, `openssl`, `docker`, `kubectl`, `terraform`, `ansible`, `nc` (via `nslookup/dig`?), `scp` â†’ transfer + exfil + pivot.

### G4 â€” Hot reload
`watch_config` hanya membandingkan `st_mtime`. Tidak ada signature. Siapa pun yang bisa tulis `config.json` (termasuk klien WS via G13, atau proses lokal) memicu eksekusi.

### G5 â€” `/auth-token` HTTP
```python
if origin and not origin.startswith(allowed_origins):
    return 403
```
Origin kosong (`origin == ""`) â†’ **lolos**, mengembalikan token. `curl` tanpa header Origin dapat token penuh.

> **Catatan penting:** ini lebih buruk dari yang tertulis di PLAN. Bukan hanya ekstensi jahat â€” proses lokal apa pun yang bisa hit `127.0.0.1:17614/auth-token` tanpa Origin header dapat secret.

### G6 â€” Token via query + plaintext
`handle_client` menerima `?token=`. Secret di `config.json` plaintext. Log startup mencetak 8 karakter pertama.

### G7 â€” Env inherit
`cmd_env = os.environ.copy()` sebelum spawn. Semua `*_KEY`, `*_TOKEN`, `AWS_*` ikut ke child.

### G8 â€” Sandbox
Job Object tanpa low-integrity token. `USAGE.md` sendiri mengakui limit advisory. `deniedPaths` di config **tidak dipakai** oleh `sandbox_job.py` (hanya `memoryLimitMB`, `maxProcesses`, `exe`, `args`, `cwd` dibaca).

### G10 â€” Log
`security_logger` menulis plain text; tidak ada hash chain; tidak ada mirror ke Event Log.

### G12 â€” Rate limit
`RateLimiter.check(client_id)` dipanggil dengan `client_ip`. Untuk semua koneksi loopback `client_ip == "127.0.0.1"` â†’ bucket tunggal = rate limit global, bukan per-klien.

---

## Area yang belum diverifikasi (input terpotong)

`vscode_mcp.py` terpotong di bagian `normalize_target`, `read_file`, `search_files`, `SAFE_PROJECT_SCRIPTS`. Perlu baca ulang di M1 sebelum WS-5 dieksekusi:

- `normalize_target` â€” apakah benar-benar menolak `..`, symlink, UNC?
- `read_file` / `read_files` â€” apakah ada containment ke workspace?
- `search_files` â€” apakah path traversal mungkin?
- `run_command` â€” `shell=True` di Windows (fallback cmd) = injection surface sama dengan G1.

**Aksi:** `read_file_lines` per rentang untuk `vscode_mcp.py` (skip bagian besar yang sudah terbaca).

---

## Kesimpulan M0

1. **G13 adalah temuan terpenting.** Bridge punya API manajemen server yang setara `eval()` remote. Ini harus ditutup di M1 sebelum apa pun.
2. **G15 memperluas G5.** Origin prefix bukan otorisasi.
3. **G14** membocorkan secret ke klien yang sudah masuk â€” tidak ada gunanya, harus dihapus.
4. **G5 punya bypass tanpa Origin header** yang tidak tercatat di PLAN â€” token bisa diambil `curl` polos.
5. `vscode_mcp.py` perlu audit lanjutan karena input terpotong.

**Rekomendasi urutan revisi PLAN:**
- P0 baru: G13, G14, G15, G5-bypass (sebelum WS-1)
- P0 lama tetap: WS-1, WS-3, WS-4.1
- P1: WS-2, WS-5 (butuh audit `vscode_mcp.py` dulu)

---

## Langkah berikutnya

1. Revisi `PLAN.md` untuk memasukkan G13â€“G15 dan bypass G5.
2. Audit lanjutan `vscode_mcp.py` per rentang baris.
3. Setelah itu baru eksekusi M1 (WS-1 + G13/G14/G15 + G5).

â€” [M]
