"""Bounded old-I14-LK HACC replay through caller-owned callbacks only.

No device discovery, USB, storage, clock, efuse, or newer SEJ register access.
Callbacks must have their own finite transport timeouts: Python cannot interrupt
an already blocked callback. A successful return includes complete cleanup.
"""
import math
import struct
import time
from typing import Callable

_BASE = 0x10008000
_IV = (0x9ED40400, 0x00E884A1, 0xE3F083BD, 0x2F4E6D8A)
_PATTERN = (0x2D44BB70, 0xA744D227, 0xD0A9864B, 0x83FFC244,
            0x7EC8266B, 0x43E80FB2, 0x01A6348A, 0x2067F9A0,
            0x54536405, 0xD546A6B1, 0x1CC3EC3A, 0xDE377A83)
_KEYS = tuple(range(0x20, 0x40, 4))
_OTP = tuple(range(0x60, 0x80, 4))
_READS = frozenset((0x08, 0x0C, *range(0x50, 0x60, 4)))
_WRITES = frozenset((0x04, 0x08, 0x0C, *range(0x10, 0x50, 4), *_OTP))


class HaccError(RuntimeError):
    """Replay failed; no output may be committed."""


class HaccTimeout(HaccError):
    """A responsive operation or total replay exceeded its finite budget."""


def transform(data: bytes, *, encrypt: bool,
              read32: Callable[[int], int], write32: Callable[[int, int], bool],
              timeout=120.0, ready_timeout=0.5) -> bytes:
    if not isinstance(data, bytes) or not data or len(data) % 16 or len(data) > 0x1830:
        raise ValueError("Data must be nonempty bytes, a multiple of 16, at most 0x1830 bytes")
    if type(encrypt) is not bool or not callable(read32) or not callable(write32):
        raise TypeError("Boolean mode and callable read32/write32 are required")
    for value in (timeout, ready_timeout):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Timeouts must be finite positive numbers")
    deadline = time.monotonic() + timeout
    healthy = True

    def check_deadline():
        if time.monotonic() >= deadline:
            raise HaccTimeout("HACC total deadline expired")

    def invoke(callback, *args):
        nonlocal healthy
        check_deadline()
        try:
            return callback(*args)
        except Exception as exc:
            healthy = False
            raise HaccError("HACC transport failed; cleanup incomplete, channel untrusted") from exc
        except BaseException:
            healthy = False
            raise

    def read(offset):
        nonlocal healthy
        if offset not in _READS:
            raise HaccError("Read address is outside the old-LK HACC allowlist")
        value = invoke(read32, _BASE + offset)
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            healthy = False
            raise HaccError("Invalid uint32 read; cleanup incomplete, channel untrusted")
        check_deadline()
        return value

    def write(offset, value):
        nonlocal healthy
        if offset not in _WRITES or type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise HaccError("Write is outside the old-LK HACC uint32 allowlist")
        if invoke(write32, _BASE + offset, value) is not True:
            healthy = False
            raise HaccError("Missing write ACK; cleanup incomplete, channel untrusted")
        check_deadline()

    def write_words(offset, words):
        for index, word in enumerate(words):
            write(offset + 4 * index, word)

    def wait_ready():
        ready_deadline = min(deadline, time.monotonic() + ready_timeout)
        for _ in range(4096):
            value = read(0x08)
            remaining = ready_deadline - time.monotonic()
            if remaining <= 0:
                raise HaccTimeout("HACC READY timeout")
            if value & 0x8000:
                return
            time.sleep(min(0.001, remaining))
        raise HaccTimeout("HACC READY poll limit reached")

    try:
        for offset in (*_OTP, *_KEYS):
            write(offset, 0)
        write(0x04, 2)
        write(0x0C, 0x10)
        write(0x0C, read(0x0C) | 0x100)
        write(0x08, 2)
        write_words(0x40, _IV)
        for start in (0, 4, 8):
            write_words(0x10, _PATTERN[start:start + 4])
            write(0x08, 1)
            wait_ready()
        write(0x08, 2)
        write_words(0x40, _IV)
        write(0x04, 3 if encrypt else 2)
        write(0x0C, 0)
        output = bytearray()
        for start in range(0, len(data), 16):
            write_words(0x10, struct.unpack_from("<4I", data, start))
            write(0x08, 1)
            wait_ready()
            output.extend(struct.pack("<4I", *(read(offset) for offset in range(0x50, 0x60, 4))))
        return bytes(output)
    finally:
        if healthy:
            try:
                write(0x08, 2)
                for offset in (*_KEYS, *_OTP):
                    write(offset, 0)
            except HaccTimeout as exc:
                raise HaccTimeout("HACC total deadline expired; cleanup incomplete") from exc
            except HaccError as exc:
                raise HaccError("HACC cleanup incomplete; channel untrusted") from exc
