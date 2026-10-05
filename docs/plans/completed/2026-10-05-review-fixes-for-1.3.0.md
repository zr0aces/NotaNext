# Review Fixes for 1.3.0

- **Created:** 2026-10-05
- **Status:** Plan review passed — implementation and release checks remain pending (no application code changed; review does not authorize implementation)
- **Source:** two-axis code review of commit `ea8d94c` (Standards and Spec), run against `docs/plans/2026-10-05-audit-remediation-and-dependency-update.md` as committed
- **Release:** stays **1.3.0**. `VERSION` is already 1.3.0 and no `v1.3.0` tag exists locally or on `origin`, so these fixes fold into the unreleased 1.3.0 entry. No version bump.

**Revision 2026-10-05 (plan review):**
- 1.1 no longer makes the half-queue helpers async.
- The preferences temp file leaves `in_flight_paths`.
- `print_file` checks configuration before merging.
- 4.4 uses a relative memory bound.
- The spec choice is an explicit approval item.

**Revision 2026-10-05 (second review):**
- The 384 MiB merge limit was found too tight for the documented image limits (measured below).
- **Decision (user): keep 384 MiB and lower the image limits** to JPEG 48 MP and PNG/GIF 12 MP. This is new Phase 2.2.
- The `.tmp` name-skip is dropped.
- CHANGELOG wording fixed.
- The config-before-merge behaviour change is listed.

**Revision 2026-10-05 (third review):**
- JPEG validation includes near-square inputs; the landscape-only measurements do not establish a safe 48 MP ceiling.
- Allocation exhaustion is forced inside the real CLI's merge call, not with an image rejected by the new pixel cap.
- Ownership regression exercises the real flush and periodic sweep together.
- The 512 MB container parent-survival gate is restored for amd64 and arm64; no memory-limit increase is proposed.
- `HELP_TEXT` includes the image caps, and obsolete-name checks exclude historical plans/audits.

**Revision 2026-10-05 (review/fix loop):**
- Pinned-runtime probes confirmed the near-square JPEG failure; Phase 2.3 now specifies a JPEG-only early thumbnail while preserving the approved caps and output resolution.
- Runtime sweeps preserve the fixed preferences temp path: a reproduced stat/unlink race makes age-only protection insufficient when an old temp name is reused.
- Preference-save cancellation waits for the worker to finish, subprocess teardown tolerates an already-exited child, and focused tests cover these paths.
- Review completion concerns this plan's correctness and completeness, not completion of its future implementation, Docker, or printer acceptance checks.

## Governing approval and verified design evidence

- [x] **Which spec governs.** _Approved 2026-10-05: committed version (keep `in_flight_paths` and the 384 MiB `RLIMIT_AS`)._ Keep that ownership model and memory bound; this plan corrects the code, documentation and checks listed below. No public cap or memory-bound changes beyond the previously approved JPEG 48 MP / PNG-GIF 12 MP decision are proposed.
- [x] **JPEG design probe, not implementation acceptance.** With Python 3.10.20, Pillow 12.3.0 and pypdf 6.19.0, a temporary copy of the real CLI under 384 MiB failed on 6928×6928 RGB JPEGs, with and without EXIF orientation 6. A scratch copy adding only JPEG thumbnailing after `draft()` and before EXIF conversion passed those inputs and a same-sized CMYK JPEG with orientation 6. Merely using in-place EXIF transpose and skipping redundant conversion still failed on rotated inputs, so that is not the selected fix. Phase 2.3 specifies the verified approach; the final implementation must repeat the wider Phase 4 cases.

## Governing decisions
- **`scripts/check_io.py` and `scripts/check_runtime.py` stay.** The review flagged them as breaching the old `CLAUDE.md` rule ("test_bot.py … anything touching … the filesystem is out of its reach"). That rule constrains `test_bot.py`, which still runs on a bare interpreter. The fix is to document the two scripts as the sanctioned home for I/O and dependency-backed checks, not to delete them.
- **User-visible changes:**
  - The wizard's `/cancel` fallback ignores edited messages.
  - A missing `CUPS_SERVER`/`PRINTER_NAME` now fails before the merge instead of after it.
  - **Half-mode image limits drop: JPEG 120 → 48 MP, PNG/GIF 40 → 12 MP** (Phase 2.2).

## Non-goals

