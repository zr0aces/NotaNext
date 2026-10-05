# NotaNext

[![Release](https://img.shields.io/github/v/release/zr0aces/NotaNext)](https://github.com/zr0aces/NotaNext/releases)
[![Docker Release](https://github.com/zr0aces/NotaNext/actions/workflows/docker-release.yml/badge.svg)](https://github.com/zr0aces/NotaNext/actions/workflows/docker-release.yml)
[![Docker](https://img.shields.io/badge/ghcr.io-zr0aces%2Fnotanext-blue)](https://github.com/zr0aces/NotaNext/pkgs/container/notanext)
[![Python](https://img.shields.io/badge/python-3.10%20(Docker)-blue)](https://www.python.org/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-22.8-blue)](https://python-telegram-bot.org/)

## Overview

NotaNext is a Telegram bot that sends photos and documents straight to a CUPS-connected printer. Run it on a Raspberry Pi (or any Linux box) as a systemd service or a Docker container, then print anything from your phone by just sending it to the bot.

**Key features**

- 📄 Print photos and documents sent via Telegram
- ⚙️ Print options — send `bw`, `1x`–`4x`, `a4`/`a5`, `half` before a file to customise the print
- 🧩 Guided `/preferences` wizard to save per-chat default print settings
- 🔒 Access control via numeric chat IDs (`ALLOWED_CHAT_IDS`)
- 🖨️ CUPS integration — uses `lp` with explicit `-h <server>` and `-d <printer>` flags
- 🌐 Remote CUPS server support — connects over TCP port 631
- 🟢 `/status` command to check live printer availability
- 📭 `/jobs` and `/cancel` commands for queue management
- 🏠 Optional Home Assistant webhook after each print
- 🐳 Docker (multi-arch: amd64 & arm64) and systemd deployment options
- 🪵 Configurable log level via `LOG_LEVEL` env var
- ✅ No shell injection — all commands use argument lists

At startup, the bot registers its Telegram command menu automatically via `set_my_commands`.

---

## How to Use

### 1. Prerequisites

- A printer configured in CUPS (`http://<cups-host>:631`)
- A Telegram Bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram chat ID — send a message to [@userinfobot](https://t.me/userinfobot) to find it

For **systemd** deployment, you also need:

```bash
sudo apt-get install cups-client python3 python3-pip
```

For **Docker** deployment:

```bash
sudo apt-get install docker.io docker-compose-plugin
```

---

### 2. Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
chmod 600 .env   # restrict read access — .env contains your Telegram token
```

| Variable | Required | Description |
|----------|----------|-------------|
| `TOKEN` | ✅ Yes | Telegram Bot API token from [@BotFather](https://t.me/BotFather) |
| `CUPS_SERVER` | ✅ Yes | CUPS server hostname or IP (e.g. `cups` or `192.168.1.100`). Default: `cups`. |
| `PRINTER_NAME` | ✅ Yes | Printer name as registered in CUPS (e.g. `Canon_LBP7110Cw`). Run `lpstat -v` to list available printers. |
| `ALLOWED_CHAT_IDS` | ❌ No | Comma-separated numeric chat IDs permitted to print/clean. **If unset, anyone can print.** Find your ID via [@userinfobot](https://t.me/userinfobot). |
| `LOG_LEVEL` | ❌ No | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default: `INFO`. |
| `HA_URL` | ❌ No | Home Assistant base URL (e.g. `http://homeassistant:8123`). Required together with `HA_TOKEN`. |
| `HA_TOKEN` | ❌ No | Home Assistant long-lived access token. Required together with `HA_URL`. |
| `TZ` | ❌ No | Timezone for container logs (e.g. `Asia/Bangkok`). Default: UTC. |

---

### 3. Run

#### Option A — Docker Compose (recommended)

```bash
git clone https://github.com/zr0aces/NotaNext.git
cd NotaNext
cp .env.example .env   # fill in TOKEN, CUPS_SERVER, PRINTER_NAME
docker compose up -d
docker compose logs -f
```

The `docker-compose.yml` pulls directly from GitHub Container Registry — no local build required.

To build locally instead:

```bash
docker compose up -d --build
```

#### Option B — systemd Service (Raspberry Pi / Linux)

```bash
git clone https://github.com/zr0aces/NotaNext.git /home/pi/notanext
cd /home/pi/notanext
mkdir -p data
pip install -r requirements.txt
cp .env.example .env   # fill in TOKEN, CUPS_SERVER, PRINTER_NAME

sudo cp notanext.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now notanext.service
sudo systemctl status notanext.service
```

> **Note:** The `notanext.service` file assumes the user `pi` and path `/home/pi/notanext`. Adjust `User=` and `WorkingDirectory=` as needed. On Raspberry Pi OS Bookworm (PEP 668), installing into the system Python requires `pip install --break-system-packages -r requirements.txt` or a virtual environment with `ExecStart` pointed at its Python binary.

#### Option C — Run directly

```bash
git clone https://github.com/zr0aces/NotaNext.git
cd NotaNext
pip install -r requirements.txt
cp .env.example .env   # fill in TOKEN, CUPS_SERVER, PRINTER_NAME
python3 bot.py
```

---

### 4. Send a File to Print

Once the bot is running, open it in Telegram and:

1. Send any **photo** or **document** — the bot forwards it to the printer automatically.
2. Run `/preferences` (or `/start`) once to save your default print mode.
3. Use the commands below for status and maintenance.

#### Bot Commands

| Command | Description | Who can use |
|---------|-------------|-------------|
| `/start` | Show the welcome message and open the preferences wizard | Allowed chat IDs only |
| `/help` | List available commands | Everyone |
| `/preferences` | Set persistent default print preferences (color/B&W, normal/half, A4/A5) | Allowed chat IDs only |
| `/status` | Check printer availability via CUPS | Everyone |
| `/jobs` | Show active jobs on this printer | Allowed chat IDs only |
| `/cancel` | Cancel all jobs on this printer — or, while the `/preferences` wizard is open, abort the wizard | Allowed chat IDs only |
| `/clean` | Delete cached files and clear your half-mode queue | Allowed chat IDs only |

#### Default Preferences

`/preferences` saves your per-chat default profile:

- Color mode: Color or Gray (B&W)
- Sheet mode: Normal (full page) or Half sheet (2-up)
- Paper size: A4 or A5

Saved defaults are persistent and automatically used when no temporary override is active.

#### Print Options

Before sending a file, text the bot with one or more options for the **next** print:

| Option | Effect |
|--------|--------|
| `bw` or `gray` | Print in black & white |
| `color` | Print in colour |
| `1x`, `2x`, `3x`, `4x` | Number of copies (`1x` resets to a single copy) |
| `a4`, `a5` | Paper size |
| `half` or `2up` | Half-sheet mode — see below |
| `normal`, `full`, `single`, `1up` | One page per sheet (leaves half mode) |
| `print` | Print whatever is queued in half mode right now |
| `bw 2x a5` | Combine options |

A message containing an unrecognised word is rejected in full — no partial settings are applied.

Options apply to all subsequent files for the **next 30 minutes**, and each file you send extends that window. Once it lapses, the bot falls back to the defaults you saved with `/preferences` (or to colour, 1 copy, A4 if you never saved any). Temporary options always override saved defaults while the window is active.

##### Half-sheet mode

`half` prints **two pages side by side on one sheet** (`number-up=2`). It does not change the paper size — combine it with `a4` or `a5` to choose that separately.

Half mode also changes the workflow: files are **queued** rather than printed immediately, so two of them can share a sheet.

- Send a file → it is queued, and the bot tells you how many sheets are pending.
- Send a second file → both print together on one sheet.
- Send `print` at any time → whatever is queued prints now. A lone file is padded with a blank half so it still lands on half a sheet.
- Send `normal` → half mode is switched off and any queued files are discarded.

Because half mode merges files into a single PDF, it only accepts images and PDFs (`.gif`, `.jpeg`, `.jpg`, `.pdf`, `.png`). Send `normal` first to print a `.docx`, `.odt`, `.txt` or `.ps`. Half mode enforces safe admission limits: up to 10 files queued per chat, at most 50 pages per merged print job, JPEGs up to 48 MP, and PNG/GIF images up to 12 MP.

---

## Releases

Docker images are published automatically to [GitHub Container Registry](https://github.com/zr0aces/NotaNext/pkgs/container/notanext) when a version tag is pushed. Multi-arch images are built for **linux/amd64** and **linux/arm64** (Raspberry Pi).

```bash
# Pull the latest release
docker pull ghcr.io/zr0aces/notanext:latest

# Pin to a specific version
docker pull ghcr.io/zr0aces/notanext:1.3.0
```

Version bumps across all project files are automated from the single source of truth (`VERSION`):

```bash
python3 scripts/bump_version.py check       # Verify version consistency
python3 scripts/bump_version.py patch       # Bump patch version
```

See [docs/USER-SPEC.md](docs/USER-SPEC.md) for the full user-facing specification — every command, option, limit and lifetime in one place — and [docs/CHANGELOG.md](docs/CHANGELOG.md) for release notes.

---

## Tech Stack

| Component | Version |
|-----------|---------|
| Python | 3.10 (Docker image) |
| [python-telegram-bot](https://python-telegram-bot.org/) | 22.8 |
| [httpx](https://www.python-httpx.org/) | 0.28.1 (async HTTP — Home Assistant webhook) |
| [Pillow](https://python-pillow.org/) | 12.3.0 (image → PDF conversion for half mode) |
| [pypdf](https://pypdf.readthedocs.io/) | 6.19.0 (PDF merging for half mode) |
| CUPS client (`lp` / `lpstat` / `cancel`) | System package |
| Docker base image | `ubuntu:22.04` |

> **Supply-chain hardening:** To pin `requirements.txt` with SHA-256 hashes, run
> `pip install pip-tools && pip-compile --generate-hashes requirements.txt`
> and commit the resulting `requirements.txt` lockfile.

### Self-check and Tests

- `python3 test_bot.py`: Pure logic self-check (print-option parsing, preference persistence round-trip, half-mode extension gate, version consistency across all files). Stubs third-party imports and runs on a bare interpreter.
- `python3 scripts/check_io.py`: Subprocess argument vectors, preference persistence quarantine and async offload, in-flight path tracking, and cleanup races with fakes (bare interpreter).
- `python3 scripts/check_runtime.py`: Tests real PTB handler filters, conversation re-entry mid-wizard, and Pillow/pypdf merge, downscaling, and memory limits (requires `requirements.txt`).

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `No default destination` error | Ensure `PRINTER_NAME` is set and matches a printer registered on the CUPS server (`lpstat -v -h <server>`). |
| Printer not detected | Check CUPS at `http://<cups-host>:631`; ensure the printer is added and enabled. |
| Bot not responding | Verify `TOKEN` in `.env` is correct and the bot process is running (`docker compose logs notanext`). |
| `/status` shows no printers | Run `lpstat -p -h <cups-host>` on the host to confirm CUPS sees your printer. |
| Connection refused on port 631 | Make sure the CUPS container/service exposes port 631 and `CUPS_SERVER` points to the correct host. |
| Permission issues (systemd) | Ensure the running user is in the `lpadmin` group (`sudo usermod -aG lpadmin $USER`). |
| Canon driver not found | Verify the PPD file `CNRCUPSLBP7110CZNK.ppd` is installed in `/usr/share/cups/model/` on the CUPS server. |

---

Contributions and issues are welcome!
