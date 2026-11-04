import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from simplescheduler.core import Store, command_for, detect_language, discover
from simplescheduler.scheduler import Scheduler, unit_quote


class LibraryFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="simplescheduler-test-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "data")
        self.files = self.root / "files"
        self.files.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, text):
        file = self.files / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        return file


class LibraryTests(LibraryFixture, unittest.TestCase):
    def test_discovery_finds_new_scripts_without_enabling_them(self):
        self.write("backup.sh", "#!/bin/sh\necho backup\n")
        self.write("plain", "#!/usr/bin/env python3\nprint('hi')\n")
        self.write("readme.txt", "This is not a script")
        result = discover(self.store, [str(self.files)])
        self.assertEqual(result.added, 2)
        self.assertTrue(all(not s.enabled for s in self.store.scripts()))
        self.write("later.py", "print('later')\n")
        self.assertEqual(discover(self.store, [str(self.files)]).added, 1)
        self.assertEqual(discover(self.store, [str(self.files)]).added, 0)

    def test_skips_dependencies_binaries_and_symlink_loops(self):
        self.write("node_modules/dependency.js", "console.log('skip')")
        self.write(".venv/skip.py", "pass")
        self.write("binary.sh", "\x00\x01")
        self.write("good.py", "pass")
        (self.files / "loop").symlink_to(self.files, target_is_directory=True)
        result = discover(self.store, [str(self.files), str(self.files)])
        self.assertEqual(result.added, 1)
        self.assertEqual(len(self.store.scripts()), 1)

    def test_forget_survives_scans_and_import_can_restore(self):
        path = self.write("job.sh", "echo test")
        script = self.store.add(path)
        self.store.forget(script)
        discover(self.store, [str(self.files)])
        self.assertEqual(self.store.scripts(), [])
        self.assertEqual(self.store.add(path).path, str(path))

    def test_missing_file_keeps_its_schedule_and_is_restored(self):
        path = self.write("job.py", "print('ok')")
        script = self.store.add(path)
        self.store.update(replace(script, enabled=True, schedule="daily"))
        path.unlink()
        discover(self.store, [str(self.files)])
        missing = self.store.get(script.id)
        self.assertTrue(missing.missing)
        self.assertEqual(missing.schedule, "daily")
        path.write_text("print('back')")
        discover(self.store, [str(self.files)])
        self.assertFalse(self.store.get(script.id).missing)

    def test_create_never_overwrites_and_quotes_paths_as_arguments(self):
        script = self.store.create("My task", "shell", "echo hello")
        self.assertTrue(os.access(script.path, os.X_OK))
        with self.assertRaises(FileExistsError):
            self.store.create("My task", "shell", "bad")
        self.assertEqual(Path(script.path).read_text(), "echo hello")
        tricky = self.write("a space; $(echo hi).py", "print('safe')")
        self.assertEqual(command_for(tricky, "python")[-1], str(tricky))
        with self.assertRaises(ValueError):
            self.store.create("...", "shell", "")

    def test_invalid_schedule_is_not_persisted(self):
        script = self.store.create("Test", "shell", "echo ok")
        for candidate in (replace(script, time_of_day="25:00"), replace(script, interval=0),
                          replace(script, schedule="unknown")):
            with self.assertRaises(ValueError):
                self.store.update(candidate)
        self.assertEqual(self.store.get(script.id).schedule, "manual")


