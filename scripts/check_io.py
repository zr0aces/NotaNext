"""Dependency-free I/O and lifecycle checks for NotaNext.

Tests subprocess construction, preference durability, cleanup semantics,
in-flight tracking, and version CLI git handling using temporary directories
and mocks without touching real data, live Telegram, or CUPS servers.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
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
    """In-flight paths survive the periodic sweep regardless of age through real flush and sweep."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pref = os.path.join(tmpdir, "preferences.json")
        Path(pref).write_text("{}")

        # Create input file older than SESSION_TTL
        input_file = os.path.join(tmpdir, "queued_input.pdf")
        Path(input_file).write_text("pdf data")
        past_time = time.time() - 10000
        os.utime(input_file, (past_time, past_time))

        chat_id = 42
        bot.last_print_time.pop(chat_id, None)
        bot.half_queue[chat_id] = HalfQueueEntry(
            files=[input_file],
            ts=time.monotonic(),  # active queue
        )

        in_communicate = asyncio.Event()
        release_merge = asyncio.Event()

        class PausingMergeProcess:
            def __init__(self, cmd):
                self.cmd = cmd
                self.returncode = 0
                merged_out = cmd[2]
                Path(merged_out).write_text("merged content")

            async def communicate(self):
                in_communicate.set()
                await release_merge.wait()
                return b"", b""

            def kill(self):
                pass

            async def wait(self):
                return 0

        class DummyLpProcess:
            def __init__(self, cmd):
                self.returncode = 0

            async def communicate(self):
                return b"OK", b""

            def kill(self):
                pass

            async def wait(self):
                return 0

        async def fake_subproc(*cmd, **kwargs):
            if "merge_pdf.py" in cmd[1]:
                return PausingMergeProcess(list(cmd))
            return DummyLpProcess(list(cmd))

        fake_update = mock.MagicMock()
        fake_update.effective_message.reply_text = mock.AsyncMock()
        fake_context = mock.MagicMock()
        opts = PrintOptions(number_up=2)

        orig_lp = bot.LP_BIN
        orig_server = os.environ.get("CUPS_SERVER")
        orig_printer = os.environ.get("PRINTER_NAME")
        bot.LP_BIN = "lp"
        os.environ["CUPS_SERVER"] = "test-server"
        os.environ["PRINTER_NAME"] = "test-printer"

        async def run_test():
            with mock.patch("bot.DATA_DIR", tmpdir), \
                 mock.patch("bot.PREFERENCES_FILE", pref), \
                 mock.patch("bot.CORRUPT_PREFERENCES_FILE", os.path.join(tmpdir, "preferences.json.corrupt")), \
                 mock.patch("asyncio.create_subprocess_exec", side_effect=fake_subproc):

                flush_task = asyncio.create_task(
                    bot._flush_half_queue(fake_update, fake_context, chat_id, opts)
                )
                try:
                    await asyncio.wait_for(in_communicate.wait(), timeout=5.0)

                    assert chat_id not in bot.half_queue
                    assert input_file in bot.in_flight_paths

                    await bot._sweep_once(time.monotonic())

                    assert os.path.exists(input_file)

                    release_merge.set()
                    await asyncio.wait_for(flush_task, timeout=5.0)

                    assert not os.path.exists(input_file)
                    assert len(bot.in_flight_paths) == 0
                finally:
                    release_merge.set()
                    if not flush_task.done():
                        flush_task.cancel()
                        try:
                            await flush_task
                        except (asyncio.CancelledError, Exception):
                            pass

        try:
            run_async(run_test())
        finally:
            bot.LP_BIN = orig_lp
            if orig_server is not None:
                os.environ["CUPS_SERVER"] = orig_server
            else:
                os.environ.pop("CUPS_SERVER", None)
            if orig_printer is not None:
                os.environ["PRINTER_NAME"] = orig_printer
            else:
                os.environ.pop("PRINTER_NAME", None)
            bot.half_queue.pop(chat_id, None)
            bot.last_print_time.pop(chat_id, None)
            bot.in_flight_paths.clear()


