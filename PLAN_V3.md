# PLAN_V3.md -- Operator-Grade Agent: Full Local Capability, Remote-Attack-Proof, Provider-Opaque

**Proyek:** snapgent-connector (ArteX-TNT)
**Tanggal:** 14-11-26
**Mode:** Plan-only. Belum ada perubahan kode.
**Pemilik:** Tiger
**Penyusun:** [M]
**Status:** DRAFT v3 -- menggantikan asumsi model di PLAN.md v2. PLAN.md v2 tetap berlaku untuk WS-0 (tutup G13-G15) sebagai *prasyarat* v3.

---

## 0. Perubahan Model -- kenapa v3 beda

### Model v2 (sekarang)
Bridge = *restricted shell* untuk web AI. Keamanan lewat allowlist + sandbox. Hasilnya: allowlist memblokir "any code, any tool", dan lubang kontrol (G13-G15) tetap ada. Dua-duanya gagal.

### Model v3 (target)
Pisahkan tiga bidang yang selama ini dicampur:

| Bidang | Siapa yang pegang | Mekanisme |
|---|---|---|
| **Control plane** | Operator (Tiger, di keyboard) | Pairing 6-digit, tier kapabilitas, TTL, kill-switch. Hanya operator yang bisa *arm* sesi. |
| **Data plane** | Sesi web AI yang sudah di-arm | Menjalankan tool sesuai tier yang diberikan. T0 baca, T1 tulis workspace, T2 proses, T3 kernel/Win32. |
| **Provider plane** | LLM provider (ChatGPT, Claude, dsb.) | Yang *dilihat* model. Tiga profil: Reasoned (plaintext), Hybrid (opaque ID), Relay (ciphertext saja). |

**Klaim keamanan yang berubah:**
- Dulu: "batasi AI supaya tidak berbahaya." (gagal, karena allowlist bisa di-bypass)
- Sekarang: "autentikasi operator, arm sesi dengan tier + TTL, audit semuanya, kill on demand." Penyerang eksternal tidak punya jalur masuk karena control plane hanya bisa disentuh dari keyboard fisik.

**Klaim anonimitas yang berubah:**
- Dulu: "command dikirim plaintext ke chat." Provider lihat semua.
- Sekarang: profil Hybrid/Relay mengirim ciphertext atau capability-ID. Provider tidak bisa merekonstruksi argv, path, atau isi file.

**Klaim stealth yang berubah:**
- Dulu: tidak ada. Binary unsigned, spawn lewat shell, log plaintext.
- Sekarang: no-window spawn, direct exec, signing, log hygiene, timing jitter, parent opsional.

---

## 1. Matriks Kapabilitas (apa artinya "any code, any tool")

| Kapabilitas | Server baru/lama | Tier | Contoh konkret |
|---|---|---|---|
| baca file, list tree, search | `vscode_mcp` | T0 | `read_file`, `list_tree`, `search_files` |
| tulis/edit/delete file, git ops | `vscode_mcp`, `git_mcp` | T1 | `write_file`, `apply_patch`, `git_commit` |
| **spawn argv apa pun** | **`exec_mcp` (baru)** | T2 | `exec.spawn(argv=["python","-c","..."])`, `exec.spawn(argv=["x64dbg.exe","--file","x.vcxproj"])` |
| **pty / shell interaktif** | **`exec_mcp`** | T2 | ConPTY di Windows, `forkpty` di Unix, sesi persisten |
| **scan & register MCP otomatis** | **`mcp_scan` (baru)** | T2 | walk PATH + `%LOCALAPPDATA%\Programs`, deteksi `*-mcp*`, register otomatis |
| **Win32/NT bridge** | **`win32_mcp` (baru)** | T3 | `proc.open`, `proc.read`, `proc.write`, `proc.alloc`, `proc.thread`, `mem.read`, `mem.write`, `dev.ioctl`, `reg.read/write`, `svc.query` |
| **driver IO** | **`win32_mcp`** | T3 | `DeviceIoControl` ke device apa pun yang punya handle |
| **network keluar** | child proses | T2 | `curl`, `requests`, `invoke-webrequest` (via exec) |
| **process injection** | **`win32_mcp`** | T3 | `VirtualAllocEx` + `WriteProcessMemory` + `CreateRemoteThread` |

Tier di-set saat pairing. Operator bisa arm T2 saja, atau T3 dengan TTL pendek. Default: T1 selama 8 jam.

---

