"""Dependency-free I/O and lifecycle checks for NotaNext.

Tests subprocess construction, preference durability, cleanup semantics,
in-flight tracking, and version CLI git handling using temporary directories
and mocks without touching real data, live Telegram, or CUPS servers.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

# Stub external dependencies before importing bot
for _name in ("httpx", "telegram", "telegram.ext", "telegram.warnings"):
    sys.modules.setdefault(_name, mock.MagicMock())

import bot  # noqa: E402
from bot import (  # noqa: E402
    CORRUPT_PREFERENCES_FILE,
    DATA_DIR,
    PREFERENCES_FILE,
    HalfQueueEntry,
    PrintOptions,
    clean,
    discard_half_queue,
    get_half_queue,
    load_preferences,
    perform_cleanup,
    print_file,
    save_preferences,
)


def run_async(coro):
    return asyncio.run(coro)


def test_print_file_argv():
    """Verify lp and merge argv, flags, and merge error propagation."""
    orig_lp = bot.LP_BIN
    orig_server = os.environ.get("CUPS_SERVER")
    orig_printer = os.environ.get("PRINTER_NAME")

    bot.LP_BIN = "lp"
    os.environ["CUPS_SERVER"] = "test-server"
    os.environ["PRINTER_NAME"] = "test-printer"

    executed_cmds = []

    class FakeProcess:
        def __init__(self, cmd, returncode=0, stdout=b"", stderr=b""):
            self.cmd = cmd
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr
            self.killed = False
            self.waited = False

        async def communicate(self):
            return self._stdout, self._stderr

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        executed_cmds.append(list(cmd))
        return FakeProcess(list(cmd), returncode=0, stdout=b"OK", stderr=b"")

    try:
        # 1. Normal mode with custom options
        opts_normal = PrintOptions(color=False, copies=2, media="A5", number_up=1)
        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            executed_cmds.clear()
            run_async(print_file(["/tmp/file1.pdf"], opts_normal))

        assert len(executed_cmds) == 1
        cmd = executed_cmds[0]
        assert cmd[0] == "lp"
        assert "-h" in cmd and cmd[cmd.index("-h") + 1] == "test-server"
        assert "-d" in cmd and cmd[cmd.index("-d") + 1] == "test-printer"
        assert "media=A5" in cmd[cmd.index("-o") + 1] or any("media=A5" in x for x in cmd)
        assert "ColorModel=Gray" in cmd
        assert "CNColorMode=mono" in cmd
        assert "-n" in cmd and cmd[cmd.index("-n") + 1] == "2"
        assert cmd[-1] == "/tmp/file1.pdf"

        # 2. Half mode: should run merge_pdf first, then lp with number-up=2
        opts_half = PrintOptions(color=True, copies=1, media="A4", number_up=2)
        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            executed_cmds.clear()
            run_async(print_file(["/tmp/photo.jpg"], opts_half))

        assert len(executed_cmds) == 2
        merge_cmd = executed_cmds[0]
        lp_cmd = executed_cmds[1]

        assert merge_cmd[0] == sys.executable
        assert merge_cmd[1] == bot.MERGE_SCRIPT
        # Single file -> pad_for_half == "1"
        assert merge_cmd[3] == "1"
        assert merge_cmd[4] == "/tmp/photo.jpg"

        assert lp_cmd[0] == "lp"
        assert "-o" in lp_cmd and "number-up=2" in lp_cmd

        # 3. Merge process killed by signal (returncode = -9)
        async def fake_killed_merge(*cmd, **kwargs):
            return FakeProcess(list(cmd), returncode=-9, stdout=b"", stderr=b"")

        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_killed_merge):
            try:
                run_async(print_file(["/tmp/photo.jpg"], opts_half))
                assert False, "Expected RuntimeError on merge kill"
            except RuntimeError as e:
                assert "Merge was stopped (file too large or too complex)." in str(e)

    finally:
        bot.LP_BIN = orig_lp
        if orig_server is None:
            os.environ.pop("CUPS_SERVER", None)
        else:
            os.environ["CUPS_SERVER"] = orig_server
        if orig_printer is None:
            os.environ.pop("PRINTER_NAME", None)
        else:
            os.environ["PRINTER_NAME"] = orig_printer


def test_load_preferences_corrupt():
    """Corrupt JSON or non-dict top level renames the file to preferences.json.corrupt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pref_file = os.path.join(tmpdir, "preferences.json")
        corrupt_file = os.path.join(tmpdir, "preferences.json.corrupt")

        with mock.patch("bot.DATA_DIR", tmpdir), \
             mock.patch("bot.PREFERENCES_FILE", pref_file), \
             mock.patch("bot.CORRUPT_PREFERENCES_FILE", corrupt_file):

            # 1. Malformed JSON
            with open(pref_file, "w") as f:
                f.write("{invalid json syntax")

            bot.user_preferences = {"test": PrintOptions()}
            load_preferences()

            assert bot.user_preferences == {}
            assert not os.path.exists(pref_file)
            assert os.path.exists(corrupt_file)
            with open(corrupt_file, "r") as f:
                assert f.read() == "{invalid json syntax"

            # 2. JSON list instead of dict
            with open(pref_file, "w") as f:
                f.write('[{"color": true}]')

            bot.user_preferences = {"test": PrintOptions()}
            load_preferences()

            assert bot.user_preferences == {}
            assert not os.path.exists(pref_file)
            assert os.path.exists(corrupt_file)
            with open(corrupt_file, "r") as f:
                assert f.read() == '[{"color": true}]'


