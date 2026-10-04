# Audit Remediation and Dependency Update

- **Created:** 2026-10-05
- **Status:** Draft — awaiting approval (no code changed yet)
- **Source:** `docs/audit/2026-10-05-audit-report.md` (findings F-01 … F-16)
- **Target release:** 1.3.0 (behaviour changes: edited messages ignored, half-queue cap, `/jobs` and `/cancel` scoped to one printer)

## Goal

Fix every confirmed audit finding at the layer that owns it. Upgrade runtime dependencies to current releases. Keep the existing invariants in `CLAUDE.md`. The app becomes two files: `bot.py`, plus `merge_pdf.py` for the isolated merge (Phase 2a). Everything else stays in `bot.py`.

**Revision 2026-10-05:** plan review applied. Changes:
- Phase 6 argparse fix corrected.
- Phase 2 decode order made explicit.
- `build_application()` extracted so the runtime checks test the real wiring.
- Child-kill messages defined.
- F-08 protects in-flight paths explicitly; file age is only a supplementary guard.
- Home Assistant `file_name` (F-16 part) deferred.
- Follow-up review: child memory bounded and stress-tested; merge cleanup covers the whole lifecycle; runtime UID checked on the bot process; automatic commits reject unrelated staged changes; I/O tests separated from the pure self-check.

## Non-goals

- No `concurrent_updates`, no per-chat locking. Updates stay sequential.
- No new runtime dependencies. In particular, no `python-telegram-bot[job-queue]` (so no `conversation_timeout`).
- No new environment variables. New limits are module constants, the same as `MAX_PREFERENCES`.
- No change to the `lp` command shape or to the Canon dual grayscale flags.
- **Deferred: Home Assistant `file_name` (part of F-16).** Sending real filenames instead of UUIDs is a privacy-visible change and reshapes `HalfQueueEntry`. Track it separately.

## Pre-verified facts (scratch venv, Python 3.10.20 = Docker base interpreter)

- Latest on PyPI: `python-telegram-bot 22.8` (needs `httpx>=0.27,<0.29`), `httpx 0.28.1`, `Pillow 12.3.0` (needs Python ≥3.10), `pypdf 6.19.0`.
- With those four installed, the current `merge_to_pdf` runs unchanged: a single JPEG is padded to 2 pages, and a mixed JPEG+GIF+PDF input merges to 4 pages.
- `pip-audit --local` on that set: **No known vulnerabilities found**.
- `conversation_timeout` silently needs the PTB JobQueue extra (APScheduler), which is not installed. So F-04 uses `allow_reentry` only.
- `setpriv` exists in `ubuntu:22.04` at `/usr/bin/setpriv`. F-07 uses it, so no new package is needed.

---

## Phase 1 — Dependency update (F-01)

**Files:** `requirements.txt`, `README.md` (Tech Stack table, badges)

1. Pin the new versions in `requirements.txt`:
   ```
   python-telegram-bot==22.8
   httpx==0.28.1
   Pillow==12.3.0
   pypdf==6.19.0
   ```
   Keep the existing httpx comment, but update "22.7" to "22.8".
2. In `README.md`, update the python-telegram-bot badge and the Tech Stack rows (PTB 22.8, Pillow 12.3.0, pypdf 6.19.0). Fix the Python row and badge from 3.12 to **3.10 (Docker image)** (F-16).
3. Update the version mentions in `CLAUDE.md` ("python-telegram-bot 22.7").

**Acceptance**
- `pip-audit -r requirements.txt` reports no known vulnerabilities.
- `docker compose build` succeeds for amd64. The arm64 check is left to the release workflow (Phase 8).

---

## Phase 2 — Harden and slim half-mode merging (F-01, F-02, F-05)

**Files:** new `merge_pdf.py` (repo root), `bot.py`

### 2a. Move the merge into its own lightweight script

