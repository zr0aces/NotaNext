# Changelog

All notable changes to this project will be documented in this file.

## [1.0.8] – 2026-04-15

### Added
- Added support for A5 printing (`a5` option) and printing A5 content on A4 paper (`half` option).
- Print settings (color, copies, paper size) now persist for 30 minutes instead of resetting after the first print.
- Added `a4` option to explicitly reset paper size to A4.

---

## [1.0.7] – 2026-04-15

### Fixed
- **`asyncio.get_event_loop()` deprecation**: `perform_cleanup_async()` now uses `asyncio.get_running_loop()` (introduced in Python 3.7, preferred from 3.10+), eliminating the `DeprecationWarning` emitted by newer Python runtimes when `get_event_loop()` is called with no running loop.

### Improved
- **Configurable Docker image via environment variable**: `docker-compose.yml` now reads the image name from the `DOCKER_IMAGE` env var (default: `ghcr.io/zr0aces/printbot:1.0.7`). This makes it easy to switch between registry, local, and custom builds without editing the Compose file. Set `DOCKER_IMAGE=local/printbot:dev` in `.env` to use a locally built image.

---

## [1.0.6] – 2026-04-15

### Fixed — Bugs
- **Subprocess leak on timeout** (`run_cups_command`): child process is now killed and reaped with `process.kill()` + `await process.wait()` before `TimeoutError` is re-raised, preventing zombie processes and open-pipe fd leaks.
- **Subprocess leak on timeout** (`print_file`): same kill/wait fix applied to the `lp` subprocess — previously the process was abandoned silently on the 30-second timeout.
- **Filename collision under concurrent prints**: switched from `int(time.time())` (1-second resolution) to `uuid.uuid4().hex` — eliminates race conditions when two users send a file in the same second.
- **Home Assistant notify in `finally` block**: moved `notify_homeassistant()` into the `try` block (after confirmed print, before the Telegram reply) to make the intent explicit and remove dependency on the `success` flag.
- **Silent `LOG_LEVEL` fallback**: invalid values (e.g. `VERBOSE`) now print a warning before defaulting to `INFO` instead of failing silently.
- **CUPS HTTP probe unreliable**: `docker-entrypoint.sh` now uses a raw TCP connect (`/dev/tcp/$CUPS_SERVER/631`) instead of `curl http://...`, which works even when the CUPS web UI is disabled.

### Fixed — Security
- **No file size limit**: file size is checked against `MAX_FILE_BYTES` (20 MB) from message metadata _before_ `download_to_drive` is called — prevents disk fill attacks.
- **No rate limiting**: added `PRINT_COOLDOWN = 10` seconds per chat with a `last_print_time` dict; users who send too fast receive a friendly message with the remaining wait time.
- **No file type whitelist**: documents are now validated against `PRINTABLE_EXTENSIONS` (`{.pdf, .ps, .jpg, .jpeg, .png, .gif, .txt, .doc, .docx, .odt}`) before download.
- **Blocking HTTP on event loop**: `notify_homeassistant` was using `urllib.request.urlopen` (a blocking call that stalled the entire event loop up to 3 s). Replaced with `async def` + `httpx.AsyncClient`.
- **`.env` file permissions**: added `chmod 600 .env` to the setup instructions in `README.md`.

### Improved — Performance
- **Cached `shutil.which` calls**: `LP_BIN`, `LPSTAT_BIN`, and `CANCEL_BIN` are now resolved once at import time instead of on every command invocation.
- **`print_options` memory leak**: `cleanup_task` now sweeps and evicts expired entries that were set but never consumed (e.g. user sets `bw` and never sends a file).
- **Blocking I/O in async context**: `perform_cleanup` (uses `os.listdir` / `os.remove`) is no longer called directly from async handlers. A new `perform_cleanup_async()` wrapper offloads the work to a thread pool via `run_in_executor`.

### Improved — Code Quality
- **Type hints on all handler functions**: every command and message handler now carries `(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None` signatures.
- **`docker-compose.yml` `image:` tag**: added `image: ghcr.io/zr0aces/printbot:1.0.6` alongside `build:` so `docker compose up -d` pulls the pre-built image without requiring a local build.
- **Docker log rotation**: added `logging: driver: json-file, max-size: 10m, max-file: 3` to prevent unbounded log growth on long-running hosts.
- **`docker-entrypoint.sh` shell strictness**: changed to `set -euo pipefail` to catch undefined variable references and pipeline failures.
- **`printbot.service` hardening**: added `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ReadWritePaths`, and `ProtectHome` systemd directives.
- **`requirements.txt`**: added `httpx` as an explicit direct dependency (was previously only a transitive dep of `python-telegram-bot`).
- **Supply-chain guidance**: added `pip-compile --generate-hashes` instructions to `README.md` Tech Stack section.