def test_download_tracked_failure():
    """Verify partial file removal and in_flight_paths release on download failure and cancellation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with mock.patch("bot.DATA_DIR", tmpdir):
            class FailingDownloadFile:
                def __init__(self, exc):
                    self.exc = exc
                    self.created_path = None

                async def download_to_drive(self, path):
                    self.created_path = path
                    Path(path).write_text("partial download")
                    raise self.exc

            # 1. Regular exception
            file_err = FailingDownloadFile(IOError("Network disconnected"))
            try:
                run_async(bot._download_tracked(file_err, ".pdf"))
                assert False, "Expected IOError"
            except IOError as e:
                assert "Network disconnected" in str(e)

            assert file_err.created_path is not None
            assert not os.path.exists(file_err.created_path)
            assert file_err.created_path not in bot.in_flight_paths
            assert len(bot.in_flight_paths) == 0

            # 2. CancelledError
            file_cancel = FailingDownloadFile(asyncio.CancelledError())
            try:
                run_async(bot._download_tracked(file_cancel, ".jpg"))
                assert False, "Expected CancelledError"
            except asyncio.CancelledError:
                pass

            assert file_cancel.created_path is not None
            assert not os.path.exists(file_cancel.created_path)
            assert file_cancel.created_path not in bot.in_flight_paths
            assert len(bot.in_flight_paths) == 0


def test_config_failure_before_merge():
    """Missing PRINTER_NAME fails before merge child or lp subprocess is started."""
    orig_lp = bot.LP_BIN
    orig_printer = os.environ.get("PRINTER_NAME")
    orig_server = os.environ.get("CUPS_SERVER")
    bot.LP_BIN = "lp"
    os.environ["CUPS_SERVER"] = "test-server"
    os.environ.pop("PRINTER_NAME", None)

    started_subprocs = []

    async def fake_subproc(*cmd, **kwargs):
        started_subprocs.append(list(cmd))
        raise RuntimeError("Subprocess should not be started!")

    try:
        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_subproc):
            opts = PrintOptions(number_up=2)
            try:
                run_async(print_file(["/tmp/file1.pdf", "/tmp/file2.pdf"], opts))
                assert False, "Expected RuntimeError for missing PRINTER_NAME"
            except RuntimeError as e:
                assert "PRINTER_NAME" in str(e)

        # Assert no subprocess at all was started
        assert len(started_subprocs) == 0, f"Subprocesses were started: {started_subprocs}"
    finally:
        bot.LP_BIN = orig_lp
        if orig_printer is not None:
            os.environ["PRINTER_NAME"] = orig_printer
        else:
            os.environ.pop("PRINTER_NAME", None)
        if orig_server is not None:
            os.environ["CUPS_SERVER"] = orig_server
        else:
            os.environ.pop("CUPS_SERVER", None)


def test_merge_lifecycle_and_cleanup():
    """Verify merged output removal, child reap, registration release, and no lp on failure."""
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
        started_cmds = []
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
                return 0

        async def fake_failing_subprocess(*cmd, **kwargs):
            nonlocal fake_proc
            started_cmds.append(list(cmd))
            fake_proc = PartialOutputProcess(list(cmd), failure_type=current_failure)
            return fake_proc

        with mock.patch("bot.DATA_DIR", tmpdir), \
             mock.patch("bot.PREFERENCES_FILE", pref), \
             mock.patch("bot.CORRUPT_PREFERENCES_FILE", os.path.join(tmpdir, "preferences.json.corrupt")), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=fake_failing_subprocess):

            opts = PrintOptions(number_up=2)

            # 1. Error during merge
            current_failure = "error"
            started_cmds.clear()
            try:
                run_async(print_file(["/tmp/in1.jpg", "/tmp/in2.jpg"], opts))
                assert False, "Expected error"
            except RuntimeError as e:
                assert "Merge syntax error" in str(e)
            assert not os.path.exists(created_merged[-1]), "Partial output was not removed"
            assert len(bot.in_flight_paths) == 0
            assert not any("lp" == c[0] for c in started_cmds), "lp must not be called after merge error"

            # 2. Timeout during merge
            current_failure = "timeout"
            started_cmds.clear()
            try:
                run_async(print_file(["/tmp/in1.jpg", "/tmp/in2.jpg"], opts))
                assert False, "Expected timeout"
            except RuntimeError as e:
                assert "Merging took too long" in str(e)
            assert fake_proc.killed is True
            assert fake_proc.waited is True
            assert not os.path.exists(created_merged[-1]), "Partial output was not removed on timeout"
            assert len(bot.in_flight_paths) == 0
            assert not any("lp" == c[0] for c in started_cmds), "lp must not be called after merge timeout"

            # 3. Cancellation during merge
            current_failure = "cancel"
            started_cmds.clear()

            async def run_cancel():
                task = asyncio.create_task(print_file(["/tmp/in1.jpg", "/tmp/in2.jpg"], opts))
                await asyncio.sleep(0.01)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            run_async(run_cancel())
            assert fake_proc.killed is True
            assert fake_proc.waited is True
            assert not os.path.exists(created_merged[-1]), "Partial output was not removed on cancel"
            assert len(bot.in_flight_paths) == 0
            assert not any("lp" == c[0] for c in started_cmds), "lp must not be called after merge cancellation"

            # 4. Subprocess launch failure after merged_path created
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


def test_preference_worker_and_temp_path_protection():
    """Verify runtime temp-path protection, startup cleanup of stale temp, and worker drain on cancel."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pref = os.path.join(tmpdir, "preferences.json")
        pref_tmp = pref + ".tmp"

        # 1. Startup perform_cleanup removes a stale .tmp file
        Path(pref_tmp).write_text("stale temp from crash")
        with mock.patch("bot.DATA_DIR", tmpdir), \
             mock.patch("bot.PREFERENCES_FILE", pref), \
             mock.patch("bot.CORRUPT_PREFERENCES_FILE", os.path.join(tmpdir, "preferences.json.corrupt")):
            removed = perform_cleanup(min_age=0)
            assert removed == 1
            assert not os.path.exists(pref_tmp)

        # 2. Pre-create an old temp file to exercise reuse race
        Path(pref_tmp).write_text("pre-existing old temp")
        past_time = time.time() - 10000
        os.utime(pref_tmp, (past_time, past_time))

        fsync_started = threading.Event()
        fsync_release = threading.Event()
        orig_fsync = os.fsync

        def pausing_fsync(fd):
            fsync_started.set()
            fsync_release.wait()
            orig_fsync(fd)

        async def run_protection_and_cancellation():
            with mock.patch("bot.DATA_DIR", tmpdir), \
                 mock.patch("bot.PREFERENCES_FILE", pref), \
                 mock.patch("bot.CORRUPT_PREFERENCES_FILE", os.path.join(tmpdir, "preferences.json.corrupt")), \
                 mock.patch("os.fsync", side_effect=pausing_fsync):

                bot.user_preferences.clear()
                bot.user_preferences["12345"] = PrintOptions(color=False, copies=2)

                fake_query = mock.MagicMock()
                fake_query.answer = mock.AsyncMock()
                fake_query.data = "pref_paper_A4"
                fake_query.edit_message_text = mock.AsyncMock()

                fake_update = mock.MagicMock()
                fake_update.callback_query = fake_query
                fake_update.effective_chat.id = 12345

                fake_context = mock.MagicMock()
                fake_context.user_data = {"pref_draft": PrintOptions(color=False, copies=2)}

                handler_task = asyncio.create_task(
                    bot.pref_paper_callback(fake_update, fake_context)
                )

                # Wait until worker reaches fsync
                while not fsync_started.is_set():
                    await asyncio.sleep(0.01)

                # Now run real _sweep_once. The temp file MUST survive because it is in skip paths
                await bot._sweep_once(time.monotonic())
                assert os.path.exists(pref_tmp)

                # Cancel the handler task while worker is still paused
                handler_task.cancel()

                # Yield to let cancellation propagate into the handler.
                # Because handler shields and drains worker, it must NOT finish yet!
                await asyncio.sleep(0.05)
                assert not handler_task.done(), "Handler should not finish until worker is released"

                # Release the worker
                fsync_release.set()

                # Now handler task should finish and raise CancelledError
                try:
                    await asyncio.wait_for(handler_task, timeout=5.0)
                    assert False, "Handler should have raised CancelledError"
                except asyncio.CancelledError:
                    pass

                # Verify preferences were saved and file is intact
                assert os.path.exists(pref)
                with open(pref) as f:
                    saved = json.load(f)
                assert "12345" in saved
                assert saved["12345"]["color"] is False

        try:
            run_async(run_protection_and_cancellation())
        finally:
            fsync_release.set()
            bot.user_preferences.clear()