## 2. Arsitektur Eksekusi

### 2.1 `exec_mcp.py` (server baru)
JSON-RPC stdio, tools:

| Tool | Params | Return |
|---|---|---|
| `exec.spawn` | `{argv:[...], cwd, env:obj|null, stdin:str|null, timeout_ms, no_window:bool, parent_spoof:str|null, name_mask:str|null}` | `{pid, stdout, stderr, exit_code, duration_ms}` |
| `exec.spawn_bg` | sama, tanpa tunggu | `{pid, session_id}` |
| `exec.pty` | `{argv, cwd, env, cols, rows}` | `{pty_id, output_stream_url}` (WS loopback ephemeral) |
| `exec.pty_write` | `{pty_id, data}` | `{ok}` |
| `exec.pty_read` | `{pty_id, max_bytes}` | `{data}` |
| `exec.kill` | `{pid}` atau `{pty_id}` | `{ok}` |
| `exec.list` | - | `[{pid, argv0, cwd, started_at, tier}]` |

**Windows implementation:**
- `CreateProcessW` dengan `lpApplicationName` diisi (bukan `cmd.exe /c`), `lpCommandLine` di-escape pakai `escape_arg` yang sudah ada di `sandbox_job.py`.
- Flags: `CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_PROCESS_GROUP | (no_window ? CREATE_NO_WINDOW : 0)`.
- `STARTUPINFOEXW` + `InitializeProcThreadAttributeList` untuk:
  - `PROC_THREAD_ATTRIBUTE_PARENT_PROCESS` (parent_spoof) -- set `explorer.exe` untuk menyembunyikan bridge sebagai parent. **T1134.** Catatan: ini bisa men-trigger EDR; hanya dipakai kalau operator minta.
  - `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` (pty) -- `CreatePseudoConsole` + `CreateProcessW`.
  - `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` -- hanya inherit handle yang perlu.
- `name_mask`: copy `argv[0]` ke `%TEMP%\<rand>.exe` sebelum spawn, ganti `lpApplicationName`. **T1036.005.** Hanya untuk binary unsigned; signed binary yang di-rename akan gagal `WinVerifyTrust`.

**Unix implementation:**
- `posix_spawn` dengan `POSIX_SPAWN_SETSID | POSIX_SPAWN_SETSIGDEF`, `argv` array langsung (bukan `sh -c`).
- `pty`: `pty.openpty()` + `subprocess.Popen(stdin=slave, stdout=slave, stderr=slave)`.

**Kenapa ini aman dari injeksi:** tidak ada shell string, tidak ada `shlex.split` yang bisa diakali. `argv` datang sebagai array dari operator, extension, atau planner lokal. Klien yang mengirim string harus di-parse **ketat** dan ditolak kalau ada metakarakter shell -- tapi jalur utama v3 adalah array.

### 2.2 `mcp_scan.py` (server baru)
- Walk `PATH`, `%LOCALAPPDATA%\Programs`, `%PROGRAMFILES%`, direktori yang di-set operator.
- Deteksi: nama match `*-mcp`, `mcp-*`, `*_mcp*`, atau respons `--mcp-probe` dalam <2 detik.
- Coba `initialize` + `tools/list` dengan timeout 2 detik.
- Kalau lolos: tambahkan ke config sebagai server MCP dengan signature (WS-4).
- Tool MCP: `scan.run({roots:[]})`, `scan.register({binary})`, `scan.list()`.

### 2.3 `win32_mcp.py` (server baru, T3)
Bridge JSON-RPC ke Win32/NT. Tool:

| Tool | Win32/NT | Tujuan |
|---|---|---|
| `proc.open` | `OpenProcess` | handle ke proses by pid |
| `proc.read` | `ReadProcessMemory` | baca memori proses |
| `proc.write` | `WriteProcessMemory` | tulis memori proses |
| `proc.alloc` | `VirtualAllocEx` | alokasi di proses target |
| `proc.thread` | `CreateRemoteThread` / `NtCreateThreadEx` | eksekusi di proses target |
| `proc.query` | `NtQueryInformationProcess`, `EnumProcesses` | info proses |
| `mem.read` / `mem.write` | `NtReadVirtualMemory` / `NtWriteVirtualMemory` | primitif langsung |
| `dev.ioctl` | `DeviceIoControl` | IO ke device apa pun |
| `reg.read` / `reg.write` | `RegOpenKeyEx`, `RegQueryValueEx`, `RegSetValueEx` | registry |
| `svc.query` / `svc.create` | `OpenSCManager`, `CreateService` | service |
| `handle.dup` | `DuplicateHandle` | handle duplication |
| `token.imp` | `OpenProcessToken`, `ImpersonateLoggedOnUser` | impersonation (T1134) |

