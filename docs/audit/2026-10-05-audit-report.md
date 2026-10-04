# NotaNext — Full Codebase Engineering Audit

- **Date:** 2026-10-05
- **Revision audited:** `d439843` (branch `master`, clean worktree)
- **Version:** 1.2.2
- **Coverage:** Full (every tracked first-party file read; see §9)

---

## 1. Executive Summary

**Architecture.** NotaNext is a single-process Telegram bot (`bot.py`, python-telegram-bot 22.7, long polling). Incoming photos/documents are validated against Telegram metadata, downloaded into `data/`, and handed to `lp` on a remote CUPS server. Half-sheet mode queues files per chat in memory, merges them into one PDF with Pillow + pypdf in a worker thread, and prints with `number-up=2`. State is four module-level dicts keyed by chat; only saved preferences persist (`data/preferences.json`). Deployment is Docker (Ubuntu 22.04, root, compose with 512 MB limit) or systemd. CI only builds/pushes images on `v*.*.*` tags.

PTB processes updates **sequentially** (`concurrent_updates == 1`, verified), so every handler blocks every other chat while it awaits downloads, `lp` (≤30 s), the PDF merge (no timeout), or Home Assistant (≤3 s).

**Most important risks**

- **Availability via untrusted file parsing (F-01, F-02).** Half mode parses user PDFs/images in-process with pypdf 4.1.0 (≈45 distinct published DoS advisories: infinite loops, RAM exhaustion; fixes up to 6.19.0) and Pillow 10.2.0 (multiple decoder memory-safety advisories). The merge has no timeout and runs inside the sequential update pipeline, so one bad PDF can hang the bot for every chat until restart. A 144 MP PNG (0.6 MB file) — below Pillow's hard bomb limit — peaked at **1,146 MB RSS** in `merge_to_pdf`, more than double the compose memory limit.
- **Correctness (F-03, F-04, F-05).** Editing the caption of an already-sent photo/document re-runs `print_msg` and prints it again (verified handler match). An abandoned preferences wizard makes `/start` and `/preferences` silently do nothing forever for that user (verified). Transparent PNGs print with black backgrounds in half mode (verified pixel conversion).
- **Deployment (F-06, F-07).** The systemd unit's `ReadWritePaths=/home/pi/notanext/data` names a directory a fresh clone does not have; the README install steps never create it. The container runs as root while parsing untrusted files.
- **Testing.** `test_bot.py` passes (14/14) but covers only pure helpers; none of the failure paths above, the half-queue state machine, `merge_to_pdf`, `print_file` command construction, or preference loading is exercised.

**Needs deeper investigation:** per-advisory reachability of the pypdf/Pillow CVEs through `PdfReader` → `add_page` → `write`; real CUPS behaviour of `cancel -a` from a remote root client; systemd start behaviour on the target Pi.

---

## 2. Findings

### [F-01] [HIGH] — Vulnerable PDF/image parsers process untrusted uploads

- **Category:** Security / Dependencies / Availability
- **Location:** `requirements.txt:6-7`; `bot.py:884-922` (`merge_to_pdf`); `bot.py:906` (`Image.open`); `bot.py:913-914` (`PdfReader`)
- **Confidence:** High that the versions are affected; Medium on which specific advisories are reachable (not individually exercised).
- **Problem:** `pypdf==4.1.0` and `Pillow==10.2.0` are pinned. `pip-audit --local` (pinned set installed in a scratch venv) reports pypdf advisories fixed across 6.0.0–6.19.0 — crafted PDFs causing infinite loops, long runtimes, and RAM exhaustion (e.g. CVE-2025-55197, CVE-2025-62707, CVE-2025-62708, CVE-2026-24688, CVE-2026-54531, CVE-2026-102993) — and Pillow advisories fixed in 10.3.0/12.2.0/12.3.0, including decoder issues reachable from `Image.open` (CVE-2026-54058 raw-codec load, CVE-2026-59204 JPEG 2000 decode). `Image.open(fp)` sniffs the format from content, not the extension, so a file named `.png` is decoded by whichever plugin matches its bytes.
- **Evidence:** `pip-audit` output (§9); `bot.py:906` calls `Image.open(fp)` with no `formats=` restriction; `bot.py:914` passes user PDFs to `PdfReader`.
- **Impact:** Process hang, OOM kill, or (for native decoder bugs) memory corruption in a root process (see F-07). With `ALLOWED_CHAT_IDS` unset, any Telegram user can reach this; with it set, any allowed user or any malformed PDF they forward.
- **How it can occur:** Send `half`, then a crafted PDF or an image whose bytes are JPEG 2000/another plugin format under a `.png` name.
- **Recommended fix:** Upgrade to current pypdf (≥6.19.0) and Pillow (≥12.3.0); re-run `merge_to_pdf` checks against the new APIs (`PdfWriter.add_page`, `add_blank_page`, `mediabox` are stable). Restrict `Image.open(fp, formats=["JPEG", "PNG", "GIF"])`. Add `pip-audit` to the release workflow (F-15).
- **Expected impact:** Removes the known-exploitable parser paths and narrows the image attack surface to three decoders.

