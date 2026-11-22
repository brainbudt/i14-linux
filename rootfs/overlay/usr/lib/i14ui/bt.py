"""Bluetooth through MediaTek's /dev/stpbt: raw HCI in H4 framing (the stock kernel has no BlueZ).

Opening the device powers Bluetooth on (WMT), closing powers it off. This module does what the
radio can be proven to do without a full host stack: switch on/off, read the controller's
address and search for nearby devices (classic inquiry with names, and BLE advertisements).
Pairing and using devices (L2CAP, SDP, SSP, HID) is not implemented.
"""
import os
import struct
import time

DEV = "/dev/stpbt"
CMD, EVT = 0x01, 0x04
# opcodes (OGF << 10 | OCF)
RESET, SET_EVENT_MASK = 0x0C03, 0x0C01
WRITE_INQUIRY_MODE, READ_BD_ADDR = 0x0C45, 0x1009
INQUIRY, INQUIRY_CANCEL, REMOTE_NAME = 0x0401, 0x0402, 0x0419
LE_SET_EVENT_MASK, LE_SCAN_PARAMS, LE_SCAN_ENABLE = 0x2001, 0x200B, 0x200C
EV_INQ_COMPLETE, EV_INQ_RESULT, EV_CMD_COMPLETE, EV_CMD_STATUS = 0x01, 0x02, 0x0E, 0x0F
EV_NAME, EV_INQ_RSSI, EV_EXT_INQ, EV_LE = 0x07, 0x22, 0x2F, 0x3E
INQUIRY_S, LE_S = 10.24, 6.0


def command(opcode, params=b""):
    return struct.pack("<BHB", CMD, opcode, len(params)) + params


def addr_str(raw):
    return ":".join(f"{b:02X}" for b in reversed(raw))


def eir_name(data):
    """Complete (0x09) or short (0x08) local name from EIR / advertising data."""
    i, short = 0, None
    while i + 1 < len(data):
        n = data[i]
        if n == 0 or i + 1 + n > len(data):
            break
        kind, value = data[i + 1], data[i + 2:i + 1 + n]
        if kind == 0x09:
            return value.decode("utf-8", "replace")
        if kind == 0x08:
            short = value.decode("utf-8", "replace")
        i += 1 + n
    return short


