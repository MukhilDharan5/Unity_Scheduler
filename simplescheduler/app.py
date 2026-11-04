from __future__ import annotations

import sys
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Adw, Gio, Gdk, GLib, Gtk

from . import APP_ID, __version__
from .controller import Controller
from .core import Store
from .window import MainWindow


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        self.controller = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        actions = {
            "new": lambda: self.window.new_script(),
            "new-group": lambda: self.window.new_group(),
            "import": lambda: self.window.import_script(),
            "preferences": lambda: self.window.show_preferences(),
            "refresh": lambda: self.controller.scan(),
            "search": self._focus_search,
            "about": self._about,
            "shortcuts": self._shortcuts,
            "remove-script": lambda: self.window.remove_script(),
            "reveal-script": lambda: self.window.reveal_script(),
            "edit": lambda: self.window.edit_script(),
            "duplicate": lambda: self.window.duplicate_script(),
            "copy-path": lambda: self.window.copy_path(),
            "last-output": lambda: self.window.show_latest_output(),
            "run": lambda: self.window.run_current(),
            "back": lambda: self.window._return_to_library(),
            "quit": lambda: self.window.close() if self.window else self.quit(),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _, parameter, callback=callback: callback())
            self.add_action(action)
        script_action = Gio.SimpleAction.new("script", GLib.VariantType.new("(ss)"))
        script_action.connect("activate", lambda _, parameter: self.window.script_action(*parameter.unpack()))
        self.add_action(script_action)
        for name, shortcut in {"new": "<Control>n", "import": "<Control>o", "preferences": "<Control>comma",
                               "refresh": "<Control>r", "search": "<Control>f", "quit": "<Control>q",
                               "shortcuts": "<Control>question", "edit": "<Control>e", "duplicate": "<Control>d",
                               "run": "<Control>Return", "back": "<Alt>Left"}.items():
            self.set_accels_for_action("app." + name, [shortcut])

    def do_activate(self):
        if not self.window:
            store = Store()
            self.controller = Controller(store)
            appearance = min(max(store.get_setting("appearance", 0), 0), 2)
            self.get_style_manager().set_color_scheme(
                [Adw.ColorScheme.DEFAULT, Adw.ColorScheme.FORCE_LIGHT, Adw.ColorScheme.FORCE_DARK][appearance])
            display = Gdk.Display.get_default()
            root = Path(__file__).resolve().parent
            provider = Gtk.CssProvider()
            provider.load_from_path(str(root / "style.css"))
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            icons = root.parent / "data/icons"
            if icons.is_dir():
                Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))
            self.window = MainWindow(self, self.controller)
            self.window.set_default_size(store.get_setting("window_width", 1160), store.get_setting("window_height", 800))
        self.window.present()

    def do_shutdown(self):
        if self.window and self.window.theme_handler:
            Adw.StyleManager.get_default().disconnect(self.window.theme_handler)
            self.window.theme_handler = None
        if self.controller:
            self.controller.close()
        Adw.Application.do_shutdown(self)

    def _focus_search(self):
        self.window._return_to_library(then=self.window.search.grab_focus)

    def _about(self):
        dialog = Adw.AboutDialog(application_name="SimpleScheduler", application_icon=APP_ID,
            version=__version__, developer_name="SimpleScheduler Contributors",
            comments="A little less to do.\nA native home for your Linux scripts and routines.",
            license_type=Gtk.License.GPL_3_0, copyright="© 2026 SimpleScheduler Contributors")
        dialog.add_credit_section("Built with", ["GTK 4", "libadwaita", "GtkSourceView", "Python", "systemd"])
        dialog.present(self.window)

    def _shortcuts(self):
        dialog = Adw.Dialog(title="Keyboard Shortcuts", content_width=480)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        group = Adw.PreferencesGroup(margin_start=24, margin_end=24, margin_top=12, margin_bottom=24)
        for title, shortcut in [("New script", "Ctrl + N"), ("Import script", "Ctrl + O"),
                                ("Search", "Ctrl + F"), ("Discover scripts", "Ctrl + R"),
                                ("Edit current script", "Ctrl + E"), ("Duplicate current script", "Ctrl + D"),
                                ("Run or stop current script", "Ctrl + Enter"), ("Save in editor", "Ctrl + S"),
                                ("Find in editor", "Ctrl + F"), ("Back to library", "Alt + Left"),
                                ("Preferences", "Ctrl + ,"), ("Quit", "Ctrl + Q")]:
            row = Adw.ActionRow(title=title)
            value = Gtk.Label(label=shortcut)
            value.add_css_class("dim-label")
            row.add_suffix(value)
            group.add(row)
        toolbar.set_content(group)
        dialog.set_child(toolbar)
        dialog.present(self.window)


def main():
    if not Gtk.init_check():
        print("SimpleScheduler needs a graphical session. Launch it from your GNOME desktop.", file=sys.stderr)
        return 1
    return Application().run(sys.argv)