---

## [1.0.5] – 2026-04-15



### Changed
- Bumped `VERSION` constant in `bot.py` to `1.0.5`
- Updated `docker-compose.yml` build arg to `1.0.5`
- Updated `README.md`: corrected Docker base image reference (`ubuntu:22.04`), updated pinned pull example to `1.0.5`, refreshed troubleshooting table
- Updated `.env.example`: improved inline comments and ensured all variables are documented
- Updated `.gitignore`: added `.DS_Store`, `*.egg-info/`, `dist/`, and `*.log` patterns
- Updated `.dockerignore`: added `*.md` exclusion for `CONTRIBUTING.md` and improved comments

---

## [1.0.4] – 2026-04-14

### Added
- Pre-built multi-arch Docker images published to GitHub Container Registry (`ghcr.io/zr0aces/printbot`) — no local build required
- Health check improvements: `HEALTHCHECK` in `Dockerfile` now uses `pgrep -f bot.py`

### Changed
- `LOG_LEVEL` environment variable supported — set to `DEBUG`, `INFO`, `WARNING`, etc. (default: `INFO`)
- Noisy `httpx` and `telegram` library loggers suppressed to `WARNING` level at startup
- `docker-entrypoint.sh` startup wait loop now retries up to 10 times with 2-second intervals before proceeding

---

## [1.0.3] – 2026-04-13

### Added
- All CUPS commands (`lp`, `lpstat`, `cancel`) now explicitly pass `-h <CUPS_SERVER>` for reliable remote server targeting
- `print_file()` explicitly passes `-d <PRINTER_NAME>` to `lp` — eliminates "No default destination" errors
- `print_msg()` attaches the shell command string to `RuntimeError` for inclusion in Telegram error replies
- `run_cups_command()` helper extracts shared async subprocess logic for `status`, `jobs`, and `cancel` handlers
- Async timeout (30 s) on the `lp` subprocess inside `print_file()`

### Changed
- `CUPS_SERVER` and `PRINTER_NAME` are now **required** at runtime; missing values raise `RuntimeError` with a clear message
- `get_cups_server()` and `get_printer_name()` helper functions added for centralised env-var access
- Error replies in Telegram now include truncated CUPS `stderr` (max 300 chars) via `MAX_STDERR_LENGTH` constant
- `requirements.txt` pinned to `python-telegram-bot==22.7` (CUPS 2.4.1 compatible)

---

## [1.0.2] – 2026-04-14

### Fixed
- Corrected Canon PPD filename to `CNRCUPSLBP7110CZNK.ppd` across `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, and `README.md`
- `PRINTER_NAME` default aligned to `Canon_LBP7110Cw` in all configuration files and examples

---

## [1.0.1] – 2026-04-14

### Added
- `docker-entrypoint.sh`: dynamically writes `/etc/cups/client.conf` (`ServerName`) from `CUPS_SERVER` env var
- `docker-entrypoint.sh`: waits for CUPS server reachability (HTTP probe on port `631`) before setting the default printer
- `lpoptions -d <PRINTER_NAME>` call in entrypoint to set a system-wide default printer inside the container

### Changed
- `docker-entrypoint.sh` falls back to `cups` if `CUPS_SERVER` is not set

---

## [1.0.0] – 2026-04-11

### Added
- Print options via message prefix (`bw`, `gray`, `2x`–`4x`) — set per chat, applied to next file, then reset
- `/jobs` command — shows the current CUPS print queue via `lpstat -o`
- `/cancel` command — cancels all pending jobs via `cancel -a`
- Home Assistant webhook integration — fires `printbot_job_sent` event after each successful print (`HA_URL` + `HA_TOKEN` env vars)
- `VERSION` constant in `bot.py`, logged at startup
- OCI image labels in `Dockerfile` (`org.opencontainers.image.*`)
- `.dockerignore` to minimise image build context
- GitHub Actions workflow to build and push multi-arch Docker images on version tags
- `CHANGELOG.md`

### Improved
- Print errors now include CUPS stderr output in the Telegram reply
- Auto-cleanup: downloaded file is removed immediately after a successful print
- `HELP_TEXT` updated to document all commands and print options
- `docker-compose.yml` uses a versioned image name

### Removed
- `Pipfile` (redundant alongside `requirements.txt`)
- `.vscode/settings.json` (machine-specific local path)

### Initial feature set
- Telegram bot using python-telegram-bot v22.7
- CUPS integration via `lp` subprocess
- Access control via `ALLOWED_CHAT_IDS` (numeric chat IDs)
- `/start`, `/help`, `/status`, `/clean` commands
- Docker Compose and systemd deployment options
