"""Keep filesystem and process work off the GTK main loop."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from gi.repository import Gio, GLib, GObject

from .core import SUFFIXES, Store, detect_language, discover
from .scheduler import Scheduler


class Controller(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "scan-state": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "message": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "run-finished": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.scheduler = Scheduler(store)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="simplescheduler")
        self.monitors = {}
        self.known_paths = set()
        self.processes = {}
        self.pending = set()
        self.scanning = False
        self.rescan_pending = False
        self.debounce_id = 0
        self.last_scan = None
        self.closed = False
        self._run_signature = None
        self.auto = store.get_setting("auto_discover", True)
        self.store.reconcile_runs()
        self.poll_id = GLib.timeout_add_seconds(2, self._poll)
        self.scan_timer = GLib.timeout_add_seconds(60, self._periodic_scan)

    def background(self, work, done=None):
        future = self.pool.submit(work)

        def complete(result):
            try:
                value = result.result()
            except Exception as error:
                GLib.idle_add(self._error, str(error))
                value = None
            if done:
                GLib.idle_add(done, value)
        future.add_done_callback(complete)

    def _error(self, message):
        if not self.closed:
            self.emit("error", message)
        return GLib.SOURCE_REMOVE

    def scan(self):
        if self.closed:
            return
        if self.scanning:
            self.rescan_pending = True
            return
        self.scanning = True
        self.emit("scan-state", True)
        self.background(lambda: discover(self.store), self._scanned)

    def _scanned(self, result):
        if self.closed:
            return GLib.SOURCE_REMOVE
        self.scanning = False
        if result:
            self.last_scan = result
            self.known_paths = {s.path for s in self.store.scripts()}
            if self.auto:
                self._watch(result.directories)
        self.emit("scan-state", False)
        self.emit("changed")
        if self.rescan_pending:
            self.rescan_pending = False
            GLib.idle_add(self.scan)
        return GLib.SOURCE_REMOVE

    def _watch(self, directories):
        scanned = set(directories)
        # Keep the library and configured roots responsive even on very large devices.
        priorities = [str(self.store.scripts_dir), *self.store.roots()]
        ordered = list(dict.fromkeys([p for p in priorities if p in scanned] + directories))
        wanted = set(ordered[:2048])
        # Also watch parents of missing roots, so creating a watched folder is noticed.
        for root in self.store.roots():
            path = Path(root).expanduser()
            if not path.exists() and path.parent.is_dir():
                wanted.add(str(path.parent))
        for path in set(self.monitors) - wanted:
            self.monitors.pop(path).cancel()
        for path in wanted - set(self.monitors):
            try:
                monitor = Gio.File.new_for_path(path).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
                monitor.connect("changed", self._file_changed)
                self.monitors[path] = monitor
            except GLib.Error:
                pass

    def _file_changed(self, monitor, file, other, event):
        if not self.auto:
            return
        # Logs and SQLite updates should not continuously restart discovery.
        path = file.get_path() or ""
        if path.startswith(str(self.store.directory)) and not path.startswith(str(self.store.scripts_dir)):
            return
        candidate = Path(path)
        if (path not in self.known_paths and candidate.suffix.lower() not in SUFFIXES
                and not candidate.is_dir() and candidate.exists() and not detect_language(candidate)):
            return
        if self.debounce_id:
            GLib.source_remove(self.debounce_id)
        self.debounce_id = GLib.timeout_add(1400, self._debounced_scan)

    def _debounced_scan(self):
        self.debounce_id = 0
        self.scan()
        return GLib.SOURCE_REMOVE

    def set_auto(self, enabled):
        self.auto = enabled
        self.store.set_setting("auto_discover", enabled)
        if not enabled:
            for monitor in self.monitors.values():
                monitor.cancel()
            self.monitors.clear()
        else:
            self.scan()
        self.emit("changed")

    def _periodic_scan(self):
        if self.auto:
            self.scan()
        return GLib.SOURCE_CONTINUE

    def _poll(self):
        self.store.reconcile_runs()
        runs = self.store.runs(limit=100)
        signature = [(r["id"], r["status"]) for r in runs]
        if signature != self._run_signature:
            self._run_signature = signature
            self.emit("changed")
        return GLib.SOURCE_CONTINUE

    def save(self, script, callback=None):
        if script.id in self.pending:
            self.emit("message", "A change is already being saved. Try again in a moment.")
            if callback:
                callback(False)
            return
        self.pending.add(script.id)

        def done(result):
            self.pending.discard(script.id)
            self.emit("changed")
            if callback:
                callback(result)
            return GLib.SOURCE_REMOVE

        def work():
            self.scheduler.apply(script)
            return True
        self.background(work, done)

    def set_enabled(self, script, enabled):
        self.save(replace(script, enabled=enabled))

    def run(self, script):
        if script.id in self.processes or script.last_status == "running":
            self.emit("message", "This script is already running.")
            return
        if not script.enabled:
            self.emit("message", "Enable this script before running it.")
            return
        try:
            process = Gio.Subprocess.new(self.scheduler.command(script),
                                         Gio.SubprocessFlags.STDERR_PIPE | Gio.SubprocessFlags.STDOUT_PIPE)
            self.processes[script.id] = process
            process.communicate_utf8_async(None, None, self._run_finished, script.id)
            self.emit("message", f"Running {script.name}…")
            GLib.timeout_add(200, lambda: (self.emit("changed"), GLib.SOURCE_REMOVE)[1])
        except GLib.Error as error:
            self.emit("message", error.message)

    def _run_finished(self, process, result, script_id):
        self.processes.pop(script_id, None)
        try:
            _, stdout, stderr = process.communicate_utf8_finish(result)
            if stderr and not process.get_successful():
                self.emit("message", stderr.strip())
        except GLib.Error as error:
            self.emit("message", error.message)
        self.emit("changed")
        self.emit("run-finished", script_id)

    def cancel(self, script):
        process = self.processes.get(script.id)
        if process:
            import signal
            process.send_signal(signal.SIGTERM)
        else:
            self.background(lambda: self.scheduler._run(
                ["systemctl", "--user", "stop", self.scheduler.unit_name(script) + ".service"]))

    def close(self):
        self.closed = True
        for source in (self.poll_id, self.scan_timer, self.debounce_id):
            if source:
                GLib.source_remove(source)
        for monitor in self.monitors.values():
            monitor.cancel()
        self.pool.shutdown(wait=False)