def test_cancel_and_jobs_argv():
    """Verify /jobs and /cancel include PRINTER_NAME in command arguments."""
    orig_server = os.environ.get("CUPS_SERVER")
    orig_printer = os.environ.get("PRINTER_NAME")
    orig_lpstat = bot.LPSTAT_BIN
    orig_cancel = bot.CANCEL_BIN

    os.environ["CUPS_SERVER"] = "cups-host"
    os.environ["PRINTER_NAME"] = "OfficePrinter"
    bot.LPSTAT_BIN = "/usr/bin/lpstat"
    bot.CANCEL_BIN = "/usr/bin/cancel"

    run_queries = []

    async def fake_run_cups_query(binary, tool, flags, action):
        run_queries.append((binary, tool, flags, action))
        return "mock output", None

    try:
        with mock.patch("bot.run_cups_query", side_effect=fake_run_cups_query):
            mock_update = mock.MagicMock()
            mock_update.effective_message.reply_text = mock.AsyncMock()
            mock_context = mock.MagicMock()

            run_queries.clear()
            run_async(bot.jobs_command(mock_update, mock_context))
            assert len(run_queries) == 1
            bin_used, tool, flags, _ = run_queries[0]
            assert flags == ["-o", "OfficePrinter"]

            run_queries.clear()
            run_async(bot.cancel_command(mock_update, mock_context))
            assert len(run_queries) == 1
            bin_used, tool, flags, _ = run_queries[0]
            assert flags == ["-a", "OfficePrinter"]
    finally:
        bot.LPSTAT_BIN = orig_lpstat
        bot.CANCEL_BIN = orig_cancel
        if orig_server is None:
            os.environ.pop("CUPS_SERVER", None)
        else:
            os.environ["CUPS_SERVER"] = orig_server
        if orig_printer is None:
            os.environ.pop("PRINTER_NAME", None)
        else:
            os.environ["PRINTER_NAME"] = orig_printer


def test_expired_half_queue():
    """get_half_queue removes expired entries and deletes their disk files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        f1 = os.path.join(tmpdir, "part1.jpg")
        f2 = os.path.join(tmpdir, "part2.jpg")
        Path(f1).write_text("data1")
        Path(f2).write_text("data2")

        chat_id = 999
        bot.half_queue[chat_id] = HalfQueueEntry(
            files=[f1, f2],
            ts=time.monotonic() - bot.SESSION_TTL - 10,
        )

        entry = get_half_queue(chat_id)
        assert entry is None
        assert chat_id not in bot.half_queue
        assert not os.path.exists(f1)
        assert not os.path.exists(f2)


def test_perform_cleanup_min_age():
    """perform_cleanup preserves fresh files and deletes files older than min_age."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pref = os.path.join(tmpdir, "preferences.json")
        corrupt = os.path.join(tmpdir, "preferences.json.corrupt")
        fresh = os.path.join(tmpdir, "fresh.tmp")
        old = os.path.join(tmpdir, "old.tmp")

        Path(pref).write_text("{}")
        Path(corrupt).write_text("corrupt")
        Path(fresh).write_text("fresh")
        Path(old).write_text("old")

        # Set old.tmp and preferences to 10000s in the past
        past_time = time.time() - 10000
        os.utime(old, (past_time, past_time))
        os.utime(pref, (past_time, past_time))
        os.utime(corrupt, (past_time, past_time))

        with mock.patch("bot.DATA_DIR", tmpdir), \
             mock.patch("bot.PREFERENCES_FILE", pref), \
             mock.patch("bot.CORRUPT_PREFERENCES_FILE", corrupt):

            removed = perform_cleanup(min_age=bot.SESSION_TTL)

            assert removed == 1
            assert not os.path.exists(old)
            assert os.path.exists(fresh)
            assert os.path.exists(pref)
            assert os.path.exists(corrupt)


