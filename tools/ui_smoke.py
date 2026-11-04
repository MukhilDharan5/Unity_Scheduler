#!/usr/bin/env python3
"""Exercise real GTK windows using an isolated temporary library.

Run from a graphical session: GSETTINGS_BACKEND=memory python3 tools/ui_smoke.py
No real scripts are enabled and no systemd units are installed.
"""

import os
import sys
import tempfile
import time
import traceback
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
fixture = Path(tempfile.mkdtemp(prefix="simplescheduler-ui-"))
os.environ["SIMPLESCHEDULER_DATA_DIR"] = str(fixture / "library")
os.environ["GSETTINGS_BACKEND"] = "memory"

from simplescheduler.core import Store, discover

store = Store()
store.set_setting("roots", [str(fixture / "discovery")])
store.set_setting("auto_discover", True)
store.set_setting("appearance", 1)
discovery = fixture / "discovery"
discovery.mkdir()
examples = [
    ("Back up documents", "shell", "daily", True),
    ("A fresh desktop", "shell", "login", True),
    ("Organize downloads", "python", "hourly", True),
    ("Sync project notes", "javascript", "interval", False),
    ("Weekly housekeeping", "shell", "weekly", True),
    ("Good morning", "python", "manual", True),
]
for name, language, schedule, enabled in examples:
    script = store.create(name, language, "#!/usr/bin/env python3\nprint('Hello from the UI check!')\n" if language == "python" else "echo 'UI check'\n")
    store.update(replace(script, schedule=schedule, enabled=enabled))
    if name in ("Back up documents", "Good morning"):
        store.set_flag(script.id, "favorite", True)
    if name in ("Back up documents", "Good morning", "Organize downloads"):
        store.set_flag(script.id, "quick_access", True)
(discovery / "photo-sorter.py").write_text("print('photos')\n")
(discovery / "clean-cache.sh").write_text("#!/bin/sh\necho clean\n")
discover(store)

from simplescheduler.app import Application
from simplescheduler.dialogs import EditorDialog, OutputDialog, PreferencesDialog
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk


