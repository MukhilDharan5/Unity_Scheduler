from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import APP_ID
from .core import LANGUAGES, SCHEDULE_NAMES, SCHEDULES, short_path
from .dialogs import EditorDialog, OutputDialog, PreferencesDialog
from .groups import BASE_GROUPS, GroupCatalog, within
from .library import FILTERS, SORTS, matching_scripts
from .widgets import (boxed_list, button, clear, error_dialog, label, relative_time,
                      group_icon, script_icon, scrolled, section, status_pill)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, controller):
        super().__init__(application=app, title="SimpleScheduler", default_width=1160, default_height=800)
        self.controller = controller
        self.category = "all"
        self.query = ""
        self.limit = 100
        self.detail_id = None
        self.detail_page = None
        self.detail_dirty = False
        self.sidebar_counts = {}
        self.groups = GroupCatalog(controller.store)
        self.scripts_cache = []
        self.group_assignments = {}
        self.application_groups = []
        self.group_counts = {}
        self.app_sidebar_keys = ()
        self.show_discovered = controller.store.get_setting("show_discovered", False)
        self.hidden_count = 0
        self.render_serial = 0
        self.visible_scripts = []
        self.active_script_id = None
        self.editor_dialog = None
        self.schedule_prompt = None
        self.set_icon_name(APP_ID)
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        self.split = Adw.OverlaySplitView(min_sidebar_width=220, max_sidebar_width=260,
                                           sidebar_width_fraction=.22)
        self.toast_overlay.set_child(self.split)
        self._build_sidebar()
        self._build_content()
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 780sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)
        self.controller.connect("changed", self._changed)
        self.controller.connect("message", lambda _, message: self.toast(message))
        self.controller.connect("error", lambda _, message: error_dialog(self, message))
        self.controller.connect("scan-state", self._scan_state)
        self.controller.connect("run-finished", self._run_completed)
        self.connect("close-request", self._close)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._files_dropped)
        self.add_controller(drop)
        self._sync_theme()
        self.theme_handler = Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._sync_theme())
        self._changed()
        GLib.idle_add(self.controller.scan)

    def _build_sidebar(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        header.set_title_widget(label(""))
        toolbar.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        brand = Gtk.Box(spacing=10)
        brand.add_css_class("sidebar-brand")
        icon = Gtk.Image.new_from_icon_name(APP_ID)
        icon.set_pixel_size(38)
        brand.append(icon)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        title_box.append(label("SimpleScheduler", "title-3", xalign=0))
        title_box.append(label("A little less to do.", "dim-label caption", xalign=0))
        brand.append(title_box)
        box.append(brand)
        self.navigation = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.navigation.add_css_class("navigation-sidebar")
        self.navigation.set_header_func(self._sidebar_header)
        self.navigation_rows = {}
        for key, title, icon_name in [("all", "All Scripts", "view-grid-symbolic"),
                ("favorites", "Favorites", "starred-symbolic"),
                ("quick", "Quick Scripts", "media-playback-start-symbolic"),
                ("scheduled", "Scheduled", "alarm-symbolic"),
                ("discovered", "Discovered", "folder-saved-search-symbolic"),
                ("history", "Run History", "document-open-recent-symbolic")]:
            self._add_sidebar_row(key, title, Gtk.Image.new_from_icon_name(icon_name))
        for group in BASE_GROUPS:
            self._add_sidebar_row(group.key, group.title, group_icon(group))
        self.navigation.select_row(self.navigation_rows["all"])
        self.navigation.connect("row-selected", self._category_selected)
        box.append(scrolled(self.navigation, vexpand=True))
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        footer.add_css_class("sidebar-footer")
        footer.append(button(icon="folder-new-symbolic", text="New App Group", css="flat",
                             clicked=lambda _: self.new_group()))
        preferences = button(icon="preferences-system-symbolic", text="Preferences", css="flat",
                             clicked=lambda _: self.show_preferences())
        preferences.set_halign(Gtk.Align.FILL)
        footer.append(preferences)
        footer.append(label("Small scripts. More possibilities.", "dim-label", wrap=True))
        box.append(footer)
        toolbar.set_content(box)
        self.split.set_sidebar(toolbar)

    def _add_sidebar_row(self, key, title, icon):
        row = Gtk.ListBoxRow()
        row.category = key
        row.set_tooltip_text(title)
        content = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        content.append(icon)
        text = label(title, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        text.set_max_width_chars(25)
        content.append(text)
        count = label("", "sidebar-count")
        content.append(count)
        row.set_child(content)
        self.navigation.append(row)
        self.navigation_rows[key] = row
        self.sidebar_counts[key] = count

    def _sidebar_header(self, row, before):
        title = "LIBRARY" if row.category == "all" else "SCRIPT GROUPS" if row.category == "user" else "BY APPLICATION" if row.category.startswith("app:") and (before is None or not before.category.startswith("app:")) else None
        row.set_header(label(title, "sidebar-section", xalign=0) if title else None)

    def _update_group_sidebar(self):
        keys = tuple(group.key for group in self.application_groups)
        if keys != self.app_sidebar_keys:
            # Preserve the selected page while updating automatic application groups.
            self.navigation.handler_block_by_func(self._category_selected)
            for key in self.app_sidebar_keys:
                self.navigation.remove(self.navigation_rows.pop(key))
                self.sidebar_counts.pop(key, None)
            for group in self.application_groups:
                self._add_sidebar_row(group.key, group.title, group_icon(group))
            self.app_sidebar_keys = keys
            self.navigation.select_row(self.navigation_rows.get(self.category, self.navigation_rows["all"]))
            self.navigation.handler_unblock_by_func(self._category_selected)
        for group in BASE_GROUPS:
            count = sum(self.group_counts.get(g.key, 0) for g in self.application_groups) if group.key == "applications" else self.group_counts.get(group.key, 0)
            self.sidebar_counts[group.key].set_label(f"{count:,}")
            self.navigation_rows[group.key].set_visible(group.key == "user" or self.show_discovered or count > 0)
        for group in self.application_groups:
            self.sidebar_counts[group.key].set_label(f"{self.group_counts.get(group.key, 0):,}")

    def select_group(self, key):
        if self.detail_dirty:
            self._confirm_schedule_navigation(lambda: self.select_group(key))
            return
        row = self.navigation_rows.get(key)
        if row:
            self.search.set_text("")
            self.query = ""
            self.navigation.select_row(row)

    def _build_content(self):
        toolbar = Adw.ToolbarView()
        self.navigation_view = Adw.NavigationView()
        self.navigation_view.add(Adw.NavigationPage.new(toolbar, "All Scripts"))
        self.split.set_content(self.navigation_view)
        header = Adw.HeaderBar()
        sidebar_button = button(icon="sidebar-show-symbolic", tooltip="Show sidebar",
                                 clicked=lambda _: self.split.set_show_sidebar(not self.split.get_show_sidebar()))
        self.split.bind_property("collapsed", sidebar_button, "visible", 2)
        header.pack_start(sidebar_button)
        self.header_title = Adw.WindowTitle(title="All Scripts")
        header.set_title_widget(self.header_title)
        self.rescan_button = button(icon="view-refresh-symbolic", tooltip="Discover scripts again (Ctrl+R)",
                                    clicked=lambda _: self.controller.scan())
        header.pack_start(self.rescan_button)
        new = button(icon="list-add-symbolic", text="New Script", css="suggested-action",
                     tooltip="Create a script (Ctrl+N)", clicked=lambda _: self.new_script())
        header.pack_end(new)
        menu = Gio.Menu()
        menu.append("Import Script…", "app.import")
        menu.append("Preferences", "app.preferences")
        menu.append("Keyboard Shortcuts", "app.shortcuts")
        menu.append("About SimpleScheduler", "app.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                     tooltip_text="Main menu")
        header.pack_end(menu_button)
        toolbar.add_top_bar(header)
        self.page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        self.page_box.add_css_class("page-content")
        clamp = Adw.Clamp(maximum_size=1120, tightening_threshold=800)
        clamp.set_child(self.page_box)
        self.scroll = scrolled(clamp, vexpand=True)
        toolbar.set_content(self.scroll)
        heading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.heading = label("Your scripts, organized.", "page-heading", xalign=0)
        heading_box.append(self.heading)
        self.description = label("Browse personal, system, and application scripts in one place.", "page-description", xalign=0, wrap=True)
        heading_box.append(self.description)
        self.page_box.append(heading_box)
        self.summary = Gtk.Box(spacing=12, homogeneous=True)
        self.summary_values = []
        for name, icon in [("Scripts enabled", "object-select-symbolic"),
                           ("On a schedule", "alarm-symbolic"), ("Runs today", "media-playback-start-symbolic")]:
            card = Gtk.Box(spacing=14)
            card.add_css_class("summary-card")
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
            number = label("0", "summary-number", xalign=0)
            text.append(number)
            text.append(label(name, "summary-label", xalign=0))
            card.append(text)
            image = Gtk.Image.new_from_icon_name(icon)
            image.add_css_class("summary-icon")
            card.append(image)
            self.summary_values.append(number)
            self.summary.append(card)
        self.page_box.append(self.summary)
        self.search = Gtk.SearchEntry(placeholder_text="Search scripts, groups, or apps…", hexpand=True)
        self.search.set_tooltip_text("Search your library (Ctrl+F)")
        self.search.connect("search-changed", self._search_changed)
        self.search.connect("stop-search", lambda _: self.search.set_text(""))
        self.search.connect("activate", self._open_first_result)
        self.page_box.append(self.search)
        filters = Gtk.Box(spacing=10)
        filters.append(label("Show", "dim-label caption"))
        self.status_filter = Gtk.DropDown(model=Gtk.StringList.new(FILTERS), tooltip_text="Filter scripts by status")
        filters.append(self.status_filter)
        filters.append(Gtk.Box(hexpand=True))
        filters.append(label("Sort", "dim-label caption"))
        self.sort_order = Gtk.DropDown(model=Gtk.StringList.new(SORTS), tooltip_text="Sort your scripts",
            selected=min(self.controller.store.get_setting("sort_order", 0), len(SORTS) - 1))
        filters.append(self.sort_order)
        self.status_filter.connect("notify::selected", self._filter_changed)
        self.sort_order.connect("notify::selected", self._filter_changed)
        self.page_box.append(filters)
        self.filter_bar = filters
        list_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.list_heading = section("Script library", "Ready when you are")
        list_area.append(self.list_heading)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        list_area.append(self.list_box)
        self.empty = Adw.StatusPage(icon_name="utilities-terminal-symbolic", title="A fresh start",
                                    description="Create your first script, or let SimpleScheduler find the ones already on your device.")
        empty_actions = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.empty_create = button(icon="list-add-symbolic", text="Create a Script", css="suggested-action pill",
                                   clicked=lambda _: self.new_script())
        self.empty_reset = button(text="Clear Filters", css="suggested-action pill", clicked=lambda _: self.clear_filters())
        self.empty_browse = button(text="Browse Library", css="pill", clicked=lambda _: self.select_group("all"))
        for action in (self.empty_create, self.empty_reset, self.empty_browse):
            empty_actions.append(action)
        self.empty.set_child(empty_actions)
        self.empty.set_visible(False)
        list_area.append(self.empty)
        self.library_hint = Gtk.Box(spacing=10, margin_top=16, halign=Gtk.Align.CENTER)
        self.library_hint.append(button(icon="document-open-symbolic", text="Import Script…", css="pill",
                                        clicked=lambda _: self.import_script()))
        self.library_hint.append(button(icon="preferences-system-symbolic", text="Library Settings", css="flat",
                                        clicked=lambda _: self.show_preferences()))
        list_area.append(self.library_hint)
        self.load_more = button(text="Show More", css="flat", clicked=self._more)
        self.load_more.set_margin_top(12)
        list_area.append(self.load_more)
        self.page_box.append(list_area)
        footer = Gtk.Box(spacing=7)
        self.scan_spinner = Gtk.Spinner()
        self.scan_spinner.set_visible(False)
        footer.append(self.scan_spinner)
        self.discovery_icon = Gtk.Image.new_from_icon_name("object-select-symbolic")
        self.discovery_icon.add_css_class("discovery-dot")
        footer.append(self.discovery_icon)
        self.discovery_label = label("Looking for scripts on your device…", xalign=0, wrap=True)
        footer.append(self.discovery_label)
        footer.add_css_class("discovery-footer")
        self.page_box.append(footer)

    def _sync_theme(self):
        if Adw.StyleManager.get_default().get_dark():
            self.add_css_class("dark")
        else:
            self.remove_css_class("dark")

    def _filter_changed(self, *_):
        self.limit = 100
        self.controller.store.set_setting("sort_order", self.sort_order.get_selected())
        self._render_list(reset_scroll=True)

    def clear_filters(self):
        self.search.set_text("")
        self.query = ""
        self.status_filter.set_selected(0)
        self._render_list(reset_scroll=True)

    def _open_first_result(self, _):
        if self.visible_scripts:
            self.show_script(self.visible_scripts[0])

    def _category_selected(self, _, row):
        if row is None:
            return
        if self.detail_dirty:
            target = row.category
            self.navigation.handler_block_by_func(self._category_selected)
            self.navigation.select_row(self.navigation_rows.get(self.category))
            self.navigation.handler_unblock_by_func(self._category_selected)
            self._confirm_schedule_navigation(lambda: self.select_group(target))
            return
        self.category = row.category
        self.active_script_id = None
        self.limit = 100
        self._return_to_library()
        titles = {"all": ("All Scripts", "Your scripts, organized.", "Browse personal, system, and application scripts in one place."),
                  "favorites": ("Favorites", "Your favorites.", "The scripts you’ve starred, always close at hand."),
                  "quick": ("Quick Scripts", "Ready in a click.", "Pin your go-to scripts here for quick runs."),
                  "scheduled": ("Scheduled", "A rhythm for your routines.", "Your automations keep working, even when this window is closed."),
                  "discovered": ("Discovered", "Already on your device.", "Scripts found in your folders. Enable one when you’re ready to use it."),
                  "history": ("Run History", "Every run has a story.", "See what ran, how it went, and the output it left behind.")}
        group = self.groups.groups.get(self.category)
        if group:
            title, heading, description = group.title, group.title, group.description
        else:
            title, heading, description = titles[self.category]
        self.header_title.set_title(title)
        self.heading.set_label(heading)
        self.description.set_label(description)
        self.summary.set_visible(self.category == "all")
        self.filter_bar.set_visible(self.category != "history")
        self.search.set_placeholder_text("Search run history…" if self.category == "history" else "Search scripts, groups, or apps…")
        self._render_list(reset_scroll=True)
        self.scroll.get_vadjustment().set_value(0)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _return_to_library(self, then=None):
        if self.detail_dirty:
            self._confirm_schedule_navigation(lambda: self._return_to_library(then))
            return False
        if self.detail_page:
            self.navigation_view.pop_to_page(self.navigation_view.get_navigation_stack().get_item(0))
        self.detail_page = None
        self.detail_id = None
        if then:
            then()
        return True

    def _search_changed(self, entry):
        self.query = entry.get_text().strip().lower()
        self.limit = 100
        self._render_list(reset_scroll=True)

    def _more(self, _):
        self.limit += 100
        self._render_list()

    def _changed(self, *_):
        all_scripts = self.controller.store.scripts()
        scripts = self.controller.store.library_scripts(all_scripts)
        self.show_discovered = self.controller.store.get_setting("show_discovered", False)
        self.hidden_count = len(all_scripts) - len(scripts)
        self.scripts_cache = scripts
        self.group_assignments, self.group_counts, self.application_groups = self.groups.organize(scripts)
        selected_group = self.groups.groups.get(self.category)
        if selected_group and selected_group.application and any(self.group_assignments[s.id].key == selected_group.key for s in scripts) and all(g.key != selected_group.key for g in self.application_groups):
            self.application_groups.append(selected_group)
            self.application_groups.sort(key=lambda g: g.title.casefold())
        self._update_group_sidebar()
        self.navigation_rows["discovered"].set_visible(self.show_discovered)
        selected_row = self.navigation_rows.get(self.category)
        if selected_row is None or not selected_row.get_visible():
            self._category_selected(self.navigation, self.navigation_rows["all"])
            self.navigation.select_row(self.navigation_rows["all"])
        runs = self.controller.store.runs()
        enabled = sum(bool(s.enabled) for s in scripts)
        scheduled = sum(s.enabled and s.schedule != "manual" for s in scripts)
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        with self.controller.store.connect() as db:
            today = db.execute("SELECT COUNT(*) FROM runs WHERE started>=?", (midnight,)).fetchone()[0]
        for widget, value in zip(self.summary_values, (enabled, scheduled, today)):
            widget.set_label(str(value))
        for key, value in {"all": len(scripts), "scheduled": sum(s.schedule != "manual" for s in scripts),
                           "favorites": sum(bool(s.favorite) for s in scripts),
                           "quick": sum(bool(s.quick_access) for s in scripts),
                           "discovered": sum(not s.in_library for s in scripts), "history": len(runs)}.items():
            self.sidebar_counts[key].set_label(str(value))
        self._render_list()
        self._update_discovery_footer()
        if self.detail_id:
            self._refresh_detail()

    def _render_list(self, reset_scroll=False):
        if not hasattr(self, "list_box"):
            return
        position = 0 if reset_scroll else self.scroll.get_vadjustment().get_value()
        self.render_serial += 1
        serial = self.render_serial
        self.visible_scripts = []
        clear(self.list_box)
        grouped = False
        if self.category == "history":
            runs = self.controller.store.runs(limit=1000)
            items = [r for r in runs if self.query in (r["name"] + " " + r["status"]).lower()]
            listing = boxed_list()
            listing.add_css_class("script-list")
            for run in items[:self.limit]:
                listing.append(self._run_row(run))
            self.list_box.append(listing)
            self.list_heading.get_first_child().set_label("Recent runs")
            self.list_heading.get_last_child().set_label(f"{len(items)} runs")
        else:
            items = []
            for script in self.scripts_cache:
                group = self.group_assignments[script.id]
                if self.category == "scheduled" and script.schedule == "manual":
                    continue
                if self.category == "discovered" and script.in_library:
                    continue
                if self.category == "favorites" and not script.favorite:
                    continue
                if self.category == "quick" and not script.quick_access:
                    continue
                if self.category == "applications" and not group.application:
                    continue
                if self.category not in ("all", "favorites", "quick", "scheduled", "discovered", "applications") and group.key != self.category:
                    continue
                items.append(script)
            items = matching_scripts(items, self.query, self.status_filter.get_selected(),
                                     self.sort_order.get_selected(), self.group_assignments, self.controller.processes)
            self.visible_scripts = items
            grouped = not self.query and self.category in ("all", "scheduled", "discovered", "applications")
            if self.category == "quick":
                flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                   min_children_per_line=1, max_children_per_line=3,
                                   column_spacing=14, row_spacing=14)
                for script in items[:self.limit]:
                    flow.append(self._quick_card(script))
                self.list_box.append(flow)
            elif grouped:
                buckets = {}
                for script in items:
                    buckets.setdefault(self.group_assignments[script.id].key, []).append(script)
                order = ["user", *[g.key for g in self.application_groups], "system", "kernel", "os"]
                for key in order:
                    if key not in buckets:
                        continue
                    group = self.groups.groups[key]
                    members = buckets[key]
                    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                    heading = Gtk.Box(spacing=9, margin_start=2, margin_end=2)
                    heading.append(group_icon(group, 18))
                    heading.append(label(group.title, "section-heading", xalign=0, hexpand=True))
                    heading.append(label(f"{len(members):,}", "section-description"))
                    if len(members) > 3:
                        heading.append(button(text="View All", css="flat", tooltip=f"Show all {group.title} scripts",
                                              clicked=lambda _, key=key: self.select_group(key)))
                    card.append(heading)
                    listing = self._script_listing()
                    for script in members[:3]:
                        listing.append(self._script_row(script))
                    card.append(listing)
                    self.list_box.append(card)
            else:
                listing = self._script_listing()
                for script in items[:self.limit]:
                    listing.append(self._script_row(script))
                self.list_box.append(listing)
            current_group = self.groups.groups.get(self.category)
            self.list_heading.get_first_child().set_label("Search results" if self.query else "Pinned for quick runs" if self.category == "quick" else "Starred scripts" if self.category == "favorites" else "Browse by group" if grouped else f"{current_group.title} scripts" if current_group else "Script library")
            self.list_heading.get_last_child().set_label(f"{len(items):,} script{'s' if len(items) != 1 else ''}")
        self.list_box.set_visible(bool(items))
        self.empty.set_visible(not items)
        self.load_more.set_visible(not grouped and len(items) > self.limit)
        self.library_hint.set_visible(not items and not self.query and not self.status_filter.get_selected() and self.category in ("all", "discovered"))
        if not items:
            filtered = bool(self.query or self.category != "history" and self.status_filter.get_selected())
            self.empty.set_icon_name("system-search-symbolic" if self.query else "starred-symbolic" if self.category == "favorites" else "media-playback-start-symbolic" if self.category == "quick" else "document-open-recent-symbolic" if self.category == "history" else "utilities-terminal-symbolic")
            current_group = self.groups.groups.get(self.category)
            self.empty.set_title("No matches" if filtered else "No favorites yet" if self.category == "favorites" else "Keep your go-to scripts here" if self.category == "quick" else "No runs yet" if self.category == "history" else "No scripts in this group" if current_group else "Nothing scheduled yet" if self.category == "scheduled" else "A fresh start")
            self.empty.set_description("Try a different search or select All scripts in the status filter." if filtered else
                "Star a script in your library to find it here." if self.category == "favorites" else
                "Open a script and turn on Quick Scripts to pin it here." if self.category == "quick" else
                "Run a script to see its output and status here." if self.category == "history" else
                "Choose a script and give it a schedule." if self.category == "scheduled" else
                "Scripts assigned to this group will appear here as they’re discovered." if current_group else
                "Create or import a script to get started. Other device scripts are hidden by default.")
            self.empty_create.set_visible(not filtered and self.category in ("all", "discovered"))
            self.empty_reset.set_visible(filtered)
            self.empty_browse.set_visible(not filtered and self.category in ("favorites", "quick"))
            self.empty.get_child().set_visible(self.empty_create.get_visible() or self.empty_reset.get_visible() or self.empty_browse.get_visible())
        def restore_scroll():
            if self.render_serial == serial:
                adjustment = self.scroll.get_vadjustment()
                adjustment.set_value(min(position, max(0, adjustment.get_upper() - adjustment.get_page_size())))
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(30, restore_scroll)

    def _script_listing(self):
        listing = boxed_list()
        listing.set_activate_on_single_click(True)
        listing.add_css_class("script-list")
        listing.connect("row-activated", self._script_row_activated)
        return listing

    def _script_row_activated(self, _, row):
        script = self.controller.store.get(row.script_id)
        if script:
            self.active_script_id = script.id
            self.show_script(script)

    def _script_row(self, script):
        row = Gtk.ListBoxRow(activatable=True)
        row.script_id = script.id
        row.set_tooltip_text("Click for details · Use the pencil to edit")
        box = Gtk.Box(spacing=10)
        box.append(script_icon(script.language))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, hexpand=True, valign=Gtk.Align.CENTER)
        title = label(script.name, "script-title", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        title.set_max_width_chars(50)
        title.set_tooltip_text(script.name)
        title_row = Gtk.Box(spacing=10)
        title.set_hexpand(True)
        title_row.append(title)
        group = self.group_assignments.get(script.id)
        if group and self.query:
            badge = label(group.title, "group-badge", ellipsize=Pango.EllipsizeMode.END, valign=Gtk.Align.CENTER)
            badge.set_max_width_chars(25)
            title_row.append(badge)
        text.append(title_row)
        path_label = label(short_path(script.path), "script-path", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        path_label.set_tooltip_text(script.path)
        text.append(path_label)
        box.append(text)
        metadata = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, valign=Gtk.Align.CENTER)
        metadata.append(label(script.schedule_label, "schedule-label", xalign=1))
        if script.missing or script.last_status:
            pill = status_pill("missing" if script.missing else script.last_status)
            pill.set_halign(Gtk.Align.END)
            metadata.append(pill)
        else:
            metadata.append(label("Ready" if script.enabled else "Disabled", "script-path", xalign=1))
        box.append(metadata)
        star = button(icon="starred-symbolic" if script.favorite else "non-starred-symbolic", css="flat circular",
                      tooltip="Remove from Favorites" if script.favorite else "Add to Favorites",
                      clicked=lambda _: self._toggle_library_flag(script.id, "favorite"))
        star.set_valign(Gtk.Align.CENTER)
        if script.favorite:
            star.add_css_class("favorite-star")
        box.append(star)
        edit = button(icon="document-edit-symbolic", css="flat circular", tooltip="Edit script",
                      clicked=lambda _: self.edit_script(script.id))
        edit.set_valign(Gtk.Align.CENTER)
        edit.set_sensitive(not script.missing)
        box.append(edit)
        running = script.id in self.controller.processes or script.last_status == "running"
        play = button(icon="media-playback-stop-symbolic" if running else "media-playback-start-symbolic",
                       css="flat circular", tooltip="Stop script" if running else "Run now",
                       clicked=lambda _: self.controller.cancel(script) if running else self.controller.run(script))
        play.set_valign(Gtk.Align.CENTER)
        play.set_sensitive(bool(script.enabled and not script.missing) or running)
        box.append(play)
        switch = Gtk.Switch(active=bool(script.enabled), valign=Gtk.Align.CENTER,
                             tooltip_text="Enable or disable script")
        switch.set_sensitive(script.id not in self.controller.pending and not script.missing)
        switch.connect("state-set", self._toggle_script, script)
        box.append(switch)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=self._script_menu(script),
                              tooltip_text="Script actions", valign=Gtk.Align.CENTER)
        more.add_css_class("flat")
        box.append(more)
        context = Gtk.GestureClick(button=3)
        def popup(gesture, count, x, y):
            more.popup()
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        context.connect("pressed", popup)
        row.add_controller(context)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: setattr(self, "active_script_id", script.id))
        row.add_controller(focus)
        row.set_child(box)
        return row

    def _toggle_library_flag(self, script_id, flag):
        script = self.controller.store.get(script_id)
        if script:
            self.controller.store.set_flag(script_id, flag, not getattr(script, flag))
            self.controller.emit("changed")

    def _script_menu(self, script):
        menu = Gio.Menu()
        sections = [(("Edit Script", "edit"), ("Duplicate…", "duplicate")),
                    (("Remove from Favorites" if script.favorite else "Add to Favorites", "favorite"),
                     ("Unpin from Quick Scripts" if script.quick_access else "Pin to Quick Scripts", "pin")),
                    (("Copy File Path", "copy"), ("Open Containing Folder", "reveal"), ("Last Run Output", "output")),
                    (("Remove from Library…", "remove"),)]
        for entries in sections:
            section_menu = Gio.Menu()
            for title, verb in entries:
                item = Gio.MenuItem.new(title, None)
                item.set_action_and_target_value("app.script", GLib.Variant("(ss)", (verb, script.id)))
                section_menu.append_item(item)
            menu.append_section(None, section_menu)
        return menu

    def script_action(self, verb, script_id):
        script = self.controller.store.get(script_id)
        if not script:
            return
        self.active_script_id = script.id
        actions = {"edit": lambda: self.edit_script(script.id),
                   "duplicate": lambda: self.duplicate_script(script.id),
                   "favorite": lambda: self._toggle_library_flag(script.id, "favorite"),
                   "pin": lambda: self._toggle_library_flag(script.id, "quick_access"),
                   "copy": lambda: self.copy_path(script.id),
                   "reveal": lambda: self.reveal_script(script.id),
                   "output": lambda: self.show_latest_output(script.id),
                   "remove": lambda: self.remove_script(script.id)}
        if verb in actions:
            actions[verb]()

    def current_script(self):
        script_id = self.detail_id or self.active_script_id
        if not script_id and self.query and self.visible_scripts:
            script_id = self.visible_scripts[0].id
        return self.controller.store.get(script_id) if script_id else None

    def copy_path(self, script_id=None):
        script = self.controller.store.get(script_id) if script_id else self.current_script()
        if script:
            self.get_clipboard().set(script.path)
            self.toast("File path copied")

    def show_latest_output(self, script_id=None):
        script = self.controller.store.get(script_id) if script_id else self.current_script()
        runs = self.controller.store.runs(script.id, limit=1) if script else []
        if runs:
            OutputDialog(self, runs[0]).present(self)
        else:
            self.toast("This script hasn’t run yet")

    def _run_completed(self, _, script_id):
        runs = self.controller.store.runs(script_id, limit=1)
        if runs:
            run = runs[0]
            result = {"success": "completed", "failed": "failed", "cancelled": "stopped"}.get(run["status"], run["status"])
            self.toast(f"{run['name']} {result}", "View Output", lambda: OutputDialog(self, run).present(self))

    def run_current(self):
        script = self.current_script()
        if script:
            if script.id in self.controller.processes or script.last_status == "running":
                self.controller.cancel(script)
            else:
                self.controller.run(script)

    def duplicate_script(self, script_id=None):
        script = self.controller.store.get(script_id) if script_id else self.current_script()
        if script:
            try:
                dialog = EditorDialog(self, duplicate=script)
                self.editor_dialog = dialog
                dialog.present(self)
                return dialog
            except (OSError, ValueError) as error:
                error_dialog(self, error)

    def _quick_card(self, script):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("card")
        card.add_css_class("quick-card")
        top = Gtk.Box(spacing=10)
        top.append(script_icon(script.language))
        title = label(script.name, "script-title", xalign=0, hexpand=True, wrap=True)
        title.set_max_width_chars(24)
        top.append(title)
        top.append(button(icon="window-close-symbolic", css="flat circular", tooltip="Unpin from Quick Scripts",
                          clicked=lambda _: self._toggle_library_flag(script.id, "quick_access")))
        card.append(top)
        card.append(label(script.schedule_label + (" · Disabled" if not script.enabled else ""), "dim-label caption", xalign=0))
        running = script.id in self.controller.processes or script.last_status == "running"
        actions = Gtk.Box(spacing=8)
        run = button(icon="media-playback-stop-symbolic" if running else "media-playback-start-symbolic",
                     text="Stop" if running else "Run Now", css="suggested-action", clicked=lambda _: self.controller.cancel(script) if running else self.controller.run(script))
        run.set_hexpand(True)
        run.set_sensitive(running or bool(script.enabled and not script.missing))
        actions.append(run)
        actions.append(button(icon="document-properties-symbolic", tooltip="Script details", clicked=lambda _: self.show_script(script)))
        actions.append(button(icon="document-edit-symbolic", tooltip="Edit script", clicked=lambda _: self.edit_script(script.id)))
        card.append(actions)
        return card

    def _toggle_script(self, switch, enabled, script):
        switch.set_sensitive(False)
        self.controller.set_enabled(script, enabled)
        return True

    def _run_row(self, run):
        row = Adw.ActionRow(title=run["name"], subtitle=relative_time(run["started"]), activatable=True,
                            use_markup=False)
        row.add_prefix(Gtk.Image.new_from_icon_name("object-select-symbolic" if run["status"] == "success" else
                                                   "dialog-warning-symbolic" if run["status"] == "failed" else "document-open-recent-symbolic"))
        row.add_suffix(status_pill(run["status"]))
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated", lambda _: OutputDialog(self, run).present(self))
        return row

    def _scan_state(self, _, scanning):
        if not scanning:
            self.groups.refresh()
        self.scan_spinner.set_visible(scanning)
        self.scan_spinner.set_spinning(scanning)
        self.discovery_icon.set_visible(not scanning)
        self.rescan_button.set_sensitive(not scanning)
        self._update_discovery_footer()

    def _update_discovery_footer(self):
        if self.controller.scanning:
            self.discovery_label.set_label("Discovering scripts on your device…")
        elif self.controller.last_scan:
            result = self.controller.last_scan
            if not self.show_discovered:
                self.discovery_label.set_label("Your library · Other device scripts are hidden. Change visibility in Preferences.")
                return
            text = f"Automatic discovery is on · Watching {len(self.controller.monitors):,} folders" if self.controller.auto else "Automatic discovery is off · Refresh to find new scripts"
            if self.controller.auto and len(result.directories) > len(self.controller.monitors):
                text += " · Remaining folders checked every minute"
            if result.errors:
                text += f" · {result.errors} unreadable folders skipped"
            self.discovery_label.set_label(text)
        else:
            self.discovery_label.set_label("Ready to discover your scripts")

    def show_script(self, script):
        if self.detail_dirty:
            self._confirm_schedule_navigation(lambda: self.show_script(script))
            return
        if self.detail_page:
            self._return_to_library()
        script = self.controller.store.get(script.id)
        if not script:
            return
        script.missing = not Path(script.path).is_file()
        self.active_script_id = script.id
        self.detail_id = script.id
        self.detail_dirty = False
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_back_button=False)
        header.pack_start(button(icon="go-previous-symbolic", tooltip="Back (Alt+Left)", clicked=lambda _: self._return_to_library()))
        header.set_title_widget(Adw.WindowTitle(title="Script Details"))
        self.detail_favorite = button(icon="starred-symbolic" if script.favorite else "non-starred-symbolic",
            tooltip="Remove from Favorites" if script.favorite else "Add to Favorites",
            clicked=lambda _: self._toggle_library_flag(script.id, "favorite"))
        header.pack_end(self.detail_favorite)
        menu = Gio.Menu()
        menu.append("Copy File Path", "app.copy-path")
        menu.append("Duplicate…", "app.duplicate")
        menu.append("Last Run Output", "app.last-output")
        menu.append("Open Containing Folder", "app.reveal-script")
        menu.append("Remove from Library…", "app.remove-script")
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu))
        toolbar.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22,
                          margin_top=16, margin_start=30, margin_end=30, margin_bottom=30)
        clamp = Adw.Clamp(maximum_size=740, tightening_threshold=500)
        clamp.set_child(content)
        toolbar.set_content(scrolled(clamp))
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        hero.add_css_class("detail-hero")
        hero.append(script_icon(script.language, large=True))
        self.detail_name = label(script.name, "detail-name", wrap=True, justify=Gtk.Justification.CENTER)
        hero.append(self.detail_name)
        hero.append(label(short_path(script.path), "dim-label caption", wrap=True, selectable=True,
                          justify=Gtk.Justification.CENTER))
        actions = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=6)
        self.detail_run = button(icon="media-playback-start-symbolic", text="Run Now", css="suggested-action pill",
                                 clicked=lambda _: self._detail_run_clicked())
        actions.append(self.detail_run)
        actions.append(button(icon="document-edit-symbolic", text="Edit Script", css="pill",
                               clicked=lambda _: self.edit_script()))
        hero.append(actions)
        self.detail_adopt = button(icon="list-add-symbolic", text="Add to Library", css="pill",
                                   clicked=lambda _: self._adopt_script(script.id))
        self.detail_adopt.set_halign(Gtk.Align.CENTER)
        self.detail_adopt.set_visible(not script.in_library)
        hero.append(self.detail_adopt)
        content.append(hero)
        enabled_group = Adw.PreferencesGroup()
        self.detail_enabled = Adw.SwitchRow(title="Enable script", subtitle="Allow manual runs and your chosen schedule.",
                                            active=bool(script.enabled))
        self.detail_enabled.set_sensitive(not script.missing)
        self.detail_enabled.connect("notify::active", self._detail_enabled_changed)
        enabled_group.add(self.detail_enabled)
        content.append(enabled_group)
        organization = Adw.PreferencesGroup(title="Organization",
            description="Detected from file locations and installed app information. You can choose a different group.")
        self.detail_quick = Adw.SwitchRow(title="Quick Scripts", subtitle="Pin this script for one-click runs.",
                                         active=bool(script.quick_access))
        self.detail_quick.connect("notify::active", self._detail_quick_changed)
        organization.add(self.detail_quick)
        automatic = self.groups.automatic(script)
        choices = [g for g in BASE_GROUPS if g.key != "applications"] + self.application_groups
        self.detail_group_keys = [None] + [g.key for g in choices]
        self.detail_group = Adw.ComboRow(title="Group", model=Gtk.StringList.new(
            [f"Automatic · {automatic.title}"] + [g.title for g in choices]))
        override = self.groups.overrides.get(script.id)
        self.detail_group.set_selected(self.detail_group_keys.index(override) if override in self.detail_group_keys else 0)
        self.detail_group.connect("notify::selected", self._detail_group_changed)
        organization.add(self.detail_group)
        add_group = Adw.ActionRow(title="Create an app group", subtitle="Keep a project’s scripts together.", activatable=True)
        add_group.add_prefix(Gtk.Image.new_from_icon_name("folder-new-symbolic"))
        add_group.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        add_group.connect("activated", lambda _: self.new_group(script))
        organization.add(add_group)
        content.append(organization)
        schedule = Adw.PreferencesGroup(title="Give it a rhythm", description="Schedules keep working when SimpleScheduler is closed.")
        self.schedule_combo = Adw.ComboRow(title="Run", model=Gtk.StringList.new(SCHEDULE_NAMES),
                                          selected=SCHEDULES.index(script.schedule))
        schedule.add(self.schedule_combo)
        self.time_entry = Adw.EntryRow(title="Time · 24-hour format")
        self.time_entry.set_text(script.time_of_day)
        schedule.add(self.time_entry)
        self.interval_spin = Adw.SpinRow(title="Minutes between runs", subtitle="From 1 minute to 7 days",
            adjustment=Gtk.Adjustment(value=script.interval, lower=1, upper=10080, step_increment=1, page_increment=15))
        schedule.add(self.interval_spin)
        self.schedule_hint = Adw.ActionRow()
        self.schedule_hint.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
        schedule.add(self.schedule_hint)
        self.schedule_combo.connect("notify::selected", self._schedule_changed)
        self.time_entry.connect("changed", self._schedule_dirty)
        self.interval_spin.connect("notify::value", self._schedule_dirty)
        self._schedule_visibility()
        content.append(schedule)
        self.save_schedule = button(text="Save Schedule", css="suggested-action", clicked=self._save_schedule)
        self.save_schedule.set_halign(Gtk.Align.END)
        self.save_schedule.set_sensitive(False)
        self.schedule_bar = Gtk.Revealer(reveal_child=False)
        schedule_actions = Gtk.Box(spacing=12, margin_start=20, margin_end=20, margin_top=12, margin_bottom=12)
        schedule_actions.append(label("Unsaved schedule changes", "dim-label", xalign=0, hexpand=True))
        schedule_actions.append(button(text="Reset", clicked=lambda _: self._reset_schedule()))
        schedule_actions.append(self.save_schedule)
        self.schedule_bar.set_child(schedule_actions)
        toolbar.add_bottom_bar(self.schedule_bar)
        history = Adw.PreferencesGroup(title="Recent runs", description="Select a run to see its output.")
        self.detail_history = boxed_list()
        history.add(self.detail_history)
        content.append(history)
        self.detail_page = Adw.NavigationPage.new(toolbar, script.name)
        self.detail_page.connect("hidden", self._detail_hidden)
        self.navigation_view.push(self.detail_page)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)
        self._refresh_detail()

    def _detail_hidden(self, page):
        if self.detail_page == page:
            self.detail_id = None
            self.detail_page = None

    def _detail_enabled_changed(self, row, _):
        script = self.controller.store.get(self.detail_id)
        if script and bool(script.enabled) != row.get_active():
            row.set_sensitive(False)
            self.controller.set_enabled(script, row.get_active())

    def _detail_group_changed(self, row, _):
        script = self.controller.store.get(self.detail_id)
        if script:
            self.controller.store.add_to_library(script.id)
            self.groups.assign(script, self.detail_group_keys[row.get_selected()])
            self.controller.emit("changed")
            self.toast("Script group updated")

    def _detail_quick_changed(self, row, _):
        if self.detail_id:
            self.controller.store.set_flag(self.detail_id, "quick_access", row.get_active())
            self.controller.emit("changed")

    def _adopt_script(self, script_id):
        self.controller.store.add_to_library(script_id)
        self.controller.emit("changed")
        self.toast("Script added to your library")

    def _detail_run_clicked(self):
        script = self.controller.store.get(self.detail_id)
        if not script:
            return
        if script.id in self.controller.processes or script.last_status == "running":
            self.controller.cancel(script)
        else:
            self.controller.run(script)

    def _schedule_visibility(self):
        kind = SCHEDULES[self.schedule_combo.get_selected()]
        self.time_entry.set_visible(kind in ("daily", "weekly"))
        self.interval_spin.set_visible(kind == "interval")
        hints = {"manual": "Run it whenever you need it using Run Now.",
                 "login": "Runs when your graphical desktop session starts.",
                 "startup": "Runs before login when background startup is enabled in Preferences.",
                 "hourly": "Runs at the start of every hour, in your local time zone.",
                 "daily": "Runs daily at this local time. A missed run is caught up when your user services start.",
                 "weekly": "Runs every Monday at this local time.",
                 "interval": "First runs after this interval, then repeats while your user services are active."}
        self.schedule_hint.set_title(hints[kind])
        self.schedule_hint.set_title_lines(3)

    def _schedule_changed(self, *_):
        self._schedule_visibility()
        self._schedule_dirty()

    def _schedule_dirty(self, *_):
        script = self.controller.store.get(self.detail_id)
        self.detail_dirty = bool(script and (SCHEDULES[self.schedule_combo.get_selected()] != script.schedule or
            self.time_entry.get_text().strip() != script.time_of_day or int(self.interval_spin.get_value()) != script.interval))
        if hasattr(self, "save_schedule"):
            self.save_schedule.set_sensitive(self.detail_dirty)
            self.schedule_bar.set_reveal_child(self.detail_dirty)
        if self.detail_page:
            self.detail_page.set_can_pop(not self.detail_dirty)

    def _reset_schedule(self):
        script = self.controller.store.get(self.detail_id)
        if script:
            self.schedule_combo.set_selected(SCHEDULES.index(script.schedule))
            self.time_entry.set_text(script.time_of_day)
            self.interval_spin.set_value(script.interval)
            self._schedule_visibility()
        self.detail_dirty = False
        self.save_schedule.set_sensitive(False)
        self.schedule_bar.set_reveal_child(False)
        if self.detail_page:
            self.detail_page.set_can_pop(True)

    def _confirm_schedule_navigation(self, callback):
        if not self.detail_dirty:
            callback()
            return
        if self.schedule_prompt:
            return
        alert = Adw.AlertDialog(heading="Save your schedule changes?", body="Choose whether to save or discard this script’s unsaved schedule.")
        alert.add_response("cancel", "Keep Editing")
        alert.add_response("discard", "Discard Changes")
        alert.add_response("save", "Save Changes")
        alert.set_default_response("save")
        alert.set_close_response("cancel")
        alert.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        self.schedule_prompt = alert
        def response(_, choice):
            self.schedule_prompt = None
            if choice == "save":
                self._save_schedule(None, after=callback)
            elif choice == "discard":
                self._reset_schedule()
                callback()
        alert.connect("response", response)
        alert.present(self)

    def _save_schedule(self, _, after=None):
        script = self.controller.store.get(self.detail_id)
        if not script:
            return
        candidate = replace(script, schedule=SCHEDULES[self.schedule_combo.get_selected()],
                             time_of_day=self.time_entry.get_text().strip(), interval=int(self.interval_spin.get_value()))
        self.save_schedule.set_sensitive(False)
        for control in (self.schedule_combo, self.time_entry, self.interval_spin):
            control.set_sensitive(False)
        detail_id = self.detail_id

        def done(success):
            if self.detail_id != detail_id:
                return
            for control in (self.schedule_combo, self.time_entry, self.interval_spin):
                control.set_sensitive(True)
            if success:
                self._reset_schedule()
                self.toast("Schedule saved" if candidate.enabled else "Schedule saved · enable the script when you’re ready")
                if after:
                    after()
            else:
                self.save_schedule.set_sensitive(True)
        self.controller.save(candidate, done)

    def _refresh_detail(self):
        script = self.controller.store.get(self.detail_id)
        if not script or not self.detail_page:
            return
        script.missing = not Path(script.path).is_file()
        self.detail_name.set_label(script.name)
        self.detail_favorite.set_icon_name("starred-symbolic" if script.favorite else "non-starred-symbolic")
        self.detail_favorite.set_tooltip_text("Remove from Favorites" if script.favorite else "Add to Favorites")
        if script.favorite:
            self.detail_favorite.add_css_class("favorite-star")
        else:
            self.detail_favorite.remove_css_class("favorite-star")
        self.detail_adopt.set_visible(not script.in_library)
        self.detail_quick.handler_block_by_func(self._detail_quick_changed)
        self.detail_quick.set_active(bool(script.quick_access))
        self.detail_quick.handler_unblock_by_func(self._detail_quick_changed)
        running = script.id in self.controller.processes or script.last_status == "running"
        self.detail_run.set_child(None)
        self.detail_run.set_label("Stop Script" if running else "Run Now")
        self.detail_run.set_sensitive((bool(script.enabled) and not script.missing) or running)
        self.detail_enabled.set_sensitive(not script.missing and script.id not in self.controller.pending)
        # Do not write back while reflecting a successful or rejected async change.
        self.detail_enabled.handler_block_by_func(self._detail_enabled_changed)
        self.detail_enabled.set_active(bool(script.enabled))
        self.detail_enabled.handler_unblock_by_func(self._detail_enabled_changed)
        clear(self.detail_history)
        runs = self.controller.store.runs(script.id, limit=5)
        if runs:
            for run in runs:
                self.detail_history.append(self._run_row(run))
        else:
            row = Adw.ActionRow(title="Ready for its first run", subtitle="Output and results will appear here.")
            row.add_prefix(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
            self.detail_history.append(row)

    def new_script(self):
        self.editor_dialog = EditorDialog(self)
        self.editor_dialog.present(self)
        return self.editor_dialog

    def new_group(self, script=None):
        dialog = Adw.Dialog(title="New App Group", content_width=480)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.pack_start(button(text="Cancel", clicked=lambda _: dialog.force_close()))
        save = button(text="Create Group", css="suggested-action")
        header.pack_end(save)
        toolbar.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_start=24, margin_end=24, margin_top=12, margin_bottom=24)
        group = Adw.PreferencesGroup(description="Choose a name, and optionally a folder whose scripts should join this group automatically.")
        name = Adw.EntryRow(title="App or project name")
        group.add(name)
        folder_row = Adw.ActionRow(title="Match a folder", subtitle="Optional · includes subfolders", use_markup=False)
        folder = {"path": None}

        def chosen(file_dialog, result):
            try:
                path = file_dialog.select_folder_finish(result).get_path()
                if path:
                    folder["path"] = path
                    folder_row.set_subtitle(short_path(path))
            except GLib.Error:
                pass

        choose = button(icon="folder-open-symbolic", tooltip="Choose a folder", clicked=lambda _: Gtk.FileDialog(
            title="Choose the App’s Script Folder", modal=True).select_folder(self, None, chosen))
        choose.set_valign(Gtk.Align.CENTER)
        folder_row.add_suffix(choose)
        group.add(folder_row)
        box.append(group)
        if script:
            box.append(label(f"“{script.name}” will be moved into this group.", "dim-label caption", wrap=True, xalign=0))

        def create(_):
            try:
                created = self.groups.create_group(name.get_text(), folder["path"])
                if script:
                    self.groups.assign(script, created.key)
                if folder["path"]:
                    roots = self.controller.store.roots()
                    if not any(within(folder["path"], root) for root in roots):
                        self.controller.store.set_setting("roots", roots + [folder["path"]])
                dialog.force_close()
                self.controller.emit("changed")
                if folder["path"]:
                    self.controller.scan()
                self.select_group(created.key)
                self.toast("App group created")
            except ValueError as error:
                error_dialog(dialog, error)
        save.connect("clicked", create)
        toolbar.set_content(box)
        dialog.set_child(toolbar)
        dialog.present(self)
        return dialog

    def edit_script(self, script_id=None):
        visible = self.get_visible_dialog()
        if isinstance(visible, EditorDialog):
            visible.present(self)
            return visible
        script = self.controller.store.get(script_id) if script_id else self.current_script()
        if script:
            try:
                self.active_script_id = script.id
                self.editor_dialog = EditorDialog(self, script)
                self.editor_dialog.present(self)
                return self.editor_dialog
            except Exception as error:
                error_dialog(self, error)

    def import_script(self):
        dialog = Gtk.FileDialog(title="Import Scripts", modal=True)
        dialog.open_multiple(self, None, self._imported)

    def _imported(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
            self.import_paths([files.get_item(i).get_path() for i in range(files.get_n_items())])
        except GLib.Error:
            pass
        except Exception as error:
            error_dialog(self, error)

    def _files_dropped(self, target, value, x, y):
        self.import_paths([file.get_path() for file in value.get_files()])
        return True

    def import_paths(self, paths):
        paths = [path for path in paths if path]
        if not paths:
            self.toast("Choose files on this device")
            return
        def done(result):
            if result:
                imported, errors = result
                self.controller.emit("changed")
                if len(imported) == 1:
                    self.show_script(imported[0])
                elif imported:
                    self.select_group("all")
                if imported:
                    self.toast(f"{len(imported)} script{'s' if len(imported) != 1 else ''} added to your library")
                if errors:
                    error_dialog(self, "Some files could not be imported:\n\n" + "\n".join(
                        f"{Path(path).name}: {message}" for path, message in errors[:10]))
        self.controller.background(lambda: self.controller.store.import_scripts(paths), done)

    def remove_script(self, script_id=None):
        script = self.controller.store.get(script_id) if script_id else self.current_script()
        if not script:
            return
        alert = Adw.AlertDialog(heading=f"Remove “{script.name}”?",
            body="Its schedule will be disabled and it will be hidden from discovery. The script file and run history will stay on your device. You can import it again later.")
        alert.add_response("cancel", "Cancel")
        alert.add_response("remove", "Remove from Library")
        alert.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.connect("response", lambda _, response: self._remove_confirmed(script) if response == "remove" else None)
        alert.present(self)

    def _remove_confirmed(self, script):
        if self.detail_id == script.id and self.detail_dirty:
            self._reset_schedule()
        def done(success):
            if success:
                if self.detail_id == script.id:
                    self._return_to_library()
                self.controller.emit("changed")
                self.toast("Script removed from your library", "Undo", lambda: self._undo_remove(script), timeout=12)

        def work():
            self.controller.scheduler.remove(script)
            return True
        self.controller.background(work, done)

    def _undo_remove(self, script):
        def work():
            self.controller.store.restore(script)
            self.controller.scheduler.apply(script)
            return True
        def done(success):
            if success:
                self.controller.emit("changed")
                self.toast("Script restored")
        self.controller.background(work, done)

    def reveal_script(self, script_id=None):
        script = self.controller.store.get(script_id) if script_id else self.current_script()
        if script:
            try:
                Gio.AppInfo.launch_default_for_uri(Path(script.path).parent.as_uri(), None)
            except GLib.Error as error:
                self.toast(error.message)

    def show_preferences(self):
        PreferencesDialog(self).present(self)

    def toast(self, message, action=None, callback=None, timeout=5):
        toast = Adw.Toast(title=message, timeout=timeout, use_markup=False)
        if action and callback:
            toast.set_button_label(action)
            toast.connect("button-clicked", lambda _: callback())
        self.toast_overlay.add_toast(toast)
        return toast

    def _close(self, _):
        dialog = self.get_visible_dialog()
        dialogs = self.get_dialogs()
        if dialog and not isinstance(dialog, EditorDialog) and any(isinstance(dialogs.get_item(i), EditorDialog) for i in range(dialogs.get_n_items())):
            self.toast("Finish the open dialog before closing the editor")
            return True
        if isinstance(dialog, EditorDialog):
            dialog._request_close(after_close=self.close)
            return True
        if self.detail_dirty:
            self._confirm_schedule_navigation(self.close)
            return True
        self.controller.store.set_setting("window_width", self.get_width())
        self.controller.store.set_setting("window_height", self.get_height())
        return False
