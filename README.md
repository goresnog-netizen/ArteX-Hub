<div align="center">

<img src="https://readme-typing-svg.demolab.com?font=JetBrains+Mono&weight=800&size=52&duration=3000&pause=1000&color=FACC15&center=true&vCenter=true&width=700&height=90&lines=ArteX-Hub;+AI+Chat+to+IDE+Bridge" alt="ArteX-Hub: AI chat to IDE bridge" />

<p><b>Mengubah chat AI di browser menjadi agen kerja untuk VS Code, Cursor, dan Zed — lewat bridge MCP lokal.</b></p>

[![License](https://img.shields.io/badge/LICENSE-GPL--3.0-FACC15?style=flat-square&labelColor=111111&logo=opensourceinitiative&logoColor=FACC15)](LICENSE)
[![Manifest](https://img.shields.io/badge/MANIFEST-V3-FACC15?style=flat-square&labelColor=111111&logo=googlechrome&logoColor=FACC15)](https://developer.chrome.com/docs/extensions/develop/migrate/what-is-mv3/)
[![Bridge](https://img.shields.io/badge/BRIDGE-V3.11.0-FACC15?style=flat-square&labelColor=111111&logo=socket&logoColor=FACC15)](CHANGELOG.md)
[![MCP](https://img.shields.io/badge/PROTOCOL-MCP-FACC15?style=flat-square&labelColor=111111&logo=modelcontextprotocol&logoColor=FACC15)](https://modelcontextprotocol.io/)

</div>

```console
  ██████╗██████╗ ███████╗███████╗██╗  ██╗██╗   ██╗██╗  ██████╗██╗   ██╗
 ██╔════╝██╔══██╗██╔════╝██╔════╝╚██╗██╔╝██║   ██║██║ ██╔═══██╗╚██╗ ██╔╝
 ██║     ██████╔╝█████╗  ███████╗ ╚███╔╝ ██║   ██║██║ ██║   ██║ ╚████╔╝
 ██║     ██╔══██╗██╔══╝  ╚════██║ ██╔██╗ ██║   ██║██║ ██║   ██║  ╚██╔╝
 ╚██████╗██║  ██║███████╗███████║██╔╝ ██╗╚██████╔╝██║ ╚██████╔╝   ██║
  ╚═════╝╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═════╝    ╚═╝

  > browser extension + local bridge + A2A coordination hub
  > no API key · no subscription · loopback only
```

**ArteX-Hub** adalah kelanjutan dan pengembangan lanjutan dari **Snapgent Connector**. Ekstensi di browser membaca perintah berbentuk JSON dari balasan AI, lalu bridge lokal meneruskannya ke server MCP yang terhubung. Hasilnya dikirim kembali ke chat, sehingga sesi kerja berlanjut tanpa copy-paste.

Gratis dan open source — tidak memerlukan API key, langganan, atau login tambahan.

---

## `01` &nbsp; Atribusi dan lisensi

Proyek ini berdiri di atas karya pembuat awal. Atribusi ini **tidak dihapus dan tidak diubah**:

| Peran | Pihak |
|---|---|
| **Pembuat awal / proyek induk** | **herdo dimas pratirto** ([@herdodp](https://github.com/herdodp)) — [Snapgent Connector](https://github.com/herdodp/snapgent-connector) |
| **Lisensi** | **GNU GPL v3.0** — melihat berkas [LICENSE](LICENSE) |
| **Pengembangan lanjutan** | ArteX-Hub — penamaan ulang, integrasi multi-IDE, privasi, dan koordinasi A2A |

> Seluruh perubahan tetap berada di bawah GPL-3.0. Jika Anda mendistribusikan ulang ArteX-Hub, sertakan berkas `LICENSE` dan atribusi pembuat awal di atas.

**Catatan kompatibilitas:** identifier internal, nama paket, dan beberapa string UI masih memakai `snapgent-*` / `ArteX-TNT` agar kompatibel dengan rilis upstream dan konfigurasi yang sudah ada. Branding repositori dan dokumentasi adalah **ArteX-Hub**.

---

## `02` &nbsp; Yang sudah di-upgrade

Ringkasan dari [CHANGELOG.md](CHANGELOG.md) (V3.9.5 → **V3.11.0**):

### Privasi & keamanan
- **LOCAL-ONLY mode** — saat aktif, halaman provider hanya menerima token amplop berukuran konstan (512 karakter). Metadata (tool, status, ukuran, digest) dienkripsi AES-GCM-256 dengan kunci yang tidak pernah keluar dari browser; payload asli lewat WebSocket ke bridge, bukan lewat chat.
- **Opaque Envelope Protocol (OEP) v1** — kunci sesi X25519 + HKDF + AES-256-GCM. Perintah dan hasil menjadi `[ENV:v1:...]` / `[RES:v1:...]`; provider tidak bisa membaca isinya.
- **WS token auth** — `Authorization: Bearer`, `?token=`, atau subprotocol; soket tanpa token ditutup dengan kode `4001`.
- **Loopback only** — WebSocket `127.0.0.1:17613`, health HTTP `127.0.0.1:17614`. `/auth-token` hanya untuk origin ekstensi, tanpa CORS, dan dibatasi laju.
- **Sandbox Windows Job Objects** — flag `BREAKAWAY_OK` dihapus sehingga child process tidak bisa keluar dari job. URI workspace dinormalisasi; skema non-`file` ditolak.
- **Untrusted peer findings** — hasil agen lain dibatasi 600 karakter, dibersihkan dari karakter kontrol, dan dibungkus penanda `<<<PEER_FINDINGS_UNTRUSTED>>>` sebelum masuk prompt.

### Multi-IDE & koordinasi
- **Deteksi workspace per-IDE** — VS Code, Cursor, dan Zed. Urutan: override `SNAPGENT_WORKSPACE_ROOT` / `SNAPGENT_WORKSPACE`, lalu folder IDE yang benar-benar berjalan, lalu root repositori.
- **Zed sebagai warga pertama** — `zed_a2a_mcp.py` (stdio MCP, 10 tools: health, agents, inbox, send, coord, mesh), pendaftaran idempoten lima server (`snapgent-git`, `snapgent-shell`, `snapgent-shell-sandboxed`, `snapgent-workspace`, `snapgent-a2a`).
- **Coordination hub** — registry provider dengan prioritas, kuota request/token, concurrent limit, health `healthy/degraded/down`, failover otomatis, dan **4 strategi memori** (`sliding`, `append-stable`, `hierarchical`, `rolling-summary`) dengan hard budget.
- **Project mesh** — proyek berupa DAG tugas yang dipersistenkan sebagai JSON; tiap agen punya inbox; temuan satu agen memengaruhi tugas agen berikutnya lewat bus A2A.
- **Shared memory** — memori jangka pendek/panjang bersama di `state/shared_memory.json`, diperlakukan sebagai konten tidak tepercaya saat dimasukkan ke prompt.

### Bridge & operasional
- **Hot reload konfigurasi** — perubahan `config.json` memicu restart server MCP tanpa mematikan bridge.
- **Startup paralel** — semua MCP server dinyalakan bersamaan dengan `asyncio.gather`; kegagalan satu server tidak menunda yang lain.
- **Provider manager** — pemilihan provider berdasarkan kuota, health, latensi, dan prioritas; habis kuota → pindah otomatis.
- **Workspace MCP selalu mengiklankan 28 tools** meski VS Code tidak berjalan; bridge melaporkan ±103 tools dari 4 server aktif.

---

## `03` &nbsp; Cara kerja

```
┌─────────────┐  perintah JSON  ┌──────────────────┐   WebSocket   ┌────────────┐   MCP   ┌──────────────────┐
│  AI chat    │ ──────────────▶ │ ArteX-Hub        │ ────────────▶ │  Bridge    │ ──────▶ │ VS Code/Cursor/  │
│  (browser)  │ ◀────────────── │ (ekstensi MV3)   │ ◀──────────── │ (lokal)    │ ◀────── │ Zed / MCP lain   │
└─────────────┘     hasil       └──────────────────┘               └─────┬──────┘         └──────────────────┘
                                                                         │
                                                        ┌────────────────┴────────────────┐
                                                        │ coordination hub · mesh · A2A    │
                                                        │ quota · failover · shared memory │
                                                        └─────────────────────────────────┘
```

1. Anda mengetik permintaan dalam bahasa natural di chat AI yang didukung.
2. AI membalas dengan perintah JSON ArteX-Hub (atau amplop OEP bila privasi aktif).
3. Ekstensi mendeteksi perintah dan mengirimkannya lewat WebSocket ke bridge lokal.
4. Bridge merutekan ke MCP server yang dipilih; coordination hub memilih provider dan mengelola kuota.
5. Hasil dikembalikan ke chat dan AI melanjutkan pekerjaan.

---

## `04` &nbsp; Provider yang didukung

| Provider | URL | Status |
|---|---|---|
| DeepSeek | `chat.deepseek.com` | ✅ |
| ChatGPT | `chatgpt.com` | ✅ |
| Claude | `claude.ai` | ✅ |
| Google Gemini | `gemini.google.com` | ✅ |
| Kimi | `kimi.ai` | ✅ |
| Grok | `grok.com` | ✅ |
| GLM (Z.ai) | `chat.z.ai` | ✅ |
| Qwen | `chat.qwen.ai` | ✅ |
| Arena | `arena.ai` | ✅ (mode **Direct**) |
| Meta AI | `meta.ai` | ✅ |
| Ptero | `ptero.pro` | ✅ |
| Mistral (Vibe Code) | `chat.mistral.ai` | 🧪 eksperimental |

Aktivasi provider diatur di `snapgent-extension/manifest.json` (`content_scripts`, `host_permissions`) dan `PROVIDER_URLS` di `background.js`.

---

## `05` &nbsp; Persyaratan & instalasi

- **Browser** Chromium (Chrome/Edge), Manifest V3.
- **Bridge** — Windows: `start.bat` (bundled `bridge.exe`). macOS: `MacOS_Start.command`. Linux: `bash Linux_Start.sh` (butuh Python 3.9+, modul `websockets`).
- **IDE** — VS Code (default), Cursor, atau Zed. Buka folder proyek yang ingin dipakai sebelum menekan **Start**.

```bash
# 1. Muat ekstensi
# edge://extensions atau chrome://extensions → Developer mode → Load unpacked
# pilih folder snapgent-extension/

# 2. Jalankan bridge
# Windows: klik dua kali start.bat
# macOS:   klik dua kali MacOS_Start.command
# Linux:   bash Linux_Start.sh

# 3. Cek kesehatan
curl http://127.0.0.1:17614/health
```

`config.json` dibuat lokal oleh launcher dan **tidak** disimpan di Git — di dalamnya ada secret bridge dan path mesin Anda.

Alternatif tanpa browser: **Snapgent App** (Electron, Windows) di `snapgent-app/` dengan browser AI, toolbar, dan bridge bawaan.

```bash
cd snapgent-app
npm install
npm start        # pengembangan
npm run dist     # build installer Windows ke dist/
```

---

## `06` &nbsp; Struktur proyek

```
ArteX-Hub/
├── snapgent-extension/        # ekstensi browser MV3 (muat sebagai unpacked)
│   ├── core/                  # prompt, parser, agent loop, UI, privacy.js
│   ├── providers/             # integrasi per situs AI
│   ├── background.js          # koneksi WebSocket ke bridge
│   └── manifest.json          # izin & content scripts
├── snapgent-app/              # aplikasi desktop Electron (opsional)
├── bridge.py                  # bridge WebSocket + HTTP health + MCP router
├── bridge.exe                 # bridge Windows (bundled)
├── coordination_hub.py        # provider registry, kuota, failover, memori, A2A
├── mesh.py                    # DAG tugas antar-agen + inbox
├── oep.py                     # Opaque Envelope Protocol v1 (X25519 + AES-GCM)
├── zed_a2a_mcp.py             # stdio MCP untuk Zed
├── vscode_mcp.py              # 28 workspace tools
├── git_mcp.py / shell_mcp.py  # Git tools / shell tools (Windows Job Object)
├── autochat/auto_chat.py      # orkestrator auto-chat
├── scripts/                   # register MCP, health check, pack extension, tests
├── docs/USAGE.md              # panduan operasional + bukti verifikasi
├── CHANGELOG.md               # riwayat rilis lengkap
└── LICENSE                    # GNU GPL v3.0
```

---

## `07` &nbsp; Verifikasi

Bukti historis tersimpan di [docs/USAGE.md](docs/USAGE.md) dan [CHANGELOG.md](CHANGELOG.md): health `200` dengan 4/4 server aktif, autentikasi WS ditolak tanpa token (`4001`), origin asing ditolak (`403`), uji `test_coordination.py` 82 checks, `test_mesh.py` 26 checks, `test_bridge.py` 4 tests, serta uji E2E dan Zed MCP stdio lulus.

Untuk memeriksa ulang di mesin Anda:

```bash
python -m pytest -q
python scripts/check_bridge_health.py
```

---

## `08` &nbsp; Masalah umum

| Gejala | Penyebab | Perbaikan |
|---|---|---|
| Bar ArteX-Hub tidak muncul | Ekstensi nonaktif / akses situs diblokir | Aktifkan ekstensi, izinkan di situs chat, muat ulang tab |
| Bridge offline | Launcher belum jalan | Jalankan `start.bat` / `MacOS_Start.command` / `bash Linux_Start.sh` |
| AI menjawab tanpa tools | Model berhenti mengikuti format perintah | Ingatkan format perintah ArteX-Hub atau mulai chat baru |
| Target workspace salah | IDE lain aktif / env override mengarah ke folder lama | Set `SNAPGENT_WORKSPACE_ROOT` lalu restart bridge |
| `bridge old` di popup | Bridge lama masih memegang port 17613 | Tutup launcher, jalankan ulang, lalu reload ekstensi & refresh tab |

---

<div align="center">

**Dibangun di atas karya [herdodp / Snapgent Connector](https://github.com/herdodp/snapgent-connector) — GPL-3.0.**

[Repo ArteX-Hub](https://github.com/goresnog-netizen/ArteX-Hub) · [Changelog](CHANGELOG.md) · [Lisensi](LICENSE)

</div>