Semua dipanggil lewat `ctypes` dari server Python, hasilnya di-serialize ke JSON. Sandbox: server ini **hanya** jalan kalau tier sesi T3 dan operator sudah konfirmasi hotkey.

---

## 3. Arsitektur Keamanan -- Control Plane

### 3.1 Pairing (menggantikan `/auth-token` dan `?token=`)
1. Operator buka UI lokal (`operator_ui.py` / tray app).
2. UI generate `session_id` + `tier` + `ttl` + 6-digit code. Tampilkan di layar fisik.
3. Extension popup minta 6-digit. Extension kirim `{code, ext_id}` ke bridge via WS yang sudah terbuka.
4. Bridge verifikasi: `HMAC(session_secret, code||tier||ttl||nonce) == provided`. Kalau cocok, extension dapat `session_token` (disimpan di `chrome.storage.session`, TTL = ttl).
5. Semua frame WS setelah itu ditandatangani `HMAC(session_token, payload||seq)`.

**Yang didapat penyerang:** nol. Tanpa akses ke layar fisik (6-digit) atau ke `chrome.storage.session` (butuh kode extension), tidak ada jalan masuk.

### 3.2 Origin & identitas
- `SNAPGENT_EXTENSION_ID` di config. WS `handle_client` hanya menerima `chrome-extension://<ID>` **persis**, atau origin provider di `allowedOrigins`.
- `/auth-token` **dihapus**. Tidak ada HTTP endpoint yang mengembalikan secret.
- `?token=` **dihapus**.

### 3.3 Socket ACL
- Bind `127.0.0.1` + `SO_EXCLUSIVEADDRUSE`.
- Windows: `SetSecurityInfo` pada listening socket, ACL = SID user saat ini saja. Proses lain (termasuk SYSTEM) tidak bisa connect.
- Unix: `chmod 0600` pada Unix domain socket **plus** TCP loopback; prefer UDS untuk control plane, TCP hanya untuk provider-origin.

### 3.4 Kill-switch
- Global hotkey `Ctrl+Shift+F12` (Windows) / `Cmd+Shift+F12` (macOS).
- Handler: `TerminateJobObject` semua job, `closesocket` semua WS, `CryptDestroyKey` session key, tulis `audit.killswitch` ke log chain.
- Tombol tray juga memicu hal yang sama.

### 3.5 Prasyarat: WS-0 dari PLAN.md v2
G13 (add_server RCE), G14 (auth_token leak), G15 (origin prefix), bypass G5 -- **wajib** ditutup dulu. Kalau tidak, pairing pun tidak berarti.

---

## 4. Arsitektur Anonimitas -- Opaque Envelope Protocol (OEP)

### 4.1 Tujuan
Provider LLM tidak boleh bisa merekonstruksi apa yang dijalankan di mesin Tiger. Yang dilihat provider hanya ciphertext (Relay) atau capability-ID (Hybrid). Ini "anonymous" yang jujur: **anonim terhadap provider**, bukan terhadap OS.

### 4.2 Kriptografi
- Per sesi: X25519 keypair di bridge (`bridge_pub`, `bridge_priv`). `bridge_pub` diberikan ke extension saat pairing.
- Extension buat X25519 keypair sendiri (`ext_pub`, `ext_priv`), kirim `ext_pub` ke bridge.
- Shared secret = `X25519(bridge_priv, ext_pub) = X25519(ext_priv, bridge_pub)`.
- Session key = `HKDF-SHA256(shared, salt=session_id, info="snapgent-oep-v1")`.
- Envelope: `AES-256-GCM(session_key, nonce, plaintext)`.

### 4.3 Format
Command envelope (extension -> chat -> bridge):
```
[ENV:v1:<base64(nonce||ct||tag)>]
```
Plaintext = `{id, tier, tool, params}`.

Result envelope (bridge -> chat -> extension):
```
[RES:v1:<base64(nonce||ct||tag)>]
```
Plaintext = `{id, ok, stdout, stderr, exit_code, dur_ms}`.

### 4.4 Tiga profil provider