- Create `merge_pdf.py`. It holds `merge_to_pdf()` (moved verbatim from `bot.py`, then changed by 2c–2e), its limits, and a CLI entry point: `python3 merge_pdf.py <output> <pad 0|1> <input>...`.
  - Exit 0 on success.
  - The entry point wraps resource-limit setup, parser imports and merging in error handling: catch `MemoryError` with the explicit message from 2b, otherwise `except Exception as e:` writes only `str(e)` (one line, no traceback) to stderr, and exits 1.
- `merge_pdf.py` imports only the standard library, Pillow and pypdf. It never imports `bot`, telegram or httpx.
- `bot.py` drops its `PIL`/`pypdf`/`io` imports and its `merge_to_pdf` definition. The parent bot process no longer loads the imaging libraries at all. `MERGEABLE_EXTENSIONS` stays in `bot.py` because the queue-time gate uses it.
- Why: measured child startup was **0.14 s / 42 MB** when it imports all of `bot.py`, and **0.02 s / 34 MB** with only Pillow and pypdf (x86, Python 3.10; expect the gap to be larger on a Pi).

### 2b. Run the merge in a child process with a timeout (F-02)

- Replace `await asyncio.to_thread(merge_to_pdf, …)` in `print_file` with `asyncio.create_subprocess_exec(sys.executable, MERGE_SCRIPT, output_path, "1"|"0", *inputs)`, where `MERGE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merge_pdf.py")`.
- Use the kill-and-reap timeout pattern that `print_file` already uses for `lp`. Module constant `MERGE_TIMEOUT = 60`.
- Move `print_file`'s outer `try/finally` to cover merged-path creation, merging, configuration resolution, and both subprocess launches. The existing `finally` starts after merging and cannot clean partial output from a failed merge. Register the merged path as in-flight before launching the child; remove the output and release its registration in the outer `finally`, including launch failures.
- On timeout or coroutine cancellation, kill and reap the active child before deleting its output; propagate cancellation after cleanup. Apply the same lifecycle discipline to `lp` so no active subprocess outlives ownership of its input files.
- Map the child's result to a `RuntimeError` whose message the user sees. `_print_and_reply` already turns that into a reply.
  - **Timeout:** "Merging took too long (over 60 s) — the file may be too complex."
  - **Return code < 0** (killed by a signal, typically the OOM-killer with SIGKILL and empty stderr): "Merge was stopped (file too large or too complex)."
  - **Any other nonzero return code:** the child's one-line stderr, truncated to `MAX_STDERR_LENGTH`; if empty, "Merge failed."
  - **Memory exhaustion:** the CLI catches `MemoryError` separately and writes "Merge exceeded its memory limit (file too large or too complex)." rather than the empty `str(MemoryError())`.
- Why a process and not a thread:
  - A thread stuck in a pypdf loop cannot be cancelled. A process can be killed.
  - A child process is killable, but the shared compose memory limit does **not** guarantee the OOM-killer chooses it rather than the bot. Do not rely on that for containment.
  - The child's peak memory returns to the OS when it exits. Today the bot keeps its post-merge peak RSS for the rest of its life.
- Bound the CLI child on both Linux deployment paths with standard-library `resource.setrlimit(resource.RLIMIT_AS, (MERGE_MEMORY_BYTES, MERGE_MEMORY_BYTES))` before importing Pillow/pypdf or opening input. Defer their imports into `merge_to_pdf()` so the CLI can establish the limit first. Start with a fixed `MERGE_MEMORY_BYTES = 384 * 1024 * 1024`; this bounds virtual address space, not just RSS. Failure to establish the limit aborts the merge with a reason rather than running unbounded.
- Validate that bound on amd64 and arm64 against accepted image/PDF fixtures and the actual 512 MB compose limit, measuring parent/child and total cgroup memory with headroom. Lower the bound or decode pixel ceiling if the envelope is unsafe; do not raise the child bound without re-proving the total envelope. Pixel/page caps are admission checks, not a promise every admitted complex file can merge within the resource limits.

### 2c. Safe image decoding (F-01, F-05)