def test_subprocess_teardown_races():
    """Verify _communicate_or_kill handles ProcessLookupError, cancellation during wait, and distinct caller errors."""
    # 1. ProcessLookupError on kill()
    class ExitedProcess:
        def __init__(self):
            self.waited = False
            self.returncode = 0

        async def communicate(self):
            raise asyncio.TimeoutError()

        def kill(self):
            raise ProcessLookupError("No such process")

        async def wait(self):
            self.waited = True
            return 0

    proc = ExitedProcess()
    try:
        run_async(bot._communicate_or_kill(proc, timeout=0.1))
        assert False, "Expected TimeoutError"
    except asyncio.TimeoutError:
        pass
    assert proc.waited is True

    # 2. Cancellation during wait()
    class SlowWaitProcess:
        def __init__(self):
            self.killed = False
            self.wait_event = asyncio.Event()
            self.returncode = None

        async def communicate(self):
            raise asyncio.TimeoutError()

        def kill(self):
            self.killed = True

        async def wait(self):
            await self.wait_event.wait()
            self.returncode = 0
            return 0

    proc_slow = SlowWaitProcess()

    async def run_slow_wait():
        task = asyncio.create_task(bot._communicate_or_kill(proc_slow, timeout=0.01))
        for _ in range(50):
            if proc_slow.killed:
                break
            await asyncio.sleep(0.005)
        assert proc_slow.killed is True
        task.cancel()
        await asyncio.sleep(0.01)
        proc_slow.wait_event.set()
        try:
            await task
            assert False, "Expected TimeoutError"
        except asyncio.TimeoutError:
            pass

    run_async(run_slow_wait())
    assert proc_slow.killed is True
    assert proc_slow.returncode == 0

    # 3. Verify distinct timeout errors:
    orig_lpstat = bot.LPSTAT_BIN
    orig_server = os.environ.get("CUPS_SERVER")
    bot.LPSTAT_BIN = "lpstat"
    os.environ["CUPS_SERVER"] = "test-server"

    async def fake_timing_out_exec(*cmd, **kwargs):
        class TimingOutProc:
            async def communicate(self):
                raise asyncio.TimeoutError()
            def kill(self):
                pass
            async def wait(self):
                return 0
        return TimingOutProc()

    try:
        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_timing_out_exec):
            stdout, err = run_async(bot.run_cups_query("lpstat", "lpstat", ["-p"], "Printer status check"))
            assert stdout is None
            assert err == "⚠️ Printer status check timed out."
    finally:
        bot.LPSTAT_BIN = orig_lpstat
        if orig_server is not None:
            os.environ["CUPS_SERVER"] = orig_server
        else:
            os.environ.pop("CUPS_SERVER", None)


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
        test_download_tracked_failure,
        test_config_failure_before_merge,
        test_preference_worker_and_temp_path_protection,
        test_subprocess_teardown_races,
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