class SmokeApplication(Application):
    def __init__(self):
        super().__init__()
        self.set_application_id("io.github.simplescheduler.SimpleScheduler.Smoke")
        self.failed = False
        self.dialog = None
        self.stage = 0

    def do_activate(self):
        super().do_activate()
        self.controller.scheduler.unit_dir = fixture / "units"
        self.controller.scheduler._run = lambda argv: "yes" if argv[0] == "loginctl" else ""
        GLib.timeout_add(1000, self.advance)

    def snapshot(self, name):
        if os.environ.get("SIMPLESCHEDULER_SKIP_SCREENSHOTS") == "1":
            return
        widget = self.window
        # Wait for a drawable frame after a dialog or navigation transition.
        deadline = time.monotonic() + 2
        node = None
        while node is None and time.monotonic() < deadline:
            visible = self.window.get_visible_dialog()
            widget = visible.get_child() if visible and visible.get_child() else self.window
            paintable = Gtk.WidgetPaintable.new(widget)
            widget.queue_draw()
            context = GLib.MainContext.default()
            while context.pending():
                context.iteration(False)
            snapshot = Gtk.Snapshot.new()
            paintable.snapshot(snapshot, widget.get_width(), widget.get_height())
            node = snapshot.to_node()
            if node is None:
                time.sleep(.03)
        if node is None:
            if not widget.get_mapped() or not self.window.get_mapped():
                print(f"Snapshot deferred for {name}: the desktop is not drawing this widget", flush=True)
                return
            raise RuntimeError(f"No drawable GTK frame for {name}: visible={widget.get_visible()}, mapped={widget.get_mapped()}, size={widget.get_width()}x{widget.get_height()}")
        renderer = self.window.get_renderer()
        bounds = Graphene.Rect().init(0, 0, widget.get_width(), widget.get_height())
        texture = renderer.render_texture(node, bounds)
        folder = ROOT / "screenshots"
        folder.mkdir(exist_ok=True)
        texture.save_to_png(str(folder / f"{name}.png"))
        print(f"Captured {name}", flush=True)

    def advance(self):
        try:
            self.check_stage()
        except Exception:
            traceback.print_exc()
            self.failed = True
            self.quit()
            return GLib.SOURCE_REMOVE
        self.stage += 1
        return GLib.SOURCE_CONTINUE

    def descendants(self, widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from self.descendants(child)
            child = child.get_next_sibling()

    def respond(self, text):
        dialog = self.window.get_visible_dialog()
        widget = next(w for w in self.descendants(dialog) if isinstance(w, Gtk.Button) and w.get_label() == text)
        widget.emit("clicked")

    def check_stage(self):
        window = self.window
        if self.stage == 0:
            assert window.get_width() > 0
            self.snapshot("01-library-light")
            row = next(w for w in self.descendants(window.list_box) if isinstance(w, Gtk.ListBoxRow) and hasattr(w, "script_id"))
            listing = row.get_parent()
            assert listing.get_activate_on_single_click()
            listing.emit("row-activated", row)
            assert window.detail_id == row.script_id, "Mouse activation did not open the script"
        elif self.stage == 1:
            self.snapshot("02-script-details")
            window.schedule_combo.set_selected(4)
            window.time_entry.set_text("10:15")
            window._save_schedule(None)
        elif self.stage == 2:
            assert store.get(window.detail_id).time_of_day == "10:15"
            window._return_to_library()
            self.dialog = EditorDialog(window)
            self.dialog.present(window)
        elif self.stage == 3:
            self.snapshot("03-new-script")
            self.dialog.name.set_text("UI created script")
            self.dialog._save(None)
        elif self.stage == 4:
            assert store.get(window.detail_id).name == "UI created script"
            assert store.get(window.detail_id).enabled
            window._detail_run_clicked()
        elif self.stage == 5:
            runs = store.runs(window.detail_id)
            assert runs and runs[0]["status"] == "success", runs
            self.dialog = OutputDialog(window, runs[0])
            self.dialog.present(window)
        elif self.stage == 6:
            self.snapshot("04-run-output")
            self.dialog.force_close()
            self.dialog = PreferencesDialog(window)
            self.dialog.present(window)
        elif self.stage == 7:
            self.snapshot("05-preferences")
            assert not self.dialog.show_discovered.get_active()
            assert not window.navigation_rows["discovered"].get_visible()
            self.dialog.show_discovered.set_active(True)
            assert window.navigation_rows["discovered"].get_visible()
            self.dialog.force_close()
            window._return_to_library()
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        elif self.stage == 8:
            self.snapshot("06-library-dark")
            window.navigation.select_row(window.navigation_rows["discovered"])
            (discovery / "new-arrival.sh").write_text("#!/bin/sh\necho arrived\n")
        elif self.stage == 11:
            assert any(s.path.endswith("new-arrival.sh") for s in store.scripts()), "Live discovery missed a new script"
            window.navigation.select_row(window.navigation_rows["history"])
        elif self.stage == 12:
            self.snapshot("07-history")
            window.navigation.select_row(window.navigation_rows["all"])
            window.set_default_size(620, 800)
        elif self.stage == 13:
            self.snapshot("08-narrow-layout")
            minimum = window.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            assert minimum <= 780, f"The library requires an unexpectedly wide window: {minimum}px"
            if window.get_width() <= 780:
                assert window.split.get_collapsed()
            else:
                print(f"Compositor deferred resize at {window.get_width()}px; checking collapsed navigation directly", flush=True)
                window.split.set_collapsed(True)
            script = next(s for s in store.scripts() if s.name == "UI created script")
            window.show_script(script)
            self.dialog = EditorDialog(window, script)
            self.dialog.present(window)
        elif self.stage == 14:
            self.dialog.name.set_text("Edited & verified")
            self.dialog.buffer.set_text("#!/usr/bin/env bash\nprintf 'Edited successfully\\n'\n")
            self.dialog._save(None)
        elif self.stage == 15:
            script = store.get(window.detail_id)
            assert script.name == "Edited & verified"
            assert os.access(script.path, os.X_OK), "Editing lost the executable permission"
            assert "Edited successfully" in Path(script.path).read_text()
            window.detail_enabled.set_active(False)
        elif self.stage == 16:
            assert not store.get(window.detail_id).enabled
            window.detail_enabled.set_active(True)
        elif self.stage == 17:
            assert store.get(window.detail_id).enabled
            window._return_to_library()
            window.search.set_text("no-script-matches-this")
        elif self.stage == 18:
            assert window.empty.get_visible()
            window.search.set_text("")
            window.navigation.select_row(window.navigation_rows["scheduled"])
        elif self.stage == 19:
            assert window.category == "scheduled"
            from simplescheduler import APP_ID
            assert window.get_icon_name() == APP_ID
            window.select_group("user")
            assert window.category == "user"
            script = next(s for s in store.scripts() if s.name == "Edited & verified")
            window.show_script(script)
            window.detail_group.set_selected(window.detail_group_keys.index("system"))
        elif self.stage == 20:
            script = store.get(window.detail_id)
            assert window.group_assignments[script.id].key == "system"
            window.detail_group.set_selected(0)
        elif self.stage == 21:
            script = store.get(window.detail_id)
            assert window.group_assignments[script.id].key == "user"
            self.dialog = window.new_group(script)
        elif self.stage == 22:
            def descendants(widget):
                yield widget
                child = widget.get_first_child()
                while child:
                    yield from descendants(child)
                    child = child.get_next_sibling()
            widgets = list(descendants(self.dialog.get_child()))
            entry = next(widget for widget in widgets if isinstance(widget, Adw.EntryRow))
            entry.set_text("UI App Group")
            save = next(widget for widget in widgets if isinstance(widget, Gtk.Button) and widget.get_label() == "Create Group")
            save.emit("clicked")
        elif self.stage == 23:
            assert window.category == "app:uiappgroup"
            assert window.group_counts["app:uiappgroup"] == 1
            self.snapshot("09-application-group")
            window.select_group("kernel")
        elif self.stage == 24:
            assert window.category == "kernel"
            assert window.empty.get_visible()
            store.set_setting("show_discovered", False)
            window.controller.emit("changed")
            assert not window.navigation_rows["discovered"].get_visible()
            assert all(s.in_library and not s.missing for s in window.scripts_cache)
            self.script_id = next(s.id for s in store.scripts() if s.name == "Edited & verified")
            window.show_script(store.get(self.script_id))
            window.detail_favorite.emit("clicked")
            window.detail_quick.set_active(True)
        elif self.stage == 25:
            script = store.get(self.script_id)
            assert script.favorite and script.quick_access
            window.select_group("favorites")
            assert window.category == "favorites"
        elif self.stage == 26:
            self.snapshot("10-favorites")
            window.select_group("quick")
            assert window.category == "quick"
        elif self.stage == 27:
            self.snapshot("11-quick-scripts")
            window.controller.run(store.get(self.script_id))
        elif self.stage == 28:
            assert store.runs(self.script_id)[0]["status"] == "success"
            window._toggle_library_flag(self.script_id, "quick_access")
            window._toggle_library_flag(self.script_id, "favorite")
            assert not store.get(self.script_id).quick_access
            assert not store.get(self.script_id).favorite
            Path(store.get(self.script_id).path).unlink()
            window.controller.emit("changed")
            assert self.script_id not in {s.id for s in window.scripts_cache}
            self.dialog = PreferencesDialog(window)
            self.dialog.present(window)
            self.dialog.show_missing.set_active(True)
        elif self.stage == 29:
            assert self.script_id in {s.id for s in window.scripts_cache}
            assert next(s for s in window.scripts_cache if s.id == self.script_id).missing
            window.show_script(store.get(self.script_id))
            assert not window.detail_run.get_sensitive()
            assert not any(s.path.endswith("new-arrival.sh") for s in window.scripts_cache)
            self.dialog.show_discovered.set_active(True)
        elif self.stage == 30:
            assert any(s.path.endswith("new-arrival.sh") for s in window.scripts_cache)
            self.dialog.force_close()
            window._return_to_library()
            self.qol = store.create("QoL example", "shell", "#!/usr/bin/env bash\nprintf 'search me\\n'\n")
            store.update(replace(self.qol, enabled=True))
            window.controller.emit("changed")
            window.show_script(self.qol)
            self.dialog = window.edit_script(self.qol.id)
        elif self.stage == 31:
            control = Gdk.ModifierType.CONTROL_MASK
            assert self.dialog._key_pressed(None, Gdk.KEY_f, 0, control)
            self.dialog.find_entry.set_text("printf")
            self.dialog._find_changed()
            start, end = self.dialog.buffer.get_selection_bounds()
            assert self.dialog.buffer.get_text(start, end, True) == "printf"
            self.dialog.buffer.insert(self.dialog.buffer.get_end_iter(), "\n# Ctrl+S saved this\n")
            assert self.dialog._key_pressed(None, Gdk.KEY_Return, 0, control)
            assert self.qol.id not in window.controller.processes, "An unsaved draft was run"
            assert self.dialog._key_pressed(None, Gdk.KEY_s, 0, control)
            assert not self.dialog.changed
            assert "Ctrl+S saved this" in Path(self.qol.path).read_text()
            assert self.dialog.get_mapped(), "Ctrl+S closed the editor"
            self.snapshot("12-editor-find")
        elif self.stage == 32:
            self.dialog.buffer.insert(self.dialog.buffer.get_end_iter(), "# unsaved draft\n")
            Path(self.qol.path).write_text("printf 'external version\\n'\n")
            assert not self.dialog._save(None, close=False)
            assert Path(self.qol.path).read_text() == "printf 'external version\\n'\n"
            self.respond("Keep Editing")
        elif self.stage == 33:
            assert self.dialog.changed
            assert "unsaved draft" in self.dialog._text()
            self.dialog._request_close()
            self.respond("Discard Changes")
            self.dialog = window.duplicate_script(self.qol.id)
            assert self.dialog.name.get_text() == "QoL example Copy"
            assert self.dialog._text() == Path(self.qol.path).read_text()
            self.dialog._save(None)
            self.copy = store.get(window.detail_id)
            assert self.copy.id != self.qol.id and self.copy.path != self.qol.path
            assert self.copy.schedule == "manual"
        elif self.stage == 34:
            window.schedule_combo.set_selected(4)
            assert window.detail_dirty and window.schedule_bar.get_reveal_child()
            window.select_group("favorites")
            assert window.detail_id == self.copy.id
            self.respond("Keep Editing")
        elif self.stage == 35:
            assert window.detail_dirty
            window._return_to_library()
            self.respond("Discard Changes")
            assert not window.detail_dirty and window.detail_id is None
            assert store.get(self.copy.id).schedule == "manual"
        elif self.stage == 36:
            self.dropped = [discovery / "drop-one.sh", discovery / "drop-two.py"]
            self.dropped[0].write_text("echo one\n")
            self.dropped[1].write_text("print('two')\n")
            files = Gdk.FileList.new_from_list([Gio.File.new_for_path(str(path)) for path in self.dropped])
            assert window._files_dropped(None, files, 0, 0)
        elif self.stage == 37:
            imported = [s for s in store.scripts() if s.path in {str(path) for path in self.dropped}]
            assert len(imported) == 2 and all(s.user_added for s in imported)
            window.status_filter.set_selected(2)
            assert all(not s.enabled for s in window.visible_scripts)
            window.sort_order.set_selected(1)
            assert all(a.added >= b.added for a, b in zip(window.visible_scripts, window.visible_scripts[1:]))
            window.search.set_text("drop python")
            window._search_changed(window.search)
            assert len(window.visible_scripts) == 1
            window.search.emit("activate")
            assert store.get(window.detail_id).path.endswith("drop-two.py")
        elif self.stage == 38:
            window._return_to_library()
            window.search.set_text("")
            window.status_filter.set_selected(0)
            window._remove_confirmed(self.copy)
        elif self.stage == 39:
            assert store.get(self.copy.id) is None
            window._undo_remove(self.copy)
        elif self.stage == 40:
            assert store.get(self.copy.id) is not None
            window.search.set_text("QoL example Copy")
            window._search_changed(window.search)
            row = next(w for w in self.descendants(window.list_box) if isinstance(w, Gtk.ListBoxRow) and getattr(w, "script_id", None) == self.copy.id)
            edit = next(w for w in self.descendants(row) if isinstance(w, Gtk.Button) and w.get_tooltip_text() == "Edit script")
            edit.emit("clicked")
            assert window.editor_dialog.script.id == self.copy.id
            assert window.detail_id is None, "The edit button also activated its row"
        elif self.stage == 41:
            window.editor_dialog.force_close()
            self.lookup_action("script").activate(GLib.Variant("(ss)", ("pin", self.copy.id)))
            assert store.get(self.copy.id).quick_access
            window.controller.run(store.get(self.qol.id))
        elif self.stage == 42:
            run = store.runs(self.qol.id)[0]
            assert run["status"] == "success"
            self.dialog = OutputDialog(window, run)
            self.dialog.present(window)
        elif self.stage == 43:
            target = fixture / "exported-output.log"
            class SaveResult:
                def save_finish(self, result):
                    return Gio.File.new_for_path(str(target))
            self.dialog._exported(SaveResult(), None)
            assert target.read_text() == self.dialog.output_text
            assert "external version" in target.read_text()
            assert not window.toast("Names with & stay readable").get_use_markup()
            self.dialog.force_close()
        elif self.stage == 44:
            window.show_script(store.get(self.copy.id))
            window.schedule_combo.set_selected(4)
            window.select_group("favorites")
            self.respond("Save Changes")
        elif self.stage == 45:
            assert store.get(self.copy.id).schedule == "daily"
            assert window.category == "favorites" and window.detail_id is None
            self.dialog = window.edit_script(self.copy.id)
            self.dialog.name.set_text("Renamed copy")
            self.close_callback = False
            self.dialog._request_close(after_close=lambda: setattr(self, "close_callback", True))
            self.respond("Save Changes")
        elif self.stage == 46:
            assert self.close_callback
            assert store.get(self.copy.id).name == "Renamed copy"
            assert store.get(self.copy.id).schedule == "daily"
            print("GTK smoke checks passed: click activation, direct edit, editor find and Ctrl+S, external file conflicts, schedule guards, duplicate, drag-and-drop bulk import, search filters and sorting, undo removal, context actions, output export, plus the existing script workflows.", flush=True)
            self.quit()


app = SmokeApplication()
exit_code = app.run([sys.argv[0]])
raise SystemExit(1 if app.failed else exit_code)