- Format allowlist: `Image.open(fp, formats=["JPEG", "PNG", "GIF"])`. This stops content sniffing from reaching any other decoder.
- Pixel limits:
  - `Image.MAX_IMAGE_PIXELS = 120_000_000` (hard ceiling, checked from the header at open).
  - `DecompressionBombWarning` escalated to an error with `warnings.simplefilter("error", Image.DecompressionBombWarning)`.
  - PNG/GIF cannot be decoded at reduced scale, so the script rejects them over `MAX_FULL_DECODE_PIXELS = 40_000_000` before any decode.
  - JPEG up to 120 MP is allowed, because it is decoded at reduced scale (2d).
- EXIF: `ImageOps.exif_transpose`, so photos sent as documents print upright.
- Transparency: flatten onto white. Images with an alpha channel or palette transparency (`img.mode in ("RGBA", "LA")`, or `"transparency" in img.info`) go through `RGBA` and are pasted onto a white `RGB` canvas using alpha as the mask. Everything else uses `convert("RGB")` as today.

### 2d. Downscale to print resolution (resource optimisation)

- Constant `PRINT_MAX_PX = 3508`: the long side of A4 at 300 DPI. A5 and half-sheet output need less, so this never reduces print quality.
- **JPEG draft:** request an aspect-preserving size, `img.draft("RGB", (ceil(w * f), ceil(h * f)))` with `f = PRINT_MAX_PX / max(w, h)`, only when `f < 1`. libjpeg then decodes at 1/2, 1/4 or 1/8 scale directly.
  - Do not use a square request like `(3508, 3508)`; it makes `draft()` skip the reduction for landscape photos.
  - Do not rely on `thumbnail()`'s own built-in draft: measured at 322 MB peak on the same 48 MP JPEG, because its request is square too.

### 2c+2d. Required per-image order (memory depends on it)

`exif_transpose`, `convert` and the flatten step all force a full decode. They must run after `draft()`.

1. `Image.open(fp, formats=[...])`
2. Pixel checks, from the header only.
3. `draft()` (JPEG only).
4. `ImageOps.exif_transpose(img)`. This is the first full decode, now at reduced scale.
5. `convert("RGBA")` if transparent, else `convert("RGB")`.
6. `img.thumbnail((PRINT_MAX_PX, PRINT_MAX_PX))`. Done before flattening, so the full-size RGBA and RGB copies never coexist.
7. If RGBA, paste onto a white `RGB` canvas of the thumbnail size.
8. `save(format="PDF")`.

Measured on a 48 MP worst-case (noise) JPEG, steps 1–8 against the current code:

| | Peak RAM | PDF sent to CUPS | Time |
|---|---|---|---|
| Current code | 438 MB | 24.6 MB | 0.63 s |
| Draft + thumbnail | 185 MB | 2.9 MB | 0.54 s |

PDF inputs pass through unchanged. Re-rendering them would cost more than it saves.

### 2e. Bound the work per job (F-02)

- `bot.py` constant `MAX_HALF_QUEUE_FILES = 10`. In `print_msg`'s half path, check it **before** download. If the queue is full, reply asking the user to send `print` first, and do not queue the file.
- `merge_pdf.py` constant `MAX_MERGED_PAGES = 50`. Raise once `len(writer.pages)` exceeds it. The message reaches the user through 2b.

**Acceptance**
- `bot.py` imports neither `PIL` nor `pypdf`. `merge_pdf.py` imports neither `telegram` nor `httpx`.
- A transparent RGBA PNG merges onto a white background.
- A 48 MP JPEG merges to a page image whose long side is ≤ 3508 px.
- A PNG over 40 MP is rejected with a reply, and the bot keeps answering other chats.
- Partial-output failure, timeout, cancellation, configuration failure, or subprocess-launch failure leaves no `*_merged.pdf` in `data/`; cancellation and timeout reap the child before cleanup.
- When the child is killed (`kill -9` during the manual run), the user gets the "Merge was stopped" reply, not a blank or traceback.
- Under the actual 512 MB compose limit, force allocation past the CLI child's address-space bound and test near-limit PNG/GIF/JPEG and complex PDF fixtures. The child fails with a reason, the bot PID/restart count is unchanged, and a subsequent `/help` succeeds. Repeat on amd64 and arm64 before release; record peak memory and retain headroom. These are execution-time checks, not pre-verified claims.
- An 11th queued file is refused before download.
- A single file is still padded to 2 pages, and mixed JPEG+GIF+PDF still merges.