| Profil | Yang dilihat provider | Yang bisa dilakukan AI | Anonim? |
|---|---|---|---|
| **Reasoned** (default lama) | plaintext command penuh | AI bisa reasoning, pilih tool, tulis argv | TIDAK |
| **Hybrid** | `{"cap":"exec.spawn","arg_hash":"a1b2...","cwd":"<redacted>"}` | AI pilih capability dari menu + argumen ter-hash; extension expand | YA (parsial) |
| **Relay** | `[ENV:v1:...]` ciphertext | AI cuma echo; planner lokal (operator UI / LLM lokal) yang isi plaintext | YA (penuh) |

**Hybrid** adalah mode yang cocok untuk "auto": AI tetap bisa memutuskan apa yang harus dilakukan, tapi provider tidak tahu path, isi file, atau argv asli. Extension punya tabel `cap_id -> real_tool + real_params`; yang dikirim ke chat hanya `cap_id` + hash argumen.

**Relay** untuk operasi sensitif: AI provider jadi pipa buta. Planner lokal (di operator UI, pakai model lokal seperti Llama 3.1 8B / Qwen2.5 7B) yang menyusun envelope. Provider tidak punya plaintext sama sekali.

### 4.5 Redaksi di profil Reasoned
Kalau Tiger tetap mau Reasoned (karena butuh reasoning penuh), tambahkan redaksi output:
- Path -> `<path:sha256:...>`
- Username, home dir -> `<user>`
- Pola secret (`sk-`, `ghp_`, `AKIA`, high-entropy) -> `[REDACTED]`
- Nama file di luar workspace -> `<ext:sha256:...>`

### 4.6 Taint tracking
Kalau output tool mengandung pola instruksi (`ignore previous`, `you are now`, `system:`, `</?system`), bridge tandai sesi `tainted=true` dan **turunkan tier** satu level sampai operator klik "untaint" di UI. Ini mitigasi prompt injection yang tetap memungkinkan auto untuk operasi normal.

---

## 5. Arsitektur Stealth

| Kontrol | Implementasi | ATT&CK | Catatan |
|---|---|---|---|
| No visible window | `CREATE_NO_WINDOW` di semua spawn | - | default |
| Direct exec | tidak lewat `cmd.exe`/`powershell.exe` kecuali diminta | - | menghindari command-line heuristic |
| Parent spoof (opsional) | `PROC_THREAD_ATTRIBUTE_PARENT_PROCESS = explorer.exe` | T1134 | bisa trigger EDR; opt-in per sesi |
| Name mask (opsional) | copy exe unsigned ke `%TEMP%\<rand>.exe` | T1036.005 | signed binary tidak bisa |
| Log hygiene | log di `%LOCALAPPDATA%\snapgent\logs\<hash>.log`, argv di-hash bukan plaintext | T1070 | debug mode bisa plaintext |
| Binary signing | `signtool` semua exe rilis; `WinVerifyTrust` sebelum spawn | T1553.002 | wajib sebelum distribusi |
| Timing jitter | random 0-200 ms antar tool call | - | menghindari pola |
| No outbound dari bridge | bridge tidak punya socket keluar; hanya browser | T1041 | egress anak dipantau, tidak diblokir |
| Defender exclusion | dokumentasi tambah `%LOCALAPPDATA%\snapgent` ke exclusion | - | **jangan** coba disable Defender programatik |

**Yang tidak dilakukan** (dan kenapa):
- Menonaktifkan Defender/AMSI secara programatik -> memicu alert yang jauh lebih keras daripada tool dev biasa.
- Unhook ntdll / patch ETW -> butuh driver, hanya untuk T3, dan itu eskalasi risiko yang tidak perlu untuk dev tool.
- Rootkit / BYOVD -> di luar scope; Tiger minta "agent", bukan implant kernel.

---

## 6. Pemetaan ATT&CK

### 6.1 Kapabilitas yang di-enable (sisi operator)

| ATT&CK | Teknik | Tool | Tier |
|---|---|---|---|
| T1059.001/.003/.004 | Command interpreter | `exec.spawn` | T2 |
| T1106 | Native API | `win32_mcp` | T3 |
| T1055 | Process injection | `win32_mcp` (`proc.alloc`+`proc.thread`) | T3 |
| T1134 | Token manipulation | `win32_mcp::token.imp`, parent spoof | T3 |
| T1036.005 | Match legitimate name | `exec.spawn` name_mask | T2 |
| T1620 | Reflective code loading | `win32_mcp::mem.write` + `proc.thread` | T3 |
| T1485 | Data destruction | `exec.spawn` (`rm`, `del`) | T2 |
| T1560 | Archive collected data | `exec.spawn` (`tar`, `zip`) | T2 |
| T1041 | Exfil over C2 | `exec.spawn` (`curl`) | T2 |
| T1105 | Ingress tool transfer | `exec.spawn` (`curl`, `wget`) | T2 |

