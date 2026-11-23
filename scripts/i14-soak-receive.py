#!/usr/bin/env python3
"""Receive one closed-phase export through an owner-supplied foreground transport."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


spec = importlib.util.spec_from_file_location("soak_export", Path(__file__).with_name("i14-soak-observer-export.py"))
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


class TransportStream:
    def __init__(self, pipe, selector, deadline):
        self.pipe = pipe
        self.selector = selector
        self.deadline = deadline

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("phase transport deadline")
        return left

    def read(self, size):
        if not 0 < size <= export.CHUNK:
            raise ValueError("invalid transport read size")
        if not self.selector.select(self.remaining()):
            raise TimeoutError("phase transport stalled")
        return os.read(self.pipe.fileno(), size)


def receive(command, destination, binding, seconds):
    export.binding_bytes(binding)
    if not command or not 0 < seconds <= 900:
        raise ValueError("foreground command and timeout (0, 900] required")
    destination.mkdir(mode=0o700)
    deadline = time.monotonic() + seconds
    # The transport inherits stdin (reviewed producer inputs) and stderr (owner's private log).
    with selectors.DefaultSelector() as selector, subprocess.Popen(
            command, stdout=subprocess.PIPE, start_new_session=True) as child:
        try:
            selector.register(child.stdout, selectors.EVENT_READ)
            stream = TransportStream(child.stdout, selector, deadline)
            manifest = export.unpack_tar(stream, destination / "raw", binding)
            if child.wait(timeout=stream.remaining()) != 0:
                raise ValueError(f"export transport exited {child.returncode}")
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise ValueError("export transport left a process group")
        except BaseException:
            child.poll()  # Reap an already-exited leader before examining its group.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin may report EPERM for a leader that just became a zombie.
                if child.poll() is None:
                    raise
                try:
                    os.killpg(child.pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise
            child.wait()
            raise
    receipt = dict(binding=binding, transport_exit=0, files=len(manifest["files"]),
                   bytes=manifest["total_bytes"], manifest=manifest)
    pending = destination / "receipt.partial"
    with pending.open("x") as output:
        json.dump(receipt, output, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    pending.rename(destination / "verified.json")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout", required=True, type=int)
    parser.add_argument("command", nargs="+")
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    receive(args.command, args.out, json.loads(args.binding.read_text()), args.timeout)


if __name__ == "__main__":
    main()