---

## Phase 3 — Handler fixes (F-03, F-04, F-12)

**File:** `bot.py` (`main()`, `_get_file_info`)

0. **Extract `build_application(token: str, allowed_chat_ids: list[int]) -> Application`.** Move everything in `main()` from `ApplicationBuilder()` through the last `add_handler` into this function, unchanged. `main()` keeps:
   - the `TOKEN` check;
   - the startup logs and binary warnings;
   - `perform_cleanup()` and `load_preferences()`;
   - `build_application(...).run_polling()`.

   This is a pure move, done first. It lets the Phase 7 runtime checks inspect the real registered handlers (`app.handlers[0]`) instead of a copy of the filters.
1. **Ignore edited messages (F-03).** In `main()`, build `new_message = filters.UpdateType.MESSAGE` and fold it into `chat_id_filter`: `new_message & filters.Chat(...)` when an allowlist is set, `new_message` when it is not. Also pass `filters=new_message` to the unfiltered `/help` and `/status` handlers. Every handler then inherits it from one place.
2. **Wizard re-entry (F-04).** Add `allow_reentry=True` to the `ConversationHandler`. Register a top-level `CallbackQueryHandler(stale_pref_button, pattern="^pref_")` **after** `pref_conv`. It answers the query and edits the message to "This menu has expired — send /preferences to start again.", so buttons pressed after a restart no longer spin forever.
3. **Markdown-safe rejection (F-12).** Send the unsupported-type reply in `_get_file_info` without `parse_mode` (plain text, extension shown in quotes). Leave the other Markdown replies alone: none of them interpolate user text.

**Acceptance**
- An `edited_message` update with a photo does not match the print handler (Phase 7 probe).
- With the conversation in `PREF_COLOR`, `/preferences` and `/start` are handled (Phase 7 probe).

---

## Phase 4 — State, cleanup and scope fixes (F-08, F-09, F-10, F-11, F-13)

**File:** `bot.py`

1. **In-flight file protection (F-08).** Track owned paths in a module-level `in_flight_paths: set[str]`; file age alone cannot protect old queued files during a flush.
   - Register a download path before its first `await`. In half mode, transfer ownership to `half_queue` synchronously after download. Normal-mode ownership lasts through `_print_and_reply`'s cleanup. In both modes, failure/cancellation during download must delete partial output and release the registration, even when `_print_and_reply` is never reached.
   - `_flush_half_queue` registers all inputs **before** popping the queue, with no intervening `await`. Their registration lasts through `_print_and_reply`'s `finally`, even if printing fails or is cancelled. `print_file` separately owns the merged output as described in Phase 2b.
   - `save_preferences` registers `preferences.json.tmp` before writing and releases it in `finally`, cleaning any leftover temporary file on failure. Use consistent path representation for registration and sweep comparisons.
   - `cleanup_task` snapshots both active half-queue files and `in_flight_paths` before offloading the sweep. Do not evict in-flight ownership by TTL: each owner's `finally` releases it, so a running job stays protected regardless of file age.
   - Add a parameter `perform_cleanup(skip_paths=None, min_age: float = 0)`. It skips any file whose `entry.stat().st_mtime` is newer than `time.time() - min_age`.
   - `cleanup_task` also passes `min_age=SESSION_TTL` as a supplementary guard for freshly created files appearing after the skip-set snapshot. Old inputs transferred from a live queue remain protected by explicit ownership, not mtime.
   - Startup cleanup and `/clean` keep `min_age=0`. Startup runs before polling; `/clean` cannot overlap another update under sequential processing and still includes in-flight paths alongside other chats' queued files in its skip set. Concurrent updates remain out of scope.
