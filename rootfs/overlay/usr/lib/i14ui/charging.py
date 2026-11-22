"""Stock 3.18 BCCT charge limit and low-battery guard, owned by i14-power.

Never READ current_cmd: that callback disables charging! battery_cmd is the readback.
0 releases charging to the stock driver; it does not replace its temperature/current limits.
"""
import datetime
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

from .config import Config, PATH

BATTERY = "/sys/class/power_supply/battery"
# With a charger attached the stock driver holds capacity at 100 after a full charge (battery_common.c
# "recharging"), and resets its coulomb-counter percentage to 100 whenever charging resumes from that state.
# The band therefore uses the driver's open-circuit voltage estimate (battery voltage + current x resistance).
GAUGE = "/sys/devices/platform/battery_meter/FG_g_fg_dbg_bat_zcv"
# The cached current, ZCV and CAR are updated together by fgauge_algo_run().
GAUGE_ACTIVITY = "/sys/devices/platform/battery_meter/FG_g_fg_dbg_bat_current"
# (percent, open-circuit mV) from the stock kernel's own profile, table T2 (generic; an approximation).
# 15%/3712 and 80%/4109 interpolate skipped T2 entries (DOD 85 and 20).
OCV = [(0, 3400), (5, 3668), (10, 3685), (15, 3712), (20, 3736), (25, 3756), (30, 3772), (35, 3789),
       (40, 3808), (45, 3825), (50, 3850), (55, 3876), (60, 3924), (65, 3962), (70, 4003), (75, 4067),
       (80, 4109), (85, 4158), (90, 4213), (95, 4260), (100, 4324)]
TYPE = "/sys/devices/platform/battery/Charger_Type"
COMMAND = "/proc/mtk_battery_cmd/current_cmd"
READBACK = "/proc/mtk_battery_cmd/battery_cmd"
USB = "/sys/class/power_supply/usb/online"
AC = "/sys/class/power_supply/ac/online"
LOG = "/var/log/i14-battery.log"
STATUS = "/run/i14-charge-status.json"
BOOT_ID = "/proc/sys/kernel/random/boot_id"
# Drain mode (the nearly-empty chain test): a root-only tmpfs file "<boot_id> <deadline>\n", the deadline in
# boot-relative uptime seconds (/proc/uptime's clock). While it is valid the guard keeps BCCT paused below its
# band and reserve, so the battery drains on the cable down to the low-battery poweroff below; every other
# protection stays. A request the guard does not honour is removed, so a removed file never comes back.
DRAIN_REQUEST = "/run/i14-drain-request"
DRAIN_MAX_S = 43200      # a deadline may lie at most 12 h ahead
DRAIN_FLOOR_MV = 3330    # batt_vol at or below: drain mode ends (charging resumes)
DRAIN_MAX_TEMP = 400     # batt_temp (0.1 C) at or above: drain mode ends
DRAIN_FORMAT = re.compile(r"([0-9A-Za-z-]{1,64}) ([0-9]{1,9})\n?")


def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return ""


def boottime():
    """Seconds since boot including suspend (/proc/uptime's clock); None when unavailable."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError, ValueError):
        return None


def drain_request(path, boot_id, now, owner=0):
    """(deadline, None) for a request the guard honours, else (None, reason); (None, None) = no request."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"unreadable (errno {exc.errno})"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "not a regular file"
        if info.st_uid != owner:
            return None, "not owned by root"
        if info.st_mode & 0o077:
            return None, "group or other access"
        data = os.read(fd, 129)
    except OSError as exc:
        return None, f"unreadable (errno {exc.errno})"
    finally:
        os.close(fd)
    if len(data) > 128 or not data.isascii():
        return None, "malformed"
    match = DRAIN_FORMAT.fullmatch(data.decode("ascii"))
    if match is None:
        return None, "malformed"
    if not boot_id or match[1] != boot_id:
        return None, "other boot"
    if now is None:
        return None, "clock unavailable"
    deadline = int(match[2])
    if deadline <= now:
        return None, "expired"
    if deadline - now > DRAIN_MAX_S:
        return None, "too long"
    return deadline, None


def drain_hazard(readings, voltage, temp):
    """Why drain mode must end now despite a valid request, or None. readings: everything the normal
    validity needs except the 3700 mV reserve (gauge live, temperature, charger, status, feedback, config)."""
    if not readings or voltage is None or temp is None:
        return "invalid reading"
    if voltage <= DRAIN_FLOOR_MV:
        return f"voltage {voltage} mV at or below {DRAIN_FLOOR_MV} mV"
    if temp >= DRAIN_MAX_TEMP:
        return f"battery temperature {temp // 10}.{temp % 10} C"
    return None