class Packets:
    """H4 byte stream to whole HCI event packets (event code, parameters)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while self.buf:
            if self.buf[0] != EVT:  # only events are expected; resync on anything else
                del self.buf[0]
                continue
            if len(self.buf) < 3 or len(self.buf) < 3 + self.buf[2]:
                break
            n = self.buf[2]
            out.append((self.buf[1], bytes(self.buf[3:3 + n])))
            del self.buf[:3 + n]
        return out


class Bluetooth:
    def __init__(self, dev=DEV, opener=None):
        self.dev = dev
        self.opener = opener or (lambda p: os.open(p, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))
        self.fd = None
        self.packets = Packets()
        self.devices = {}   # address -> {"name", "rssi", "kind"}
        self.address = ""
        self.phase = None   # None, "inquiry", "names", "le"
        self.phase_end = 0.0
        self.names_todo = []
        self.message = ""
        self.changed = True
        self.pending = {}   # opcode -> callback(status, params)
        self.ready = False  # reset and set-up done: commands like INQUIRY may go
        self.auto_on = False  # switched on by a search: off again when it ends

    @property
    def on(self):
        return self.fd is not None

    def _send(self, opcode, params=b"", then=None):
        """Queue one HCI command (never blocks); on a write error Bluetooth is switched off."""
        if self.fd is None:
            return False
        if then:
            self.pending[opcode] = then
        try:
            os.write(self.fd, command(opcode, params))
            return True
        except OSError as e:
            self.pending.pop(opcode, None)
            self.power(False)
            self.message = f"Bluetooth-fout: {e.strerror or e}"
            return False

    def power(self, on):
        if on and self.fd is None:
            try:
                self.fd = self.opener(self.dev)
            except OSError as e:
                self.message = f"Bluetooth starten mislukt: {e.strerror or e}"
                self.changed = True
                return False
            self.packets = Packets()
            self.ready = False
            self.message = "Bluetooth aan"
            self._send(RESET, then=lambda st, p: self._after_reset(st))
            if self.fd is None:  # the reset could not be sent: _send switched it off with the reason
                self.changed = True
                return False
        elif not on and self.fd is not None:
            self.auto_on = False
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd, self.phase, self.pending = None, None, {}
            self.message = "Bluetooth uit"
        self.changed = True
        return True

    def _after_reset(self, status):
        if status:  # a controller that refuses its reset is never ready: off, with the reason
            self.phase = None
            self.power(False)
            self.message = f"Bluetooth-reset mislukt (status {status})"
            return
        self._send(READ_BD_ADDR, then=lambda st, p: setattr(self, "address", addr_str(p[:6]) if not st else ""))
        self._send(SET_EVENT_MASK, struct.pack("<Q", 0x3DBFF807FFFBFFFF))
        self._send(WRITE_INQUIRY_MODE, b"\x02", then=self._ready)  # results with RSSI and names where possible

    def _ready(self, status, _):
        self.ready = True
        if self.phase == "start":  # a search waited for the radio
            self._inquiry()

    def search(self):
        """Search; a radio switched on just for this goes off again when the search ends. The inquiry
        starts only once the controller has finished its reset and set-up (one command at a time)."""
        if self.fd is None:
            if not self.power(True):
                return
            self.auto_on = True
        self.devices.clear()
        self.names_todo = []
        self.message = "zoeken..."
        self.changed = True
        if not getattr(self, "ready", False):
            self.phase, self.phase_end = "start", time.monotonic() + 5
            return
        self._inquiry()

    def _inquiry(self):
        self.names_todo = []
        self.phase, self.phase_end = "inquiry", time.monotonic() + INQUIRY_S + 3
        self.message = "zoeken..."
        # general inquiry access code 0x9E8B33, 8 x 1.28 s, unlimited responses
        self._send(INQUIRY, b"\x33\x8b\x9e\x08\x00", then=self._inquiry_status)
        self.changed = True

    def _inquiry_status(self, status, _):
        if status:
            self.message = f"zoeken geweigerd (status {status})"
            self._start_le()

    def _start_le(self):
        self.phase, self.phase_end = "le", time.monotonic() + LE_S
        self._send(LE_SET_EVENT_MASK, struct.pack("<Q", 0x1F))
        # active scan, 100 ms interval/window, public address, accept all
        self._send(LE_SCAN_PARAMS, struct.pack("<BHHBB", 1, 0xA0, 0xA0, 0, 0))
        self._send(LE_SCAN_ENABLE, b"\x01\x00")

    def _finish(self):
        if self.phase == "le":
            try:
                self._send(LE_SCAN_ENABLE, b"\x00\x00")
            except OSError:
                pass
        self.phase = None
        if getattr(self, "auto_on", False):
            self.auto_on = False
            self.power(False)
        n = len(self.devices)
        self.message = f"{n} apparaat gevonden" if n == 1 else f"{n} apparaten gevonden"
        self.changed = True

    def _next_name(self):
        while self.names_todo:
            addr = self.names_todo.pop(0)
            raw = bytes.fromhex(addr.replace(":", ""))[::-1]
            mode = self.devices[addr].get("psrm", 1)
            self.phase_end = time.monotonic() + 6
            self._send(REMOTE_NAME, raw + bytes([mode, 0]) + b"\x00\x00")
            return
        self._start_le()

    def _seen(self, raw, rssi, kind, name=None, psrm=None):
        addr = addr_str(raw)
        d = self.devices.setdefault(addr, {"name": None, "rssi": rssi, "kind": kind})
        if rssi is not None:
            d["rssi"] = rssi
        if name:
            d["name"] = name
        if psrm is not None:
            d["psrm"] = psrm
        self.changed = True

    def fileno(self):
        return self.fd

    def read(self):
        if self.fd is None:
            return
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except OSError as e:
            self.message = f"Bluetooth-fout: {e.strerror or e}"
            self.power(False)
            return
        for code, p in self.packets.feed(data):
            self.handle(code, p)

    def handle(self, code, p):
        self.changed = True
        if code == EV_CMD_COMPLETE and len(p) >= 4:
            op = struct.unpack_from("<H", p, 1)[0]
            cb = self.pending.pop(op, None)
            if cb:
                cb(p[3], p[4:])
        elif code == EV_CMD_STATUS and len(p) >= 4:
            op = struct.unpack_from("<H", p, 2)[0]
            cb = self.pending.pop(op, None)
            if cb:
                cb(p[0], b"")
        elif code in (EV_INQ_RESULT, EV_INQ_RSSI, EV_EXT_INQ) and p:
            n = 1 if code == EV_EXT_INQ else p[0]
            size = {EV_INQ_RESULT: 14, EV_INQ_RSSI: 14, EV_EXT_INQ: 14}[code]
            for i in range(n):
                off = 1 + i * size
                rec = p[off:off + size]
                if len(rec) < size:
                    break
                rssi = struct.unpack("b", rec[13:14])[0] if code != EV_INQ_RESULT else None
                name = eir_name(p[1 + size:]) if code == EV_EXT_INQ else None
                self._seen(rec[:6], rssi, "klassiek", name, psrm=rec[6])
        elif code == EV_INQ_COMPLETE and self.phase == "inquiry":
            self.names_todo = [a for a, d in self.devices.items() if not d["name"]]
            self.phase = "names"
            self._next_name()
        elif code == EV_NAME and len(p) >= 7:
            if p[0] == 0:
                name = p[7:].split(b"\0")[0].decode("utf-8", "replace")
                self._seen(p[1:7], None, "klassiek", name)
            if self.phase == "names":
                self._next_name()
        elif code == EV_LE and p and p[0] == 0x02:  # LE advertising report(s)
            n, off = p[1], 2
            for _ in range(n):
                if off + 9 > len(p):
                    break
                raw, ln = p[off + 2:off + 8], p[off + 8]
                data = p[off + 9:off + 9 + ln]
                rssi = struct.unpack("b", p[off + 9 + ln:off + 10 + ln] or b"\0")[0]
                self._seen(raw, rssi, "BLE", eir_name(data))
                off += 10 + ln

    def tick(self):
        if self.phase and time.monotonic() > self.phase_end:
            if self.phase == "inquiry":
                try:
                    self._send(INQUIRY_CANCEL)
                except OSError:
                    pass
                self.handle(EV_INQ_COMPLETE, b"\x00")
            elif self.phase == "start":
                self.message = "Bluetooth reageert niet"
                self.phase = None
                if getattr(self, "auto_on", False):
                    self.power(False)
                    self.message = "Bluetooth reageert niet"
            elif self.phase == "names":
                self._next_name()
            else:
                self._finish()

    def listing(self):
        """Devices with a name first, then by signal strength: (address, name, rssi, kind)."""
        items = [(a, d["name"] or "", d["rssi"], d["kind"]) for a, d in self.devices.items()]
        return sorted(items, key=lambda t: (not t[1], -(t[2] if t[2] is not None else -200)))
