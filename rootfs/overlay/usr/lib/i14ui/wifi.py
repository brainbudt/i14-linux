"""Wi-Fi settings through wpa_supplicant's control socket (/run/wpa_supplicant/wlan0).

The only Wi-Fi path of the UI: it never touches the driver or wmt_loader (a second wmt_loader
crashes the kernel). Networks the user adds are saved to the userdata file that /etc/init.d/i14-wifi
merges at start (update_config=0 stays); passwords are stored as the derived 256-bit PSK, not as text.
Every change is written to userdata first and then to wpa_supplicant's start file (i14-wifi-conf), so a
respawned wpa_supplicant starts with the same networks. Wi-Fi "off" = a flag file on userdata: the start
file then marks every network disabled, and i14-netwatch leaves Wi-Fi alone.
"""
import hashlib
import os
import socket
import subprocess
import tempfile
import time

from .settings_files import ensure_private_directory, open_private_settings

CTRL = "/run/wpa_supplicant/wlan0"
IFACE = "wlan0"
DATA = "/mnt/data/media/0/i14-linux"
NETWORKS = DATA + "/wpa_supplicant.conf"
ROOTFS_NETWORKS = "/etc/wpa_supplicant/wpa_supplicant.conf"
OFF_FLAG = DATA + "/wifi-off"
UDHCPC_PID = "/run/i14-udhcpc-wlan0.pid"
CONF_TOOL = "/usr/sbin/i14-wifi-conf"
ASSOC_TIMEOUT, DHCP_TIMEOUT = 30, 25


class CtrlError(OSError):
    pass


class Ctrl:
    """One client socket to wpa_supplicant (requests, or events after attach())."""

    _n = 0

    def __init__(self, path=CTRL, local_dir="/run"):
        Ctrl._n += 1
        self.path = path
        self.local = f"{local_dir}/i14-ui-wpa-{os.getpid()}-{Ctrl._n}"
        self.sock = None

    def open(self):
        if self.sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                os.unlink(self.local)
            except OSError:
                pass
            try:
                s.bind(self.local)
                s.connect(self.path)
            except OSError as e:
                s.close()
                self._unlink()
                raise CtrlError(str(e)) from e
            self.sock = s
        return self.sock

    def _unlink(self):
        try:
            os.unlink(self.local)
        except OSError:
            pass

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        self._unlink()

    def fileno(self):
        return self.sock.fileno()

    def request(self, cmd, timeout=1.0):  # short: the screen waits for it
        s = self.open()
        try:
            s.settimeout(timeout)
            s.send(cmd.encode())
            end = time.monotonic() + timeout
            while True:
                s.settimeout(max(0.05, end - time.monotonic()))
                reply = s.recv(65536).decode("utf-8", "replace")
                if not reply.startswith("<"):  # skip unsolicited events on this socket
                    return reply
        except OSError as e:
            self.close()
            raise CtrlError(f"{cmd.split()[0]}: {e}") from e

    def attach(self):
        if self.request("ATTACH").strip() != "OK":
            raise CtrlError("ATTACH refused")
        self.sock.setblocking(False)

    def events(self):
        out = []
        while True:
            try:
                out.append(self.sock.recv(65536).decode("utf-8", "replace"))
            except BlockingIOError:
                return out
            except OSError:
                self.close()
                return out


