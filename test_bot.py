"""Runnable self-check for NotaNext's pure logic.

Covers the parts that would silently misbehave if broken: print-option keyword
parsing, the PrintOptions persistence round-trip, and the half-mode extension
gate. Everything here is pure — no printer, no network, no Telegram.

The third-party modules bot.py imports are stubbed, so this runs on a bare
interpreter with nothing installed:

    python3 test_bot.py
"""

import sys
from unittest import mock

for _name in ("httpx", "telegram", "telegram.ext", "telegram.warnings"):
    sys.modules.setdefault(_name, mock.MagicMock())

import bot  # noqa: E402
from bot import HalfQueueEntry, PrintOptions, parse_option_tokens  # noqa: E402


def test_defaults():
    opts = PrintOptions()
    assert opts.color is True
    assert opts.copies == 1
    assert opts.media == "A4"
    assert opts.number_up == 1


def test_half_queue_entry_defaults():
    entry = HalfQueueEntry()
    assert entry.files == []
    assert entry.ts == 0.0
    entry.files.append("test.pdf")
    assert entry.files == ["test.pdf"]


def test_parses_every_documented_keyword():
    """Every keyword the README and HELP_TEXT advertise must parse."""
    documented = [
        "bw", "gray", "color",
        "1x", "2x", "3x", "4x",
        "a4", "a5",
        "half", "2up",
        "normal", "full", "single", "1up",
    ]
    for token in documented:
        assert parse_option_tokens([token], PrintOptions()), f"{token!r} was rejected"


def test_combined_options():
    opts = PrintOptions()
    assert parse_option_tokens(["bw", "2x", "a5"], opts)
    assert opts.color is False
    assert opts.copies == 2
    assert opts.media == "A5"


def test_unknown_token_invalidates_whole_message():
    opts = PrintOptions()
    assert not parse_option_tokens(["bw", "sideways"], opts)


def test_half_and_normal_toggle_number_up():
    opts = PrintOptions()
    parse_option_tokens(["half"], opts)
    assert opts.number_up == 2
    parse_option_tokens(["normal"], opts)
    assert opts.number_up == 1


def test_persistence_round_trip():
    """to_dict/from_dict must preserve the profile and never persist `ts`."""
    original = PrintOptions(color=False, copies=3, media="A5", number_up=2, ts=123.4)
    encoded = original.to_dict()
    assert "ts" not in encoded, "session timestamp must not be persisted"
    assert PrintOptions.from_dict(encoded) == PrintOptions(
        color=False, copies=3, media="A5", number_up=2, ts=0.0
    )


def test_from_dict_tolerates_missing_keys():
    assert PrintOptions.from_dict({}) == PrintOptions()
    assert PrintOptions.from_dict({"media": "A5"}).copies == 1


def test_labels():
    assert PrintOptions(color=True).color_label == "Color"
    assert PrintOptions(color=False).color_label == "Gray (B&W)"
    assert PrintOptions(number_up=2).mode_label == "Half sheet"
    assert PrintOptions(number_up=1).mode_label == "Normal"


def test_half_mode_gate_is_narrower_than_printable():
    """Half mode merges to PDF, so it must accept less than normal printing."""
    assert bot.MERGEABLE_EXTENSIONS < bot.PRINTABLE_EXTENSIONS
    for ext in (".docx", ".txt", ".odt", ".doc", ".ps"):
        assert ext in bot.PRINTABLE_EXTENSIONS
        assert ext not in bot.MERGEABLE_EXTENSIONS


def test_cooldown_remaining():
    """The shared rate-limit helper must block the whole cooldown window."""
    import time

    chat = -999
    bot.last_print_time.pop(chat, None)
    assert bot.cooldown_remaining(chat) == 0, "an unseen chat is never rate-limited"

    bot.last_print_time[chat] = time.monotonic()
    remaining = bot.cooldown_remaining(chat)
    assert remaining == bot.PRINT_COOLDOWN, f"a fresh print should block for the full window, got {remaining}"

    # A fraction of a second left must still block, and must never report "0s".
    bot.last_print_time[chat] = time.monotonic() - (bot.PRINT_COOLDOWN - 0.4)
    assert bot.cooldown_remaining(chat) == 1

    bot.last_print_time[chat] = time.monotonic() - bot.PRINT_COOLDOWN
    assert bot.cooldown_remaining(chat) == 0
    bot.last_print_time.pop(chat, None)


def test_preferences_cap_is_a_constant():
    """The cap is a safety bound, not a tunable — it must not read the environment."""
    assert isinstance(bot.MAX_PREFERENCES, int) and bot.MAX_PREFERENCES >= 1
    assert not hasattr(bot, "get_preferences_limit"), "the env-var parser should be gone"


def test_version_matches_changelog():
    """The single source of truth VERSION and all repository files must not drift apart."""
    with open("VERSION") as f:
        single_source_version = f.read().strip()
    with open("docs/CHANGELOG.md") as f:
        changelog = f.read()
    with open("docker-compose.yml") as f:
        compose = f.read()
    with open("README.md") as f:
        readme = f.read()
    with open("docs/USER-SPEC.md") as f:
        spec = f.read()

    assert bot.VERSION == single_source_version, "bot.VERSION does not match VERSION file"
    assert f"## [{bot.VERSION}]" in changelog, "no CHANGELOG entry for VERSION"
    assert bot.VERSION in compose, "docker-compose.yml is pinned to another version"
    assert f"notanext:{bot.VERSION}" in readme, "README.md does not reference current VERSION"
    assert f"Version {bot.VERSION}" in spec, "docs/USER-SPEC.md documents another VERSION"


def test_bump_version_helpers():
    """Verify semver calculations and check consistency helper."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("bump_version", "scripts/bump_version.py")
    bump_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bump_module)

    assert bump_module.calculate_next_version("1.2.0", "patch") == "1.2.1"
    assert bump_module.calculate_next_version("1.2.0", "minor") == "1.3.0"
    assert bump_module.calculate_next_version("1.2.0", "major") == "2.0.0"
    assert bump_module.read_version() == bot.VERSION
    assert bump_module.check_consistency() is True


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {name}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
