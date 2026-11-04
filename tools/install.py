#!/usr/bin/env python3
"""A dependency-free user installation; does not enable or run any script."""

import argparse
import shutil
import subprocess
from pathlib import Path

APP_ID = "io.github.simplescheduler.SimpleScheduler"
parser = argparse.ArgumentParser()
parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
parser.add_argument("--uninstall", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
prefix = args.prefix.expanduser().resolve()
library = prefix / "share/simplescheduler"
launcher = prefix / "bin/simple-scheduler"
desktop = prefix / f"share/applications/{APP_ID}.desktop"
metadata = prefix / f"share/metainfo/{APP_ID}.metainfo.xml"
icon = prefix / f"share/icons/hicolor/scalable/apps/{APP_ID}.svg"
symbolic = prefix / f"share/icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg"

if args.uninstall:
    # Schedules use the installed runner. Refuse to silently break active schedules.
    from sys import path
    path.insert(0, str(root))
    from simplescheduler.core import Store
    scheduled = [s for s in Store().scripts() if s.enabled and s.schedule != "manual"]
    if scheduled:
        raise SystemExit("Disable your scheduled scripts in SimpleScheduler before uninstalling.")
    for target in (launcher, desktop, metadata, icon, symbolic):
        target.unlink(missing_ok=True)
    if library.exists():
        shutil.rmtree(library)
    print("SimpleScheduler uninstalled. Your script library and history have been kept.")
else:
    library.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / "simplescheduler", library / "simplescheduler", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(root / "data/icons", library / "data/icons", dirs_exist_ok=True)
    shutil.copy2(root / "LICENSE", library / "LICENSE")
    for source, target in [(root / "simplescheduler-launcher", launcher),
                           (root / f"data/{APP_ID}.desktop", desktop),
                           (root / f"data/{APP_ID}.metainfo.xml", metadata),
                           (root / f"data/icons/hicolor/scalable/apps/{APP_ID}.svg", icon),
                           (root / f"data/icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg", symbolic)]:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    launcher.chmod(0o755)
    # An absolute desktop command also works when ~/.local/bin is not in GNOME's PATH.
    command = str(launcher).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    desktop.write_text(desktop.read_text().replace("Exec=simple-scheduler", f'Exec="{command}"'))
    print(f"Installed SimpleScheduler to {prefix}. Launch it from your application grid.")

for command, target in [("update-desktop-database", prefix / "share/applications"),
                         ("gtk-update-icon-cache", prefix / "share/icons/hicolor")]:
    if shutil.which(command):
        subprocess.run([command, "-q", "-f", "--ignore-theme-index", str(target)] if "icon" in command else [command, str(target)],
                       capture_output=True, check=False)
