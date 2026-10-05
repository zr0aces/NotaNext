# NotaNext — User Specification

What the bot does, from the user's side. Version 1.3.1.

Behaviour here is what `bot.py` actually implements; the constants named in
brackets are the ones to grep for if you need to change a number.

---

## 1. What it is

A Telegram bot that prints. Send it a photo or a document and it forwards the
file to a CUPS-connected printer, then replies when the job has been submitted.
There is no web UI and no queue of its own — CUPS owns the queue, and the bot is
a front end to `lp`.

---

## 2. Access

| Who | What they can do |
|---|---|
| Anyone | `/help`, `/status` |
| Chat IDs listed in `ALLOWED_CHAT_IDS` | everything else |

If `ALLOWED_CHAT_IDS` is unset, **every Telegram user who finds the bot can
print**, and the bot logs a warning saying so at startup. Chat IDs are permanent
numeric identifiers assigned by Telegram and cannot be spoofed, which is why
they are used instead of usernames.

`/start` and `/preferences` are access-controlled on purpose: saved profiles are
a capped resource, so an open wizard would let strangers fill the store and lock
real users out.

---

## 3. Commands

| Command | Effect |
|---|---|
| `/start` | Welcome message, then the preferences wizard. Shows current defaults if any are saved. |
| `/help` | The command and print-option reference. |
| `/preferences` | The preferences wizard on its own. |
| `/status` | Printer reachability — runs `lpstat -p` against the configured server. |
| `/jobs` | The current CUPS queue for the configured printer — `lpstat -o <PRINTER_NAME>`. |
| `/cancel` | Cancels all print jobs on the configured printer — `cancel -a <PRINTER_NAME>`. |
| `/clean` | Deletes cached files in the data directory and clears your own half-mode queue. |

`/cancel` means two things depending on context. While the preferences wizard is
open it aborts the wizard; the reply then tells you to send it again to reach the
print-queue meaning.

---

## 4. Printing a file

Send a photo or a document. That is the whole interaction — there is no "confirm"
step.

**Accepted document types**: `.pdf`, `.ps`, `.jpg`, `.jpeg`, `.png`, `.gif`,
`.txt`, `.doc`, `.docx`, `.odt`. Photos sent as Telegram photos are always
accepted and are treated as `.jpg`. A document outside that list is refused
with the supported list; anything that's neither a photo nor a document — a
voice note, video, sticker, GIF, contact card — gets a reply pointing at
`/help` instead of being silently ignored.

**Size limit**: 20 MB [`MAX_FILE_BYTES`] — Telegram's own bot limit. Checked
against the message metadata *before* the file is downloaded, so an oversized
file never touches the disk.

**Rate limit**: one accepted job per chat per 10 seconds [`PRINT_COOLDOWN`].
Sending faster gets a reply with the seconds remaining. In normal mode the slot
is claimed before the download starts, so two files sent at once cannot both slip
through; the slot is released again if the message turns out to hold no printable
file.

For a photo, the largest available resolution is chosen.
Edited messages are ignored: editing a photo caption or document after sending
does not trigger a reprint.

---

## 5. Print options

Send these as a plain text message *before* the file. They are not commands — no
leading slash.

| Keyword | Effect |
|---|---|
| `bw`, `gray` | Black and white |
| `color` | Full colour |
| `1x`, `2x`, `3x`, `4x` | Number of copies |
| `a4` | A4 paper |
| `a5` | A5 paper |
| `half`, `2up` | Half-sheet mode — two pages per physical sheet (see below) |
| `normal`, `full`, `single`, `1up` | One page per sheet |
| `print` | Print whatever is queued in half mode right now |

Combine them in one message: `bw 2x a5`.

**One unknown word rejects the whole message.** `bw sideways` changes nothing at
all — you get the "Unknown option" reply and your previous settings stand. This
is deliberate: a partly-applied message would leave you printing with settings
you never asked for.

Options are per chat, not per file. Once set they apply to every file you send
until they expire.

---

## 6. Half-sheet mode

`half` prints two pages onto one physical sheet. It works differently from the
other options because it needs two things to pair up.

1. Send `half`.
2. Send a file. It is **queued**, not printed. The bot confirms and tells you how
   many sheets that adds up to so far.
3. Send a second file. The pair is merged and printed automatically.
4. Half mode stays on — the next two files pair up the same way.

To print an odd number of files, send `print` and the queue flushes immediately.
A single file printed this way gets a blank second page appended, so the printer
lays it out on half the sheet instead of scaling it to fill the page.

**Half mode accepts fewer file types than normal printing**: only `.jpg`,
`.jpeg`, `.png`, `.gif` and `.pdf`. Everything is merged into one PDF before it
reaches the printer, and the other formats cannot be merged. A `.docx` sent in
half mode is refused at queue time with a pointer to `normal` mode — it is not
silently printed full-page.

**Queue cap**: up to 10 files [`MAX_HALF_QUEUE_FILES`] can wait in the queue.
Sending an 11th file is rejected until the queue is printed with `print` or reset
with `normal` / `/clean`.

**Queue expiration**: a half queue expires after 30 minutes of inactivity
[`SESSION_TTL`]. Expiration is enforced on use — sending a file after 30 minutes
of inactivity automatically clears any stale queued files rather than pairing
with them.

**Half-mode limits and processing**:
- **Page cap**: at most 50 pages total per merged print job [`MAX_MERGED_PAGES`].
- **Pixel limits**: JPEGs up to 48 MP [`MAX_IMAGE_PIXELS`]; PNG and GIF up to 12 MP [`MAX_FULL_DECODE_PIXELS`].
- **Downscaling**: images are downscaled to at most 3508 px on the long side (A4 at 300 DPI, [`PRINT_MAX_PX`]).
- **Transparency**: transparent PNG and GIF images are composited onto a solid white background before merging.
- **Resource protection**: merge execution runs in an isolated subprocess with a 384 MiB virtual address limit and a 60-second timeout [`MERGE_TIMEOUT`]. Complex or adversarial files exceeding these limits are safely aborted.

