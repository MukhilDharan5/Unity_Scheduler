"""Organize script files without executing them or changing their schedules."""

from __future__ import annotations

import configparser
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScriptGroup:
    key: str
    title: str
    icon: str
    description: str
    application: bool = False


BASE_GROUPS = (
    ScriptGroup("user", "User", "avatar-default-symbolic", "Your personal scripts and automations."),
    ScriptGroup("system", "System", "preferences-system-symbolic", "Locally installed tools and system administration scripts."),
    ScriptGroup("kernel", "Kernel", "computer-chip-symbolic", "Kernel, boot image, and kernel module tools."),
    ScriptGroup("os", "Operating System", "computer-symbolic", "Scripts supplied with your Linux operating system."),
    ScriptGroup("applications", "Applications", "application-x-executable-symbolic", "Scripts organized by the app or project they belong to."),
)
PROJECT_CONTAINERS = {"repos", "repositories", "projects", "projects_linux", "developer", "development", "code", "src", "workspace", "workspaces"}
GENERIC_COMMANDS = {"env", "sh", "bash", "dash", "python", "python3", "node", "ruby", "perl", "flatpak", "gjs", "xdg-open"}
KERNEL_PREFIXES = ("/boot", "/usr/lib/modules", "/lib/modules", "/usr/src/kernels", "/usr/src/linux",
                   "/usr/lib/kernel", "/etc/kernel", "/usr/lib/dracut", "/etc/dracut.conf.d",
                   "/usr/share/initramfs-tools", "/etc/initramfs-tools")
KERNEL_COMMANDS = ("akmods", "dracut", "mkinitrd", "mkinitcpio", "update-initramfs", "kernel-install",
                   "installkernel", "kmod", "depmod", "modprobe", "grub2-get-kernel-settings")


def within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def human_name(value: str) -> str:
    name = re.sub(r"[-_]+", " ", value).strip()
    return " ".join(word if any(c.isupper() for c in word) else word.capitalize() for word in name.split())


