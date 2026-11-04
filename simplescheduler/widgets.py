"""Small reusable native widgets."""

import time

from gi.repository import Adw, Gdk, Gio, Gtk, Pango

from .core import LANGUAGES


def label(text, css=None, **kwargs):
    widget = Gtk.Label(label=text, **kwargs)
    if css:
        for name in css.split():
            widget.add_css_class(name)
    return widget


def button(icon=None, text=None, css=None, tooltip=None, clicked=None):
    widget = Gtk.Button()
    if icon and text:
        box = Gtk.Box(spacing=7, halign=Gtk.Align.CENTER)
        box.append(Gtk.Image.new_from_icon_name(icon))
        box.append(label(text))
        widget.set_child(box)
    elif icon:
        widget.set_icon_name(icon)
    else:
        widget.set_label(text or "")
    if css:
        for name in css.split():
            widget.add_css_class(name)
    if tooltip:
        widget.set_tooltip_text(tooltip)
    if clicked:
        widget.connect("clicked", clicked)
    return widget


def script_icon(language, large=False):
    box = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    box.add_css_class("script-icon")
    box.add_css_class("language-" + language)
    if large:
        box.add_css_class("detail-icon")
    box.set_size_request(72 if large else 44, 72 if large else 44)
    if language == "shell":
        icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
    elif language in ("python", "javascript", "ruby", "perl"):
        icon = label({"python": "Py", "javascript": "{}", "ruby": "rb", "perl": "pl"}[language], "code-glyph")
    else:
        icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
    icon.set_halign(Gtk.Align.CENTER)
    icon.set_valign(Gtk.Align.CENTER)
    # A center box keeps all language tiles the same width, independent of filenames.
    center = Gtk.CenterBox()
    center.set_center_widget(icon)
    center.set_size_request(72 if large else 44, 72 if large else 44)
    box.append(center)
    return box


def group_icon(group, size=16):
    if group.icon.startswith("/"):
        image = Gtk.Image.new_from_gicon(Gio.FileIcon.new(Gio.File.new_for_path(group.icon)))
    else:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        name = group.icon if theme.has_icon(group.icon) else "application-x-executable-symbolic"
        image = Gtk.Image.new_from_icon_name(name)
    image.set_pixel_size(size)
    return image


def relative_time(value):
    if value is None:
        return "Never run"
    seconds = max(0, time.time() - value)
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        n = int(seconds / 60)
        return f"{n} minute{'s' if n != 1 else ''} ago"
    if seconds < 86400:
        n = int(seconds / 3600)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    return time.strftime("%b %d · %H:%M", time.localtime(value))


def status_pill(status):
    names = {"success": "Completed", "failed": "Failed", "running": "Running",
             "cancelled": "Stopped", "interrupted": "Interrupted", "missing": "Missing"}
    return label(names.get(status, "Not run yet"), "status-pill status-" + (status or "idle"),
                 valign=Gtk.Align.CENTER)


def scrolled(child, **kwargs):
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, **kwargs)
    scroll.set_child(child)
    return scroll


def section(title, subtitle=None):
    row = Gtk.Box(spacing=10, margin_top=4, margin_bottom=8)
    row.append(label(title, "section-heading", xalign=0, hexpand=True))
    if subtitle:
        row.append(label(subtitle, "section-description"))
    return row


def boxed_list():
    widget = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    widget.add_css_class("boxed-list")
    return widget


def clear(container):
    child = container.get_first_child()
    while child:
        sibling = child.get_next_sibling()
        container.remove(child)
        child = sibling


def error_dialog(parent, message):
    dialog = Adw.AlertDialog(heading="Something needs your attention", body=str(message))
    dialog.add_response("ok", "OK")
    dialog.present(parent)