- No new constants, env vars or dependencies.
- No change to `MERGE_MEMORY_BYTES` (stays 384 MiB), error messages (except interpolating the existing timeout wording), or the `lp` command. The only limits that change are the two pixel caps in Phase 2.2. Phase 2.3 moves JPEG thumbnailing before EXIF/conversion to bound decoded copies; PNG/GIF keep their existing decode order.
- No restructuring of `in_flight_paths` beyond removing duplicated call sites (the "scattered global" smell is accepted; consolidating the download path in Phase 1 already removes two of its eight sites).

---

## Phase 1 — Code fixes in `bot.py`

### 1.1 Blocking filesystem work: narrow the rule, offload only `fsync` (Standards, hard)

`CLAUDE.md` says: "Blocking filesystem work never runs directly in a handler." That rule was written for `perform_cleanup`'s whole-directory sweep. The code before this commit already deleted files inline in `handle_text_message` and in the per-job `finally` blocks.

- **Keep inline:**
  - `discard_half_queue` and `get_half_queue` stay synchronous.
  - The work is bounded to at most `MAX_HALF_QUEUE_FILES` (10) files, but its latency depends on the filesystem; do not claim a universal microsecond bound.
  - Making them async would touch five call sites, and a missed `await` would return a truthy coroutine and silently break the queue logic.
- **`clean()`** (bot.py:655-670): replace its hand-copied pop-and-remove loop with `discard_half_queue(chat_id)` (Duplicated Code).
- **Offload `fsync`:**
  - `pref_paper_callback` (bot.py:561) offloads `save_preferences` through `asyncio.to_thread`, because `fsync` on an SD card can stall for tens to hundreds of milliseconds; await the shielded task as specified below rather than a bare cancellable offload.
  - Schedule that offload as a task and shield it from handler cancellation. If the handler is cancelled, drain the worker before propagating cancellation (including a repeated cancellation); do not let a new update or service shutdown race a still-running save of the same fixed temp file. This changes cancellation handling, not normal preference replies.
  - `save_preferences` stays synchronous, so `check_io` and the startup path can call it directly.
- **Remove the preferences temp file from `in_flight_paths`** (bot.py:371 and :388).
  - Keep `in_flight_paths` mutations on the event-loop thread rather than making the worker a second owner of that shared set.
  - Both runtime sweep callers (`clean` and the extracted `_sweep_once`) add `PREFERENCES_FILE + ".tmp"` to their skip paths. Do not rely only on mtime: a sweep can stat an old leftover temp file, then unlink it after the worker truncates it for a fresh save. The race was reproduced by pausing removal after the age check and rewriting the same path before unlink.
  - Startup `perform_cleanup()` still deletes a crash-leftover `.tmp`, because it runs before any worker or polling. The writer's success/failure cleanup remains responsible for its own temp file; runtime preservation does not make it persistent data.
- Phase 3 rewrites the `CLAUDE.md` rule to match. Directory sweeps and `fsync` go through an executor. Per-chat deletes of at most `MAX_HALF_QUEUE_FILES` files, and the per-job `finally` cleanups, may run inline.

### 1.2 One download helper for both modes (Duplicated Code)

The guard at bot.py:914-923 and :971-980 is identical.

- Add `async def _download_tracked(file_obj, orig_ext: str) -> str`. It does `os.makedirs`, builds the UUID path, adds it to `in_flight_paths`, downloads, and on `BaseException` discards from `in_flight_paths`, removes the partial file, and re-raises. It returns the path, still in `in_flight_paths`.
- Both `print_msg` branches call it. The half branch keeps its existing `in_flight_paths.discard(file_path)` after appending to the queue; the normal branch keeps handing ownership to `_print_and_reply`, whose `finally` already discards it.

### 1.3 One kill-and-reap helper (Duplicated Code)

The `kill()` + `await wait()` pair appears five times: `run_cups_command` (bot.py:405), the merge child (TimeoutError and CancelledError), and `lp` (both again).

- Add `async def _communicate_or_kill(proc, timeout: float) -> tuple[bytes, bytes]`. It awaits `wait_for(proc.communicate(), timeout)`; on `TimeoutError` or `CancelledError` it kills and reaps, then re-raises the original exception. If the process already exited, tolerate `ProcessLookupError` from `kill()` and still await `wait()`; that race must not mask the original timeout/cancellation. Cancellation during teardown must not let the caller delete inputs before reaping completes.
- `run_cups_command`, the merge child and `lp` call it. Each caller keeps its own `except asyncio.TimeoutError` mapping to its user message, and its own `ex.cmd` attachment for `lp`.
- `CLAUDE.md` names `run_cups_command()` and `print_file()` as the reaping sites; Phase 3 updates that sentence to name the helper.