class GroupCatalog:
    def __init__(self, store, home=None, desktop_dirs=None):
        self.store = store
        self.home = Path(home or Path.home())
        if desktop_dirs is None:
            data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            bases = [data_home, *[Path(p) for p in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if p]]
            desktop_dirs = [base / "applications" for base in bases]
        self.desktop_dirs = desktop_dirs
        self.refresh()

    def refresh(self):
        self.groups = {g.key: g for g in BASE_GROUPS}
        self.aliases = {}
        self.cache = {}
        self.project_cache = {}
        self.overrides = self.store.get_setting("script_groups", {})
        self.rules = self.store.get_setting("group_folders", {})
        self._load_desktops()
        for key, metadata in self.store.get_setting("saved_groups", {}).items():
            if key.startswith("app:") and key not in self.groups:
                self.groups[key] = ScriptGroup(key, metadata["title"], metadata["icon"],
                                               f"Scripts belonging to {metadata['title']}.", True)
        for name in self.store.get_setting("custom_groups", []):
            self._application(name)

    def _load_desktops(self):
        seen = set()
        for directory in self.desktop_dirs:
            for file in sorted(Path(directory).glob("*.desktop")):
                if file.name in seen:
                    continue
                seen.add(file.name)
                try:
                    config = configparser.ConfigParser(interpolation=None, strict=False)
                    config.read(file, encoding="utf-8")
                    entry = config["Desktop Entry"]
                    if entry.get("Hidden", "false").lower() == "true" or entry.get("Type", "Application") != "Application":
                        continue
                    title = entry.get("Name", "").strip()
                    if not title:
                        continue
                    key = "app:" + file.stem
                    group = ScriptGroup(key, title, entry.get("Icon") or "application-x-executable-symbolic",
                                        f"Scripts belonging to {title}.", True)
                    self.groups[key] = group
                    aliases = [title, file.stem.split(".")[-1]]
                    command = shlex.split(entry.get("Exec", ""))
                    for part in command:
                        name = Path(part).name
                        if part.startswith(("-", "%")) or "=" in part or name in GENERIC_COMMANDS:
                            continue
                        aliases.append(Path(name).stem if name.endswith((".py", ".sh", ".js")) else name)
                        # -m module names and absolute script arguments also identify an app.
                        if "/" not in part:
                            aliases.append(part.split(".")[0])
                        break
                    for alias in aliases:
                        token = normalized(alias)
                        if len(token) >= 4 and token not in GENERIC_COMMANDS:
                            self.aliases.setdefault(token, key)
                except (configparser.Error, OSError, UnicodeError, ValueError, KeyError):
                    continue

    def _match_application(self, names):
        for name in names:
            token = normalized(name)
            if token in self.aliases:
                return self.groups[self.aliases[token]]
            # A title such as ChatGPT can also identify chatgpt-titlebar or chatgpt-1.2.
            pieces = re.split(r"[-_. ]+", name.lower())
            for length in range(len(pieces) - 1, 0, -1):
                prefix = normalized("".join(pieces[:length]))
                if prefix in self.aliases:
                    return self.groups[self.aliases[prefix]]
        return None

    def _application(self, name, icon=None):
        existing = self._match_application([name])
        if existing:
            return existing
        key = "app:" + (normalized(name) or "other")
        if key not in self.groups:
            self.groups[key] = ScriptGroup(key, name, icon or "application-x-executable-symbolic",
                                           f"Scripts belonging to {name}.", True)
        return self.groups[key]

    def _project(self, directory):
        key = str(directory)
        if key in self.project_cache:
            return self.project_cache[key]
        # Desktop metadata bundled with a project is the best display name.
        candidates = [*sorted(directory.glob("*.desktop")), *sorted((directory / "data").glob("*.desktop"))]
        group = self._match_application([directory.name])
        if group is None:
            for file in candidates:
                try:
                    config = configparser.ConfigParser(interpolation=None, strict=False)
                    config.read(file, encoding="utf-8")
                    name = config["Desktop Entry"].get("Name", "").strip()
                    if name:
                        group = self._application(name, config["Desktop Entry"].get("Icon"))
                        break
                except (configparser.Error, KeyError, OSError, UnicodeError):
                    continue
        if group is None:
            name = directory.name
            package = directory / "package.json"
            try:
                if package.is_file() and package.stat().st_size < 256 * 1024:
                    metadata = json.loads(package.read_text())
                    name = metadata.get("productName") or metadata.get("name") or name
            except (ValueError, OSError, UnicodeError):
                pass
            group = self._application(human_name(name))
        self.project_cache[key] = group
        return group

    def automatic(self, script):
        path = script.path
        if script.managed:
            return self.groups["user"]
        if path in self.cache:
            return self.cache[path]
        group = self._classify(path)
        self.cache[path] = group
        return group

    def _classify(self, path):
        file = Path(path)
        # Folder assignments are explicit and use whole path components.
        for root, name in sorted(self.rules.items(), key=lambda item: len(item[0]), reverse=True):
            if within(path, root):
                return self._application(name)
        if within(path, str(self.home)):
            parts = file.relative_to(self.home).parts
            if len(parts) >= 3 and parts[0].lower() in PROJECT_CONTAINERS:
                return self._project(self.home / parts[0] / parts[1])
            # Config directories identify applications, while generic user helpers stay personal.
            if len(parts) >= 3 and parts[0] == ".config" and parts[1] not in ("systemd", "autostart", "environment.d"):
                return self._match_application([parts[1]]) or self._application(human_name(parts[1]))
            match = self._match_application(parts[:-1])
            if match:
                return match
            return self.groups["user"]
        if any(within(path, root) for root in KERNEL_PREFIXES):
            return self.groups["kernel"]
        system_command = any(within(path, root) for root in ("/usr/bin", "/usr/sbin", "/bin", "/sbin"))
        if system_command and any(file.name == name or file.name.startswith(name + "-") or
                                  (name in ("akmods", "kmod") and file.name.startswith(name)) for name in KERNEL_COMMANDS):
            return self.groups["kernel"]
        # Resolve known application folders and launchers before general OS paths.
        match = self._match_application([file.stem, *reversed(file.parts[:-1])])
        if match:
            return match
        if within(path, "/opt") and len(file.parts) >= 4:
            return self._application(human_name(file.parts[2]))
        if within(path, "/usr/local") or any(within(path, root) for root in ("/etc", "/var", "/srv")):
            return self.groups["system"]
        if any(within(path, root) for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64")):
            return self.groups["os"]
        return self.groups["system"]

    def classify(self, script):
        override = self.overrides.get(script.id)
        return self.groups[override] if override in self.groups and override != "applications" else self.automatic(script)

    def assign(self, script, key=None):
        if key is None:
            self.overrides.pop(script.id, None)
        elif key in self.groups and key != "applications":
            self.overrides[script.id] = key
            group = self.groups[key]
            if group.application:
                saved = self.store.get_setting("saved_groups", {})
                saved[key] = {"title": group.title, "icon": group.icon}
                self.store.set_setting("saved_groups", saved)
        else:
            raise ValueError("Choose a script group.")
        self.store.set_setting("script_groups", self.overrides)

    def create_group(self, name, folder=None):
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("Give the group a name between 1 and 80 characters.")
        names = self.store.get_setting("custom_groups", [])
        if name not in names:
            self.store.set_setting("custom_groups", names + [name])
        if folder:
            self.rules[str(Path(folder).expanduser().absolute())] = name
            self.store.set_setting("group_folders", self.rules)
            self.cache.clear()
        return self._application(name)

    def organize(self, scripts):
        # Build inferred project groups before resolving overrides that refer to them.
        for script in scripts:
            self.automatic(script)
        assignments = {script.id: self.classify(script) for script in scripts}
        counts = {}
        for group in assignments.values():
            counts[group.key] = counts.get(group.key, 0) + 1
        custom_names = self.store.get_setting("custom_groups", [])
        applications = sorted((g for g in self.groups.values() if g.application and
                               (g.key in counts or g.title in custom_names)),
                              key=lambda g: g.title.casefold())
        return assignments, counts, applications
