#!/usr/bin/env python3
"""Experimental pinned MT6735 eMMC readback guards; import never opens USB."""
import argparse
import os
from pathlib import Path
import runpy
import re
import struct
import subprocess
import sys

PIN = "118a000a83ea31c4f427fe5225abfe964546e05e"
USB_CHUNK = 16384
DA_PACKET = 16384
ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "artifacts/tooling/mtkclient"

class ReadbackError(RuntimeError):
    pass


def usbread(self, resplen=None, maxtimeout=100, w_max_packet_size=None):
    """Read exact logical bytes with finite endpoint timeouts and no retries."""
    one_packet = resplen is None
    if one_packet:
        resplen = self.maxsize
    if resplen < 0:
        raise ReadbackError("Negative USB read length")
    packet = self.EP_IN.wMaxPacketSize
    if packet <= 0:
        raise ReadbackError("Unsupported endpoint packet size")
    cap = (min(w_max_packet_size or packet, USB_CHUNK) // packet) * packet
    if cap < packet:
        raise ReadbackError("Unsupported endpoint packet size")
    result = bytearray()
    while len(result) < resplen:
        remaining = resplen - len(result)
        if self.rxleft:
            count = min(remaining, len(self.rxleft))
            result.extend(self.rxleft[:count])
            del self.rxleft[:count]
        else:
            request = min(cap, ((remaining + packet - 1) // packet) * packet)
            try:
                data = bytes(self.EP_IN.read(request, max(1, self.timeout)))
            except Exception as exc:
                raise ReadbackError("USB read failed; session must stop without retry") from exc
            if not data or len(data) > request:
                raise ReadbackError("Empty or oversized USB transfer; session must stop")
            result.extend(data[:remaining])
            self.rxleft.extend(data[remaining:])
        if one_packet:
            break
    return bytes(result)


def emmc_readflash(self, addr, length, filename, parttype=None, display=True):
    """Keep the legacy wire framing; verify every DA block before ACK/publish."""
    if self.config.hwcode != 0x321 or self.config.iot or self.daconfig.storage.flashtype != "emmc":
        raise ReadbackError("Readback guard supports only measured HW 0x321 legacy eMMC")
    length, partition = self.daconfig.legacy_storage.partitiontype_and_size(parttype=parttype, length=length)
    if addr < 0 or length <= 0:
        raise ReadbackError("Invalid readback range")
    output = None
    destination = Path(filename) if filename else None
    partial = Path(str(destination) + ".partial") if destination else None
    if destination:
        if destination.exists():
            raise FileExistsError("Readback destination already exists")
        output = os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
    result = bytearray()
    try:
        if self.check_usb_cmd() is None:
            raise ReadbackError("DA USB status check failed")
        if not self.sdmmc_switch_part(partition):
            raise ReadbackError("eMMC partition selection failed")
        for command in (self.Cmd.READ_CMD, b"\x0c", b"\x02", struct.pack(">Q", addr),
                        struct.pack(">Q", length), struct.pack(">I", DA_PACKET)):
            if not self.usbwrite(command):
                raise ReadbackError("Read command USB write failed")
        if bytes(self.usbread(1)) != self.Rsp.ACK:
            raise ReadbackError("Read command was not acknowledged")
        position = 0
        while position < length:
            count = min(DA_PACKET, length - position)
            data = bytes(self.usbread(count, w_max_packet_size=USB_CHUNK))
            if len(data) != count:
                raise ReadbackError("Short DA block; no checksum ACK sent")
            checksum = bytes(self.usbread(2))
            if len(checksum) != 2 or struct.unpack(">H", checksum)[0] != (sum(data) & 65535):
                raise ReadbackError("DA checksum mismatch or missing checksum; no ACK sent")
            if output:
                if output.write(data) != count:
                    raise ReadbackError("Short local file write")
            else:
                result.extend(data)
            if not self.usbwrite(self.Rsp.ACK):
                raise ReadbackError("DA block ACK USB write failed")
            position += count
        if output:
            output.flush()
            os.fsync(output.fileno())
            if output.tell() != length:
                raise ReadbackError("Readback file length mismatch")
            output.close()
            output = None
            # Hard-link publication refuses an existing final path, including races.
            os.link(partial, destination)
            partial.unlink()
            print(f"Verified readback bytes={length} to {destination}", flush=True)
            return True
        return bytes(result)
    finally:
        if output:
            output.close()


def checked_commands(text):
    commands = []
    arities = {"printgpt": 1, "r": 3, "ro": 4, "rf": 2}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if any(any(c in item for c in "'\";\\") for item in parts):
            raise ReadbackError("Script tokens must be plain paths/arguments without quotes or separators")
        options = [x for x in parts if x.startswith("--")]
        if any(x not in {"--parttype=user", "--parttype=boot1", "--parttype=boot2"} for x in options):
            raise ReadbackError("Only explicit eMMC parttype options are allowed")
        positional = [x for x in parts if not x.startswith("--")]
        if not positional or positional[0] not in arities or len(positional) != arities[positional[0]] or len(options) > 1:
            raise ReadbackError("Script contains an unsupported command or argument count")
        if positional[0] == "printgpt" and options:
            raise ReadbackError("printgpt accepts no parttype option")
        if positional[0] == "ro":
            try:
                if int(positional[1], 0) < 0 or int(positional[2], 0) <= 0:
                    raise ValueError
            except ValueError as exc:
                raise ReadbackError("Invalid explicit read range") from exc
        commands.append(" ".join(parts + ["--stock", "--noreconnect"]))
    if not commands:
        raise ReadbackError("Readback script is empty")
    return commands


def serial_port_allowed(path, ports):
    if not re.fullmatch(r"/dev/cu\.usbmodem[A-Za-z0-9]+", path):
        raise ReadbackError("Serial port must be an explicit /dev/cu.usbmodem path")
    matches = [port for port in ports if port.device == path]
    if not matches:
        return False
    allowed = {0x0003, 0x6000, 0x2000, 0x2001, 0x201d, 0x20ff, 0x3000}
    if len(matches) != 1 or matches[0].vid != 0x0e8d or matches[0].pid not in allowed:
        raise ReadbackError("Requested serial port does not have an allowed MTK VID/PID")
    return True


def serial_detectdevices(self):
    import serial.tools.list_ports
    return [self.portname] if serial_port_allowed(self.portname, serial.tools.list_ports.comports()) else []


def serial_connect(self, ep_in=-1, ep_out=-1):
    import serial
    if self.connected:
        self.close()
    if not serial_detectdevices(self):
        return False
    device = serial.Serial(port=None, baudrate=115200, timeout=1, write_timeout=1,
                           xonxoff=False, dsrdtr=False, rtscts=False)
    # pyserial POSIX open normally discards input; preserve all session evidence.
    device._reset_input_buffer = lambda: None
    device.port = self.portname
    try:
        device.open()
    except Exception as exc:
        device.close()
        raise ReadbackError("Native serial open failed; do not retry this session") from exc
    self.device = device
    self.connected = device.is_open
    return self.connected


def serial_usbread(self, resplen=None, maxtimeout=0, timeout=0, w_max_packet_size=None):
    if resplen is None:
        resplen = self.device.in_waiting
    if resplen < 0:
        raise ReadbackError("Negative serial read length")
    self.device.timeout = 1
    result = bytearray()
    while len(result) < resplen:
        count = min(USB_CHUNK, resplen - len(result))
        try:
            data = bytes(self.device.read(count))
        except Exception as exc:
            raise ReadbackError("Native serial read failed; no retry") from exc
        if len(data) != count:
            raise ReadbackError("Native serial read was empty or short; no retry")
        result.extend(data)
    return bytes(result)


def serial_read(self, length=None, timeout=-1):
    return serial_usbread(self, length)


def serial_write(self, command, pktsize=None):
    data = command.encode() if isinstance(command, str) else bytes(command)
    self.device.write_timeout = 1
    chunk = min(pktsize or 512, USB_CHUNK)
    if chunk <= 0:
        raise ReadbackError("Invalid serial write chunk")
    for offset in range(0, len(data), chunk):
        part = data[offset:offset + chunk]
        try:
            count = self.device.write(part)
        except Exception as exc:
            raise ReadbackError("Native serial write failed; no retry") from exc
        if count != len(part):
            raise ReadbackError("Native serial write was short; no retry")
    return True


def serial_usbreadwrite(self, data, resplen):
    serial_write(self, data)
    return serial_usbread(self, resplen)


def install_serial_guards(cls):
    cls.connect = serial_connect
    cls.detectdevices = serial_detectdevices
    cls.usbread = serial_usbread
    cls.read = serial_read
    cls.write = serial_write
    cls.usbwrite = serial_write
    cls.usbreadwrite = serial_usbreadwrite
    cls.flush = lambda self: None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serialport", help="Exact MTK native /dev/cu.usbmodem path; optional")
    parser.add_argument("script", type=Path, help="Read-only mtk commands: printgpt, r, ro, rf")
    args = parser.parse_args(argv)
    commands = checked_commands(args.script.read_text())
    if args.serialport:
        serial_port_allowed(args.serialport, [])
    revision = subprocess.check_output(["rtk", "proxy", "git", "-C", str(VENDOR), "rev-parse", "HEAD"], text=True).strip()
    if revision != PIN:
        raise ReadbackError("Vendor revision mismatch")
    subprocess.run(["rtk", "proxy", "git", "-C", str(VENDOR), "diff", "--exit-code", "--quiet", "HEAD"], check=True)
    sys.path.insert(0, str(VENDOR))
    from mtkclient.Library.Connection.usblib import UsbClass
    from mtkclient.Library.DA.legacy.dalegacy_lib import DALegacy
    UsbClass.usbread = usbread
    DALegacy.readflash = emmc_readflash
    if args.serialport:
        from mtkclient.Library.Connection.seriallib import SerialClass
        install_serial_guards(SerialClass)
    sys.argv = [str(VENDOR / "mtk.py"), "--stock", "--noreconnect", "multi", ";".join(commands)]
    if args.serialport:
        sys.argv[1:1] = ["--serialport", args.serialport]
    print(f"Experimental readback guard: vendor={PIN}, host_chunk={USB_CHUNK}, DA_packet={DA_PACKET}; no automatic read retry", flush=True)
    runpy.run_path(str(VENDOR / "mtk.py"), run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except ReadbackError as exc:
        print(f"Readback stopped: {exc}. Preserve .partial files; start no automatic retry.", file=sys.stderr)
        sys.exit(1)