### [F-02] [HIGH] — Unbounded, untimed PDF merge blocks the whole bot

- **Category:** Reliability / Performance / Resource
- **Location:** `bot.py:958` (`await asyncio.to_thread(merge_to_pdf, ...)`); `bot.py:903-922`; `bot.py:831-848` (unbounded `half_queue` growth); `docker-compose.yml:19`
- **Confidence:** High (memory figure measured; sequential processing verified).
- **Problem:** The merge has no timeout, no page cap, no pixel cap below Pillow's 178 MP error threshold, and no cap on queued files. It runs in a worker thread, but the handler awaits it and PTB handles updates one at a time, so a slow or hung merge stalls every chat. `to_thread` work cannot be cancelled, so even adding `wait_for` alone would leak a spinning thread.
- **Evidence:** `ApplicationBuilder().build().concurrent_updates == 1`. A 12000×12000 RGBA PNG (595 KB on disk, below the 20 MB limit, 144 MP < 178 MP so only a warning) made `merge_to_pdf` peak at 1,146 MB RSS. Compose limits the container to 512 MB with `restart: always`. Half-mode queueing (`bot.py:831`) is not rate-limited; while flushes are rate-limited the queue keeps growing and the next flush merges all of it at once.
- **Impact:** OOM-kill and restart (losing all sessions and queues; the startup cleanup then deletes queued files), or an indefinite hang of all chats until a manual restart. On systemd there is no memory limit, so a Pi swaps.
- **How it can occur:** Allowed user sends a large-dimension photo-as-document in half mode; or a pypdf infinite-loop PDF (F-01); or rapid uploads during cooldown building a 30+ file queue.
- **Recommended fix:** Run the merge in a subprocess (`asyncio.create_subprocess_exec(sys.executable, "-c", ...)` or a small `merge` entry point in `bot.py`) with the same kill-and-reap timeout pattern `print_file` already uses, plus an `RLIMIT_AS` in the child. Set `Image.MAX_IMAGE_PIXELS` to a print-appropriate bound (e.g. 50 MP) and treat `DecompressionBombWarning` as an error. Cap queued files per chat and total pages per job; reject with a clear reply.
- **Expected impact:** A bad file fails one job with a reply instead of hanging or killing the bot.

### [F-03] [MEDIUM] — Editing a sent photo/document caption reprints it

