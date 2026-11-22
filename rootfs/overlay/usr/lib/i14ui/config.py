"""UI settings on userdata: brightness, screen timeout, PIN, charge limit.

A settings file that exists but cannot be read or is damaged fails closed: the screen stays locked
(no PIN can open it) until ui.json is repaired or removed over SSH or the USB console. No file at all
means first use: no PIN.
"""
import hashlib
import hmac
import json
import os
import tempfile

from .settings_files import ensure_private_directory, open_private_settings

PATH = "/mnt/data/media/0/i14-linux/ui.json"
TIMEOUTS = [30, 60, 120, 300, 600, 0]  # seconds; 0 = never
CHARGE_LIMITS = [60, 70, 80, 90, 100]
DEFAULTS = {"brightness": 120, "timeout": 60, "pin": None,
            "charge_limit_enabled": False, "charge_limit": 80}
PIN_ROUNDS = 100_000


class Config:
    def __init__(self, path=PATH):
        self.path = path
        self.data = dict(DEFAULTS)
        self.damaged = False
        try:
            with open_private_settings(path) as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("not an object")
            self.data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
            pin = self.data["pin"]
            if pin is not None and not (isinstance(pin, dict) and {"salt", "hash", "rounds"} <= set(pin)):
                raise ValueError("bad pin entry")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeDecodeError):
            self.damaged = True
            self.data = dict(DEFAULTS)
        if self.data["timeout"] not in TIMEOUTS:
            self.data["timeout"] = DEFAULTS["timeout"]
        try:
            self.data["brightness"] = max(5, min(255, int(self.data["brightness"])))
        except (TypeError, ValueError, OverflowError):
            self.data["brightness"] = DEFAULTS["brightness"]
        if (type(self.data["charge_limit_enabled"]) is not bool or
                type(self.data["charge_limit"]) is not int or self.data["charge_limit"] not in CHARGE_LIMITS):
            self.data["charge_limit_enabled"] = False
            self.data["charge_limit"] = DEFAULTS["charge_limit"]

    def __getitem__(self, key):
        return self.data[key]

    def set(self, key, value):
        if self.damaged:
            return False  # never overwrite a damaged file (it may hold the PIN); repair it by hand
        old = dict(self.data)
        self.data[key] = value
        if self.save():
            return True
        self.data = old
        return False

    def save(self):
        d = os.path.dirname(self.path) or "."
        tmp = None
        try:
            ensure_private_directory(d)
            fd, tmp = tempfile.mkstemp(prefix=".ui-", dir=d)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False

    # PIN
    @property
    def has_pin(self):
        return self.damaged or bool(self.data.get("pin"))

    def set_pin(self, pin):
        if pin is None:
            return self.set("pin", None)
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PIN_ROUNDS)
        return self.set("pin", {"salt": salt.hex(), "hash": digest.hex(), "rounds": PIN_ROUNDS})

    def check_pin(self, pin):
        if self.damaged:
            return False
        p = self.data.get("pin")
        if not p:
            return True
        try:
            digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(p["salt"]), int(p["rounds"]))
            return hmac.compare_digest(digest.hex(), p["hash"])
        except (KeyError, TypeError, ValueError):
            return False  # a damaged entry never unlocks; remove it over SSH/USB