### 1.4 Small correctness fixes

- **Timeout text** (bot.py:1041): `f"Merging took too long (over {MERGE_TIMEOUT} s) — the file may be too complex."`
- **Wizard `/cancel` fallback** (bot.py:1377): `CommandHandler("cancel", cancel_preferences, filters=new_message)`. The plan's §3.1 says "Every handler then inherits it"; this is the one that didn't.
- **Check configuration before merging** (bot.py:1058): move `server = get_cups_server()` and `printer = get_printer_name()` to the top of `print_file`, before the merge. Without this, a missing `CUPS_SERVER` or `PRINTER_NAME` wastes a merge of up to `MERGE_TIMEOUT` seconds before failing.

**Acceptance (Phase 1)**
- `clean()` contains no `os.remove`; it calls `discard_half_queue`.
- `pref_paper_callback` reaches `save_preferences` only through `asyncio.to_thread`.
- `grep -n "in_flight_paths" bot.py` has no hit inside `save_preferences`.
- `grep -c "\.kill()" bot.py` is 1.
- `python3 test_bot.py` and `python3 scripts/check_io.py` pass.

> **Ponytail note.** Bounded per-chat deletes stay inline: at most 10 files. Revisit offloading only if measurements show handler latency warrants it.

---

## Phase 2 — `merge_pdf.py`

### 2.1 Pad flag

At merge_pdf.py:115, use `pad_for_half = sys.argv[2] == "1"`. The only caller sends `"1"` or `"0"` (Speculative Generality).

### 2.2 Lower the approved admission caps and validate the memory envelope

The earlier plan reported these measurements using the real `python3 merge_pdf.py out.pdf 0 <file>`, `MERGE_MEMORY_BYTES` = 384 MiB, and a Python 3.10 venv with pinned deps. They are historical fixture results, not a substitute for the current review's pinned-runtime probes or the final implementation gates.

| Input (measured fixtures, not every aspect ratio) | Result |
|---|---|
| JPEG 12 / 24 / 48 MP (noise, q90–95) | pass |
| JPEG 120 MP (current documented max) | **fail: "Merge exceeded its memory limit"** |
| PNG RGBA with transparency, 8 / 12 MP | pass |
| GIF 12 MP with transparency | pass |
| PNG RGBA 24 MP, 40 MP (current documented max) | **fail** |

The JPEG success measurements above are not sufficient to establish a 48 MP memory ceiling. `draft()` reduces an 8000×6000 image to 4000×3000, but leaves a 6928×6928 image (47,997,184 pixels) at full size because half-scale would be smaller than the requested 3508 px long side. The latter failed under 384 MiB with pinned dependencies. Phase 2.3 fixes the excessive decoded-copy lifetime without lowering the approved cap.

Changes:
- `merge_pdf.py:18`: `MAX_IMAGE_PIXELS = 48_000_000`. Pillow's warning fires only *above* the cap, so exactly 8000×6000 is still accepted.
- `merge_pdf.py:19`: `MAX_FULL_DECODE_PIXELS = 12_000_000`.
- Keep `MERGE_MEMORY_BYTES = 384 * 1024 * 1024` unchanged.
- Keep the existing rejection messages. They already name the limit, so check they interpolate the constant rather than a literal "120" or "40"; fix any literal.

The caps are admission checks, not a guarantee every image/PDF below them merges: resource-bound rejection remains supported. Document that distinction. Do not claim that a few passing fixtures establish a universal memory ceiling.

### 2.3 Thumbnail JPEGs before creating full decoded copies

- Keep header checks first, followed by the existing aspect-preserving JPEG `draft()` request. Do not request a more aggressive decoder scale that would undershoot the intended print resolution.
- For JPEG inputs, call `thumbnail((PRINT_MAX_PX, PRINT_MAX_PX))` immediately after `draft()`, before `ImageOps.exif_transpose` and RGB conversion. Cache the detected format before those transformations. The early thumbnail forces any remaining full decode but bounds the images subsequently copied for EXIF/conversion.
- Keep the resulting orientation, white-background handling, RGB PDF encoding and padding semantics. EXIF transpose follows the thumbnail for JPEG only; PNG/GIF still transpose and convert before thumbnailing, so palette transparency and resampling behavior are not silently changed.
- Run the existing post-conversion thumbnail only for non-JPEG inputs; avoid duplicate work rather than maintaining two JPEG resize paths. Close transformed images/streams on success and failure without closing a still-needed PDF reader input.
- Record this JPEG order in `CLAUDE.md` and the unreleased CHANGELOG entry. All outputs retain the same `PRINT_MAX_PX` cap; this does not add a setting or change admission limits.

