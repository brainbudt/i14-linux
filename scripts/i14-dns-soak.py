#!/usr/bin/env python3
"""I14 reserve-DNS soak test (systemd user timer i14-soak.timer on the monitoring host, every 5 minutes;
timestamps use the monitoring host clock, UTC). Report: scripts/i14-dns-soak-report.py. One CSV line per run:
time, then per check: rcode or ERROR and milliseconds. Checks: health name UDP and TCP, a real name
over UDP, a DNSSEC-signed name (wants the AD flag) and a bogus-signed name (wants SERVFAIL)."""
import datetime, os, random, socket, struct, time

SERVER = os.environ.get("I14_DNS")
if not SERVER:
    raise SystemExit("Set I14_DNS to the intended phone address before running this script.")
OUT = os.path.expanduser("~/i14-soak/dns.csv")


def query(name, tcp=False, do=False):
    msg = struct.pack(">HHHHHH", random.randint(0, 65535), 0x0100, 1, 0, 0, 1)
    msg += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    msg += b"\0" + struct.pack(">HHIH", 41, 1232, 0x8000 if do else 0, 0)
    t = time.time()
    try:
        if tcp:
            with socket.create_connection((SERVER, 53), 5) as s:
                s.sendall(struct.pack(">H", len(msg)) + msg)
                n = struct.unpack(">H", s.recv(2))[0]
                r = b""
                while len(r) < n:
                    r += s.recv(n - len(r))
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(5)
                s.sendto(msg, (SERVER, 53))
                r = s.recv(4096)
        fl = struct.unpack(">H", r[2:4])[0]
        code = {0: "NOERROR", 2: "SERVFAIL", 3: "NXDOMAIN", 5: "REFUSED"}.get(fl & 15, str(fl & 15))
        if fl >> 5 & 1:
            code += "+AD"
    except Exception as e:
        code = "ERROR:" + type(e).__name__
    return f"{code},{(time.time() - t) * 1000:.0f}"


checks = [("i14-health.invalid", False, False), ("i14-health.invalid", True, False), ("example.org", False, False),
          ("cloudflare.com", False, True), ("dnssec-failed.org", False, True)]
line = ",".join([datetime.datetime.now().isoformat(timespec="seconds")] + [query(*c) for c in checks])
new = not os.path.exists(OUT)
with open(OUT, "a") as f:
    if new:
        f.write("time,health_udp,ms,health_tcp,ms,example_udp,ms,signed_ad,ms,bogus,ms\n")
    f.write(line + "\n")
