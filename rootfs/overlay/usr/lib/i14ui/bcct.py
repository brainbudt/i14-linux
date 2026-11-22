"""Own stock BCCT slot 00, reserved for i14-power; other thermal slots stay intact.

The caller must keep USB unlimited disabled: that stock policy bypasses BCCT.
No access to current_cmd belongs here (even reading it disables charging).
"""
import errno
import os
from pathlib import Path
import re


def write_command(path, text):
    """The vendor proc callbacks require a complete command in one write."""
    if "\n" in text or "\r" in text:
        raise ValueError("kernel command must be one line")
    data = (text + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    try:
        if os.write(fd, data) != len(data):
            raise OSError(errno.EIO, "short kernel command write", str(path))
    finally:
        os.close(fd)


class Bcct:
    def __init__(self, config="/proc/driver/thermal/clbcct", thermal_root="/sys/class/thermal"):
        self.config = Path(config)
        self.thermal_root = Path(thermal_root)

    def _snapshot(self, require_effective=True):
        devices = []
        for device in self.thermal_root.glob("cooling_device*"):
            if (device / "type").read_text(encoding="ascii").strip() == "mtk-cl-bcct00":
                devices.append(device)
        if len(devices) != 1:
            raise ValueError("BCCT slot 00 missing or ambiguous")
        device = devices[0]
        if (device / "max_state").read_text(encoding="ascii").strip() != "1":
            raise ValueError("unexpected BCCT max_state")
        for zone in self.thermal_root.glob("thermal_zone*"):
            for binding in zone.glob("cdev[0-9]*"):
                if binding.is_symlink() and binding.resolve(strict=True) == device.resolve(strict=True):
                    raise ValueError("BCCT slot 00 belongs to a thermal zone")
        lines = self.config.read_text(encoding="ascii").splitlines()
        if len(lines) != 5 or re.fullmatch(r"klog [01]", lines[0]) is None:
            raise ValueError("bad BCCT configuration")
        effective = re.fullmatch(r"curr_limit ([0-9]+)", lines[1])
        if effective is None or not 1 <= int(effective[1]) <= 65535:
            raise ValueError("bad BCCT effective limit")
        limits, states = [], []
        for index, line in enumerate(lines[2:]):
            slot = re.fullmatch(rf"mtk-cl-bcct{index:02} (-?[0-9]+) mA, state ([01])", line)
            if slot is None or not -1 <= int(slot[1]) <= 32767:
                raise ValueError("bad BCCT slot")
            limits.append(int(slot[1]))
            states.append(int(slot[2]))
        if limits[0] not in (-1, 0, 1) or (states[0] and limits[0] != 1):
            raise ValueError("BCCT slot 00 is occupied")
        if (device / "cur_state").read_text(encoding="ascii").strip() != str(states[0]):
            raise ValueError("BCCT state readback disagrees")
        if require_effective and states[0] and int(effective[1]) != 1:
            raise ValueError("BCCT pause is not effective")
        return device, int(lines[0][-1]), limits, states

    def paused(self):
        return bool(self._snapshot()[3][0])

    def set_paused(self, paused):
        if type(paused) is not bool:
            raise ValueError("BCCT pause must be boolean")
        device, klog, limits, states = self._snapshot(require_effective=paused)
        if bool(states[0]) == paused:
            return
        if paused and limits[0] != 1:
            limits[0] = 1
            write_command(self.config, " ".join(map(str, [klog, *limits])))
            actual = self._snapshot()
            if actual != (device, klog, limits, states):
                raise ValueError("BCCT configuration write did not read back")
        write_command(device / "cur_state", str(int(paused)))
        actual_device, actual_klog, actual_limits, actual_states = self._snapshot()
        if (actual_device != device or actual_klog != klog or actual_limits != limits or
                actual_states[0] != int(paused) or actual_states[1:] != states[1:]):
            raise ValueError("BCCT state write did not read back")

    def release(self):
        self.set_paused(False)