**Acceptance (Phase 2):** Phase 4's runtime cases 1, 3 and 4 pass with the approved caps, including near-square RGB/CMYK JPEGs and EXIF orientation, and Phase 4's deployment memory gate passes on both architectures before release. Further public-cap or memory-bound changes need approval; do not increase the child limit to fit a fixture.

---

## Phase 3 — Documentation corrections

### `CLAUDE.md`

| Location | Current (wrong) | Fix |
|---|---|---|
| line 28 | `test_bot.py` stubs `telegram`, `PIL`, `pypdf` and `httpx` | Stubs `telegram`, `telegram.ext`, `telegram.warnings` and `httpx`. `PIL`/`pypdf` are deliberately not stubbed: the bare-interpreter import fails if either creeps back into `bot.py`. |
| line 28 | "no pytest … The only CI … nothing runs on push or PR" | Keep "no pytest, no linter, no formatter config". Add: `scripts/check_io.py` (filesystem/subprocess checks with fakes, bare interpreter) and `scripts/check_runtime.py` (needs `requirements.txt` installed) are the sanctioned homes for anything `test_bot.py` must not touch. CI: the tag workflow's `check` job runs `test_bot.py`, `bump_version.py check`, `check_io.py`, `pip-audit` and `check_runtime.py` before `build-and-push`. Still nothing runs on push or PR. |
| Commands block | missing | Add `python3 scripts/check_io.py` and `python3 scripts/check_runtime.py   # needs deps installed`. |
| line 80 / 88 | merge limit "will not crash the parent bot" | `RLIMIT_AS` makes the child fail with a `MemoryError` and a one-line reason instead of growing. It does not by itself guarantee the kernel OOM-killer picks the child under the compose cgroup limit; that is a best-effort property, not a guarantee. |
| line 86 | `run_cups_command()` and `print_file()` both do `kill()` + `await wait()` | All subprocess waits go through `_communicate_or_kill()`, which kills and reaps on timeout or cancellation. |
| Subprocess/I/O section | (blocking FS rule, unqualified) | Directory sweeps (`perform_cleanup`) and `fsync` (`save_preferences` from the wizard) go through an executor. Per-chat deletes of at most `MAX_HALF_QUEUE_FILES` files (`discard_half_queue`) and the per-job `finally` cleanups may run inline. |
| line 93 | "atomic preferences writes" listed among in-flight paths | Remove. Runtime sweeps explicitly skip the fixed preferences temp path to prevent stat/unlink reuse races; startup removes crash leftovers. The wizard awaits or drains its save worker, keeping sequential-update ownership intact on cancellation. |
| line 81 | "JPEG up to 120 MP, PNG/GIF up to 40 MP" | "JPEG up to 48 MP, PNG/GIF up to 12 MP". These are admission caps, not universal memory-fit guarantees; resource-bound rejection remains possible. Raising either requires remeasurement of the decode path and total container envelope. |
| Merge/decode section | JPEG EXIF/conversion before thumbnail | JPEG: header checks → draft → thumbnail → EXIF transpose → RGB conversion → PDF. PNG/GIF retain transpose/conversion before thumbnail and white compositing. Explain the near-square decoded-copy memory fix. |
| line 96 | `min_age` "60s for manual `/clean`" | `/clean` uses `min_age=0`; both runtime callers skip in-flight paths, retained queued files and the fixed preferences temp path. The periodic sweep also uses `min_age=SESSION_TTL` as a supplementary guard. Startup has no temp-path skip. |
| line 100 | "degrades to `filters.ALL`" | Degrades to `filters.UpdateType.MESSAGE` (still open to everyone, but ignoring edits). |
| line 100 | "All message handlers and `/help`/`/status` combine…" | Every message/command handler, including the wizard's `/cancel` fallback, ignores edited messages. Callback-query handlers retain their callback filters. |
| line 103 | `stale_pref_button` registered "as a fallback" | Registered as a **top-level** `CallbackQueryHandler` immediately after `pref_conv`; it only fires when the conversation does not claim the button. |

