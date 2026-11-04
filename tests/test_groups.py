import tempfile
import unittest
from pathlib import Path

from simplescheduler.core import Script, Store
from simplescheduler.groups import GroupCatalog


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="simplescheduler-groups-")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.desktops = self.root / "applications"
        self.desktops.mkdir()
        self.store = Store(self.root / "data")
        (self.desktops / "io.github.ideapadcontrolcenter.IdeapadControlCenter.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Singularity Control Center\n"
            "Exec=ideapad-control-center\nIcon=ideapad-control-center\n")
        self.catalog = self.new_catalog()

    def tearDown(self):
        self.temporary.cleanup()

    def new_catalog(self):
        return GroupCatalog(self.store, home=self.home, desktop_dirs=[self.desktops])

    def script(self, path, script_id="script", **kwargs):
        return Script(script_id, str(path), Path(path).stem, "shell", **kwargs)

    def test_distinguishes_user_system_kernel_and_os(self):
        for path, expected in [(self.home / "Scripts/backup.sh", "user"),
                               ("/usr/local/bin/backup.sh", "system"),
                               ("/etc/cron.daily/cleanup", "system"),
                               ("/usr/bin/distribution-helper", "os"),
                               ("/usr/bin/dracut", "kernel"),
                               ("/usr/bin/akmodsbuild", "kernel"),
                               ("/usr/lib/modules/6.1/build/scripts/sign-file", "kernel")]:
            with self.subTest(path=path):
                self.assertEqual(self.catalog.classify(self.script(path)).key, expected)

    def test_does_not_call_personal_kernel_named_scripts_system_kernel(self):
        self.assertEqual(self.catalog.classify(self.script(self.home / "Scripts/kernel-backup.sh")).key, "user")
        self.assertTrue(self.catalog.classify(self.script(self.home / "Repos/kernel-experiments/test.sh")).application)

    def test_ideapad_project_and_helpers_share_installed_app_name_and_icon(self):
        scripts = [self.script(self.home / "Repos/Singularity-Control-Center/tools/check.sh", "one"),
                   self.script(self.home / "Repos/Singularity-Control-Center/ideapad_control_center/app.py", "two"),
                   self.script("/usr/bin/ideapad-control-center-helper", "three")]
        assignments, counts, apps = self.catalog.organize(scripts)
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].title, "Singularity Control Center")
        self.assertEqual(apps[0].icon, "ideapad-control-center")
        self.assertEqual(counts[apps[0].key], 3)
        self.assertEqual(len({group.key for group in assignments.values()}), 1)

    def test_project_metadata_supplies_name(self):
        project = self.home / "Projects/internal-name"
        (project / "data").mkdir(parents=True)
        (project / "data/example.desktop").write_text("[Desktop Entry]\nName=Friendly App\nIcon=friendly\n")
        group = self.catalog.classify(self.script(project / "tools/task.sh"))
        self.assertEqual(group.title, "Friendly App")
        self.assertEqual(group.icon, "friendly")

    def test_custom_folder_uses_boundaries_and_survives_restart(self):
        created = self.catalog.create_group("My Tool", "/opt/my-tool")
        catalog = self.new_catalog()
        self.assertEqual(catalog.classify(self.script("/opt/my-tool/tasks/run.sh")).key, created.key)
        self.assertNotEqual(catalog.classify(self.script("/opt/my-tool-extra/run.sh")).key, created.key)

    def test_explicit_group_overrides_and_automatic_reset_persist(self):
        script = self.script(self.home / "Scripts/backup.sh")
        self.catalog.assign(script, "system")
        catalog = self.new_catalog()
        self.assertEqual(catalog.classify(script).key, "system")
        catalog.assign(script, None)
        self.assertEqual(self.new_catalog().classify(script).key, "user")

    def test_inferred_project_override_resolves_before_assignment(self):
        project_script = self.script(self.home / "Repos/new-tool/app.py", "project")
        personal = self.script(self.home / "backup.sh", "personal")
        group = self.catalog.automatic(project_script)
        self.catalog.assign(personal, group.key)
        assignments, _, _ = self.new_catalog().organize([personal, project_script])
        self.assertEqual(assignments[personal.id].key, assignments[project_script.id].key)
        self.assertEqual(self.new_catalog().classify(personal).key, group.key)

    def test_config_owner_takes_precedence_over_a_nested_app_named_folder(self):
        (self.desktops / "extensions.desktop").write_text("[Desktop Entry]\nName=Extensions\nExec=gnome-extensions-app\n")
        group = self.new_catalog().classify(self.script(self.home / ".config/Codex/extensions/task.sh"))
        self.assertEqual(group.title, "Codex")

    def test_managed_scripts_start_personal_and_can_be_reassigned(self):
        script = self.script(self.store.scripts_dir / "test.sh", managed=True)
        self.assertEqual(self.catalog.classify(script).key, "user")
        group = self.catalog.create_group("My Automations")
        self.catalog.assign(script, group.key)
        self.assertEqual(self.new_catalog().classify(script).key, group.key)


if __name__ == "__main__":
    unittest.main()
