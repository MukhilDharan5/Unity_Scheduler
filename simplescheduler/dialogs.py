from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import replace
from pathlib import Path

import gi
gi.require_version("GtkSource", "5")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, GtkSource

from .core import LANGUAGES, TEMPLATES, short_path
from .widgets import button, clear, error_dialog, label, scrolled, status_pill


class EditorDialog(Adw.Dialog):
    def __init__(self, window, script=None, duplicate=None):
        super().__init__(title="Edit Script" if script else "Duplicate Script" if duplicate else "New Script", content_width=720, content_height=690)
        self.window = window
        self.script = script
        self.previous_template = None
        self.changed = False
        self.loading = True
        self.etag = None
        self.after_close = None
        self.duplicate_suffix = Path(duplicate.path).suffix if duplicate else None
        source = script or duplicate
        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)
        header = Adw.HeaderBar()
        header.pack_start(button(text="Cancel", clicked=lambda _: self._request_close()))
        self.save_button = button(text="Save" if script else "Create Script", css="suggested-action",
                                  clicked=self._save)
        header.pack_end(self.save_button)
        toolbar.add_top_bar(header)
        self.find_bar = Gtk.Revealer()
        find = Gtk.Box(spacing=8, margin_start=20, margin_end=20, margin_top=8, margin_bottom=8)
        self.find_entry = Gtk.SearchEntry(placeholder_text="Find in script…", hexpand=True)
        find.append(self.find_entry)
        self.find_status = label("", "dim-label caption")
        find.append(self.find_status)
        find.append(button(icon="go-up-symbolic", tooltip="Previous match (Shift+Enter)", clicked=lambda _: self._find(False)))
        find.append(button(icon="go-down-symbolic", tooltip="Next match (Enter)", clicked=lambda _: self._find(True)))
        find.append(button(icon="window-close-symbolic", tooltip="Close Find", clicked=lambda _: self._hide_find()))
        self.find_bar.set_child(find)
        toolbar.add_top_bar(self.find_bar)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                          margin_top=12, margin_bottom=24, margin_start=24, margin_end=24)
        toolbar.set_content(scrolled(content))
        group = Adw.PreferencesGroup()
        self.name = Adw.EntryRow(title="Name")
        copy_name = (duplicate.name[:105] + " Copy") if duplicate else ""
        if duplicate:
            names = {s.name.casefold() for s in window.controller.store.scripts()}
            index = 2
            while copy_name.casefold() in names:
                copy_name = duplicate.name[:100] + f" Copy {index}"
                index += 1
        self.name.set_text(script.name if script else copy_name)
        self.name.connect("changed", self._mark_changed)
        group.add(self.name)
        self.language_keys = list(TEMPLATES)
        if source and source.language not in self.language_keys:
            self.language_keys.append(source.language)
        self.language = Adw.ComboRow(title="Language", model=Gtk.StringList.new(
            [LANGUAGES[k][0] for k in self.language_keys]))
        self.language.set_sensitive(source is None)
        selected_language = source.language if source else "shell"
        self.language.set_selected(self.language_keys.index(selected_language))
        self.language.connect("notify::selected", self._language_changed)
        group.add(self.language)
        content.append(group)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        frame.add_css_class("editor-frame")
        editor_header = Gtk.Box(spacing=8)
        editor_header.add_css_class("editor-toolbar")
        editor_header.append(Gtk.Image.new_from_icon_name("text-x-generic-symbolic"))
        self.file_label = label(short_path(script.path) if script else "Your new script", xalign=0, hexpand=True)
        self.file_label.set_ellipsize(3)
        editor_header.append(self.file_label)
        editor_header.append(label("UTF-8"))
        frame.append(editor_header)
        self.buffer = GtkSource.Buffer()
        self.find_settings = GtkSource.SearchSettings(wrap_around=True, case_sensitive=False)
        self.find_context = GtkSource.SearchContext.new(self.buffer, self.find_settings)
        self.find_entry.connect("search-changed", self._find_changed)
        self.find_entry.connect("activate", lambda _: self._find(True))
        self.find_entry.connect("stop-search", lambda _: self._hide_find())
        self.view = GtkSource.View(buffer=self.buffer, show_line_numbers=True,
                                   auto_indent=True, indent_on_tab=True, tab_width=4,
                                   insert_spaces_instead_of_tabs=True, monospace=True,
                                   top_margin=12, bottom_margin=12, left_margin=6,
                                   right_margin=12, highlight_current_line=True)
        self.view.set_hexpand(True)
        self.view.set_vexpand(True)
        code_scroll = scrolled(self.view, min_content_height=300, vexpand=True)
        code_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        frame.append(code_scroll)
        content.append(frame)
        self.hint = label("Saved to your script library. New scripts start with manual execution.",
                          "dim-label caption", xalign=0, wrap=True)
        content.append(self.hint)
        status = Gtk.Box(spacing=12)
        self.saved_status = label("", "dim-label caption", xalign=0, hexpand=True)
        self.cursor_status = label("Ln 1, Col 1", "dim-label caption")
        status.append(self.saved_status)
        status.append(self.cursor_status)
        content.append(status)
        if source:
            path = Path(source.path)
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("This file is larger than 1 MiB. Open it in your preferred text editor.")
            _, contents, self.etag = Gio.File.new_for_path(str(path.resolve())).load_contents(None)
            self.buffer.set_text(contents.decode("utf-8"))
            self.hint.set_label("A copy will be created in your script library, with a manual schedule." if duplicate else short_path(script.path))
            writable = bool(duplicate or os.access(path, os.W_OK))
            self.save_button.set_sensitive(writable)
            self.view.set_editable(writable)
            self.name.set_editable(writable)
            if not writable:
                self.hint.set_label("This script is read-only. You can view it here.")
        else:
            self._set_template()
        self.buffer.connect("changed", self._mark_changed)
        self.buffer.connect("mark-set", self._position_changed)
        self._style_source()
        self.style_handler = Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._style_source())
        self.run_handler = window.controller.connect("run-finished", self._run_finished)
        self.connect("closed", self._closed)
        self.connect("close-attempt", lambda *_: self._request_close())
        self.loading = False
        self.saved_text = self._text()
        self.saved_name = self.name.get_text()
        self.set_can_close(False)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)
        self.connect("map", lambda *_: self.view.grab_focus() if script else self.name.grab_focus())

    def _style_source(self):
        dark = Adw.StyleManager.get_default().get_dark()
        manager = GtkSource.StyleSchemeManager.get_default()
        self.buffer.set_style_scheme(manager.get_scheme("Adwaita-dark" if dark else "Adwaita"))
        language = self.script.language if self.script else self.language_keys[self.language.get_selected()]
        language_id = {"shell": "sh", "javascript": "js"}.get(language, language)
        self.buffer.set_language(GtkSource.LanguageManager.get_default().get_language(language_id))

    def _text(self):
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

    def _mark_changed(self, *_):
        if not self.loading:
            self.changed = self._text() != self.saved_text or self.name.get_text() != self.saved_name
            self.saved_status.set_label("Unsaved changes" if self.changed else "Saved" if self.script else "New script")

    def _position_changed(self, *_):
        position = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        self.cursor_status.set_label(f"Ln {position.get_line() + 1}, Col {position.get_line_offset() + 1}")

    def _key_pressed(self, controller, keyval, keycode, state):
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)
        key = Gdk.keyval_to_lower(keyval)
        if control and key == Gdk.KEY_s:
            self._save(None, close=False)
            return True
        if control and key == Gdk.KEY_f:
            self.find_bar.set_reveal_child(True)
            self.find_settings.set_search_text(self.find_entry.get_text() or None)
            self.find_entry.grab_focus()
            return True
        if control and key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self.changed or not self.script:
                self.saved_status.set_label("Save your changes before running")
            else:
                script = self.window.controller.store.get(self.script.id)
                if script and script.enabled:
                    if script.id in self.window.controller.processes or script.last_status == "running":
                        self.window.controller.cancel(script)
                    else:
                        self.window.controller.run(script)
                        self.saved_status.set_label("Running…")
                else:
                    self.saved_status.set_label("Enable the script in its details before running")
            return True
        if control and key in (Gdk.KEY_w, Gdk.KEY_q):
            self._request_close(after_close=self.window.close if key == Gdk.KEY_q else None)
            return True
        if key == Gdk.KEY_Escape:
            self._hide_find() if self.find_bar.get_reveal_child() else self._request_close()
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.find_entry.get_focus_child() is not None:
            self._find(not bool(state & Gdk.ModifierType.SHIFT_MASK))
            return True
        return False

    def _hide_find(self):
        self.find_bar.set_reveal_child(False)
        self.find_settings.set_search_text(None)
        self.view.grab_focus()

    def _find_changed(self, *_):
        if not self.find_bar.get_reveal_child():
            return
        self.find_settings.set_search_text(self.find_entry.get_text() or None)
        self._find(True, restart=True)

    def _find(self, forward=True, restart=False):
        if not self.find_entry.get_text():
            self.find_status.set_label("")
            return False
        if restart:
            position = self.buffer.get_start_iter()
        elif self.buffer.get_has_selection():
            start, end = self.buffer.get_selection_bounds()
            position = end if forward else start
        else:
            position = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        found, start, end, wrapped = (self.find_context.forward(position) if forward else self.find_context.backward(position))
        self.find_status.set_label("Wrapped" if found and wrapped else "" if found else "No matches")
        if found:
            self.buffer.select_range(start, end)
            self.view.scroll_to_iter(start, .15, False, 0, 0)
        return found

    def _closed(self, *_):
        Adw.StyleManager.get_default().disconnect(self.style_handler)
        self.window.controller.disconnect(self.run_handler)
        if self.after_close:
            GLib.idle_add(self.after_close)
            self.after_close = None

    def _run_finished(self, _, script_id):
        if self.script and self.script.id == script_id and not self.changed:
            runs = self.window.controller.store.runs(script_id, limit=1)
            if runs:
                result = {"success": "Completed", "failed": "Failed", "cancelled": "Stopped"}.get(runs[0]["status"], runs[0]["status"].capitalize())
                self.saved_status.set_label("Saved · " + result)

    def _set_template(self):
        self.previous_template = TEMPLATES[self.language_keys[self.language.get_selected()]]
        self.buffer.set_text(self.previous_template)

    def _language_changed(self, *_):
        if not hasattr(self, "buffer"):
            return
        if self._text() == self.previous_template:
            self._set_template()
        self._style_source()
        self._mark_changed()

    def _request_close(self, after_close=None):
        if not self.changed:
            self.after_close = after_close
            self.force_close()
            return
        alert = Adw.AlertDialog(heading="Save your script changes?", body="Keep editing, save your changes, or discard them.")
        alert.add_response("cancel", "Keep Editing")
        alert.add_response("discard", "Discard Changes")
        alert.add_response("save", "Save Changes")
        alert.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        alert.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("save")
        alert.set_close_response("cancel")
        def response(_, choice):
            if choice == "discard":
                self.after_close = after_close
                self.force_close()
            elif choice == "save":
                self.after_close = after_close
                if not self._save(None):
                    self.after_close = None
        alert.connect("response", response)
        alert.present(self)

    def _save(self, _, close=True, overwrite=False):
        if not self.save_button.get_sensitive():
            return False
        try:
            name = self.name.get_text().strip()
            if not name or len(name) > 120:
                raise ValueError("Give your script a name between 1 and 120 characters.")
            was_new = self.script is None
            if self.script:
                current = self.window.controller.store.get(self.script.id)
                if not current:
                    raise ValueError("This script was removed from the library. Duplicate it to save a new copy.")
                path = Path(self.script.path).resolve()
                # Gio atomically replaces contents and preserves the existing file mode.
                file = Gio.File.new_for_path(str(path))
                _, self.etag = file.replace_contents(self._text().encode("utf-8"), None if overwrite else self.etag, False,
                                      Gio.FileCreateFlags.NONE, None)
                self.window.controller.store.rename(self.script.id, name)
                script = self.window.controller.store.get(self.script.id)
            else:
                script = self.window.controller.store.create(name,
                    self.language_keys[self.language.get_selected()], self._text(), suffix=self.duplicate_suffix)
                self.window.controller.store.update(replace(script, enabled=True))
                script = self.window.controller.store.get(script.id)
                _, _, self.etag = Gio.File.new_for_path(script.path).load_contents(None)
            self.script = script
            self.window.active_script_id = script.id
            self.loading = True
            self.name.set_text(name)
            self.loading = False
            self.saved_text, self.saved_name = self._text(), name
            self.changed = False
            self.saved_status.set_label("Saved")
            self.save_button.set_label("Save")
            self.set_title("Edit Script")
            self.language.set_sensitive(False)
            self.file_label.set_label(short_path(script.path))
            self.window.controller.emit("changed")
            self.window.toast("Your script is ready" if was_new else "Script saved")
            if close:
                self.force_close()
                if was_new:
                    self.window.show_script(script)
            return True
        except FileExistsError:
            error_dialog(self, "A script with that filename already exists. Choose another name.")
        except GLib.Error as error:
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.WRONG_ETAG):
                self._external_change(close)
            else:
                error_dialog(self, error.message)
        except Exception as error:
            error_dialog(self, error)
        return False

    def _external_change(self, close):
        alert = Adw.AlertDialog(heading="This script changed on disk", body="Another app changed this file while you were editing. Keep your draft, reload the file, or replace the file with your changes.")
        alert.add_response("cancel", "Keep Editing")
        alert.add_response("reload", "Reload File")
        alert.add_response("replace", "Replace File")
        alert.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_close_response("cancel")
        def response(_, choice):
            if choice == "replace":
                self._save(None, close=close, overwrite=True)
            elif choice == "reload":
                try:
                    _, text, self.etag = Gio.File.new_for_path(str(Path(self.script.path).resolve())).load_contents(None)
                    self.loading = True
                    self.buffer.set_text(text.decode("utf-8"))
                    self.saved_text = self._text()
                    self.loading = False
                    self._mark_changed()
                except Exception as error:
                    self.loading = False
                    error_dialog(self, error)
        alert.connect("response", response)
        alert.present(self)