def test_in_flight_ownership_and_periodic_sweep():
    """In-flight paths survive the periodic sweep regardless of age."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pref = os.path.join(tmpdir, "preferences.json")
        Path(pref).write_text("{}")

        # Create input file older than SESSION_TTL
        input_file = os.path.join(tmpdir, "queued_input.pdf")
        Path(input_file).write_text("pdf data")
        past_time = time.time() - 10000
        os.utime(input_file, (past_time, past_time))

        chat_id = 42
        bot.half_queue[chat_id] = HalfQueueEntry(
            files=[input_file],
            ts=time.monotonic(),  # active queue
        )

        with mock.patch("bot.DATA_DIR", tmpdir), \
             mock.patch("bot.PREFERENCES_FILE", pref), \
             mock.patch("bot.CORRUPT_PREFERENCES_FILE", os.path.join(tmpdir, "preferences.json.corrupt")):

            # Simulate flush beginning: register in in_flight_paths, then pop queue
            entry = bot.half_queue.get(chat_id)
            files = list(entry.files)
            bot.in_flight_paths.update(files)
            bot.half_queue.pop(chat_id, None)

            # While flush is in-flight, run periodic sweep
            active_files = {fp for q in bot.half_queue.values() for fp in q.files}
            skip_set = frozenset(active_files | bot.in_flight_paths)
            removed = perform_cleanup(skip_paths=skip_set, min_age=bot.SESSION_TTL)

            # The input file MUST survive despite being older than SESSION_TTL
            assert removed == 0
            assert os.path.exists(input_file)

            # Cleanup release
            for fp in files:
                bot.in_flight_paths.discard(fp)
                os.remove(fp)

            assert not os.path.exists(input_file)
            assert len(bot.in_flight_paths) == 0


def test_merge_lifecycle_and_cleanup():
    """Verify merged output removal, child reap, and registration release on failure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pref = os.path.join(tmpdir, "preferences.json")
        Path(pref).write_text("{}")

        orig_lp = bot.LP_BIN
        orig_server = os.environ.get("CUPS_SERVER")
        orig_printer = os.environ.get("PRINTER_NAME")

        bot.LP_BIN = "lp"
        os.environ["CUPS_SERVER"] = "test-server"
        os.environ["PRINTER_NAME"] = "test-printer"

        created_merged = []
        fake_proc = None

        class PartialOutputProcess:
            def __init__(self, cmd, failure_type="error"):
                self.cmd = cmd
                self.failure_type = failure_type
                self.killed = False
                self.waited = False
                self.returncode = 1 if failure_type == "error" else 0
                merged_out = cmd[2]
                Path(merged_out).write_text("partial merged content")
                created_merged.append(merged_out)

            async def communicate(self):
                if self.failure_type == "timeout":
                    raise asyncio.TimeoutError()
                elif self.failure_type == "cancel":
                    raise asyncio.CancelledError()
                return b"", b"Merge syntax error"

            def kill(self):
                self.killed = True

            async def wait(self):
                self.waited = True

        async def fake_failing_subprocess(*cmd, **kwargs):
            nonlocal fake_proc
            fake_proc = PartialOutputProcess(list(cmd), failure_type=current_failure)
            return fake_proc

        with mock.patch("bot.DATA_DIR", tmpdir), \
             mock.patch("bot.PREFERENCES_FILE", pref), \
             mock.patch("bot.CORRUPT_PREFERENCES_FILE", os.path.join(tmpdir, "preferences.json.corrupt")), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=fake_failing_subprocess):

            opts = PrintOptions(number_up=2)

            # 1. Error during merge
            current_failure = "error"
            try:
                run_async(print_file(["/tmp/in1.jpg", "/tmp/in2.jpg"], opts))
                assert False, "Expected error"
            except RuntimeError as e:
                assert "Merge syntax error" in str(e)
            assert not os.path.exists(created_merged[-1]), "Partial output was not removed"
            assert len(bot.in_flight_paths) == 0

            # 2. Timeout during merge
            current_failure = "timeout"
            try:
                run_async(print_file(["/tmp/in1.jpg", "/tmp/in2.jpg"], opts))
                assert False, "Expected timeout"
            except RuntimeError as e:
                assert "Merging took too long" in str(e)
            assert fake_proc.killed is True
            assert fake_proc.waited is True
            assert not os.path.exists(created_merged[-1]), "Partial output was not removed on timeout"
            assert len(bot.in_flight_paths) == 0

            # 3. Subprocess launch failure after merged_path created
            async def launch_fails(*cmd, **kwargs):
                raise OSError("Launch failed")

            with mock.patch("asyncio.create_subprocess_exec", side_effect=launch_fails):
                try:
                    run_async(print_file(["/tmp/in1.jpg", "/tmp/in2.jpg"], opts))
                    assert False, "Expected launch failure"
                except OSError:
                    pass
                assert len(bot.in_flight_paths) == 0

        bot.LP_BIN = orig_lp
        if orig_server is None:
            os.environ.pop("CUPS_SERVER", None)
        else:
            os.environ["CUPS_SERVER"] = orig_server
        if orig_printer is None:
            os.environ.pop("PRINTER_NAME", None)
        else:
            os.environ["PRINTER_NAME"] = orig_printer