### 6.2 Serangan yang diblokir (sisi defense)

| ATT&CK | Teknik | Kontrol |
|---|---|---|
| T1528 | Steal app access token | pairing 6-digit, origin pin, mTLS; G14 & G5 ditutup |
| T1552.001 | Credentials in files | DPAPI, env scrub (WS-5) |
| T1546 | Event triggered execution | config signature, G13 ditutup |
| T1059 (injeksi) | Command injection | argv array, tidak ada shell string |
| T1070.002 | Clear logs | audit chain + Event Log mirror |
| T1195.002 | Supply chain | SBOM + hash lock + code signing |
| T1611 | Escape to host | Job Object + low-integrity token (WS-2) |
| AML.T0051 | Prompt injection | Hybrid/Relay + taint tracking |
| AML.T0054 | Jailbreak | OEP: provider tidak lihat plaintext |

---

## 7. Workstream

### WS-A -- Control Plane v3 (pairing, mTLS, tier, kill-switch) -- 3 hari
- `operator_ui.py` (tray, generate code, pilih tier/TTL, hotkey).
- `bridge.py`: hapus `auth_token`/`add_server`/`remove_server`; tambah `pair_begin`, `pair_complete`, `arm_session`, `disarm_session`.
- mTLS / X25519 handshake.
- Socket ACL, `SO_EXCLUSIVEADDRUSE`.
- Kill-switch global hotkey.

**Test:** `test_pairing.py` -- 6-digit salah 5x -> lockout; pairing benar -> token; hotkey -> semua socket tutup <1 s.

### WS-B -- `exec_mcp.py` (spawn, pty, kill) -- 2 hari
- `CreateProcessW` direct, `STARTUPINFOEXW`, `CREATE_NO_WINDOW`.
- ConPTY untuk pty.
- Unix: `posix_spawn` + `pty.openpty`.
- Parent spoof + name mask (opt-in).

**Test:** `test_exec_arbitrary.py` -- spawn `python -c "print(42)"` -> `42`; spawn `x64dbg.exe` kalau ada; pty interaktif; kill semua.

### WS-C -- `mcp_scan.py` -- 1 hari
- Walk PATH, probe `--mcp-probe`, register otomatis dengan signature.

**Test:** `test_mcp_scan.py` -- register `git_mcp.py` dan `shell_mcp.py` yang sudah ada, plus dummy MCP baru.

### WS-D -- `win32_mcp.py` (T3 bridge) -- 2 hari
- `ctypes` binding untuk tool di sec 2.3.
- Gate: hanya jalan kalau `session.tier >= 3` dan operator sudah konfirmasi.

**Test:** `test_win32_bridge.py` -- `proc.open(pid=os.getpid())` -> handle valid; `mem.read` 16 byte -> bytes; `reg.read` HKLM; semua butuh T3.

### WS-E -- Opaque Envelope Protocol -- 2 hari
- X25519 + HKDF + AES-256-GCM.
- Extension: `core/oep.js` -- wrap/unwrap, mode switch.
- Bridge: `oep.py` -- decrypt, execute, encrypt result.
- Tabel `cap_id -> tool+params` untuk mode Hybrid.
- Redaksi untuk Reasoned.

**Test:** `test_oep.py` -- round-trip Hybrid; provider log tidak mengandung plaintext; Relay dengan planner lokal dummy.

### WS-F -- Stealth hygiene + signing -- 2 hari
- No-window default, jitter, log hygiene.
- `signtool` di CI; `WinVerifyTrust` gate di `exec_mcp`.
- Dokumentasi Defender exclusion.

**Test:** `test_stealth.py` -- tidak ada console window (cek `GetConsoleWindow` di child = NULL); argv di log = hash; signature verify OK.

### WS-G -- Audit chain + detection -- 1 hari
- Warisan WS-6 dari PLAN.md v2: JSONL + hash chain + Event Log mirror + aturan deteksi.

**Test:** `test_audit_chain.py`.

---

## 8. Milestone