2. **Scope CUPS queries to the printer (F-09).** `/jobs` runs `lpstat -o <PRINTER_NAME>`; `/cancel` runs `cancel -a <PRINTER_NAME>`. Resolve the name with `get_printer_name()` and map its `RuntimeError` to the existing "Configuration error" reply. `/status` stays `lpstat -p` (server-wide reachability is its point).
3. **Preference durability (F-10).**
   - `save_preferences`: `f.flush(); os.fsync(f.fileno())` before `os.replace`.
   - `load_preferences`: on a parse or shape failure, rename the file to `data/preferences.json.corrupt` (overwriting an older one) and log at ERROR. Then start empty.
   - `perform_cleanup` preserves that filename too.
4. **Half-queue lifetime and scope (F-11).**
   - Add a helper `get_half_queue(chat_id)` that returns `None` and deletes the files when `entry.ts` is older than `SESSION_TTL`. Use it in `handle_text_message` (`print`), `_flush_half_queue` and `print_msg`. The 30-minute spec is then enforced on use, not only by the 6 h sweep.
   - `pref_paper_callback`: when the saved mode is Normal, discard the chat's half queue the same way the `normal` keyword does. Extract the existing discard block from `handle_text_message` into one helper used by both.
   - `/clean`: clear only the calling chat's queue entry. The file sweep stays global, but passes the other chats' queued files as `skip_paths`. Update the `/clean` help text: "Delete cached files".
5. **Error reply detail (F-13).** `_print_and_reply`: keep `cmd` in the log only and reply with the CUPS error text only. Replace `f"❌ Unexpected error: {e}"` with a generic message; the traceback is already logged.

**Acceptance**
- `perform_cleanup(min_age=SESSION_TTL)` keeps a freshly written file and deletes one whose mtime is older (Phase 7 test).
- A live queue with an input older than `SESSION_TTL` survives a periodic sweep while its flush is paused after the queue pop. Downloads and merged outputs are protected too; failures/cancellation leave no leaked registrations or partial files.
- `/cancel` argv ends with `-a <PRINTER_NAME>`.
- A corrupt `preferences.json` is renamed, not overwritten.

---

## Phase 5 — Container and systemd (F-06, F-07, F-16)

**Files:** `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml`, `.dockerignore`, `README.md`, `notanext.service` (comments only)

