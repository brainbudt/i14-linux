#!/usr/bin/env python3
"""Run one shell command on the I14 USB serial console and print its output.
Usage: i14-serial.py PORT 'command' [timeout_seconds]"""
import sys
import time
import uuid

import serial

port, cmd = sys.argv[1], sys.argv[2]
timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 20
s = serial.Serial(port, 115200, timeout=0.2)
s.reset_input_buffer()
tag = "END" + uuid.uuid4().hex[:8]
s.write((cmd + "\necho " + tag[:3] + "''" + tag[3:] + " $?\n").encode())
buf, t0 = b"", time.time()
while time.time() - t0 < timeout:
    buf += s.read(65536)
    text = buf.decode(errors="replace").replace("\r", "")
    done = [l for l in text.split("\n") if l.startswith(tag + " ")]
    if done:
        break
lines = text.split("\n")
print("\n".join(l for l in lines if tag[3:] not in l))
if not done:
    sys.exit("no end marker: console not answering or command still running")
sys.exit(int(done[0].split()[1]))