| Milestone | Isi | Durasi | Gate |
|---|---|---|---|
| **M0** (DONE) | Audit (AUDIT.md) | - | 15 gap terbukti |
| **M1 -- Tutup RCE** | WS-0 dari PLAN.md v2: G13/G14/G15 + bypass G5 | 0.5 hari | PoC G13-G15 gagal |
| **M2 -- Control plane** | WS-A | 3 hari | Pairing + kill-switch lulus |
| **M3 -- Eksekusi** | WS-B, WS-C | 3 hari | Spawn apa pun, pty, scan MCP |
| **M4 -- T3 bridge** | WS-D | 2 hari | Win32 primitif lulus di sesi T3 |
| **M5 -- Anonimitas** | WS-E | 2 hari | Provider log ciphertext-only di Hybrid/Relay |
| **M6 -- Stealth + audit** | WS-F, WS-G | 3 hari | Signing, no-window, audit chain |

Total: **13.5 hari** (solo). M1 bisa hari ini.

---

## 9. Test & Red-Team

### 9.1 Suite baru

| File | Isi |
|---|---|
| `test_pairing.py` | 6-digit, lockout, TTL, mTLS, kill-switch |
| `test_exec_arbitrary.py` | spawn argv, pty, no-window, parent spoof |
| `test_mcp_scan.py` | auto-register MCP |
| `test_win32_bridge.py` | T3 gate, primitif |
| `test_oep.py` | Hybrid/Relay round-trip, no plaintext di log provider |
| `test_stealth.py` | no-window, log hash, signature |
| `test_audit_chain.py` | hash chain, Event Log mirror |

### 9.2 Red-team checklist (sebelum arm T2/T3)

- [ ] Provider log Hybrid/Relay **tidak** mengandung argv asli, path asli, atau isi file.
- [ ] `curl /auth-token` -> 404 (endpoint hilang).
- [ ] WS origin `chrome-extension://evil` -> ditutup.
- [ ] WS `add_server`/`auth_token` -> ditolak.
- [ ] Pairing tanpa 6-digit -> tidak bisa arm.
- [ ] Kill-switch -> semua proses anak mati, semua socket tutup.
- [ ] Child process tidak punya console window.
- [ ] Child tidak mewarisi `AWS_*`, `*_TOKEN`, `*_KEY`.
- [ ] Log tidak mengandung plaintext argv.
- [ ] `signtool verify /pa bridge.exe` sukses.
- [ ] Sesi tainted (pola injeksi di output) -> tier turun otomatis.

---

## 10. Batasan Jujur (baca sebelum arm T3)

1. **"No detect" terhadap EDR kernel di mesin yang sama tidak bisa dijamin.** EDR melihat `PsSetCreateProcessNotifyRoutine`, `PsSetLoadImageNotifyRoutine`, thread creation, handle grant -- tidak peduli parent spoof atau name mask. Yang bisa dicapai: tidak trigger signature/heuristik AV umum, tidak bocor ke provider, tidak bocor ke log. Kalau Tiger butuh *bypass* EDR kernel, itu butuh driver (BYOVD) dan di luar scope dokumen ini.
2. **"Anonymous" terhadap provider hanya nyata di Hybrid/Relay.** Di Reasoned, provider lihat semuanya -- itu trade-off untuk reasoning penuh.
3. **Full auto + prompt injection = risiko nyata.** Mitigasi: OEP Hybrid/Relay + taint tracking + kill-switch. Bukan jaminan; ini kontrol.
4. **T3 adalah permukaan paling berbahaya.** Arm hanya kalau perlu, TTL maksimum 1 jam, dengan kill-switch di jari.
5. **PLAN.md v2 tetap wajib dieksekusi sebagai M1.** Tanpa M1, control plane v3 berdiri di atas lubang RCE yang sudah terbukti.

---

## 11. Langkah Berikutnya

1. Approval Tiger untuk arah v3.
2. Eksekusi **M1** (PLAN.md v2 WS-0) -- hari ini, ~2 jam.
3. Lanjut **M2** (WS-A, control plane) -- 3 hari.
4. Setelah M2 stabil, **M3** (exec + scan).
5. **M4** hanya kalau Tiger butuh T3; kalau tidak, T3 tetap off.
6. **M5** (OEP) dan **M6** (stealth + audit) menutup.

Satu command per perubahan, test setelah tiap file, `PLAN_V3.md`/`AUDIT.md` diperbarui kalau ada temuan.

-- [M]
