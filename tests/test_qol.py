import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from simplescheduler.core import Script, Store, discover
from simplescheduler.groups import ScriptGroup
from simplescheduler.library import matching_scripts
from simplescheduler.scheduler import Scheduler


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.alpha = Script("a", "/scripts/alpha.py", "Alpha Backup", "python", enabled=True, added=1, last_run=20)
        self.beta = Script("b", "/scripts/beta.sh", "Beta cleanup", "shell", added=3, favorite=True, last_status="failed")
        self.gamma = Script("c", "/scripts/gamma.sh", "Gamma", "shell", enabled=True, added=2, last_run=10)
        self.scripts = [self.gamma, self.beta, self.alpha]

    def test_words_match_across_name_language_path_and_group(self):
        group = ScriptGroup("backup", "Daily routines", "", "")
        self.assertEqual(matching_scripts(self.scripts, "BACKUP python daily", groups={"a": group}), [self.alpha])
        self.assertEqual(matching_scripts(self.scripts, "cleanup enabled"), [])
        self.assertEqual(matching_scripts(self.scripts, "cleanup disabled"), [self.beta])

    def test_status_filters_include_live_processes_and_missing_files(self):
        self.assertEqual(matching_scripts(self.scripts, status=1), [self.alpha, self.gamma])
        self.assertEqual(matching_scripts(self.scripts, status=2), [self.beta])
        self.assertEqual(matching_scripts(self.scripts, status=3, running={"c"}), [self.gamma])
        self.assertEqual(matching_scripts(self.scripts, status=4), [self.beta])
        self.assertEqual(matching_scripts([replace(self.gamma, missing=True)], status=4)[0].id, "c")

    def test_sort_orders_are_deterministic(self):
        self.assertEqual([s.id for s in matching_scripts(self.scripts)], ["a", "b", "c"])
        self.assertEqual([s.id for s in matching_scripts(self.scripts, sort=1)], ["b", "c", "a"])
        self.assertEqual([s.id for s in matching_scripts(self.scripts, sort=2)], ["a", "c", "b"])
        self.assertEqual([s.id for s in matching_scripts(self.scripts, sort=3)], ["b", "a", "c"])


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="simplescheduler-qol-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "library")
        self.calls = []
        self.scheduler = Scheduler(self.store, self.root / "units", run=lambda argv: self.calls.append(argv) or "yes")

    def tearDown(self):
        self.temporary.cleanup()

    def test_bulk_import_preserves_identity_and_handles_bad_files_individually(self):
        folder = self.root / "files"
        folder.mkdir()
        script = folder / "good.py"
        script.write_text("print('ok')\n")
        bad = folder / "readme.txt"
        bad.write_text("ordinary text")
        discover(self.store, [str(folder)])
        original = self.store.scripts()[0]
        self.store.set_flag(original.id, "favorite", True)
        imported, errors = self.store.import_scripts([script, script, bad, folder, folder / "missing.sh"])
        self.assertEqual([s.id for s in imported], [original.id])
        self.assertEqual(len(errors), 3)
        self.assertTrue(imported[0].favorite)
        self.assertTrue(imported[0].user_added)
        self.assertFalse(imported[0].enabled)

    def test_undo_removal_restores_identity_flags_history_and_schedule(self):
        original = self.store.create("Undo", "shell", "echo ok\n")
        self.store.set_flag(original.id, "favorite", True)
        self.store.set_flag(original.id, "quick_access", True)
        self.scheduler.apply(replace(self.store.get(original.id), enabled=True, schedule="daily"))
        original = self.store.get(original.id)
        with self.store.connect() as db:
            db.execute("INSERT INTO runs (id,script_id,name,started,finished,status,exit_code,log_path,trigger) VALUES ('run',?,?,1,2,'success',0,'','manual')", (original.id, original.name))
        self.scheduler.remove(original)
        self.assertIsNone(self.store.get(original.id))
        self.store.restore(original)
        self.assertFalse(self.store.get(original.id).enabled)
        self.scheduler.apply(original)
        restored = self.store.get(original.id)
        self.assertTrue(restored.favorite and restored.quick_access and restored.enabled)
        self.assertEqual(restored.schedule, "daily")
        self.assertEqual(len(self.store.runs(original.id)), 1)
        self.assertTrue((self.scheduler.unit_dir / (self.scheduler.unit_name(original) + ".timer")).exists())
        discover(self.store, [str(self.store.scripts_dir)])
        self.assertEqual(len(self.store.scripts()), 1)

    def test_undo_cannot_overwrite_a_reimported_entry(self):
        script = self.store.create("Same path", "shell", "echo ok")
        self.store.forget(script)
        replacement = self.store.add(Path(script.path))
        with self.assertRaises(ValueError):
            self.store.restore(script)
        self.assertEqual(self.store.scripts()[0].id, replacement.id)

    def test_rename_does_not_revert_concurrent_schedule_or_library_changes(self):
        script = self.store.create("Name", "shell", "echo ok")
        self.store.update(replace(script, enabled=True, schedule="weekly", time_of_day="12:30"))
        self.store.set_flag(script.id, "favorite", True)
        self.store.rename(script.id, "New name")
        actual = self.store.get(script.id)
        self.assertEqual((actual.name, actual.schedule, actual.time_of_day), ("New name", "weekly", "12:30"))
        self.assertTrue(actual.enabled and actual.favorite)
        with self.assertRaises(ValueError):
            self.store.rename(script.id, " ")

    def test_copy_can_preserve_interpreter_sensitive_extension(self):
        script = self.store.create("Module copy", "javascript", "export const value = 1;", suffix=".mjs")
        self.assertTrue(script.path.endswith(".mjs"))
        with self.assertRaises(ValueError):
            self.store.create("Invalid", "shell", "echo safe", suffix="/../../bad")


if __name__ == "__main__":
    unittest.main()