### `docs/CHANGELOG.md` (1.3.0 entry)

- Line 15: non-root execution is **[F-07]**, not [F-06].
- Add the missing **[F-06]** item: README systemd steps now create `data/` before first start. (`README.md:101` already has `mkdir -p data`; only the CHANGELOG line is missing.)
- Line 17: `/clean` "clears only the caller's queued files and unreferenced cached files" — remove "older than 60 seconds".
- Under the non-root item, add: "CUPS jobs are now submitted as user `notanext`. Jobs queued by an earlier version (owned by `root`) may not be cancellable with `/cancel`."
- Add one line under Fixed for this plan's code changes:
  - preference saves (`fsync`) off the event loop;
  - wizard `/cancel` ignores edits;
  - configuration checked before merging.
- Change the image-limit wording to **JPEG up to 48 MP, PNG/GIF up to 12 MP**. Describe these as approved admission caps; record the fixtures actually validated by Phase 4, rather than claiming every admitted file fits 384 MiB. Include the JPEG-only early-thumbnail fix and runtime temp-path protection.

### `docs/USER-SPEC.md` §11 (lines 248-250)

- §6 (line 143): "JPEGs up to 48 MP [`MAX_IMAGE_PIXELS`]; PNG and GIF up to 12 MP [`MAX_FULL_DECODE_PIXELS`]."
- Keep the resource-bound qualification: files within those caps can still be rejected by the merge memory/time limits.
- §11 (line 249): `MAX_IMAGE_PIXELS` (48 MP), `MAX_FULL_DECODE_PIXELS` (12 MP).
- Remove `CLEANUP_MIN_AGE_SECS (60s)`; that constant does not exist.
- Rename `MERGE_MEM_LIMIT_BYTES` to `MERGE_MEMORY_BYTES`.
- Cross-check every other constant name in that list against `bot.py`/`merge_pdf.py` with `grep -n`.

### `HELP_TEXT` and `README.md` (Standards, hard)

`CLAUDE.md`: hard limits "has to land there as well as in `HELP_TEXT` and the README."

- `HELP_TEXT`: short lines under the `half` option: `  (half mode: up to 10 queued files, images and PDFs only, max 50 pages)` and `  (JPEG max 48 MP; PNG/GIF max 12 MP; memory/time limits also apply)`. Keep the whole help reply within Telegram's message-length limit.
- `README.md` "Half-sheet mode" section: add the 10-file queue cap, the 50-page cap, the JPEG 48 MP / PNG-GIF 12 MP limits, and the 3508 px downscale, linking to USER-SPEC for the rest.

**Acceptance (Phase 3)**
- Each row of the `CLAUDE.md` table above is resolved; `grep -n "filters.ALL\|will not crash\|PIL\`, \`pypdf\|60s for manual" CLAUDE.md` returns nothing.
- `grep -nE 'CLEANUP_MIN_AGE|MERGE_MEM_LIMIT' CLAUDE.md README.md docs/USER-SPEC.md docs/CHANGELOG.md` returns nothing. Historical plans/audits, including this plan's instructions, are not searched.
- `grep -rn "120 MP\|40 MP" CLAUDE.md README.md docs/USER-SPEC.md docs/CHANGELOG.md bot.py merge_pdf.py scripts/` returns nothing. Historical plan and audit files are exempt.
- `python3 test_bot.py` still passes `test_version_matches_changelog`.

---

## Phase 4 — Make the checks prove what they claim

### `scripts/check_io.py`

1. **Real ownership transfer and sweep path.** Extract the body of `cleanup_task`'s loop into `async def _sweep_once(now: float) -> None` in `bot.py`; `cleanup_task` becomes `while True: await asyncio.sleep(6 * 3600); await _sweep_once(time.monotonic())`. Rewrite `test_in_flight_ownership_and_periodic_sweep` (check_io.py:278-321):
   - Set up a live queue with an input file older than `SESSION_TTL` and a fresh queue timestamp; clear the chat's cooldown.
   - Start the real `bot._flush_half_queue(...)` in a task. Pause it at a fake merge process's `communicate()` using events, after the production code has registered ownership and popped the queue. Do not manually add input paths to `in_flight_paths`, pop the queue, or replace `_print_and_reply` with a fake.
   - Assert the queue has been popped, then call the real `bot._sweep_once(...)` while the flush is suspended. Assert the old input survives.
   - Release the fake process, complete the flush through production cleanup, and assert input/output deletion and registration release. Restore globals and cancel/reap the task in test cleanup if an assertion fails.
   - Mutation proof must remove `_flush_half_queue`'s actual ownership registration and show that the test fails. Calling the real sweep while simulating ownership manually is not sufficient.