def test_version_cli_in_disposable_repo():
    """Verify bump_version parser dry-run, staged index rejection, and commit/tag separation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        repo_dir.mkdir()

        # Initialize disposable git repo
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)

        # Copy version files and bump script
        for f in ("VERSION", "bot.py", "docker-compose.yml", "README.md"):
            shutil.copy(ROOT_DIR / f, repo_dir / f)
        (repo_dir / "docs").mkdir(parents=True)
        for f in ("docs/CHANGELOG.md", "docs/USER-SPEC.md"):
            shutil.copy(ROOT_DIR / f, repo_dir / f)
        (repo_dir / "scripts").mkdir(parents=True)
        shutil.copy(ROOT_DIR / "scripts" / "bump_version.py", repo_dir / "scripts" / "bump_version.py")

        # Initial commit
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)

        initial_ver = (repo_dir / "VERSION").read_text().strip()

        # 1. Test dry-run placement before and after subcommand
        # bump_version.py --dry-run patch
        res1 = subprocess.run(
            [sys.executable, "scripts/bump_version.py", "--dry-run", "patch"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert res1.returncode == 0
        assert (repo_dir / "VERSION").read_text().strip() == initial_ver

        # bump_version.py patch --dry-run
        res2 = subprocess.run(
            [sys.executable, "scripts/bump_version.py", "patch", "--dry-run"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert res2.returncode == 0
        assert (repo_dir / "VERSION").read_text().strip() == initial_ver

        # 2. Stage unrelated file and verify refusal
        (repo_dir / "unrelated.txt").write_text("unrelated changes")
        subprocess.run(["git", "add", "unrelated.txt"], cwd=repo_dir, check=True)

        res_refuse = subprocess.run(
            [sys.executable, "scripts/bump_version.py", "patch", "--git-commit"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert res_refuse.returncode != 0
        assert "unrelated staged files" in res_refuse.stderr
        assert (repo_dir / "VERSION").read_text().strip() == initial_ver

        # Unstage unrelated file
        subprocess.run(["git", "reset", "HEAD", "unrelated.txt"], cwd=repo_dir, check=True, capture_output=True)

        # 3. Commit-only creates no tag
        res_commit = subprocess.run(
            [sys.executable, "scripts/bump_version.py", "patch", "--git-commit"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert res_commit.returncode == 0
        tags = subprocess.run(["git", "tag"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
        assert tags == "", "Expected no tag when only --git-commit was specified"

        # 4. Tag mode creates commit and tag
        res_tag = subprocess.run(
            [sys.executable, "scripts/bump_version.py", "minor", "--git-tag"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert res_tag.returncode == 0
        tags = subprocess.run(["git", "tag"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
        assert "v" in tags


if __name__ == "__main__":
    tests = [
        test_print_file_argv,
        test_load_preferences_corrupt,
        test_cancel_and_jobs_argv,
        test_expired_half_queue,
        test_perform_cleanup_min_age,
        test_in_flight_ownership_and_periodic_sweep,
        test_merge_lifecycle_and_cleanup,
        test_version_cli_in_disposable_repo,
    ]

    failures = 0
    for test in tests:
        name = test.__name__
        try:
            test()
            print(f"  ok   {name}")
        except Exception as e:
            failures += 1
            print(f"  FAIL {name}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'FAILED' if failures else 'PASSED'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