Switching to `normal` **discards** anything still queued, and says how many files
it dropped. Being rate-limited does not discard the queue: the files stay put and
`print` retries them once the cooldown passes.

---

## 7. Defaults versus temporary settings

Two layers, and the temporary one wins.

**Saved defaults** — set through `/preferences` or the `/start` wizard: colour
mode, sheet mode, paper size. Stored in `data/preferences.json` and survive
restarts and container recreation. The wizard never asks for a copy count, so a
saved profile always carries 1 — use `2x` when you need more.

**Temporary settings** — set by sending option keywords. They last **30 minutes**
[`SESSION_TTL`], and the window is *sliding*: every file you send and every
option you change pushes the expiry back out to a full 30 minutes. When it does
lapse, settings fall back to your saved defaults — not to the factory ones.

With no saved defaults and no active session, printing is colour, 1 copy, A4,
one page per sheet.

Saving new defaults applies them to the active session immediately, so a stale
override cannot shadow the choice you just made.

The store holds 10 chats [`MAX_PREFERENCES`]. Updating your own profile always
works; once the store is full, a chat that has never saved one is told so. This
is a bound against a stranger exhausting the store, not a setting to tune — it is
a constant in `bot.py`, not an environment variable.

---

## 8. What reaches the printer

```
lp -h $CUPS_SERVER -d $PRINTER_NAME -o fit-to-page -o media=A4|A5 \
   [-o number-up=2] [-o ColorModel=Gray -o CNColorMode=mono] [-n copies] file...
```

Grayscale sends two flags. Canon UFRII LT printers — the LBP7110Cw this was built
for — ignore the standard `ColorModel=Gray` and only respond to the proprietary
`CNColorMode=mono`. Both are needed for the bot to work across printers.

If `lp` fails, the reply carries the truncated CUPS error to help diagnose the issue.

---

## 9. Home Assistant

Optional. If both `HA_URL` and `HA_TOKEN` are set, a successful print fires the
event `notanext_job_sent`:

```json
{ "file_name": "...", "chat_id": 123456789, "copies": 1, "color": true }
```

Best-effort — a failed webhook is logged and never blocks or fails the print.

> Renamed from `pimnext_job_sent` in 1.1.1 along with the project. Automations
> written against the old event name need updating.

---

## 10. Files on disk

Everything lives under `data/`.

- Downloaded files are deleted as soon as the job is submitted, whether the print
  succeeded or failed.
- The merged PDF that half mode builds is deleted the same way.
- In-flight files currently being downloaded, merged, or printed are explicitly tracked and protected from sweeps.
- A sweep every 6 hours drops leftovers older than 30 minutes, expired sessions and expired half
  queues. Files still waiting in an active queue or currently in flight are protected from it.
- `data/preferences.json` and any quarantined `data/preferences.json.corrupt` are never touched by any cleanup, including `/clean`. The fixed temporary preferences path (`data/preferences.json.tmp`) is explicitly skipped by runtime sweeps to prevent reuse races.
- `/clean` deletes the calling chat's queued files and unreferenced cached files immediately (`min_age=0`), leaving other chats' queues and active prints intact.
- A cleanup also runs at startup, so a crash mid-print does not leave files
  behind forever.

---

## 11. Configuration

Eight environment variables, all in `.env`. Nothing else configures the bot.

| Variable | Required | Value |
|---|---|---|
| `TOKEN` | yes | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `CUPS_SERVER` | yes | Host or IP running CUPS on port 631 |
| `PRINTER_NAME` | yes | Printer name from `lpstat -v -h $CUPS_SERVER` |
| `ALLOWED_CHAT_IDS` | no | Comma- or space-separated chat IDs. **Unset means anyone can print.** |
| `LOG_LEVEL` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default `INFO`; an unrecognised value warns and falls back. |
| `HA_URL` | no | Home Assistant base URL. Needs `HA_TOKEN` too. |
| `HA_TOKEN` | no | Long-lived access token. Needs `HA_URL` too. |
| `TZ` | no | Timezone for container log timestamps. Default UTC. |

Values that are bounds rather than deployment settings are constants in
`bot.py` and `merge_pdf.py`, deliberately not variables:
`SESSION_TTL` (1800s), `PRINT_COOLDOWN` (10s), `MAX_PREFERENCES` (10),
`MAX_FILE_BYTES` (20 MB), `MAX_HALF_QUEUE_FILES` (10),
`MERGE_TIMEOUT` (60s), `MAX_MERGED_PAGES` (50),
`MAX_IMAGE_PIXELS` (48 MP), `MAX_FULL_DECODE_PIXELS` (12 MP),
`PRINT_MAX_PX` (3508 px), `MERGE_MEMORY_BYTES` (384 MiB),
`MAX_STDERR_LENGTH` (300 characters of CUPS error echoed back to the chat).

---

## 12. Limits worth knowing

- One CUPS printer per bot instance. No printer selection at runtime.
- `/cancel` cancels all jobs on the configured printer, not one job.
- Copies cap at 4, and only through the four keywords.
- Paper is A4 or A5. Nothing else.
- `number-up` is 1 or 2. There is no 4-up.
- Preferences are per chat, so a group chat shares one profile.
- Half mode pairs files in the order they arrive; there is no way to reorder or
  remove one file from the queue short of `normal`, which drops all of them.
- Half mode queue is capped at 10 files and merged jobs are capped at 50 pages.