2. **Download failure.** New case: a fake `file_obj` whose `download_to_drive` writes a partial file then raises. Assert that `bot._download_tracked` re-raises, the partial file is gone, and the path is not in `in_flight_paths`. Repeat with `asyncio.CancelledError`.
3. **Cancellation during merge.** The `"cancel"` failure type is defined at check_io.py:355 but never run. Run it: cancel the task awaiting `print_file`. Assert the fake merge process got `kill()` and `wait()`, no `lp` call was recorded, `merged_path` is gone from disk and from `in_flight_paths`.
4. **No `lp` after a failed merge.** For each existing merge failure case (error, timeout, killed), assert the recorded argv list contains no `lp` invocation.
5. **Configuration failure.** Unset `PRINTER_NAME` (restore after). Assert that `print_file` raises `RuntimeError` mentioning `PRINTER_NAME`, and that **no subprocess at all** was started: no merge child and no `lp`. This proves the check runs before the merge.
6. **Preference worker and temp-path protection.** Pause a real `save_preferences` worker at `fsync`; run the real `_sweep_once`, and assert that its skip paths include the fixed temp path regardless of age. Use an old pre-existing temp file and a deterministic barrier before truncation to exercise reuse, not only a freshly created file. Verify startup cleanup still removes a stale temp file, then run the real wizard save path with a fake Telegram query: while the worker is paused, cancel the handler and assert it does not finish until the worker is released. After draining, cancellation propagates, no worker remains, and the saved JSON is intact. Use events/timeouts and restore all mocks and chat state.
7. **Subprocess teardown races.** Cover an already-exited process whose `kill()` raises `ProcessLookupError`, plus cancellation during `wait()`. Require reaping before file removal and preservation of the original exception. Exercise CUPS-query and `lp` callers as well as the merge child after helper consolidation; their distinct timeout replies and `ex.cmd` logging must remain unchanged.

### `scripts/check_runtime.py`

1. **Downscale is real** (lines 157-167): after merging the 48 MP JPEG, read `reader.pages[0].images[0].image.size` (pypdf ≥4 API) and assert `max(size) <= merge_pdf.PRINT_MAX_PX`. Remove the comment that admits the check is missing.
   - For landscape fixtures, also assert the expected aspect-preserving dimensions, so an unnecessarily coarse `draft()` scale cannot pass merely by being smaller. For EXIF fixtures, use a non-square image with distinct corner colors and assert the expected oriented dimensions/corners (allowing JPEG compression tolerance), not just page count.
   - Strengthen the existing transparent-PNG check to inspect decoded PDF image pixels: transparent areas must be white and opaque colored areas retained. Check palette GIF transparency likewise. A page-count assertion alone cannot prove white-background compositing was preserved.
2. **The real CLI sets the limit** (lines 200-222): delete the probe that sets `RLIMIT_AS` itself. Replace it with a subprocess that imports `merge_pdf`, replaces `merge_pdf.merge_to_pdf` with a function that writes `resource.getrlimit(resource.RLIMIT_AS)` to the output path, sets `sys.argv`, and calls `merge_pdf.main()`. Assert the written limit equals `MERGE_MEMORY_BYTES` for both soft and hard. This exercises the real `main()`.
3. **Admission-boundary fixtures under the real limit.** Run the real CLI (`python3 merge_pdf.py out.pdf 0 <file>`) under its unmodified 384 MiB bound:
   - an 8000×6000 noise JPEG (48 MP, quality 95) → exit 0;
   - a 6928×6928 RGB JPEG (47,997,184 pixels), both constant-color and noise at quality 95, plus EXIF orientation 6 variants → exit 0; add a near-square CMYK JPEG to exercise conversion after early resize. These catch the near-square `draft()` scale-selection gap;
   - a 4000×3000 RGBA PNG with a transparent background → exit 0;
   - a 4000×3000 GIF with transparency → exit 0.

   And just over each cap, which must be rejected by the cap and not by the memory limit:
   - an 8001×6000 JPEG → exit 1, stderr names the pixel cap;
   - a 4001×3000 PNG → exit 1, stderr names the pixel cap.

   These cases validate the listed shapes and decode paths, not every file admitted by a pixel cap. Derive boundary assertions from the approved constants and include near-square cases whenever the JPEG cap is reconsidered. Cap changes require new measurements, not merely updated literals in the tests.
   - Large noise JPEGs may exceed Telegram's unchanged `MAX_FILE_BYTES` (20 MB). They are CLI decoder stress fixtures, not evidence that the bot admits larger uploads; use a compact near-square JPEG within 20 MB for any real bot/printer exercise.
