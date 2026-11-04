"""Per-user systemd integration. No scripts are enabled during discovery."""

from __future__ import annotations

import os
import pwd
import re
import subprocess
import sys
from pathlib import Path

from .core import Script, Store


def unit_quote(value: str) -> str:
    """Escape a single systemd command argument, including specifier expansion."""
    if any(ord(c) < 32 for c in value):
        raise ValueError("Paths must not contain control characters.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


class Scheduler:
    def __init__(self, store: Store, unit_dir: Path | None = None, run=None):
        self.store = store
        self.unit_dir = unit_dir or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
        self._run = run or self._command

    @staticmethod
    def _command(argv):
        result = subprocess.run(argv, text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "The scheduling service is unavailable.")
        return result.stdout.strip()

    @staticmethod
    def unit_name(script: Script):
        if len(script.id) != 32 or any(c not in "0123456789abcdef" for c in script.id):
            raise ValueError("Invalid script identifier.")
        return f"simplescheduler-{script.id}"

    def command(self, script: Script, trigger="manual") -> list[str]:
        return [sys.executable, str(Path(__file__).with_name("runner.py").resolve()),
                "--data-dir", str(self.store.directory.resolve()), "--id", script.id, "--trigger", trigger]

    def service_text(self, script: Script):
        argv = " ".join(unit_quote(a) for a in self.command(script, "scheduled"))
        text = "[Unit]\nDescription=SimpleScheduler script\n"
        if script.schedule == "login":
            text += "PartOf=graphical-session.target\nAfter=graphical-session-pre.target\n"
        text += f"\n[Service]\nType=oneshot\nExecStart={argv}\nTimeoutStartSec=infinity\nKillMode=control-group\n"
        if script.schedule in ("login", "startup"):
            target = "graphical-session.target" if script.schedule == "login" else "default.target"
            text += f"\n[Install]\nWantedBy={target}\n"
        return text

    def timer_text(self, script: Script):
        name = self.unit_name(script)
        when = {
            "hourly": "OnCalendar=hourly\nPersistent=true",
            "daily": f"OnCalendar=*-*-* {script.time_of_day}:00\nPersistent=true",
            "weekly": f"OnCalendar=Mon *-*-* {script.time_of_day}:00\nPersistent=true",
            "interval": f"OnActiveSec={script.interval}min\nOnUnitActiveSec={script.interval}min",
        }.get(script.schedule)
        if not when:
            return None
        return (f"[Unit]\nDescription=SimpleScheduler timer\n\n[Timer]\n{when}\n"
                f"AccuracySec=1s\nUnit={name}.service\n\n[Install]\nWantedBy=timers.target\n")

    def lingering(self) -> bool:
        username = pwd.getpwuid(os.getuid()).pw_name
        return self._run(["loginctl", "show-user", username, "--property=Linger", "--value"]) == "yes"

    def enable_background(self):
        username = pwd.getpwuid(os.getuid()).pw_name
        self._run(["loginctl", "enable-linger", username])

    def apply(self, script: Script):
        """Apply then persist, restoring the previous schedule on failure."""
        previous = self.store.get(script.id)
        if previous is None:
            raise ValueError("This script is no longer in your library.")
        # Validate the candidate before touching units.
        if script.schedule not in ("manual", "login", "startup", "hourly", "daily", "weekly", "interval"):
            raise ValueError("Unknown schedule.")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", script.time_of_day):
            raise ValueError("Enter a time in 24-hour format, such as 09:00.")
        if not 1 <= int(script.interval) <= 10080:
            raise ValueError("Choose an interval between 1 and 10080 minutes.")
        if script.enabled and not Path(script.path).is_file():
            raise ValueError("Restore the missing script before enabling it.")
        if script.enabled and script.schedule == "startup" and not self.lingering():
            raise ValueError("Enable background startup in Preferences first. This lets your scripts run before login.")
        # Manual-only changes need no systemd connection unless replacing a schedule.
        if script.schedule == "manual" and previous.schedule == "manual":
            self.store.update(script)
            return
        try:
            self._install(script)
            self.store.update(script)
        except Exception:
            try:
                self._install(previous)
            except Exception:
                pass
            raise

    def _install(self, script: Script):
        name = self.unit_name(script)
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        # Disable old triggers; do not terminate a currently running script.
        for extension in ("timer", "service"):
            file = self.unit_dir / f"{name}.{extension}"
            if file.exists():
                self._run(["systemctl", "--user", "disable", f"{name}.{extension}"])
        timer = self.unit_dir / f"{name}.timer"
        if timer.exists():
            self._run(["systemctl", "--user", "stop", f"{name}.timer"])
            timer.unlink()
        service = self.unit_dir / f"{name}.service"
        if script.enabled and script.schedule != "manual":
            self._atomic_write(service, self.service_text(script))
            timer_text = self.timer_text(script)
            if timer_text:
                self._atomic_write(timer, timer_text)
            self._run(["systemctl", "--user", "daemon-reload"])
            if timer_text:
                self._run(["systemctl", "--user", "enable", "--now", timer.name])
            else:
                # Enabling login/startup must not run the script immediately.
                self._run(["systemctl", "--user", "enable", service.name])
        else:
            service.unlink(missing_ok=True)
            self._run(["systemctl", "--user", "daemon-reload"])

    @staticmethod
    def _atomic_write(path: Path, text: str):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def remove(self, script: Script):
        from dataclasses import replace
        if script.schedule != "manual":
            self.apply(replace(script, enabled=False))
        self.store.forget(script)