# --- parsing -------------------------------------------------------------------------------------
def decode_ssid(text):
    """wpa_supplicant's printf_encode()d SSID back to bytes."""
    out, i, raw = bytearray(), 0, text.encode("utf-8", "surrogateescape")
    simple = {ord("\\"): 0x5C, ord('"'): 0x22, ord("n"): 0x0A, ord("r"): 0x0D, ord("t"): 0x09, ord("e"): 0x1B}
    while i < len(raw):
        c = raw[i]
        if c == 0x5C and i + 1 < len(raw):
            n = raw[i + 1]
            if n == ord("x") and i + 3 < len(raw):
                try:
                    out.append(int(raw[i + 2:i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
            if n in simple:
                out.append(simple[n])
                i += 2
                continue
        out.append(c)
        i += 1
    return bytes(out)


def show_ssid(ssid):
    text = ssid.decode("utf-8", "replace")
    return "".join(ch if ch.isprintable() else "?" for ch in text) or "(verborgen)"


def parse_kv(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def security(flags):
    if "EAP" in flags:
        return "eap"
    if "PSK" in flags:
        return "psk"
    if "SAE" in flags:
        return "sae"
    if "WEP" in flags:
        return "wep"
    return "open"


def parse_scan(text):
    """Strongest entry per SSID: [{"ssid": bytes, "signal": dBm, "security": ..., "flags": ...}]."""
    best = {}
    for line in text.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 5 or not parts[4]:
            continue
        try:
            signal = int(parts[2])
        except ValueError:
            continue
        ssid = decode_ssid(parts[4])
        if ssid.strip(b"\0") == b"":
            continue
        entry = {"ssid": ssid, "signal": signal, "security": security(parts[3]), "flags": parts[3],
                 "freq": parts[1]}
        if ssid not in best or signal > best[ssid]["signal"]:
            best[ssid] = entry
    return sorted(best.values(), key=lambda e: -e["signal"])


def parse_networks(text):
    """LIST_NETWORKS: [{"id": int, "ssid": bytes, "flags": str}]."""
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit():
            out.append({"id": int(parts[0]), "ssid": decode_ssid(parts[1]),
                        "flags": parts[3] if len(parts) > 3 else ""})
    return out


def bars(dbm):
    return 4 if dbm >= -55 else 3 if dbm >= -67 else 2 if dbm >= -75 else 1 if dbm >= -85 else 0


def psk_hex(passphrase, ssid):
    """The WPA2 PSK (what wpa_passphrase computes): PBKDF2-HMAC-SHA1, 4096 rounds, 32 bytes."""
    return hashlib.pbkdf2_hmac("sha1", passphrase.encode(), ssid, 4096, 32).hex()


# --- the saved networks file ---------------------------------------------------------------------
def _value_ssid(value):
    value = value.strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value[1:-1].encode()
    if value.startswith("P\""):
        return decode_ssid(value[2:-1])
    try:
        return bytes.fromhex(value)
    except ValueError:
        return value.encode()


class Store:
    """network={...} blocks of a wpa_supplicant config file; other lines are kept as they are."""

    def __init__(self, path, *, protect_parent=True):
        self.path = path
        self.protect_parent = protect_parent

    def _parts(self):
        try:
            with open_private_settings(self.path, protect_parent=self.protect_parent) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return []
        parts, block = [], None
        for line in lines:
            s = line.strip()
            if block is None and s.startswith("network={"):
                block = [line]
            elif block is not None:
                block.append(line)
                if s == "}":
                    parts.append(("net", block))
                    block = None
            else:
                parts.append(("line", [line]))
        if block is not None:
            parts.append(("line", block))  # unterminated: keep as it is
        return parts

    @staticmethod
    def _ssid(block):
        for line in block:
            s = line.strip()
            if s.startswith("ssid="):
                return _value_ssid(s[5:])
        return None

    def networks(self):
        out = []
        for kind, lines in self._parts():
            if kind == "net":
                prio = 0
                for line in lines:
                    s = line.strip()
                    if s.startswith("priority="):
                        try:
                            prio = int(s[9:])
                        except ValueError:
                            pass
                out.append({"ssid": self._ssid(lines), "priority": prio})
        return out

    def _write(self, parts):
        text = "".join(line + "\n" for _, lines in parts for line in lines)
        d = os.path.dirname(self.path) or "."
        if self.protect_parent:
            ensure_private_directory(d)
        else:
            os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".wpa-", dir=d)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def put(self, ssid, key_mgmt, psk=None, priority=None, hidden=False):
        """Add or replace the network for ssid; returns the block lines."""
        block = ["network={", f"\t# {show_ssid(ssid)}", f"\tssid={ssid.hex()}", f"\tkey_mgmt={key_mgmt}"]
        if psk:
            block.append(f"\tpsk={psk}")
        if hidden:
            block.append("\tscan_ssid=1")
        parts = self._parts()
        old = [lines for kind, lines in parts if kind == "net" and self._ssid(lines) == ssid]
        if priority is None and old:
            for line in old[0]:
                if line.strip().startswith("priority="):
                    try:
                        priority = int(line.strip()[9:])
                    except ValueError:
                        priority = None
        if priority:
            block.append(f"\tpriority={priority}")
        block.append("}")
        parts = [p for p in parts if not (p[0] == "net" and self._ssid(p[1]) == ssid)]
        parts.append(("net", block))
        self._write(parts)
        return block

    def set_priority(self, ssid, priority):
        parts = self._parts()
        found = False
        for i, (kind, lines) in enumerate(parts):
            if kind == "net" and self._ssid(lines) == ssid:
                lines = [ln for ln in lines if not ln.strip().startswith("priority=")]
                lines.insert(len(lines) - 1, f"\tpriority={priority}")
                parts[i] = ("net", lines)
                found = True
        if found:
            self._write(parts)
        return found

    def remove(self, ssid):
        parts = self._parts()
        keep = [p for p in parts if not (p[0] == "net" and self._ssid(p[1]) == ssid)]
        if len(keep) != len(parts):
            self._write(keep)
            return True
        return False


# --- the manager the screens use -----------------------------------------------------------------
def ipv4(iface=IFACE):
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", iface], capture_output=True,
                             text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            return parts[parts.index("inet") + 1].split("/")[0]
    return ""


def _q(ssid):
    return ssid.hex()


def write_start_file():
    """Regenerate /run/i14-wpa.conf from the saved networks and the off flag."""
    try:
        return subprocess.run([CONF_TOOL], timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Wifi:
    """State for the Wi-Fi screens. Call events() when the event socket is readable and tick() often."""

    STALE = "; startbestand niet bijgewerkt (geldt pas na herstart van wifi)"

    def __init__(self, ctrl_path=CTRL, store=None, rootfs_store=None, off_flag=OFF_FLAG, ip=ipv4, local_dir="/run",
                 start_file=write_start_file):
        self.start_file = start_file
        self.req = Ctrl(ctrl_path, local_dir)
        self.mon = Ctrl(ctrl_path, local_dir)
        self.store = store or Store(NETWORKS)
        self.rootfs_store = rootfs_store or Store(ROOTFS_NETWORKS, protect_parent=False)
        self.off_flag = off_flag
        self.ip = ip
        self.scan_results = []
        self.scanning = False
        self.attempt = None
        self.message = ""
        self.attached = False
        self.next_attach = 0.0
        self.changed = True
        self.lease_on_connect = False
        st = self.status()  # the network now: a later switch to another one needs a new lease
        self.last_ssid = st.get("ssid_bytes") if st.get("wpa_state") == "COMPLETED" else None

    # control socket
    def cmd(self, text):
        return self.request(text).strip()

    def request(self, text):
        """One request. A failure means wpa_supplicant is gone or restarted: on Linux the event socket
        then stays connected to the dead one and gets nothing, so it is dropped and attaches again."""
        try:
            return self.req.request(text)
        except OSError:
            self.drop_monitor()
            raise

    def drop_monitor(self):
        if self.attached:
            self.mon.close()
            self.attached = False
            self.next_attach = 0.0

    def monitor_fd(self):
        if not self.attached and time.monotonic() >= self.next_attach:
            try:
                self.mon.attach()
                self.attached = True
                self._lease_check()  # the network now; one that changed while nobody listened gets a new lease
            except OSError:
                self.mon.close()
                self.next_attach = time.monotonic() + 5
        return self.mon.sock if self.attached else None

    def available(self):
        try:
            return self.cmd("PING") == "PONG"
        except OSError:
            return False

    @property
    def enabled(self):
        return not os.path.exists(self.off_flag)

    def status(self):
        try:
            st = parse_kv(self.cmd("STATUS"))
        except OSError:
            return {"wpa_state": "GEEN_WPA"}
        if "ssid" in st:
            st["ssid_bytes"] = decode_ssid(st["ssid"])
        return st

    # actions
    def _set_flag(self, off):
        ensure_private_directory(os.path.dirname(self.off_flag) or ".")
        if off:
            try:
                with open(self.off_flag, "x") as f:
                    f.write("off\n")
                    f.flush()
                    os.fsync(f.fileno())
            except FileExistsError:
                pass
        else:
            try:
                os.unlink(self.off_flag)
            except FileNotFoundError:
                pass

    def set_enabled(self, on):
        """Switch Wi-Fi on or off for good: flag file, start file, then wpa_supplicant reloads it.
        If the start file or the reload fails, the old state is put back and nothing claims success."""
        if self.attempt:  # its network must go and the others must be enabled again, either way
            self._end_attempt("vorige poging afgebroken", keep=False)
        was_on = self.enabled
        try:
            self._set_flag(not on)
        except OSError as e:
            self.message = f"opslaan mislukt: {e.strerror or e}"
            self.changed = True
            return False
        try:
            ok = self.start_file() and self.cmd("RECONFIGURE") == "OK"
        except OSError:
            ok = False
        if not ok:
            try:
                self._set_flag(not was_on)
            except OSError:
                pass
            self.start_file()
            self.message = "Wi-Fi " + ("aanzetten" if on else "uitzetten") + " mislukt; niets veranderd"
            self.changed = True
            return False
        self.message = "Wi-Fi aan" if on else "Wi-Fi uit"
        self.lease_on_connect = on  # the lease of before "off" may have lost its route: a fresh one
        self.changed = True
        return True

    def scan(self):
        if not self.enabled:
            self.message = "Wi-Fi staat uit"
            return
        reply = self.cmd("SCAN")
        if reply == "OK" or reply == "FAIL-BUSY":
            self.scanning = True
            self.scan_started = time.monotonic()
            self.message = "zoeken..."
        else:
            self.message = f"zoeken mislukt ({reply})"
        self.changed = True

    def refresh_results(self):
        try:
            self.scan_results = parse_scan(self.request("SCAN_RESULTS"))
        except OSError:
            self.scan_results = []
        self.changed = True

    def saved(self):
        """Saved networks: the running ones (with id) plus what the files hold."""
        try:
            running = parse_networks(self.request("LIST_NETWORKS"))
        except OSError:
            running = []
        prio = {n["ssid"]: n["priority"] for n in self.rootfs_store.networks() + self.store.networks()}
        for n in running:
            n["priority"] = prio.get(n["ssid"], 0)
        return running

    def network_id(self, ssid):
        for n in self.saved():
            if n["ssid"] == ssid:
                return n["id"]
        return None

    def connect(self, ssid, sec="psk", passphrase=None, hidden=False):
        """Connect to a saved network (no passphrase) or a new one. Saved on success only."""
        if self.attempt:  # a second choice ends the first: its unconfirmed network must not stay
            self._end_attempt("vorige poging afgebroken", keep=False)
        if sec in ("sae", "wep", "eap"):  # refused before anything changes (Wi-Fi stays off if it was)
            self.message = {"sae": "WPA3-only netwerken worden niet ondersteund",
                            "wep": "WEP wordt niet ondersteund (onveilig)",
                            "eap": "Bedrijfsnetwerken (802.1X) worden niet ondersteund"}[sec]
            self.changed = True
            return False
        if sec == "psk" and passphrase is not None and not (8 <= len(passphrase) <= 63 and passphrase.isascii()):
            self.message = "Wachtwoord moet 8 tot 63 tekens zijn"  # checked before anything changes
            self.changed = True
            return False
        if not self.enabled and not self.set_enabled(True):
            return False  # its message says why
        nid = self.network_id(ssid)
        new_block, replaces = None, None
        if passphrase is not None or nid is None:
            if sec == "psk":
                if passphrase is None or not 8 <= len(passphrase) <= 63 or not passphrase.isascii():
                    self.message = "Wachtwoord moet 8 tot 63 tekens zijn"
                    self.changed = True
                    return False
                psk, key_mgmt = psk_hex(passphrase, ssid), "WPA-PSK"
            else:
                psk, key_mgmt = None, "NONE"
            replaces = nid  # a saved network with a new password: the old entry goes once this one works
            reply = self.cmd("ADD_NETWORK")
            if not reply.isdigit():
                self.message = f"Instellen mislukt ({reply})"
                self.changed = True
                return False
            nid = int(reply)
            settings = [f"ssid {_q(ssid)}", f"key_mgmt {key_mgmt}"]
            if psk:
                settings.append(f"psk {psk}")
            if hidden:
                settings.append("scan_ssid 1")
            for s in settings:
                if self.cmd(f"SET_NETWORK {nid} {s}") != "OK":
                    self.cmd(f"REMOVE_NETWORK {nid}")
                    self.message = "Instellen mislukt"
                    self.changed = True
                    return False
            new_block = (key_mgmt, psk, hidden)
        before = self.status().get("ssid_bytes")
        self.cmd(f"SELECT_NETWORK {nid}")  # disables the others until this attempt ends
        self.attempt = {"ssid": ssid, "id": nid, "new": new_block, "phase": "assoc",
                        "since": time.monotonic(), "before": before, "not_found": 0, "replaces": replaces}
        self.message = f"verbinden met {show_ssid(ssid)}..."
        self.changed = True
        return True

    def _end_attempt(self, message, keep):
        a = self.attempt
        self.attempt = None
        if a and a["new"] and not keep:
            try:
                self.cmd(f"REMOVE_NETWORK {a['id']}")
            except OSError:
                pass
        try:
            self.cmd("ENABLE_NETWORK all")
        except OSError:
            pass
        self.message = message
        self.changed = True

    def forget(self, ssid):
        """Remove the network from the files first, then from the running wpa_supplicant."""
        try:
            gone = self.store.remove(ssid)
            try:
                gone = self.rootfs_store.remove(ssid) or gone
            except OSError:
                pass
        except OSError as e:
            self.message = f"vergeten mislukt: {e.strerror or e}"
            self.changed = True
            return False
        fresh = self.start_file()
        nid = self.network_id(ssid)
        if nid is not None:
            self.cmd(f"REMOVE_NETWORK {nid}")
        self.message = f"{show_ssid(ssid)} vergeten" + ("" if fresh else self.STALE)
        self.changed = True
        return gone or nid is not None

    def prefer(self, ssid):
        """Make ssid the preferred saved network (highest priority)."""
        top = max([n["priority"] for n in self.saved()] + [0]) + 1
        if not self.store.set_priority(ssid, top):  # the file first: only networks added here
            self.message = "alleen netwerken die hier zijn toegevoegd"
            self.changed = True
            return False
        nid = self.network_id(ssid)
        if nid is not None:
            self.cmd(f"SET_NETWORK {nid} priority {top}")
        fresh = self.start_file()
        self.message = f"{show_ssid(ssid)} heeft nu voorrang" + ("" if fresh else self.STALE)
        self.changed = True
        return True

    # events and time
    def events(self):
        for ev in self.mon.events() if self.attached else []:
            self.handle(ev)
        if self.attached and self.mon.sock is None:
            self.attached = False

    def handle(self, ev):
        text = ev.split(">", 1)[-1]
        self.changed = True
        if text.startswith("CTRL-EVENT-SCAN-RESULTS"):
            self.scanning = False
            if self.message == "zoeken...":
                self.message = ""
            self.refresh_results()
            return
        if text.startswith("CTRL-EVENT-SCAN-FAILED"):
            self.scanning = False
            self.message = "zoeken mislukt"
            return
        if text.startswith("CTRL-EVENT-CONNECTED"):
            self._lease_check()
        a = self.attempt
        if not a:
            return
        if text.startswith("CTRL-EVENT-SSID-TEMP-DISABLED") and f"id={a['id']} " in text + " ":
            if "reason=WRONG_KEY" in text:
                self._end_attempt("Verkeerd wachtwoord", keep=False)
            elif "reason=CONN_FAILED" in text or "reason=AUTH_FAILED" in text:
                self._end_attempt("Verbinding geweigerd door het netwerk", keep=False)
        elif text.startswith("CTRL-EVENT-NETWORK-NOT-FOUND") and a["phase"] == "assoc":
            a["not_found"] += 1
            if a["not_found"] >= 2:
                self._end_attempt(f"{show_ssid(a['ssid'])} niet gevonden", keep=False)
        elif text.startswith("CTRL-EVENT-CONNECTED") and a["phase"] == "assoc":
            if self.status().get("ssid_bytes") == a["ssid"]:
                self._connected(a)

    def _connected(self, a):
        if a["new"]:
            key_mgmt, psk, hidden = a["new"]
            try:
                self.store.put(a["ssid"], key_mgmt, psk, hidden=hidden)
                a["saved"] = True
                a["stale"] = not self.start_file()  # saved, but a wpa_supplicant respawn would not know it
            except OSError as e:
                a["saved"] = False
                self.message = f"verbonden, maar opslaan mislukt: {e.strerror or e}"
            a["new"] = None  # keep it running whatever happens next
            if a.get("replaces") is not None:
                try:
                    self.cmd(f"REMOVE_NETWORK {a['replaces']}")
                except OSError:
                    pass
        a["phase"], a["since"] = "dhcp", time.monotonic()
        if a.get("saved", True):
            self.message = f"verbonden met {show_ssid(a['ssid'])}, adres ophalen..."
        try:
            self.cmd("ENABLE_NETWORK all")
        except OSError:
            pass

    def _lease_check(self):
        """On every association: a fresh DHCP lease when the network changed (any reason: a connect,
        a forgotten network, wpa_supplicant's own choice) or Wi-Fi was switched back on."""
        ssid = self.status().get("ssid_bytes")
        changed = self.last_ssid is not None and ssid is not None and ssid != self.last_ssid
        if changed or self.lease_on_connect:
            self.new_lease()
        self.lease_on_connect = False
        if ssid is not None:
            self.last_ssid = ssid

    def new_lease(self):
        """Make the one DHCP client (i14-wifi) drop its lease and ask anew: release, then renew."""
        try:
            with open(UDHCPC_PID) as f:
                pid = int(f.read().strip())
            os.kill(pid, 12)  # SIGUSR2: release (address and route removed)
            os.kill(pid, 10)  # SIGUSR1: renew from scratch
            return True
        except (OSError, ValueError):
            return False

    def tick(self):
        if time.monotonic() >= getattr(self, "next_ping", 0.0):  # notices a restarted wpa_supplicant
            self.next_ping = time.monotonic() + 30
            try:
                self.request("PING")
            except OSError:
                pass
        a = self.attempt
        if self.scanning and time.monotonic() - getattr(self, "scan_started", 0) > 15:
            self.scanning = False
            self.refresh_results()
        if not a:
            return
        age = time.monotonic() - a["since"]
        if a["phase"] == "assoc":
            if self.status().get("wpa_state") == "COMPLETED" and self.status().get("ssid_bytes") == a["ssid"]:
                self._connected(a)
            elif age > ASSOC_TIMEOUT:
                self._end_attempt(f"Geen verbinding met {show_ssid(a['ssid'])} (time-out)", keep=False)
        elif a["phase"] == "dhcp":
            ip = self.ip()
            if ip and age > (1 if a["before"] == a["ssid"] else 4):  # after a release the old address goes
                note = "" if a.get("saved", True) else " (niet opgeslagen)"
                note += self.STALE if a.get("stale") else ""
                self._end_attempt(f"Verbonden met {show_ssid(a['ssid'])}, adres {ip}{note}", keep=True)
            elif age > DHCP_TIMEOUT:
                self._end_attempt("Verbonden, maar geen IP-adres gekregen (DHCP)", keep=True)