4. **Update the existing PNG-rejection case** (`check_runtime.py:136`, currently "over 40 MP") to use a PNG just over 12 MP.
5. **Allocation exhaustion gives a clean reason.** Run the real `merge_pdf.main()` in a subprocess with a test-only replacement for `merge_to_pdf`; do not use the 39 MP PNG, which the new 12 MP cap rejects before decoding.
   - Before calling `main()`, the subprocess reads its own `VmSize` from `/proc/self/status`, after importing `merge_pdf`, Pillow and pypdf.
   - It patches `merge_pdf.MERGE_MEMORY_BYTES` to that value plus 32 MiB. A fixed 64 MiB can be below the address space the interpreter already holds, so the failure would land somewhere unrelated.
   - The replacement merge function writes a marker proving it was reached, then attempts a touched `bytearray` allocation larger than the established address-space limit. It must not catch `MemoryError` or call `setrlimit` itself. Pass valid CLI arguments and let production `main()` set the limit and handle the exception.
   - Assert the marker exists, proving the failure occurred in the merge call rather than argument parsing, imports or admission checks.
   - Assert exit 1, a single stderr line containing "memory limit", and no traceback.
6. **Edited wizard cancellation.** Build the real application, place its conversation in `PREF_COLOR`, and probe an edited `/cancel` update. The conversation must not claim it; a normal `/cancel` must match `cancel_preferences`. This proves the fallback filter fix without Telegram network calls.

All new subprocess probes use explicit timeouts, and every event wait has a bounded deadline and task/worker cleanup in `finally`. A broken guard should fail the check, not hang the release job.

**Acceptance (Phase 4)**
- `python3 scripts/check_io.py` passes on the bare system interpreter.
- `python3 scripts/check_runtime.py` passes in a Python 3.10 venv built from `requirements.txt`.
- Each new case is shown to fail first: temporarily comment out the guarded line (e.g. the `kill()` in `_communicate_or_kill`, the `os.remove` in `_download_tracked`, the `setrlimit` in `main()`), confirm the case fails, restore. Record which mutations were tried in the closeout.
- Include the ownership-registration mutation described above; restore every mutation before subsequent checks or commits.
- Bypassing JPEG early thumbnailing must fail the near-square regression; removing the runtime temp-path skip must fail the reuse test; removing the wizard fallback filter must fail the edited-cancel probe. Run mutations in a disposable copy, not on the user's working application files.

### Deployment memory gate — required before release

- Build and run the combined changes under the actual `docker-compose.yml` 512 MB memory limit on **amd64 and arm64**. Confirm the effective cgroup limit, not just the YAML value.
- Exercise the admission-boundary image fixtures above, a complex PDF fixture, and test-only forced allocation exhaustion through the real merge CLI path. The deployment exhaustion probe retains the production 384 MiB child bound; the relative smaller bound in local runtime case 5 is not a substitute.
- Record parent/child and total cgroup peak memory with headroom. On exhaustion, require a nonempty failure reply, unchanged bot PID and container restart count, removed temporary output, and a successful subsequent `/help`.
- If the envelope or parent survival fails on either architecture, stop release and revise the plan. A shared cgroup does not guarantee that the OOM killer selects the child. Do not raise `MERGE_MEMORY_BYTES` beyond the validated envelope to make a fixture pass.

---

## Execution order

1. In a later implementation turn, add the relevant regression cases from Phase 4 first, then implement Phases 1 and 2. `_sweep_once` extraction supplies the production seam for the ownership/sweep check; do not duplicate the loop in a test.
2. Run all Phase 4 local gates and its deployment memory gate on both architectures before release.
3. Phase 3 comes last, so the docs describe the final code and measured acceptance results.
4. Before tagging/publishing, recheck that `v1.3.0` does not exist locally or on `origin`; the no-version-bump decision applies only while 1.3.0 remains unreleased.

