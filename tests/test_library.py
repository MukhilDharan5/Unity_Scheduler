import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from simplescheduler.core import Store, discover


class LibraryVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="simplescheduler-library-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "data")
        self.folder = self.root / "device"
        self.folder.mkdir()
        self.discovered_path = self.folder / "device-helper.sh"
        self.discovered_path.write_text("#!/bin/sh\necho helper\n")
        discover(self.store, [str(self.folder)])
        self.discovered = self.store.scripts()[0]

    def tearDown(self):
        self.temporary.cleanup()

    def test_discovered_scripts_hidden_by_default_and_setting_reveals_them(self):
        self.assertEqual(self.store.library_scripts(), [])
        self.store.set_setting("show_discovered", True)
        self.assertEqual([s.id for s in self.store.library_scripts()], [self.discovered.id])
        self.store.set_setting("show_discovered", False)
        self.assertEqual(self.store.library_scripts(), [])

    def test_create_and_import_stay_visible_without_changing_schedule(self):
        created = self.store.create("My Script", "shell", "echo created\n")
        imported = self.store.add(self.discovered_path)
        self.assertEqual(imported.id, self.discovered.id)
        self.assertTrue(imported.user_added)
        self.assertEqual({s.id for s in self.store.library_scripts()}, {created.id, imported.id})
        self.assertFalse(imported.enabled)
        self.assertEqual(imported.schedule, "manual")

    def test_missing_files_require_separate_setting(self):
        self.store.add(self.discovered_path)
        self.discovered_path.unlink()
        self.assertEqual(self.store.library_scripts(), [])
        self.store.set_setting("show_discovered", True)
        self.assertEqual(self.store.library_scripts(), [])
        self.store.set_setting("show_missing", True)
        self.assertEqual(len(self.store.library_scripts()), 1)
        self.assertTrue(self.store.library_scripts()[0].missing)

    def test_favorites_and_quick_access_persist_and_adopt_script(self):
        self.store.set_flag(self.discovered.id, "favorite", True)
        self.store.set_flag(self.discovered.id, "quick_access", True)
        reopened = Store(self.store.directory)
        script = reopened.get(self.discovered.id)
        self.assertTrue(script.favorite)
        self.assertTrue(script.quick_access)
        self.assertTrue(script.user_added)
        self.assertEqual(len(reopened.library_scripts()), 1)
        reopened.set_flag(script.id, "favorite", False)
        reopened.set_flag(script.id, "quick_access", False)
        self.assertEqual(len(reopened.library_scripts()), 1)
        self.assertFalse(reopened.get(script.id).enabled)

    def test_schedule_updates_cannot_erase_favorites_or_pins(self):
        old = self.store.get(self.discovered.id)
        self.store.set_flag(old.id, "favorite", True)
        self.store.set_flag(old.id, "quick_access", True)
        self.store.update(replace(old, enabled=True, schedule="daily"))
        fresh = self.store.get(old.id)
        self.assertTrue(fresh.favorite)
        self.assertTrue(fresh.quick_access)
        self.assertEqual(fresh.schedule, "daily")
        with self.assertRaises(ValueError):
            self.store.set_flag(old.id, "enabled", True)

    def test_configuring_a_discovered_script_keeps_it_in_library(self):
        self.store.update(replace(self.discovered, schedule="daily"))
        self.assertEqual(len(self.store.library_scripts()), 1)
        self.assertFalse(self.store.get(self.discovered.id).enabled)

    def test_reopening_does_not_promote_unselected_discovered_scripts(self):
        reopened = Store(self.store.directory)
        self.assertFalse(reopened.get(self.discovered.id).user_added)
        self.assertEqual(reopened.library_scripts(), [])


class MigrationTests(unittest.TestCase):
    def test_old_library_preserves_configured_scripts_and_keeps_device_inventory_hidden(self):
        with tempfile.TemporaryDirectory(prefix="simplescheduler-migration-") as directory:
            root = Path(directory)
            database = root / "library.db"
            with sqlite3.connect(database) as db:
                db.executescript("""CREATE TABLE scripts (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    language TEXT NOT NULL, managed INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 0, schedule TEXT NOT NULL DEFAULT 'manual',
                    time_of_day TEXT NOT NULL DEFAULT '09:00', interval INTEGER NOT NULL DEFAULT 60,
                    missing INTEGER NOT NULL DEFAULT 0, added REAL NOT NULL);
                    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE runs (id TEXT PRIMARY KEY, script_id TEXT NOT NULL, name TEXT NOT NULL,
                    started REAL NOT NULL, finished REAL, status TEXT NOT NULL, exit_code INTEGER,
                    log_path TEXT NOT NULL, trigger TEXT NOT NULL);""")
                for index, name in enumerate(("created", "enabled", "scheduled", "ran", "grouped", "inventory")):
                    path = root / f"{name}.sh"
                    path.write_text("echo test\n")
                    db.execute("INSERT INTO scripts (id,path,name,language,managed,enabled,schedule,added) VALUES (?,?,?,?,?,?,?,?)",
                               (name, str(path), name, "shell", name == "created", name == "enabled",
                                "daily" if name == "scheduled" else "manual", index))
                db.execute("INSERT INTO runs VALUES ('run', 'ran', 'ran', 1, 2, 'success', 0, '', 'manual')")
                db.execute("INSERT INTO settings VALUES ('script_groups', ?)", (json.dumps({"grouped": "user"}),))
            store = Store(root)
            self.assertEqual({s.id for s in store.library_scripts()}, {"created", "enabled", "scheduled", "ran", "grouped"})
            self.assertEqual(len(store.scripts()), 6)
            self.assertEqual(store.get("scheduled").schedule, "daily")
            self.assertEqual(len(store.runs()), 1)
            self.assertFalse(store.get("inventory").user_added)
            self.assertTrue(store.get("enabled").enabled)
            self.assertTrue(all(not s.favorite and not s.quick_access for s in store.scripts()))
            self.assertEqual(len(Store(root).library_scripts()), 5)


if __name__ == "__main__":
    unittest.main()