- **Category:** Correctness
- **Location:** `bot.py:1313-1320` (photo/document `MessageHandler`); also `bot.py:1322-1329`, `1334-1343`
- **Confidence:** High (verified: an `Update(edited_message=<photo message>)` matches the print handler's filter in PTB 22.7).
- **Problem:** PTB `MessageHandler` filters match `edited_message` updates by default. No handler excludes `filters.UpdateType.EDITED`.
- **Evidence:** Scratch check printed `edited photo update matches print handler: True`.
- **Impact:** Duplicate paper/toner use; in half mode, the edited file is queued again and can trigger an auto-flush. Editing a text option message re-applies options; editing a sticker etc. gets a spurious reply.
- **How it can occur:** User sends a document, then edits its caption to fix a typo.
- **Recommended fix:** Add `& filters.UpdateType.MESSAGE` (or `~filters.UpdateType.EDITED`) to all three `MessageHandler` filters, e.g. by folding it into `chat_id_filter` once at `bot.py:1263-1270`.
- **Expected impact:** Each Telegram message prints at most once.

### [F-04] [MEDIUM] — Abandoned wizard silently disables `/start` and `/preferences`

- **Category:** Correctness / UX
- **Location:** `bot.py:1291-1302` (`ConversationHandler` without `allow_reentry` or `conversation_timeout`)
- **Confidence:** High (verified with PTB 22.7: with conversation state set, `/start` and `/preferences` are not handled; defaults are `allow_reentry=False`, `conversation_timeout=None`).
- **Problem:** If a user opens the wizard and never taps all three buttons, their conversation stays in `PREF_COLOR`/`PREF_MODE`/`PREF_PAPER` until process restart. Entry points are skipped while in a state, and no other handler accepts those commands (`unsupported_message` excludes commands), so the bot does not reply at all. After a restart, tapping buttons on an old wizard message matches no handler, so the callback query is never answered (spinner).
- **Evidence:** Scratch check: `mid-wizard /preferences handled: False`, `mid-wizard /start handled: False`.
- **Impact:** Users believe the bot is broken; only `/cancel` (undocumented for this case) recovers.
- **How it can occur:** `/preferences`, tap Color, close the chat; days later send `/preferences`.
- **Recommended fix:** `ConversationHandler(..., allow_reentry=True, conversation_timeout=SESSION_TTL)`. Optionally register a top-level `CallbackQueryHandler(pattern="^pref_")` that answers stale buttons with "This menu expired — send /preferences".
- **Expected impact:** Wizard commands always respond.

### [F-05] [MEDIUM] — Half mode prints transparent images with black backgrounds

- **Category:** Correctness
- **Location:** `bot.py:906-908`
- **Confidence:** High (verified: fully transparent black RGBA pixel converts to `(0, 0, 0)`).
- **Problem:** `img.convert('RGB')` drops alpha without compositing. Most transparent PNG/GIF content (screenshots with transparency, stickers saved as PNG, logos) stores transparent pixels as `(0,0,0,0)`, so they become solid black. Palette (`P`) images with a transparency index behave the same way. EXIF orientation is also ignored, so photos sent *as documents* print rotated.
- **Evidence:** Scratch check output; normal mode is unaffected because CUPS receives the original file.
- **Impact:** Large black areas — wasted toner and unusable prints — only in half mode.
- **How it can occur:** `half`, then send a PNG with a transparent background as a document.
- **Recommended fix:** Apply `ImageOps.exif_transpose`, then for images with alpha (`RGBA`, `LA`, or `P` with `transparency`) convert to `RGBA` and paste onto a white `RGB` canvas using the alpha channel as mask before saving to PDF.
- **Expected impact:** Half-mode output matches what users see on screen.

### [F-06] [MEDIUM] — systemd unit fails on a fresh install (missing `data/`)

- **Category:** Deployment
- **Location:** `notanext.service:21`; `README.md:98-108`
- **Confidence:** Medium-High (systemd semantics; not executed here — no systemd target available).
- **Problem:** `ReadWritePaths=/home/pi/notanext/data` has no `-` prefix. systemd fails namespace setup (exit status 226/NAMESPACE) when a listed path does not exist. `data/` is gitignored and not created by `git clone`; the README systemd steps never create it. `bot.py` would create it, but never gets to run.
- **Evidence:** `.gitignore` has `data/`; README Option B has no `mkdir`.
- **Impact:** Service never starts on a clean Pi install; error is opaque.
- **How it can occur:** Follow README Option B verbatim.
- **Recommended fix:** Add `mkdir -p data` to the README Option B steps. This keeps the unit's write restriction strict. Do not switch to the `-` prefix: the service would start, but writes to the missing directory would then fail under `ProtectSystem=strict`.
- **Expected impact:** First-run install works.

### [F-07] [MEDIUM] — Container runs as root while parsing untrusted files

- **Category:** Security / Deployment
- **Location:** `Dockerfile:1-43` (no `USER`); `docker-entrypoint.sh:7-14` (writes `/etc/cups/client.conf`)
- **Confidence:** High.
- **Problem:** The bot, Pillow and pypdf all run as UID 0. Files written to the bind-mounted `./data` are root-owned on the host. Any memory-safety bug in a decoder (F-01) executes as root inside the container. The entrypoint needs root only to write `/etc/cups/client.conf`.
- **Evidence:** No `USER` directive; `CMD ["python3", "bot.py"]` runs via `exec "$@"` as root.
- **Impact:** Larger blast radius for any parser compromise; host-side permission friction on `data/`.
- **How it can occur:** Any exploited decoder bug.
- **Recommended fix:** Create an unprivileged user in the Dockerfile and either `chown` `/etc/cups` to it at build time or set `CUPS_SERVER` via the environment only (CUPS clients honour `CUPS_SERVER`, and `bot.py` already passes `-h` everywhere, making `client.conf` redundant). Then `USER notanext`. Add `security_opt: [no-new-privileges:true]` and `read_only: true` with `/app/data` and `/tmp` writable in compose.
- **Expected impact:** Parser compromise is contained to an unprivileged user.

### [F-08] [LOW] — Periodic cleanup can delete files of in-flight jobs

- **Category:** Concurrency
- **Location:** `bot.py:1169-1180`, `bot.py:1183-1209`; in-flight paths at `bot.py:874-877`, `bot.py:776-791`, `bot.py:954-959`
- **Confidence:** Medium (logic traced; not reproduced — needs timing).
- **Problem:** `cleanup_task` runs as an independent asyncio task (not an update), so it interleaves with a handler that is awaiting a download, merge, or `lp`. It protects only files still in `half_queue`. A normal-mode download, a flushed half queue (popped at `bot.py:777` before printing), the merged PDF, and `preferences.json.tmp` are unprotected while `perform_cleanup` runs in an executor thread.
- **Impact:** Every 6 h, a job in flight at that moment can fail (`lp` cannot read the file) or a preference save can fail (`os.replace` on a deleted tmp, logged only).
- **Recommended fix:** Track in-flight paths in a module-level `set` (add before download, discard in `_print_and_reply`'s `finally` and `print_file`'s `finally`) and include them in `skip_paths`; skip files modified within the last `SESSION_TTL` as a simpler alternative.
- **Expected impact:** Removes a rare, hard-to-diagnose print failure.

### [F-09] [LOW] — `/cancel` and `/jobs` act on every queue on the CUPS server

- **Category:** Security / Correctness
- **Location:** `bot.py:571`, `bot.py:586`
- **Confidence:** Medium (CUPS server policy decides whether others' jobs are actually cancelled; not exercised).
- **Problem:** `lpstat -o` and `cancel -a` without a destination cover all printers on the server, not `PRINTER_NAME`. Any allowed chat sees other users' job titles (often file names) and cancels everything the CUPS policy permits.
- **Impact:** Information disclosure and over-broad cancellation on shared CUPS servers.
- **Recommended fix:** Pass the printer: `lpstat -o PRINTER_NAME`, `cancel -a PRINTER_NAME` (via `get_printer_name()` inside `run_cups_query` callers).
- **Expected impact:** Commands match the documented "one printer per bot" scope (USER-SPEC §12).

### [F-10] [LOW] — Preferences file can be silently discarded

- **Category:** Data integrity
- **Location:** `bot.py:301-327`, `bot.py:330-340`
- **Confidence:** High (code path).
- **Problem:** Any load failure (corrupt JSON, non-dict top level) sets `user_preferences = {}` and logs a warning; the next save overwrites the original file. Saves do not `fsync` before `os.replace`, so a power loss on an SD-card Pi can leave an empty/zero-length file, which then takes the same path.
- **Impact:** All saved profiles lost without an obvious signal.
- **Recommended fix:** On load failure, rename the bad file to `preferences.json.corrupt-<ts>` before continuing; `f.flush(); os.fsync(f.fileno())` before `os.replace`. Note `perform_cleanup` deletes everything except `preferences.json`, so the backup name must be added to its skip set (or stored outside the sweep).
- **Expected impact:** Corruption becomes recoverable.

### [F-11] [LOW] — Half-queue lifetime and cross-chat `/clean` contradict the spec

- **Category:** Correctness / Multi-tenant
- **Location:** `bot.py:1126-1167` (6 h sweep), `bot.py:591-599` (`/clean`), `bot.py:494-518` (wizard does not touch `half_queue`)
- **Confidence:** High.
- **Problem:** USER-SPEC says queues last 30 minutes, but expiry is only enforced by the 6-hourly sweep, so a queued file can still be printed with `print` up to ~6.5 h later (forced to 2-up at `bot.py:763`). Saving "Normal" in the wizard does not discard the queue, unlike the `normal` keyword. `/clean` from any allowed chat clears every chat's queue and files.
- **Impact:** Surprising prints of stale files; one user wiping another's pending work.
- **Recommended fix:** Check `entry.ts` against `SESSION_TTL` in `_flush_half_queue` and `print_msg` before use; discard the queue in `pref_paper_callback` when the saved mode is normal; scope `/clean` to the caller's queue (or document it as global admin action).
- **Expected impact:** Behaviour matches USER-SPEC §6–§7.

### [F-12] [LOW] — Markdown replies interpolate user-controlled text

- **Category:** Correctness
- **Location:** `bot.py:685-689`
- **Confidence:** Medium (Telegram legacy-Markdown parse behaviour, not sent live).
- **Problem:** The unsupported-type reply wraps the user's file extension in backticks with `parse_mode="Markdown"`. A filename like `x.a`b` yields an unbalanced entity; Telegram rejects the reply and the user gets only the generic `error_handler` apology. This is the same class of bug the 1.2.1 changelog fixed for CUPS output.
- **Recommended fix:** Send that reply without `parse_mode`, or escape with `telegram.helpers.escape_markdown`.
- **Expected impact:** Correct rejection message for odd filenames.

### [F-13] [LOW] — Error replies expose internals

- **Category:** Security (information disclosure)
- **Location:** `bot.py:729-737`
- **Confidence:** High.
- **Problem:** Print failures echo the full `lp` command (CUPS host, printer name, server paths) and unexpected exceptions echo `str(e)` to the chat.
- **Impact:** Minor; limited to allowed chats unless the allowlist is unset.
- **Recommended fix:** Keep the command in logs; reply with the CUPS stderr only. Replace `f"❌ Unexpected error: {e}"` with a generic message.
- **Expected impact:** Less infrastructure detail in chat history.

### [F-14] [LOW] — `bump_version.py` ignores flags placed before the subcommand

- **Category:** Tooling correctness
- **Location:** `scripts/bump_version.py:279-313`
- **Confidence:** High (reproduced in an isolated `git archive` copy).
- **Problem:** Flags are defined on both the main parser and each subparser; the subparser's `False` default overwrites the main parser's value. `python3 scripts/bump_version.py --dry-run patch` **writes** files (`VERSION` became `1.2.3`). `--git-commit` alone also tags; `git add -A` stages unrelated work.
- **Impact:** A "preview" modifies the tree; a release commit can capture unrelated files.
- **Recommended fix:** Define the flags once via a shared parent parser (`parents=[common]`) with `default=argparse.SUPPRESS` on the main parser, or only on subparsers. Honour `--git-commit` and `--git-tag` separately; `git add` only the files `sync_version` touched.
- **Expected impact:** Dry runs are safe.

### [F-15] [LOW] — Release pipeline publishes without any check; `latest` follows pre-releases

- **Category:** CI/CD
- **Location:** `.github/workflows/docker-release.yml:1-54`
- **Confidence:** High.
- **Problem:** Tag push builds and pushes multi-arch images with no `python3 test_bot.py`, no `bump_version.py check`, and no dependency audit. `type=raw,value=latest` is applied to every `v*.*.*` tag, including `v2.0.0-rc1`.
- **Impact:** A broken or vulnerable build can ship as `latest`.
- **Recommended fix:** Add a job running `python3 test_bot.py`, `python3 scripts/bump_version.py check`, and `pip-audit -r requirements.txt` before build; use `latest=auto` in `docker/metadata-action` flavor.
- **Expected impact:** Release gating with no runtime cost.

### [F-16] [INFO] — Weak container health check and stale docs

- **Category:** Observability / Docs
- **Location:** `Dockerfile:39-40`; `README.md:6`, `README.md:215`; `bot.py:719-724`
- **Problem:** `pgrep -f bot.py` reports healthy while polling is wedged (e.g. F-02 hang). README states Python 3.12; the image is Ubuntu 22.04 (Python 3.10). The Home Assistant event's `file_name` carries UUID filenames, not the user's original names. `.dockerignore` does not exclude `.claude/`, so a local `--build` copies agent checkpoint files into the image (CI checkouts are unaffected).
- **Recommended fix:** Touch a heartbeat file from `cleanup_task`-style periodic code or a PTB job and check its age; correct the README version; pass the original filename (`doc.file_name` / `"photo.jpg"`) through to the HA payload; add `.claude/` to `.dockerignore`.

---

## 3. Performance & Resource Optimization

- **High impact:** F-02 — isolate and bound the merge (subprocess + timeout + pixel/page/queue caps). Measured 1,146 MB peak for a 0.6 MB, 144 MP PNG.
- **Medium impact:** Sequential update processing means one chat's `lp` (≤30 s), download, or merge stalls all chats, and unfiltered `/status` from strangers enqueues `lpstat` calls ahead of real work. For a small private bot this is acceptable; if latency matters, enable `concurrent_updates` only after per-chat locking replaces the reliance on serial processing (the rate-limit slot claim at `bot.py:861` is already written for concurrency, but the half-queue code is not).
- **Low impact:** None worth changing without measurement.
- **Benchmark candidates:** Merge time/memory for realistic phone photos (12–50 MP) on the target Pi to choose the `MAX_IMAGE_PIXELS` bound; downsampling images to ~300 DPI at A5-half size before PDF conversion would cut memory and spool size substantially but needs measurement on the real printer.

## 4. Security Findings

- F-01 (HIGH) vulnerable pypdf/Pillow on an untrusted-input path.
- F-07 (MEDIUM) root container.
- F-09 (LOW) server-wide `/jobs` and `/cancel`.
- F-12 (LOW) Markdown interpolation; F-13 (LOW) internal detail in replies.
- Configuration risk already documented by the project: unset `ALLOWED_CHAT_IDS` opens printing to everyone (logged at startup). `/status` is intentionally unfiltered; it discloses printer names/state to any Telegram user.
- Verified OK: all subprocesses use argument lists; Telegram token is not logged (httpx/telegram loggers pinned to WARNING even at `LOG_LEVEL=DEBUG`); size and extension checks precede download; preference cap is enforced behind the chat filter.
- Limits: CVE reachability not exercised with exploit samples; CUPS authorization of `cancel -a` not tested against a server.

## 5. Reliability & Concurrency Findings

- F-02: hang/OOM of the single update pipeline; restart loses all in-memory sessions and queues, and startup cleanup deletes queued files.
- F-08: cleanup task races with in-flight jobs.
- F-04: conversation state leak per abandoned wizard (also unbounded `context.user_data` per user — negligible size).
- F-10: preference loss on corruption or power loss.
- F-11: half-queue TTL only enforced at sweep time.
- Verified OK: `lp`/`lpstat`/`cancel` timeouts kill and reap children; HA client is shared and closed on shutdown; `error_handler` replies best-effort.

## 6. Testing Gaps

`python3 test_bot.py` passes (14/14) and is honest about its scope. Highest-value additions, all runnable without Telegram/CUPS (Pillow/pypdf are needed only for the merge tests; skip them when not installed):

1. `merge_to_pdf`: RGBA/P transparency yields white (guards F-05), single-file padding geometry, rejection of unmergeable input, and a pixel-bound rejection (F-02).
2. `print_file` command construction: patch `asyncio.create_subprocess_exec` to capture argv for gray/copies/A5/half combinations — the Canon dual-flag rule is currently untested.
3. Handler filter test: an `edited_message` update must not match the print handler (F-03); wizard entry points must respond mid-conversation (F-04). Both need real PTB objects, so they fit a separate optional script rather than the stubbed self-check.
4. `load_preferences` with corrupt JSON, a list top level, and >`MAX_PREFERENCES` entries (F-10).
5. Half-queue state machine: odd/even flush, rate-limited flush keeps the queue, `normal` discards files, expired entry rejected (F-11).
6. `bump_version.py`: `--dry-run` before and after the subcommand leaves files untouched (F-14).

Misleading assertion: `test_version_matches_changelog` and `check_consistency()` accept any `:{version}` substring in README, so an unrelated `:1.2.2` would satisfy them.

## 7. Architecture Recommendations

- **Move file parsing out of the bot process** (F-01, F-02, F-07). A short-lived child process per merge with a timeout and memory rlimit gives isolation, cancellation, and a natural place to apply pixel/page caps — the existing `print_file` subprocess pattern already shows how.
- **Single choke point for update eligibility.** Fold "allowed chat" and "new message, not edit" into one filter built once in `main()` so every handler inherits both (F-03).
- **Track in-flight files explicitly** rather than inferring liveness from `half_queue` (F-08). One set, owned next to `perform_cleanup`.
- Keep the single-file design; nothing found justifies splitting modules.

## 8. Recommended Action Plan

**Immediate**
1. Upgrade pypdf and Pillow; restrict `Image.open` formats (F-01).
2. Exclude edited messages from all handlers (F-03).
3. Add `allow_reentry=True` and `conversation_timeout` to the wizard (F-04).

**Short term**
4. Bound and isolate the merge: pixel cap, page/queue caps, subprocess with timeout (F-02).
5. Composite transparency onto white and honour EXIF orientation (F-05).
6. Fix systemd first-run (`mkdir -p data` in README) (F-06).
7. Add test/check/audit gate to the release workflow (F-15).

**Medium term**
8. Non-root container and compose hardening (F-07).
9. In-flight file tracking for cleanup (F-08); preference corruption handling and fsync (F-10).
10. Scope `/jobs` and `/cancel` to `PRINTER_NAME` (F-09); enforce half-queue TTL on use and scope `/clean` (F-11).
11. Fix `bump_version.py` flag handling (F-14).

**Optional**
12. F-12, F-13, F-16 cleanups; heartbeat-based health check.

## 9. Audit Coverage

**Coverage: full.** Every tracked first-party file was read in full.

**Reviewed files**

- `/` — `.dockerignore`, `.env.example`, `.gitignore`, `CLAUDE.md`, `AGENTS.md` (symlink → `CLAUDE.md`), `GEMINI.md` (symlink → `CLAUDE.md`), `Dockerfile`, `README.md`, `VERSION`, `bot.py`, `docker-compose.yml`, `docker-entrypoint.sh`, `notanext.service`, `requirements.txt`, `test_bot.py`
- `.github/workflows/` — `docker-release.yml`
- `docs/` — `CHANGELOG.md`, `USER-SPEC.md`
- `scripts/` — `bump_version.py`

**Excluded / not first-party**

- `__pycache__/`, `scripts/__pycache__/` — generated bytecode, gitignored.
- `.claude/RESUME.md` — untracked local agent checkpoint (ignored via `.git/info/exclude`); read only to confirm it is not application code (relevant to F-16).
- `data/` — runtime directory, absent in this checkout.
- No lockfile exists; `requirements.txt` is the only manifest.

**Flows traced end to end:** photo/document → `print_msg` normal path → `_get_file_info` → download → `_print_and_reply` → `print_file` → `lp` → HA notify → cleanup; half-mode queue → `_flush_half_queue` → `merge_to_pdf` → `lp`; text options/`print` keyword; preference wizard → persistence; `/status`, `/jobs`, `/cancel`, `/clean`; `cleanup_task` and startup cleanup; `error_handler`; Docker entrypoint; systemd unit; release workflow; version bump script.

**Checks run (scratch venv, Python 3.10.20 to match the Docker base; pinned requirements installed there, not in the repo)**

| Command | Outcome |
|---|---|
| `python3 test_bot.py` (repo, Python 3.12.3) | PASSED, 14/14 |
| `python3 scripts/bump_version.py check` | All files match 1.2.2 |
| `python3 -m py_compile bot.py scripts/bump_version.py test_bot.py` | OK |
| `pip-audit --local` | pypdf 4.1.0: ≈45 distinct advisories (fixes up to 6.19.0); Pillow 10.2.0: ≈16 distinct (fixes 10.3.0–12.3.0); none reported for python-telegram-bot 22.7 or httpx 0.28.1 |
| `ruff check --select E,F,B,S,ASYNC --ignore E501,S101` | 13 findings, all low-signal: S105 false positives on `token` loop variable, one B904, unused `os` import and three placeholder-less f-strings in `bump_version.py`, S603/S607 on fixed `git` argv |
| PTB 22.7 probe: edited photo vs print handler filter | Matches (F-03) |
| PTB 22.7 probe: `concurrent_updates` default | 1 (sequential) |
| PTB 22.7 probe: wizard entry points while a state is active | Not handled (F-04) |
| Pillow probe: RGBA `(0,0,0,0)` → RGB | `(0,0,0)` (F-05) |
| `merge_to_pdf` on 12000×12000 RGBA PNG (595 KB) | Peak RSS 1,146 MB (F-02) |
| `bump_version.py --dry-run patch` in isolated `git archive` copy | Wrote `VERSION` = 1.2.3 (F-14); real repo untouched |

**Not verified:** behaviour against a real Telegram API or CUPS server; actual printing; Docker image build; systemd start on a Pi (F-06 is from systemd semantics); per-CVE exploit reachability; Telegram's handling of the malformed-Markdown reply (F-12); CUPS authorization for `cancel -a` (F-09).