## Verification summary

```bash
python3 test_bot.py
python3 scripts/check_io.py
python3 scripts/bump_version.py check
python3 -m py_compile bot.py merge_pdf.py scripts/*.py test_bot.py
# Python 3.10 venv from requirements.txt:
python3 scripts/check_runtime.py
pip-audit -r requirements.txt
# Before release: Phase 4 deployment memory gate under the actual 512 MB
# compose limit on amd64 and arm64; record peaks, headroom and bot survival.
```

Manual (real printer), only the paths this plan touches:
- half pair prints;
- `print` with a lone file;
- `/clean` while another chat has a queued file (that file survives);
- `/preferences` → Normal with a queued half file (file discarded, reply arrives);
- half mode with a 12 MP transparent PNG screenshot (prints), then a 13 MP one (rejected with the cap message);
- send `/preferences`, then edit the message to `/cancel` (wizard does not cancel).

## Risks

- **`save_preferences` now runs in a worker thread.** It must not touch shared mutable state the event loop iterates concurrently. After 1.1, it reads only `user_preferences` and writes only its own files.
  - `user_preferences` is safe to read there only while the sequential wizard handler awaits or drains the same thread, including cancellation. Do not let cancellation leave an unowned worker running.
  - The check: `grep -n "in_flight_paths\|half_queue" bot.py` has no hit between `def save_preferences` and the next `def`.
- **Lower caps reject some files 1.3.0's docs promised.**
  - Affected: a 13–40 MP PNG (large screenshots, scans) or a 49–120 MP JPEG in half mode now gets a rejection reply.
  - Some oversized fixtures failed in the current, unreleased 1.3.0 code with a memory-limit error. That does not establish that every file above the new admission caps failed; the lower caps are a real user-visible restriction.
  - Normal (full-page) mode is unaffected, because it does not merge.
- **JPEG shape and orientation affect memory.** The early-thumbnail design passed the pinned-runtime scratch fixtures, but final RGB/CMYK, EXIF, noise, and deployment checks remain future implementation acceptance; scratch success is not a release claim.
- **Caps measured on x86.** The Pi (aarch64) may lay out address space differently. Both architectures must pass the fixture and container memory gates before release; neither the 384 MiB child bound nor local fixture success alone guarantees parent survival under 512 MB.
- **`_sweep_once` extraction changes `cleanup_task`'s shape.** Behaviour is unchanged; `_on_cleanup_task_done` still wraps the task.

## Final plan-review record — 2026-10-05

- **Intent/scope:** Correct the unreleased 1.3.0 implementation at its existing owners; keep the approved memory/admission limits, sequential updates, printer argv and dependency set. Only this plan was edited during the review/fix loop.
- **Phase 1 trace:** Followed wizard save → fixed temp-file replacement, both download ownership transfers, subprocess timeout/cancellation paths, and runtime/startup cleanup. Reproduced the temp-path age-check/unlink race; the plan now specifies runtime name protection and worker draining, not an age-only assertion.
- **Phase 2 trace/proof:** Reproduced the near-square JPEG failure in a scratch copy of the actual CLI using Python 3.10.20 and the pinned Pillow/pypdf versions under 384 MiB. JPEG-only early thumbnailing passed RGB, rotated RGB, rotated CMYK, and a noisy rotated RGB fixture; the latter's embedded PDF image was 3508×3508. No source implementation was changed.
- **Phases 3–4 review:** Checked documentation parity, test/production seams, mutation sensitivity, fixture boundaries, error reasons, wait deadlines, ownership release, and both-architecture container gates. The proposed help text plus current help is 928 characters, below Telegram's 4096-character limit. Searches target maintained docs rather than this historical plan; noise stress fixtures do not change the 20 MB upload limit.
- **Current-code checks:** `python3 test_bot.py` (14 checks), `python3 scripts/check_io.py` (8 checks), `python3 scripts/bump_version.py check`, and pinned-Python-3.10 `scripts/check_runtime.py` (3 checks) passed. These are baseline checks, not proof that the new planned regressions already exist. Local and `origin` queries found no `v1.3.0` tag; recheck at release time.
- **Verdict:** Plan review **PASS**, with no remaining known plan findings. Implementation acceptance, mutation runs, amd64/arm64 cgroup survival measurements, dependency audit and real-printer checks remain explicit future execution/release gates; none is claimed complete by this record.
