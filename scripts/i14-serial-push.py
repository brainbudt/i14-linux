#!/usr/bin/env python3
"""Run commands on / copy files to the I14 over the USB serial console (busybox ash only).

For the RAM-booted 7.0 kernel when the Mac's USB network stays down. No base64 there, so a
file goes over as printf hex escapes in short chunks; the result must match sha256 or the
copy is deleted.
  i14-serial-push.py PORT run 'command' [timeout]
  i14-serial-push.py PORT push LOCAL /run/e34/NAME
  i14-serial-push.py PORT pull /run/www/NAME LOCAL     (text files only)"""
import hashlib
import pathlib
import sys
import time
import uuid

import serial

CHUNK = 512  # bytes per printf line (~2 KiB of text, well below the tty line limit)


class Console:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=0.2)
        self.s.reset_input_buffer()

    def run(self, cmd, timeout=30):
        tag = uuid.uuid4().hex[:10]
        # one line; the marker is split so the echoed command line cannot match it
        self.s.write(f"{cmd}; echo I14M''{tag} $?\n".encode())
        buf, t0 = b"", time.time()
        while time.time() - t0 < timeout:
            buf += self.s.read(65536)
            text = buf.decode("utf-8", "replace").replace("\r", "")
            for line in text.split("\n"):
                if line.startswith(f"I14M{tag} "):
                    out = text[:text.index(line)]
                    lines = out.split("\n")[1:]  # drop the echoed command line
                    return int(line.split()[1]), "\n".join(lines).rstrip()
        raise SystemExit(f"no end marker for: {cmd[:80]}")


def push(con, local, remote):
    data = pathlib.Path(local).read_bytes()
    want = hashlib.sha256(data).hexdigest()
    part = remote + ".part"
    rc, out = con.run(f"mkdir -p {remote.rsplit('/', 1)[0]} && : > {part}")
    if rc:
        raise SystemExit(f"cannot create {part}: {out}")
    for i in range(0, len(data), CHUNK):
        esc = "".join(f"\\x{b:02x}" for b in data[i:i + CHUNK])
        rc, out = con.run(f"printf '{esc}' >> {part}", 30)
        if rc:
            raise SystemExit(f"chunk {i // CHUNK} failed: {out}")
    rc, out = con.run(f"sha256sum {part}")
    got = out.split()[0] if out.split() else ""
    if got != want:
        con.run(f": > {part}")  # no rm/mv in this busybox: empty it instead
        raise SystemExit(f"sha256 mismatch for {remote}: {got} != {want}; emptied")
    rc, out = con.run(f"cat {part} > {remote} && : > {part} && sha256sum {remote}")
    got = out.split()[0] if out.split() else ""
    if rc or got != want:
        raise SystemExit(f"final copy of {remote} failed or differs: rc={rc} {out}")
    print(f"pushed {local} -> {remote} ({len(data)} bytes) sha256={want} (verified on the phone)")


def pull(con, remote, local):
    rc, out = con.run(f"cat {remote}", 120)
    if rc:
        raise SystemExit(f"cat {remote} failed: {out}")
    rc2, sha = con.run(f"sha256sum {remote}")
    pathlib.Path(local).write_text(out + "\n")
    print(f"pulled {remote} -> {local} ({len(out)} chars); phone sha256 {sha.split()[0] if sha else '?'}")


def main():
    port, action = sys.argv[1], sys.argv[2]
    con = Console(port)
    if action == "run":
        rc, out = con.run(sys.argv[3], float(sys.argv[4]) if len(sys.argv) > 4 else 30)
        print(out)
        sys.exit(rc)
    if action == "push":
        push(con, sys.argv[3], sys.argv[4])
    elif action == "pull":
        pull(con, sys.argv[3], sys.argv[4])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
