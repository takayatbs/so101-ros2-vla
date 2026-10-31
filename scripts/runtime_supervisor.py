#!/usr/bin/env python3
"""Own the runtime lock and forward terminal shutdown to the launch group."""
import argparse
import fcntl
import os
import signal
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["run", "stop"])
    parser.add_argument("session")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.session.isalnum():
        raise ValueError("Invalid session identifier")
    folder = Path("/workspace/data/run")
    folder.mkdir(parents=True, exist_ok=True)
    pidfile = folder/(args.session+".pid")
    if args.operation == "stop":
        if pidfile.exists():
            try: os.killpg(int(pidfile.read_text()), signal.SIGINT)
            except ProcessLookupError: pass
        return
    with Path("/workspace/data/.compute.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Runtime/training is already using Robot Compute")
        proc = subprocess.Popen(args.command, start_new_session=True)
        pidfile.write_text(str(proc.pid))
        def stop(signum, frame):
            if proc.poll() is None: os.killpg(proc.pid, signal.SIGINT)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        try:
            result = proc.wait()
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGINT)
                try: proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            pidfile.unlink(missing_ok=True)
        raise SystemExit(result)


if __name__ == "__main__":
    main()