1. **Non-root runtime (F-07).**
   - `Dockerfile`: `RUN useradd --system --uid 10001 --home /app notanext`.
   - `docker-entrypoint.sh` keeps writing `/etc/cups/client.conf` as root. It then runs `mkdir -p /app/data && chown -R notanext:notanext /app/data`, which migrates existing root-owned bind mounts.
   - Run `lpoptions -d` as the drop-privilege user (lpoptions writes per-user config), or drop it. `bot.py` passes `-d` on every `lp` call, so dropping it is the smaller change. Update the `CLAUDE.md` deployment note to match.
   - Final lines: `export HOME=/app` (setpriv keeps root's `HOME=/root`), then `exec setpriv --reuid=notanext --regid=notanext --init-groups "$@"`.
   - Keep the TCP probe to port 631 inside the `PRINTER_NAME` block when removing the `lpoptions` call. Only the `lpoptions` lines go.
   - CUPS jobs are now owned by user `notanext`, not `root`. `/cancel` may not be able to cancel jobs queued before the upgrade. Note this in the CHANGELOG.
2. **Compose hardening.** Add `security_opt: ["no-new-privileges:true"]`. Do not add `read_only: true`: the entrypoint writes `/etc/cups`, and it is not worth a tmpfs juggle.
3. **`.dockerignore`.** Add `.claude/` (F-16).
4. **systemd first run (F-06).** In README Option B, add `mkdir -p data` after `cd /home/pi/notanext`. On Raspberry Pi OS Bookworm (PEP 668), also note that the system Python needs `pip install --break-system-packages -r requirements.txt` or a venv with `ExecStart` pointed at it. Keep `ReadWritePaths` strict, without a `-` prefix.
5. **Health check (F-16).** Out of scope. `pgrep` stays. A heartbeat needs a periodic writer, and with Phase 2 the main hang source is gone. Revisit if hangs are observed.

**Acceptance**
- `docker compose up -d --build`, then `docker compose exec notanext cat /proc/1/status` shows all four `Uid:` values as `10001`; confirm PID 1 is the exec'd bot via `/proc/1/cmdline`. A plain exec `id -u` checks Docker's configured exec user (still root), not the bot's dropped privileges.
- Files in `./data` on the host are owned by UID 10001.
- A print and `/status` still work against the real CUPS server (manual).

---

## Phase 6 — Version tool fixes (F-14)

**File:** `scripts/bump_version.py`

1. Define `--dry-run`, `--git-commit` and `--git-tag` once, on a parent parser (`add_help=False`) passed as `parents=[common]` to every subparser, with `default=argparse.SUPPRESS` on the **parent**. Keep plain `default=False` on the top-level parser.
   - Then an absent subcommand flag no longer overwrites a top-level one.
   - Verified with a stub parser across `--dry-run patch`, `patch --dry-run`, `--dry-run`, `patch`: `[True, True, True, False]`.
   - The inverse placement (`SUPPRESS` on top level) yields `[False, True, True, False]` and leaves the bug in place.
2. Split `git_commit_and_tag` behaviour: `--git-commit` commits, and `--git-tag` tags (implying the commit). `git add` only the six files `sync_version` writes, not `-A`.
   - For automatic commit/tag operations, inspect the staged index **before** `sync_version` writes anything. Reject unrelated staged paths with a clear message; leave the index, working files, HEAD and tags unchanged. Targeted `git add` alone does not prevent an ordinary `git commit` from including previously staged unrelated changes. Never unstage the user's work automatically.
3. Remove the unused `os` import and the placeholder-less f-strings (ruff F401/F541).
4. Tighten the README check in both `check_consistency()` and `test_version_matches_changelog`: match `notanext:{version}` instead of `:{version}`.

**Acceptance**
- In an isolated `git archive` copy, `bump_version.py --dry-run patch` and `bump_version.py patch --dry-run` both leave `VERSION` unchanged.
- In a disposable initialized repository, stage an unrelated file and run both automatic commit and tag variants. Each refuses before version writes and preserves the index, files, HEAD and tags. With a clean index, commit-only creates no tag; tag mode creates the version commit and tag, containing only the six version files.

---

## Phase 7 — Tests

**File:** `test_bot.py` (keep it pure, stub-based and runnable on a bare interpreter).

- Keep its existing pure-logic checks. Do not add handler execution, filesystem access, or subprocess-lifecycle tests here.
- Import isolation: remove `PIL`, `PIL.Image` and `pypdf` from the stub list at the top of `test_bot.py`. Check on an interpreter without those packages so imports creeping back into `bot.py` fail.

**New file:** `scripts/check_io.py`. A separate dependency-free self-check using standard-library temporary directories and mocks, with its own third-party import stubs. Run it in a separate process, not by importing it into `test_bot.py`; resolve the repository root for imports when invoked from `scripts/`. No live Telegram/CUPS calls and no changes to real `data/`, environment or Git state.

- `print_file` argv: patch `bot.asyncio.create_subprocess_exec` with a fake that records argv and returns an object with `async communicate() -> (b"", b"")` and `returncode = 0`. Set `bot.LP_BIN = "lp"` and the `CUPS_SERVER`/`PRINTER_NAME` env vars for the test, and restore them after; otherwise `print_file` stops at "lp not found". Assert the gray/copies/A5/half flags and that half mode first runs `[sys.executable, MERGE_SCRIPT, …]`. Also assert that a fake child with `returncode = -9` raises the "Merge was stopped" message. This runs with stubs, so no Telegram, CUPS or Pillow is needed.
- `load_preferences` with corrupt JSON and with a list at the top level: the file gets renamed and the store is empty. Use a temporary `DATA_DIR`/`PREFERENCES_FILE` that you monkeypatch.
- `cancel` and `jobs` argv include `PRINTER_NAME`.
- Expired half queue: `get_half_queue` returns `None` and removes the files.
- `perform_cleanup(min_age=...)`: a new file in a temp `DATA_DIR` survives; one with `os.utime` set into the past is removed.
- In-flight ownership: pause a flush after queue removal with an input older than `SESSION_TTL` but a fresh queue timestamp, run the actual periodic sweep path, and assert the input survives. Check download/merged-output protection and registration release on success, failure and cancellation, including paths created after the sweep snapshot.
- Merge lifecycle: fake a child that writes partial output before failure/timeout/cancellation, and verify output removal, kill/reap ordering and registration release. Also cover configuration and subprocess-launch failures after merged-path creation; `lp` must not run after a failed merge.
- Version CLI: cover dry-run placement, unrelated-staged-file refusal, and commit/tag separation in disposable repositories as specified in Phase 6.

**New file:** `scripts/check_runtime.py`. This is an optional self-check that needs the real dependencies installed and skips with a message if they are missing. It holds the probes from the audit, all against `merge_pdf.merge_to_pdf` or real PTB objects:

- Built from `bot.build_application("1:TEST", [1])` (no network: `build()` does not contact Telegram), probing the real registered handlers in `app.handlers[0]`:
  - an edited photo update does not match the print handler;
  - wizard re-entry works mid-conversation;
  - a stale `pref_` callback is matched by `stale_pref_button`.
- An RGBA transparent PNG merges with a white background.
- A >40 MP PNG is rejected.
- A 48 MP JPEG is downscaled to ≤ 3508 px on the long side.
- A single-file merge is padded to 2 pages.
- Running `merge_pdf.py` as a script returns 0 on success and 1 with a stderr reason on bad input.
- The actual CLI establishes its address-space bound before parser imports and produces a nonempty reason on allocation exhaustion. Keep Docker/Pi memory-envelope stress checks separate from this local probe, as specified in Phase 2 acceptance.

Both scripts are separate because `test_bot.py` must remain a dependency-free **pure-logic** self-check (CLAUDE.md); dependency-free I/O checks are not pure logic.

**Acceptance**
- `python3 test_bot.py` passes on a bare interpreter.
- `python3 scripts/check_io.py` passes on a bare interpreter using only disposable files/repositories.
- `python3 scripts/check_runtime.py` passes in a venv built from `requirements.txt`.

---

## Phase 8 — Release workflow gate (F-15)

**File:** `.github/workflows/docker-release.yml`

1. Add a `check` job on `ubuntu-latest` with Python 3.10. Steps: `python3 test_bot.py`, `python3 scripts/check_io.py`, `python3 scripts/bump_version.py check`, `pip install -r requirements.txt pip-audit && pip-audit -r requirements.txt && python3 scripts/check_runtime.py`. Run the bare-interpreter checks before installing runtime dependencies.
2. `build-and-push` gets `needs: check`.
3. In `docker/metadata-action`, replace `type=raw,value=latest` with `flavor: latest=auto`, so pre-release tags do not move `latest`.

**Acceptance:** the workflow YAML is valid (`actionlint` if available, otherwise review). The first real run happens on the 1.3.0 tag.

---

## Phase 9 — Documentation and release

1. **`docs/USER-SPEC.md`:**
   - Edited messages are ignored.
   - Half-queue cap of 10 files.
   - Half-mode limits:
     - page cap of 50 per merged job;
     - images: JPEG up to 120 MP; PNG/GIF up to 40 MP;
     - images downscaled to 3508 px on the long side (A4 at 300 DPI).
     - complex files within the admission caps can still be rejected by the merge memory/time bounds.
   - Transparency printed on white.
   - `/jobs` and `/cancel` cover this printer only.
   - `/clean` clears only your own queue.
   - Half queue expires after 30 minutes of inactivity, enforced on use.
   - New constants added to the §11 list.
2. **`HELP_TEXT` and README:** `/clean` description; `/cancel` "Cancel all jobs on this printer". Keep `HELP_TEXT`, the README and USER-SPEC in sync, as CLAUDE.md requires.
3. **`CLAUDE.md`:**
   - The app is now two files: `bot.py`, plus `merge_pdf.py` run as a child process (not `asyncio.to_thread`). Explain why it is separate (parent stays free of imaging libs; child starts fast and can be killed) and that it must never import `bot`.
   - JPEG `draft()` precedes EXIF transpose and conversion; thumbnailing to `PRINT_MAX_PX` precedes white-background flattening, following Phase 2's required decode order.
   - In-flight path ownership, synchronous queue-to-print transfer, and owner `finally` release; periodic sweep protects those paths and queued files, with file age only a supplementary snapshot-race guard.
   - The merge CLI's fixed address-space limit and its distinction from the shared container memory limit.
   - `chat_id_filter` now also excludes edited messages.
   - Entrypoint drops privileges with `setpriv`.
   - Dependency versions.
4. **`docs/CHANGELOG.md`:** a 1.3.0 entry listing each fix with its finding ID.
5. Run `python3 scripts/bump_version.py minor` (after Phase 6 is fixed), then `check`.

---

## Execution order and dependencies

1. Phase 1, then Phase 2. The merge changes are tested against the new Pillow/pypdf.
2. Phases 3, 4 and 6 are independent of each other.
3. Phase 5 can be edited independently. Its Docker runtime verification and Phase 2 memory-envelope checks need the combined changes and both deployment architectures before release.
4. Phase 7 follows Phases 2–4 and 6.
5. Phase 8 follows Phase 7, because it runs `check_runtime.py`.
6. Phase 9 comes last.

## Verification summary (run at the end of EXECUTE)

```bash
python3 test_bot.py
python3 scripts/check_io.py
python3 scripts/bump_version.py check
python3 -m py_compile bot.py merge_pdf.py scripts/*.py test_bot.py
# in a venv built from requirements.txt (Python 3.10):
pip-audit -r requirements.txt
python3 scripts/check_runtime.py
docker compose up -d --build
docker compose exec notanext cat /proc/1/status
docker compose exec notanext cat /proc/1/cmdline
# Assert PID 1 is the bot and all Uid fields are 10001.
# Run Phase 2's bounded-memory stress checks under the 512 MB compose limit
# on amd64 and arm64 before publishing; record peaks and parent survival.
```

Manual, against a real printer:
- normal print;
- half pair;
- `print` with a lone file;
- grayscale on the Canon;
- edit a caption (no reprint);
- abandon the wizard, then `/preferences`;
- `/jobs`;
- `/cancel`.

## Risks

- **Pillow 10 to 12 and pypdf 4 to 6 are major upgrades.** Only `Image.open/convert/save(format="PDF")`, `PdfReader.pages`, `PdfWriter.add_page/add_blank_page/write` and `mediabox` are used. All were smoke-tested on the new versions. Re-check the deprecation output during Phase 7.
- **The child process adds startup per half-mode job.** That is about 0.02 s / 34 MB on x86 with the lightweight script, versus 0.14 s / 42 MB if it imported `bot.py`. The cost on a Pi is not measured; check it during the manual run.
- **Memory containment still needs deployment proof.** The initial 384 MiB address-space bound may reject otherwise admitted images/PDFs, and a shared cgroup does not guarantee which process survives OOM. Confirm parent/child headroom and bot survival on amd64 and arm64 under 512 MB before release; lower limits if needed rather than assuming child isolation is sufficient.
- **Downscaling changes output bytes, not visible quality.** The 3508 px cap equals A4 at 300 DPI. If a user ever needs A4 at 600 DPI, raise `PRINT_MAX_PX`; nothing else depends on it.
- **The `Dockerfile` copies the whole repo (`COPY . /app`)**, so `merge_pdf.py` ships without Dockerfile changes. Confirm `.dockerignore` does not exclude it.
- **Changing `./data` ownership** affects users who also read that directory on the host as root. This is harmless.
- **`/clean` scope change** is user-visible. It is documented in the CHANGELOG and USER-SPEC.