class SchedulerTests(LibraryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.commands = []

        def command(argv):
            self.commands.append(argv)
            return "yes" if argv[0] == "loginctl" else ""
        self.scheduler = Scheduler(self.store, self.root / "units", run=command)
        self.script = self.store.create("My job", "shell", "echo works")

    def test_login_is_enabled_without_running_immediately(self):
        candidate = replace(self.script, enabled=True, schedule="login")
        self.scheduler.apply(candidate)
        name = self.scheduler.unit_name(candidate)
        text = (self.scheduler.unit_dir / f"{name}.service").read_text()
        self.assertIn("WantedBy=graphical-session.target", text)
        self.assertIn("PartOf=graphical-session.target", text)
        self.assertIn(["systemctl", "--user", "enable", name + ".service"], self.commands)
        self.assertFalse(any("--now" in c for c in self.commands))

    def test_timer_can_change_to_disabled_and_leaves_no_triggers(self):
        candidate = replace(self.script, enabled=True, schedule="daily", time_of_day="08:30")
        self.scheduler.apply(candidate)
        timer = self.scheduler.unit_dir / (self.scheduler.unit_name(candidate) + ".timer")
        self.assertIn("OnCalendar=*-*-* 08:30:00", timer.read_text())
        self.scheduler.apply(replace(candidate, enabled=False))
        self.assertFalse(timer.exists())
        self.assertFalse(self.store.get(candidate.id).enabled)

    def test_startup_requires_lingering(self):
        self.scheduler._run = lambda _: "no"
        with self.assertRaisesRegex(ValueError, "Preferences"):
            self.scheduler.apply(replace(self.script, enabled=True, schedule="startup"))
        self.assertFalse(self.store.get(self.script.id).enabled)

    def test_backend_failure_does_not_commit_schedule(self):
        def fail(argv):
            raise RuntimeError("No user bus")
        self.scheduler._run = fail
        with self.assertRaises(RuntimeError):
            self.scheduler.apply(replace(self.script, enabled=True, schedule="hourly"))
        self.assertEqual(self.store.get(self.script.id).schedule, "manual")

    def test_systemd_escaping_and_validation(self):
        self.assertEqual(unit_quote('a%$"\\b'), '"a%%$$\\"\\\\b"')
        with self.assertRaises(ValueError):
            unit_quote("bad\npath")
        with self.assertRaises(ValueError):
            self.scheduler.unit_name(replace(self.script, id="../escape"))


class RunnerTests(LibraryFixture, unittest.TestCase):
    def launch(self, script, wait=True):
        runner = Path(__file__).resolve().parent.parent / "simplescheduler/runner.py"
        process = subprocess.Popen([sys.executable, str(runner), "--data-dir", str(self.store.directory),
                                    "--id", script.id], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if wait:
            stdout, stderr = process.communicate(timeout=10)
            return process.returncode, stdout, stderr
        return process

    def ready(self, process, script):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.store.runs(script.id):
                return
            if process.poll() is not None:
                self.fail(process.communicate()[1].decode())
            time.sleep(.03)
        self.fail("runner did not start")

    def test_output_success_and_failure_are_recorded(self):
        script = self.store.create("Output", "python", "import sys\nprint('hello')\nprint('error', file=sys.stderr)\n")
        self.store.update(replace(script, enabled=True))
        self.assertEqual(self.launch(script)[0], 0)
        run = self.store.runs(script.id)[0]
        self.assertEqual(run["status"], "success")
        self.assertIn("hello", Path(run["log_path"]).read_text())
        self.assertIn("error", Path(run["log_path"]).read_text())
        Path(script.path).write_text("raise SystemExit(7)\n")
        self.assertEqual(self.launch(script)[0], 7)
        self.assertEqual(self.store.runs(script.id)[0]["exit_code"], 7)

    def test_disabled_scripts_cannot_execute(self):
        marker = self.root / "unexpected"
        script = self.store.create("Disabled", "python", f"open({str(marker)!r}, 'w').close()")
        self.assertNotEqual(self.launch(script)[0], 0)
        self.assertFalse(marker.exists())
        self.assertEqual(self.store.runs(), [])

    def test_no_overlapping_runs_and_cancellation(self):
        script = self.store.create("Slow", "python", "import time\nprint('started', flush=True)\ntime.sleep(30)\n")
        self.store.update(replace(script, enabled=True))
        first = self.launch(script, wait=False)
        try:
            self.ready(first, script)
            self.assertEqual(self.launch(script)[0], 75)
            first.send_signal(signal.SIGTERM)
            first.communicate(timeout=8)
            self.assertEqual(self.store.runs(script.id)[0]["status"], "cancelled")
        finally:
            if first.poll() is None:
                first.terminate()
                first.communicate(timeout=8)

    def test_live_output_and_recovery_after_runner_crash(self):
        script = self.store.create("Recovery", "python", "import time\nprint('live line', flush=True)\ntime.sleep(30)\n")
        self.store.update(replace(script, enabled=True))
        process = self.launch(script, wait=False)
        try:
            self.ready(process, script)
            run = self.store.runs(script.id)[0]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                path = Path(run["log_path"])
                if path.exists() and "live line" in path.read_text():
                    break
                time.sleep(.03)
            else:
                self.fail("Output was buffered until script completion")
            self.store.reconcile_runs()
            self.assertEqual(self.store.runs(script.id)[0]["status"], "running")
            # Gracefully finish the child, then simulate a stale row left by a crash.
            process.terminate()
            process.communicate(timeout=8)
            with self.store.connect() as db:
                db.execute("UPDATE runs SET status='running',finished=NULL WHERE id=?", (run["id"],))
            self.store.reconcile_runs()
            self.assertEqual(self.store.runs(script.id)[0]["status"], "interrupted")
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=8)

    def test_missing_interpreter_is_recorded_as_failure(self):
        script = self.store.create("Invalid", "shell", "#!/does/not/exist\necho fail\n")
        self.store.update(replace(script, enabled=True))
        self.assertNotEqual(self.launch(script)[0], 0)
        run = self.store.runs(script.id)[0]
        self.assertEqual(run["status"], "failed")
        self.assertIn("No such file", Path(run["log_path"]).read_text())

    def test_large_output_is_bounded(self):
        script = self.store.create("Verbose", "python", "print('x' * 3000000)")
        self.store.update(replace(script, enabled=True))
        self.assertEqual(self.launch(script)[0], 0)
        run = self.store.runs(script.id)[0]
        self.assertLess(Path(run["log_path"]).stat().st_size, 2 * 1024 * 1024 + 100)


if __name__ == "__main__":
    unittest.main()
