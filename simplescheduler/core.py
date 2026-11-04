"""Storage and discovery, deliberately independent of GTK."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

LANGUAGES = {
    "shell": ("Shell", "sh", "utilities-terminal-symbolic"),
    "python": ("Python", "py", "text-x-generic-symbolic"),
    "javascript": ("JavaScript", "js", "text-x-generic-symbolic"),
    "ruby": ("Ruby", "rb", "text-x-generic-symbolic"),
    "perl": ("Perl", "pl", "text-x-generic-symbolic"),
    "other": ("Script", "", "text-x-generic-symbolic"),
}
SUFFIXES = {".sh": "shell", ".bash": "shell", ".zsh": "shell", ".fish": "shell",
            ".py": "python", ".js": "javascript", ".mjs": "javascript",
            ".rb": "ruby", ".pl": "perl"}
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
             "env", ".cache", ".cargo", ".rustup", ".npm", ".nvm", ".gradle",
             ".m2", "site-packages", "vendor", "dist", "build", ".tox", ".local",
             ".codex", ".mozilla", ".var", ".vscode", ".vscode-oss"}
TEMPLATES = {
    "shell": '#!/usr/bin/env bash\nset -euo pipefail\n\n# Make a little time for yourself.\nprintf "Hello from SimpleScheduler!\\n"\n',
    "python": '#!/usr/bin/env python3\n\n\ndef main():\n    print("Hello from SimpleScheduler!")\n\n\nif __name__ == "__main__":\n    main()\n',
    "javascript": '#!/usr/bin/env node\n\nconsole.log("Hello from SimpleScheduler!");\n',
    "ruby": '#!/usr/bin/env ruby\n\nputs "Hello from SimpleScheduler!"\n',
}
SCHEDULES = ("manual", "login", "startup", "hourly", "daily", "weekly", "interval")
SCHEDULE_NAMES = ("Only when I run it", "When I log in", "At system startup", "Every hour",
                  "Every day", "Every week", "At an interval")


@dataclass
class Script:
    id: str
    path: str
    name: str
    language: str
    managed: bool = False
    enabled: bool = False
    schedule: str = "manual"
    time_of_day: str = "09:00"
    interval: int = 60
    missing: bool = False
    added: float = 0
    last_status: str | None = None
    last_run: float | None = None
    user_added: bool = False
    favorite: bool = False
    quick_access: bool = False

    @property
    def in_library(self) -> bool:
        return bool(self.managed or self.user_added or self.enabled or self.schedule != "manual"
                    or self.favorite or self.quick_access)

    @property
    def schedule_label(self) -> str:
        return {"manual": "Manual", "login": "On login", "startup": "At startup",
                "hourly": "Every hour", "daily": f"Daily · {self.time_of_day}",
                "weekly": f"Mondays · {self.time_of_day}",
                "interval": f"Every {self.interval} min"}.get(self.schedule, "Manual")


def short_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def detect_language(path: Path) -> str | None:
    """Detect readable text scripts, including extensionless shebang scripts."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            head = stream.read(512)
        if b"\x00" in head:
            return None
        if path.suffix.lower() in SUFFIXES:
            return SUFFIXES[path.suffix.lower()]
        if head.startswith(b"#!"):
            interpreter = head.splitlines()[0].decode("utf-8", errors="replace")
            for pattern, language in (("python", "python"), ("node", "javascript"),
                                      ("ruby", "ruby"), ("perl", "perl"),
                                      ("sh", "shell"), ("fish", "shell")):
                if pattern in interpreter:
                    return language
            return "other"
    except (OSError, ValueError):
        pass
    return None


