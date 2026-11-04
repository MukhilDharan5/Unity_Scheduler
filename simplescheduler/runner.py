#!/usr/bin/env python3
"""Standalone runner shared by the app and systemd; uses only the standard library."""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simplescheduler.core import Store, command_for

MAX_LOG_BYTES = 2 * 1024 * 1024


def run(data_dir: Path, script_id: str, trigger="manual") -> int:
    store = Store(data_dir)
    script = store.get(script_id)
    if not script:
        print("This script is no longer in the library.", file=sys.stderr)
        return 1
    if not script.enabled:
        print("This script is disabled.", file=sys.stderr)
        return 1
    lock = (store.logs_dir / f"{script.id}.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("This script is already running.", file=sys.stderr)
        lock.close()
        return 75
    run_id = uuid.uuid4().hex
    log_path = store.logs_dir / f"{run_id}.log"
    with store.connect() as db:
        # A lock acquired successfully also means a previous runner is no longer alive.
        db.execute("UPDATE runs SET status='interrupted',finished=? WHERE script_id=? AND status='running'",
                   (time.time(), script.id))
        db.execute("INSERT INTO runs (id,script_id,name,started,status,log_path,trigger) VALUES (?,?,?,?,?,?,?)",
                   (run_id, script.id, script.name, time.time(), "running", str(log_path), trigger))
    process = None
    cancelled = False

    def terminate(signum, _frame):
        nonlocal cancelled
        cancelled = True
        if process:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            signal.alarm(5)

    def kill(_signum, _frame):
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGALRM, kill)
    exit_code = 1
    try:
        with log_path.open("wb") as output:
            try:
                process = subprocess.Popen(command_for(Path(script.path), script.language),
                                           cwd=Path(script.path).parent, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                size = 0
                while chunk := os.read(process.stdout.fileno(), 8192):
                    if size < MAX_LOG_BYTES:
                        output.write(chunk[:MAX_LOG_BYTES - size])
                        output.flush()
                    size += len(chunk)
                if size > MAX_LOG_BYTES:
                    output.write(b"\n[Output truncated at 2 MiB]\n")
                exit_code = process.wait()
            except Exception as error:
                output.write((str(error) + "\n").encode("utf-8", errors="replace"))
        status = "cancelled" if cancelled else "success" if exit_code == 0 else "failed"
    finally:
        signal.alarm(0)
        with store.connect() as db:
            db.execute("UPDATE runs SET finished=?, status=?, exit_code=? WHERE id=?",
                       (time.time(), locals().get("status", "interrupted"), exit_code, run_id))
        lock.close()
    return exit_code if exit_code >= 0 else 128 - exit_code


def main():
    parser = argparse.ArgumentParser(description="Run a SimpleScheduler script and record its output.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--trigger", choices=("manual", "scheduled"), default="manual")
    args = parser.parse_args()
    return run(args.data_dir, args.id, args.trigger)


if __name__ == "__main__":
    raise SystemExit(main())
