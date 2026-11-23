#!/usr/bin/env python3
"""I14 reserve-DNS soak test (systemd user timer i14-soak.timer on the monitoring host, every 5 minutes;
timestamps use the monitoring host clock, UTC). Report: scripts/i14-dns-soak-report.py. One CSV line per run:
time, then per check: rcode or ERROR and milliseconds. Checks: health name UDP and TCP, a real name
over UDP, a DNSSEC-signed name (wants the AD flag) and a bogus-signed name (wants SERVFAIL)."""
import datetime, os, random, socket, struct, time

SERVER = os.environ.get("I14_DNS")
OUT = os.path.expanduser("~/i14-soak/dns.csv")


def receive(s, size, deadline):
    data = b""
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("DNS deadline")
        s.settimeout(remaining)
        block = s.recv(size - len(data))
        if not block:
            raise EOFError("incomplete DNS TCP frame")
        data += block
    return data


def dns_name(data, offset):
    labels, seen, end, size = [], set(), None, 1
    while True:
        if offset >= len(data) or offset in seen:
            raise ValueError("short or cyclic DNS name")
        seen.add(offset)
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("short DNS pointer")
            if end is None:
                end = offset + 2
            offset = (length & 0x3F) << 8 | data[offset + 1]
            continue
        offset += 1
        if length > 63 or offset + length > len(data):
            raise ValueError("bad DNS label")
        if length == 0:
            return b".".join(labels).lower(), end if end is not None else offset
        size += length + 1
        if size > 255:
            raise ValueError("oversized DNS name")
        labels.append(data[offset:offset + length])
        offset += length


def response(r, msg, name):
    if len(r) < 12:
        raise ValueError("short DNS header")
    ident, fl, questions, answers, _, _ = struct.unpack(">HHHHHH", r[:12])
    # The request ends in an 11-byte OPT record; require the echoed A/IN question.
    question = msg[12:-11]
    if (ident != struct.unpack(">H", msg[:2])[0] or fl & 0xFA00 != 0x8000 or
            questions != 1 or r[12:12 + len(question)] != question):
        raise ValueError("unrelated or truncated DNS response")
    offset, addresses, aliases = 12 + len(question), [], {}
    for _ in range(answers):
        owner, offset = dns_name(r, offset)
        if offset + 10 > len(r):
            raise ValueError("short DNS record")
        kind, cls, _, size = struct.unpack(">HHIH", r[offset:offset + 10])
        offset += 10
        if offset + size > len(r):
            raise ValueError("short DNS data")
        if kind == 1 and cls == 1:
            if size != 4:
                raise ValueError("invalid A record length")
            addresses.append((owner, socket.inet_ntoa(r[offset:offset + size])))
        elif kind == 5 and cls == 1:
            target, end = dns_name(r, offset)
            if end != offset + size or owner in aliases and aliases[owner] != target:
                raise ValueError("invalid CNAME record")
            aliases[owner] = target
        offset += size
    owners = {name.rstrip(".").encode("ascii").lower()}
    if name != "i14-health.invalid":
        # Real-name probes may legitimately reach their A records through CNAMEs.
        while more := {target for owner, target in aliases.items() if owner in owners} - owners:
            owners.update(more)
    matching = [address for owner, address in addresses if owner in owners]
    if fl & 15 == 0 and (not matching or name == "i14-health.invalid" and "192.0.2.1" not in matching):
        raise ValueError("missing expected A answer")
    return fl


def query(name, tcp=False, do=False):
    msg = struct.pack(">HHHHHH", random.randint(0, 65535), 0x0100, 1, 0, 0, 1)
    msg += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    msg += b"\0" + struct.pack(">HHIH", 41, 1232, 0x8000 if do else 0, 0)
    t = time.monotonic()
    try:
        if tcp:
            with socket.create_connection((SERVER, 53), 5) as s:
                remaining = t + 5 - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("DNS deadline")
                s.settimeout(remaining)
                s.sendall(struct.pack(">H", len(msg)) + msg)
                n = struct.unpack(">H", receive(s, 2, t + 5))[0]
                r = receive(s, n, t + 5)
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(5)
                s.connect((SERVER, 53))
                s.send(msg)
                r = s.recv(4096)
        fl = response(r, msg, name)
        code = {0: "NOERROR", 2: "SERVFAIL", 3: "NXDOMAIN", 5: "REFUSED"}.get(fl & 15, str(fl & 15))
        if fl >> 5 & 1:
            code += "+AD"
    except Exception as e:
        code = "ERROR:" + type(e).__name__
    return f"{code},{(time.monotonic() - t) * 1000:.0f}"


def main():
    if not SERVER:
        raise SystemExit("Set I14_DNS to the intended phone's IPv4 address")
    checks = [("i14-health.invalid", False, False), ("i14-health.invalid", True, False), ("example.org", False, False),
              ("cloudflare.com", False, True), ("dnssec-failed.org", False, True)]
    line = ",".join([datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")] + [query(*c) for c in checks])
    new = not os.path.exists(OUT)
    with open(OUT, "a") as f:
        if new:
            f.write("time,health_udp,ms,health_tcp,ms,example_udp,ms,signed_ad,ms,bogus,ms\n")
        f.write(line + "\n")


if __name__ == "__main__":
    main()