def command_for(path: Path, language: str) -> list[str]:
    """Use argv arrays throughout. A script name is never a shell command."""
    if not path.is_file():
        raise ValueError("This script is missing. Restore the file or remove it from the library.")
    with path.open("rb") as stream:
        first_line = stream.readline(4096).decode("utf-8", errors="replace").strip()
    if first_line.startswith("#!"):
        interpreter = shlex.split(first_line[2:].strip())
        if not interpreter:
            raise ValueError("The script has an empty interpreter line.")
        return [*interpreter, str(path)]
    if os.access(path, os.X_OK) and language == "other":
        return [str(path)]
    executable = {"shell": "bash", "python": "python3", "javascript": "node",
                  "ruby": "ruby", "perl": "perl"}.get(language)
    if language == "shell":
        executable = {".zsh": "zsh", ".fish": "fish"}.get(path.suffix.lower(), executable)
    if not executable or not shutil.which(executable):
        raise ValueError(f"Install {executable or 'the script interpreter'} to run this script.")
    return [shutil.which(executable), str(path)]


class Store:
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory or os.environ.get("SIMPLESCHEDULER_DATA_DIR") or
                              Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "simplescheduler")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.scripts_dir = self.directory / "scripts"
        self.scripts_dir.mkdir(exist_ok=True, mode=0o700)
        self.logs_dir = self.directory / "logs"
        self.logs_dir.mkdir(exist_ok=True, mode=0o700)
        self.database = self.directory / "library.db"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS scripts (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    language TEXT NOT NULL, managed INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 0, schedule TEXT NOT NULL DEFAULT 'manual',
                    time_of_day TEXT NOT NULL DEFAULT '09:00', interval INTEGER NOT NULL DEFAULT 60,
                    missing INTEGER NOT NULL DEFAULT 0, added REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, script_id TEXT NOT NULL, name TEXT NOT NULL,
                    started REAL NOT NULL, finished REAL, status TEXT NOT NULL,
                    exit_code INTEGER, log_path TEXT NOT NULL, trigger TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_script ON runs(script_id, started DESC);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ignored (path TEXT PRIMARY KEY);
            """)
            # Additive migration keeps existing schedules, run history, and identities intact.
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(scripts)")}
            migrate_library = "user_added" not in columns
            for column in ("user_added", "favorite", "quick_access"):
                if column not in columns:
                    db.execute(f"ALTER TABLE scripts ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            if migrate_library:
                db.execute("""UPDATE scripts SET user_added=1 WHERE managed=1 OR enabled=1 OR schedule!='manual'
                    OR EXISTS (SELECT 1 FROM runs WHERE runs.script_id=scripts.id)""")
                row = db.execute("SELECT value FROM settings WHERE key='script_groups'").fetchone()
                if row:
                    db.executemany("UPDATE scripts SET user_added=1 WHERE id=?",
                                   [(key,) for key in json.loads(row[0])])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get_setting(self, key: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def roots(self) -> list[str]:
        default = [str(Path.home()), str(Path.home() / ".local/bin"), str(self.scripts_dir),
                   "/usr/local/bin", "/usr/local/sbin", "/usr/bin", "/usr/sbin", "/opt",
                   "/etc/cron.hourly", "/etc/cron.daily", "/etc/cron.weekly", "/etc/cron.monthly"]
        return self.get_setting("roots", default)

    def scripts(self) -> list[Script]:
        with self.connect() as db:
            rows = db.execute("""SELECT s.*,
                (SELECT status FROM runs r WHERE r.script_id=s.id ORDER BY started DESC LIMIT 1) last_status,
                (SELECT started FROM runs r WHERE r.script_id=s.id ORDER BY started DESC LIMIT 1) last_run
                FROM scripts s ORDER BY s.managed DESC, s.name COLLATE NOCASE, s.path""").fetchall()
        return [Script(**dict(r)) for r in rows]

    def library_scripts(self, scripts=None) -> list[Script]:
        show_discovered = self.get_setting("show_discovered", False)
        show_missing = self.get_setting("show_missing", False)
        visible = []
        for script in self.scripts() if scripts is None else scripts:
            if not show_discovered and not script.in_library:
                continue
            script.missing = not Path(script.path).is_file()
            if script.missing and not show_missing:
                continue
            visible.append(script)
        return visible

    def add_to_library(self, script_id: str):
        with self.connect() as db:
            db.execute("UPDATE scripts SET user_added=1 WHERE id=?", (script_id,))

    def rename(self, script_id: str, name: str):
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("Give your script a name between 1 and 120 characters.")
        with self.connect() as db:
            db.execute("UPDATE scripts SET name=?,user_added=1 WHERE id=?", (name, script_id))

    def set_flag(self, script_id: str, flag: str, value: bool):
        if flag not in ("favorite", "quick_access"):
            raise ValueError("Unknown library flag.")
        with self.connect() as db:
            db.execute(f"UPDATE scripts SET {flag}=?,user_added=CASE WHEN ? THEN 1 ELSE user_added END WHERE id=?",
                       (int(value), int(value), script_id))

    def get(self, script_id: str) -> Script | None:
        with self.connect() as db:
            row = db.execute("""SELECT s.*,
                (SELECT status FROM runs r WHERE r.script_id=s.id ORDER BY started DESC LIMIT 1) last_status,
                (SELECT started FROM runs r WHERE r.script_id=s.id ORDER BY started DESC LIMIT 1) last_run
                FROM scripts s WHERE s.id=?""", (script_id,)).fetchone()
        return Script(**dict(row)) if row else None

    def reconcile_runs(self):
        """Recover runs left behind by an interrupted runner, without touching live ones."""
        import fcntl
        with self.connect() as db:
            rows = db.execute("SELECT DISTINCT script_id FROM runs WHERE status='running'").fetchall()
            for row in rows:
                lock_path = self.logs_dir / f"{row['script_id']}.lock"
                with lock_path.open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    db.execute("UPDATE runs SET status='interrupted', finished=? WHERE script_id=? AND status='running'",
                               (time.time(), row["script_id"]))

    def add(self, path: Path, language: str | None = None, managed=False, name=None) -> Script:
        path = path.expanduser().absolute()
        language = language or detect_language(path)
        if not language:
            raise ValueError("Choose a readable shell, Python, JavaScript, Ruby, Perl, or shebang script.")
        with self.connect() as db:
            db.execute("DELETE FROM ignored WHERE path=?", (str(path),))
            db.execute("INSERT OR IGNORE INTO scripts (id,path,name,language,managed,added,user_added) VALUES (?,?,?,?,?,?,1)",
                       (uuid.uuid4().hex, str(path), name or path.stem.replace("_", " ").replace("-", " "),
                        language, int(managed), time.time()))
            db.execute("UPDATE scripts SET user_added=1 WHERE path=?", (str(path),))
            row = db.execute("SELECT id FROM scripts WHERE path=?", (str(path),)).fetchone()
        return self.get(row[0])

    def update(self, script: Script):
        if script.schedule not in SCHEDULES:
            raise ValueError("Unknown schedule.")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", script.time_of_day):
            raise ValueError("Enter a time in 24-hour format, such as 09:00.")
        if not 1 <= int(script.interval) <= 10080:
            raise ValueError("Choose an interval between 1 minute and 7 days (10080 minutes).")
        with self.connect() as db:
            db.execute("""UPDATE scripts SET name=?,enabled=?,schedule=?,time_of_day=?,interval=? WHERE id=?""",
                       (script.name, int(script.enabled), script.schedule, script.time_of_day,
                        int(script.interval), script.id))
            if script.enabled or script.schedule != "manual":
                db.execute("UPDATE scripts SET user_added=1 WHERE id=?", (script.id,))

    def forget(self, script: Script):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO ignored VALUES (?)", (script.path,))
            db.execute("DELETE FROM scripts WHERE id=?", (script.id,))

    def restore(self, script: Script):
        """Restore a removed library record with its original identity and history."""
        with self.connect() as db:
            existing = db.execute("SELECT id FROM scripts WHERE path=?", (script.path,)).fetchone()
            if existing and existing[0] != script.id:
                raise ValueError("This script has already been imported again. Its current library entry was kept.")
            db.execute("DELETE FROM ignored WHERE path=?", (script.path,))
            db.execute("""INSERT OR IGNORE INTO scripts
                (id,path,name,language,managed,enabled,schedule,time_of_day,interval,missing,added,user_added,favorite,quick_access)
                VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?)""",
                (script.id, script.path, script.name, script.language, int(script.managed), script.schedule,
                 script.time_of_day, script.interval, int(not Path(script.path).is_file()), script.added,
                 int(script.user_added), int(script.favorite), int(script.quick_access)))

    def import_scripts(self, paths):
        imported, errors, seen = [], [], set()
        for value in paths:
            path = Path(value).expanduser().absolute()
            if str(path) in seen:
                continue
            seen.add(str(path))
            try:
                if not path.is_file():
                    raise ValueError("Choose a file on this device.")
                imported.append(self.add(path))
            except (OSError, ValueError) as error:
                errors.append((str(path), str(error)))
        return imported, errors

    def create(self, name: str, language: str, content: str, suffix: str | None = None) -> Script:
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("Give your script a name between 1 and 120 characters.")
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()
        if not slug:
            raise ValueError("Include a letter or number in the script name.")
        if suffix and not re.fullmatch(r"\.[a-zA-Z0-9]+", suffix):
            raise ValueError("Choose a valid script file extension.")
        extension = suffix or "." + (LANGUAGES[language][1] or "script")
        path = self.scripts_dir / f"{slug}{extension}"
        # Exclusive creation also protects against overwriting symlink targets.
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return self.add(path, language, managed=True, name=name)

    def runs(self, script_id=None, limit=100) -> list[dict]:
        with self.connect() as db:
            if script_id:
                rows = db.execute("SELECT * FROM runs WHERE script_id=? ORDER BY started DESC LIMIT ?",
                                  (script_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


@dataclass
class ScanResult:
    added: int
    total: int
    directories: list[str]
    errors: int = 0


def discover(store: Store, roots: Iterable[str] | None = None) -> ScanResult:
    """Walk readable locations without following directory symlinks or dependencies.

    Returning every visited directory lets the UI install native file monitors.
    Scans never execute scripts or change an existing schedule.
    """
    found: dict[str, str] = {}
    directories: list[str] = []
    visited: set[str] = set()
    errors = 0
    with store.connect() as db:
        ignored = {r[0] for r in db.execute("SELECT path FROM ignored")}
        existing = {r["path"] for r in db.execute("SELECT path FROM scripts")}

    def onerror(_):
        nonlocal errors
        errors += 1

    for root in roots if roots is not None else store.roots():
        root_path = Path(root).expanduser().absolute()
        if not root_path.exists():
            continue
        for current, dirs, files in os.walk(root_path, followlinks=False, onerror=onerror):
            canonical = os.path.realpath(current)
            if canonical in visited:
                dirs[:] = []
                continue
            visited.add(canonical)
            directories.append(current)
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and
                             not Path(current, d).is_symlink() and
                             str(Path(current, d)) not in ("/proc", "/sys", "/dev", "/run"))
            for filename in sorted(files):
                path = Path(current, filename)
                text_path = str(path)
                if text_path in ignored:
                    continue
                if text_path in existing:
                    found[text_path] = ""
                    continue
                language = detect_language(path)
                if language:
                    found[text_path] = language
    now = time.time()
    added = 0
    with store.connect() as db:
        for path, language in found.items():
            if path not in existing:
                db.execute("INSERT OR IGNORE INTO scripts (id,path,name,language,added) VALUES (?,?,?,?,?)",
                           (uuid.uuid4().hex, path, Path(path).stem.replace("_", " ").replace("-", " "), language, now))
                added += 1
        # Missing is determined from the file itself, independent of scan scope.
        for path in existing:
            db.execute("UPDATE scripts SET missing=? WHERE path=?", (int(not Path(path).is_file()), path))
    return ScanResult(added, len(found), directories, errors)