class OutputDialog(Adw.Dialog):
    def __init__(self, window, run):
        super().__init__(title="Run Output", content_width=720, content_height=560)
        self.store = window.controller.store
        self.window = window
        self.run = run
        self.last_text = None
        self.output_text = ""
        toolbar = Adw.ToolbarView()
        self.output_toasts = Adw.ToastOverlay()
        self.output_toasts.set_child(toolbar)
        self.set_child(self.output_toasts)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=run["name"], subtitle="Run output"))
        header.pack_start(button(icon="edit-copy-symbolic", tooltip="Copy output", clicked=self._copy))
        header.pack_start(button(icon="document-save-symbolic", tooltip="Save output to a file", clicked=self._export))
        toolbar.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        metadata = Gtk.Box(spacing=12, margin_start=18, margin_end=18, margin_top=12, margin_bottom=12)
        self.status = status_pill(run["status"])
        metadata.append(self.status)
        metadata.append(label(time.strftime("%b %d, %H:%M:%S", time.localtime(run["started"])),
                              "dim-label caption", hexpand=True, xalign=0))
        self.exit_label = label("", "dim-label caption")
        metadata.append(self.exit_label)
        box.append(metadata)
        self.buffer = Gtk.TextBuffer()
        self.view = Gtk.TextView(buffer=self.buffer, editable=False, cursor_visible=False,
                                 monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 vexpand=True)
        self.view.add_css_class("output-view")
        self.output_scroll = scrolled(self.view, vexpand=True)
        box.append(self.output_scroll)
        footer = Gtk.Box(spacing=12, margin_start=18, margin_end=18, margin_top=10, margin_bottom=10)
        self.follow = Gtk.CheckButton(label="Follow output", active=True)
        footer.append(self.follow)
        footer.append(label("Scroll up to read earlier output", "dim-label caption", xalign=1, hexpand=True))
        box.append(footer)
        toolbar.set_content(box)
        self._refresh()
        self.timer = GLib.timeout_add(600, self._refresh)
        self.connect("closed", lambda *_: GLib.source_remove(self.timer))

    def _copy(self, _):
        self.get_clipboard().set(self.output_text)
        self.output_toasts.add_toast(Adw.Toast(title="Output copied", timeout=3))

    def _export(self, _):
        filename = re.sub(r"[^a-zA-Z0-9_-]+", "-", self.run["name"]).strip("-") or "script-output"
        dialog = Gtk.FileDialog(title="Save Run Output", initial_name=filename + ".log")
        dialog.save(self.window, None, self._exported)

    def _exported(self, dialog, result):
        try:
            file = dialog.save_finish(result)
            file.replace_contents(self.output_text.encode("utf-8"), None, False, Gio.FileCreateFlags.NONE, None)
            self.output_toasts.add_toast(Adw.Toast(title="Output saved", timeout=3))
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                error_dialog(self, error.message)

    def _refresh(self):
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM runs WHERE id=?", (self.run["id"],)).fetchone()
            if row:
                self.run = dict(row)
            path = Path(self.run["log_path"])
            text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            self.output_text = text
            if not text:
                text = "Waiting for output…" if self.run["status"] == "running" else "This script produced no output."
            if text != self.last_text:
                adjustment = self.output_scroll.get_vadjustment()
                follow = self.follow.get_active() and adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - 32
                if self.last_text and text.startswith(self.last_text):
                    self.buffer.insert(self.buffer.get_end_iter(), text[len(self.last_text):])
                else:
                    self.buffer.set_text(text)
                self.last_text = text
                if follow:
                    GLib.timeout_add(30, lambda: (adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size())), GLib.SOURCE_REMOVE)[1])
            names = {"success": "Completed", "failed": "Failed", "running": "Running",
                     "cancelled": "Stopped", "interrupted": "Interrupted"}
            self.status.set_label(names.get(self.run["status"], self.run["status"]))
            for name in ("success", "failed", "running"):
                self.status.remove_css_class("status-" + name)
            self.status.add_css_class("status-" + self.run["status"])
            code = self.run["exit_code"]
            elapsed = (self.run["finished"] or time.time()) - self.run["started"]
            self.exit_label.set_label(f"Exit {code} · {self.run['trigger'].capitalize()} · {elapsed:.1f}s" if code is not None else f"Live output · {elapsed:.0f}s")
        except OSError as error:
            self.buffer.set_text(str(error))
        return GLib.SOURCE_CONTINUE


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, window):
        super().__init__(title="Preferences", content_width=660, content_height=680)
        self.window = window
        self.controller = window.controller
        self.store = self.controller.store
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        self.add(page)
        general = Adw.PreferencesGroup(title="Make yourself at home")
        self.auto = Adw.SwitchRow(title="Automatic discovery",
            subtitle="Watch your folders for new scripts while SimpleScheduler is open.", active=self.controller.auto)
        self.auto.connect("notify::active", lambda row, _: self.controller.set_auto(row.get_active()))
        general.add(self.auto)
        appearance = Adw.ComboRow(title="Appearance", model=Gtk.StringList.new(["Follow System", "Light", "Dark"]))
        appearance.set_selected(self.store.get_setting("appearance", 0))
        appearance.connect("notify::selected", self._appearance)
        general.add(appearance)
        page.add(general)
        visibility = Adw.PreferencesGroup(title="Library visibility",
            description="Your created, imported, and configured scripts stay in your library. Other device scripts are hidden by default.")
        self.show_discovered = Adw.SwitchRow(title="Show discovered scripts",
            subtitle="Include scripts found on this device that you haven’t added to your library.",
            active=self.store.get_setting("show_discovered", False))
        self.show_discovered.connect("notify::active", self._visibility_changed, "show_discovered")
        visibility.add(self.show_discovered)
        self.show_missing = Adw.SwitchRow(title="Show missing scripts",
            subtitle="Include library entries whose script files no longer exist.",
            active=self.store.get_setting("show_missing", False))
        self.show_missing.connect("notify::active", self._visibility_changed, "show_missing")
        visibility.add(self.show_missing)
        page.add(visibility)
        roots = Adw.PreferencesGroup(title="Discovery locations",
            description="Scan these folders for scripts. To browse the full inventory, enable Show discovered scripts above. Dependency folders and caches are skipped.")
        roots.set_header_suffix(button(icon="list-add-symbolic", tooltip="Add a folder", clicked=self._add_root))
        self.root_group = roots
        self.root_rows = []
        self._render_roots()
        page.add(roots)
        background = Adw.PreferencesGroup(title="Before you log in",
            description="System startup schedules run as your user. Background startup lets your user services start at boot and continue after logout.")
        self.background_row = Adw.ActionRow(title="Background startup", subtitle="Checking availability…")
        self.background_button = button(text="Enable…", clicked=self._enable_background)
        self.background_button.set_valign(Gtk.Align.CENTER)
        self.background_button.set_sensitive(False)
        self.background_row.add_suffix(self.background_button)
        background.add(self.background_row)
        page.add(background)
        self.controller.background(self.controller.scheduler.lingering, self._background_checked)
        storage = Adw.PreferencesGroup(title="Your library", description="Scripts, settings, and run output are stored locally on this device.")
        location = Adw.ActionRow(title="Library location", subtitle=short_path(str(self.store.directory)), use_markup=False)
        location.add_suffix(button(icon="folder-open-symbolic", tooltip="Open library folder", clicked=lambda _: self._open_library()))
        storage.add(location)
        page.add(storage)

    def _appearance(self, row, _):
        value = row.get_selected()
        self.store.set_setting("appearance", value)
        Adw.StyleManager.get_default().set_color_scheme(
            [Adw.ColorScheme.DEFAULT, Adw.ColorScheme.FORCE_LIGHT, Adw.ColorScheme.FORCE_DARK][value])

    def _visibility_changed(self, row, _, key):
        self.store.set_setting(key, row.get_active())
        self.controller.emit("changed")

    def _render_roots(self):
        for row in self.root_rows:
            self.root_group.remove(row)
        self.root_rows = []
        for root in self.store.roots():
            row = Adw.ActionRow(title=short_path(root), use_markup=False)
            row.set_subtitle("Folder not found" if not Path(root).exists() else "Including subfolders")
            row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
            remove = button(icon="list-remove-symbolic", css="flat", tooltip="Stop watching this location")
            remove.set_valign(Gtk.Align.CENTER)
            remove.connect("clicked", lambda _, p=root: self._remove_root(p))
            row.add_suffix(remove)
            self.root_group.add(row)
            self.root_rows.append(row)

    def _remove_root(self, path):
        self.store.set_setting("roots", [p for p in self.store.roots() if p != path])
        self._render_roots()
        self.controller.scan()

    def _add_root(self, _):
        dialog = Gtk.FileDialog(title="Choose a Discovery Folder", modal=True)
        dialog.select_folder(self.window, None, self._folder_chosen)

    def _folder_chosen(self, dialog, result):
        try:
            file = dialog.select_folder_finish(result)
            path = file.get_path()
            if not path:
                raise ValueError("Choose a folder on this device.")
            roots = self.store.roots()
            if path not in roots:
                self.store.set_setting("roots", roots + [path])
            self._render_roots()
            self.controller.scan()
        except GLib.Error:
            pass
        except ValueError as error:
            error_dialog(self, error)

    def _background_checked(self, enabled):
        self.background_row.set_subtitle("Enabled · your services can start at boot" if enabled else "Needed for schedules that run at system startup")
        self.background_button.set_label("Enabled" if enabled else "Enable…")
        self.background_button.set_sensitive(not enabled)

    def _enable_background(self, _):
        self.background_button.set_sensitive(False)

        def work():
            self.controller.scheduler.enable_background()
            return self.controller.scheduler.lingering()
        self.controller.background(work, self._background_checked)

    def _open_library(self):
        Gio.AppInfo.launch_default_for_uri(self.store.directory.as_uri(), None)