def ocv_percent(mv):
    """Open-circuit voltage to percent along OCV, rounded down; None stays None."""
    if mv is None:
        return None
    for (p1, v1), (p2, v2) in zip(OCV, OCV[1:]):
        if mv < v2:
            return max(0, p1 + (mv - v1) * (p2 - p1) // (v2 - v1))
    return 100


def number(path, low, high):
    value = read(path)
    if value.isascii() and value.isdigit() and len(value) <= 6:
        value = int(value)
        if low <= value <= high:
            return value
    return None


def read_status(path=STATUS, now=None, boot_id=None, *, max_age=150):
    """Only fresh, same-boot guard snapshots may label the UI or restore gauge trust."""
    now = time.monotonic() if now is None else now
    boot_id = read(BOOT_ID) if boot_id is None else boot_id
    try:
        data = json.loads(read(path))
        required = {"schema", "backend", "boot_id", "updated_monotonic", "level", "zcv_mv",
                    "paused", "valid", "enabled", "limit", "error"}
        if not isinstance(data, dict) or not required <= data.keys():
            return None
        stamp = data.get("updated_monotonic")
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or now - stamp < 0 or (max_age is not None and now - stamp > max_age):
            return None
        if (type(data.get("schema")) is not int or data["schema"] != 1 or data.get("backend") != "bcct" or
                data.get("boot_id") != boot_id or
                any(type(data.get(key)) is not bool for key in ("paused", "valid", "enabled")) or
                type(data.get("limit")) is not int or data["limit"] not in (60, 70, 80, 90, 100)):
            return None
        for key, low, high in (("level", 0, 100), ("zcv_mv", 2500, 5000)):
            value = data.get(key)
            if value is not None and (type(value) is not int or not low <= value <= high):
                return None
        if data.get("error") is not None and not isinstance(data["error"], str):
            return None
        if data.get("online") is not None and type(data["online"]) is not bool:
            return None
        if (data.get("level") is None) != (data.get("zcv_mv") is None):
            return None
        return data
    except (ValueError, TypeError):
        return None


class Guard:
    def __init__(self, battery=BATTERY, charger_type=TYPE, command=COMMAND, readback=READBACK,
                 usb=USB, ac=AC, settings=PATH, log=LOG, gauge=GAUGE, gauge_activity=GAUGE_ACTIVITY,
                 control=None, status_path=STATUS, clock=time.monotonic, drain_path=DRAIN_REQUEST,
                 uptime=boottime, drain_owner=0):
        self.battery = Path(battery)
        self.gauge, self.gauge_activity = gauge, gauge_activity
        self.clock, self.status_path = clock, Path(status_path)
        self.boot_id = read(BOOT_ID)
        self.activity = None
        self.activity_zcv = self.activity_car = None
        self.stale_logged = False
        self.activity_changed = None
        metadata = read_status(self.status_path, now=clock(), boot_id=self.boot_id, max_age=None)
        previous = metadata if metadata and clock() - metadata["updated_monotonic"] <= 150 else None
        # Display tracking is separate from the raw estimate used by the actuator.
        self.level = previous["level"] if previous else None
        self.level_at = previous["updated_monotonic"] if previous else None
        if previous and self.boot_id:
            # Comparison values come back from any recent snapshot, so a change on the first tick
            # is seen; trust (activity_changed) only while it is still fresh.
            activity, changed = previous.get("activity"), previous.get("activity_changed_monotonic")
            if type(activity) is int and abs(activity) <= 100000:
                self.activity = activity
                if type(changed) in (int, float) and math.isfinite(changed) and 0 <= clock() - changed <= 180:
                    self.activity_changed = changed
            zcv = previous.get("activity_zcv")
            if zcv is None:
                zcv = previous.get("zcv_mv")  # status written before activity_zcv existed
            if type(zcv) is int and 2500 <= zcv <= 5000:
                self.activity_zcv = zcv
            car = previous.get("activity_car")
            if type(car) is int and abs(car) <= 100000:
                self.activity_car = car
        if control is None:
            from .bcct import Bcct
            control = Bcct()
        self.control = control
        self.charger_type, self.command, self.readback = charger_type, command, readback
        self.usb, self.ac, self.settings, self.log = usb, ac, settings, log
        self.paused = False
        self.limit = None
        self.previous = None
        self.n = 0
        self.migrated = bool(metadata and self.boot_id and metadata.get("migration_attempted") is True)
        self.drain_path, self.uptime, self.drain_owner = drain_path, uptime, drain_owner
        self.drain_until = None  # deadline (uptime s) while drain mode is on
        self.drain_note = None   # last drain line logged (each is logged once)

    def record(self, text):
        line = f"{datetime.datetime.now():%F %T} {text}\n"
        try:
            with open(self.log, "a") as f:
                f.write(line)
        except OSError:
            print(line, file=sys.stderr, end="")

    def drain_log(self, text):
        if text != self.drain_note:
            self.record(text)
        self.drain_note = text

    def uptime_now(self):
        try:
            value = self.uptime()
        except (OSError, ValueError):
            return None
        return value if type(value) in (int, float) and math.isfinite(value) else None

    def drain_step(self, until, reason):
        """Enter, keep or leave drain mode. A present request the guard does not honour is removed."""
        if until is not None and reason is None:
            if until != self.drain_until:
                self.drain_log(f"drain mode on: BCCT stays paused until uptime {until} s; "
                               "low-battery poweroff stays active")
            self.drain_until = until
            return
        suffix = ""
        if reason is not None:
            try:
                os.unlink(self.drain_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                suffix = f"; removal failed (errno {exc.errno})"
        if self.drain_until is not None:
            why = "request removed" if reason is None else "deadline reached" if reason == "expired" else reason
            self.drain_log(f"drain mode ended: {why}{suffix}")
        elif reason is not None:
            self.drain_log(f"drain request rejected: {reason}{suffix}")
        else:
            self.drain_note = None
        self.drain_until = None

    def write_command(self, unlimited, paused):
        # Only the one-time migration may touch this legacy test interface.
        from .bcct import write_command
        write_command(self.command, f"{int(unlimited)} {int(paused)}")

    def release(self):
        try:
            self.control.release()
            self.paused = False
        except (OSError, ValueError) as exc:
            self.record(f"ERROR BCCT release failed: {exc}")
            return str(exc)
        return None

    def close(self):
        error = self.release()
        # A clean stop releases hardware; retire its measurement while retaining the migration latch.
        self.publish(dict(schema=1, backend="bcct", boot_id=self.boot_id, updated_monotonic=self.clock(),
                          level=None, zcv_mv=None, paused=self.paused, valid=False, enabled=False,
                          limit=self.limit or 80, error=error, online=None, stopped=True,
                          migration_attempted=self.migrated, activity=None, activity_changed_monotonic=None))

    def publish(self, data):
        temp = None
        try:
            fd, temp = tempfile.mkstemp(prefix=".i14-charge-", dir=self.status_path.parent)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, allow_nan=False)
                os.fchmod(f.fileno(), 0o644)
            os.replace(temp, self.status_path)
        except OSError as exc:
            self.record(f"ERROR charge status unavailable: {exc}")
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)

    def tick(self):
        now = self.clock()
        cap = number(self.battery / "capacity", 0, 100)
        activity_text = read(self.gauge_activity)
        activity = int(activity_text) if re.fullmatch(r"-?[0-9]{1,6}", activity_text) else None
        if activity is not None and abs(activity) > 100000:
            activity = None
        zcv_read = number(self.gauge, 2500, 5000)
        car_text = read(Path(self.gauge_activity).parent / "FG_g_fg_dbg_bat_car")
        car = int(car_text) if re.fullmatch(r"-?[0-9]{1,6}", car_text) else None
        if car is not None and abs(car) > 100000:
            car = None
        if any(old is not None and new is not None and old != new for old, new in
               ((self.activity, activity), (self.activity_zcv, zcv_read), (self.activity_car, car))):
            self.activity_changed = now
        if activity is not None:
            self.activity = activity
        self.activity_zcv, self.activity_car = zcv_read, car
        live = (activity is not None and self.activity_changed is not None and
                0 <= now - self.activity_changed <= 180)
        stale = self.activity_changed is not None and now - self.activity_changed > 180
        if stale and not self.stale_logged:
            self.record("stale gauge activity")
        self.stale_logged = stale
        zcv = zcv_read if live else None
        gauge = ocv_percent(zcv)
        status = read(self.battery / "status")
        voltage = number(self.battery / "batt_vol", 2500, 5000)
        temp = number(self.battery / "batt_temp", 0, 450)
        kind = number(self.charger_type, 0, 8)
        usb, ac = number(self.usb, 0, 1), number(self.ac, 0, 1)
        cfg = Config(self.settings)
        limit = cfg["charge_limit"]
        feedback = read(self.readback)
        stopped = re.search(r"cmd_discharging\s*=\s*(-1|0|1)\b", feedback)
        current = re.search(r"get_usb_current_unlimited=(0|1)\b", feedback)
        readings = bool(not cfg.damaged and cfg["charge_limit_enabled"] and cap is not None and zcv is not None and
                        temp is not None and
                        kind is not None and kind > 0 and usb is not None and ac is not None and (usb or ac) and
                        status in ("Charging", "Full", "Discharging", "Not charging", "Cmd discharging") and
                        stopped is not None and current is not None)
        valid = readings and voltage is not None and voltage > 3700
        until, reason = drain_request(self.drain_path, self.boot_id, self.uptime_now(), self.drain_owner)
        if until is not None:
            reason = drain_hazard(readings, voltage, temp)
        self.drain_step(until, reason)
        error = None
        try:
            if stopped is None or current is None:
                raise ValueError("charger feedback unavailable")
            legacy = current.group(1) == "1" or stopped.group(1) == "1"
            if legacy:
                if self.migrated:
                    raise ValueError("legacy charger control changed outside i14-power")
                # Latch before I/O: failed/uncertain migration must not reset the meter every minute.
                self.migrated = True
                self.write_command(False, False)
                feedback = read(self.readback)
                if not (re.search(r"get_usb_current_unlimited=0\b", feedback) and
                        re.search(r"cmd_discharging\s*=\s*(-1|0)\b", feedback)):
                    raise ValueError("legacy migration readback failed")
            self.migrated = True
            paused = self.control.paused()
            if self.drain_until is not None:
                paused = True  # drain mode: no band, limit-change or reserve resume
            else:
                if self.limit is not None and limit != self.limit:
                    paused = False
                if not valid or gauge <= 15:
                    paused = False
                elif zcv >= dict(OCV)[limit]:
                    paused = True
                elif zcv <= dict(OCV)[limit - 10]:
                    paused = False
            if paused != self.control.paused():
                self.control.set_paused(paused)
            self.paused = self.control.paused()
        except (OSError, ValueError) as exc:
            error, valid = str(exc), False
            self.record(f"ERROR charge control failed: {exc}")
            release_error = self.release()
            if release_error:
                error += f"; release failed: {release_error}"
            if self.drain_until is not None:
                self.drain_step(None, "charge control error")
        self.limit = limit
        if gauge is None or self.level is None:
            self.level = gauge
        elif now - self.level_at >= 60:
            # Voltage relaxation must not make the screen rise while paused/discharging.
            # Match the vendor UI idea: at most one point per minute in the flow direction.
            if self.paused or activity >= 0:
                self.level = min(self.level, max(gauge, self.level - 1))
            else:
                self.level = max(self.level, min(gauge, self.level + 1))
        self.level_at = now
        state = (self.paused, valid, limit, cfg["charge_limit_enabled"], error)
        if state != self.previous or self.n % 5 == 0:
            self.record(f"capacity={cap} gauge={gauge} zcv={zcv} status={status} voltage={voltage} usb_unlimited=0 "
                        f"limit={limit} enabled={int(cfg['charge_limit_enabled'])} "
                        f"discharge={int(self.paused)} valid={int(valid)} backend=bcct")
        self.publish(dict(schema=1, backend="bcct", boot_id=self.boot_id, updated_monotonic=now,
                          level=self.level, raw_level=gauge, zcv_mv=zcv, paused=self.paused, valid=valid, limit=limit,
                          stop_mv=dict(OCV)[limit], resume_mv=dict(OCV)[limit - 10],
                          enabled=cfg["charge_limit_enabled"], error=error, migration_attempted=self.migrated,
                          online=bool(usb or ac) if usb is not None and ac is not None else None, activity=activity,
                          activity_changed_monotonic=self.activity_changed,
                          activity_zcv=self.activity_zcv, activity_car=self.activity_car,
                          drain=self.drain_until is not None, drain_until=self.drain_until))
        self.previous = state
        self.n += 1
        low = 3 if status in ("Charging", "Full") else 5
        return cap is not None and cap <= low


def main():
    guard = Guard(battery=os.environ.get("I14_BATTERY", BATTERY),
                  charger_type=os.environ.get("I14_CHARGER_TYPE", TYPE),
                  command=os.environ.get("I14_CURRENT_CMD", COMMAND),
                  readback=os.environ.get("I14_CURRENT_READBACK", READBACK),
                  usb=os.environ.get("I14_USB_ONLINE", USB), ac=os.environ.get("I14_AC_ONLINE", AC),
                  settings=os.environ.get("I14_UI_CONFIG", PATH), log=os.environ.get("I14_LOG", LOG),
                  gauge=os.environ.get("I14_FG_ZCV", GAUGE),
                  gauge_activity=os.environ.get("I14_FG_ACTIVITY", GAUGE_ACTIVITY),
                  status_path=os.environ.get("I14_CHARGE_STATUS", STATUS),
                  drain_path=os.environ.get("I14_DRAIN_REQUEST", DRAIN_REQUEST))

    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            if guard.tick():
                guard.release()
                guard.record("low battery: powering off")
                subprocess.run(["poweroff"], check=True)
                return
            time.sleep(60)
    finally:
        guard.close()
