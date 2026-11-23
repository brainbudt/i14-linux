"""Passive JSONL writer for one M0 trial inside a single host observer process."""

from contextlib import contextmanager
import json
import time
import uuid


class RecoveryCapture:
    """The observer supplies verified identities and owns the exclusively opened stream."""

    def __init__(self, stream, *, previous_boot, experimental_boot, kernel, image):
        if not all(isinstance(v, str) and v.strip() for v in
                   (previous_boot, experimental_boot, kernel, image)) or previous_boot == experimental_boot:
            raise ValueError("distinct boot IDs and verified production pins required")
        self.stream = stream
        self.records = 0
        self.session = str(uuid.uuid4())
        self.experimental = experimental_boot
        self.previous = previous_boot
        self.kernel = kernel
        self.image = image
        self.started = False
        self.dispatched = False
        self.returned = False
        self._write(type="contract", schema="i14-k70-recovery-trace/1",
                   host_clock_session=self.session, previous_production_boot_id=previous_boot,
                   experimental_boot_id=experimental_boot, expected_kernel=kernel, expected_image=image)

    def _write(self, **record):
        line = json.dumps(record, allow_nan=False) + "\n"
        if self.records >= 4096 or self.stream.tell() + len(line) > 1024**2:
            raise ValueError("trace limit exceeded")
        self.records += 1
        self.stream.write(line)
        self.stream.flush()

    @contextmanager
    def trigger(self, method):
        if self.started or method not in ("sysrq-crash", "keeper-stop"):
            raise ValueError("expected one named terminal trigger")
        self.started = True
        stamp = time.monotonic()
        yield  # The caller raises on a refused or uncertain dispatch.
        self._write(type="crash_trigger", method=method, clock="host_monotonic",
                   session=self.session, boot_id=self.experimental, t_s=stamp)
        self.dispatched = True

    def observe_return(self, *, boot_id, kernel, image):
        """Call at identity-probe completion with observed values. Keep failed probes separately."""
        stamp = time.monotonic()
        if not self.dispatched or self.returned:
            raise ValueError("expected a dispatched trigger and no prior confirmed return")
        confirmed = (isinstance(boot_id, str) and bool(boot_id.strip())
                     and boot_id not in (self.experimental, self.previous)
                     and kernel == self.kernel and image == self.image)
        self._write(type="production_return", confirmed=confirmed, clock="host_monotonic",
                   session=self.session, boot_id=boot_id, kernel=kernel, image=image, t_s=stamp)
        self.returned = confirmed
        return confirmed

    def stage(self, name, phase, boot_id, uptime_s):
        """Preserve the boot ID and uptime recorded by the stage producer."""
        self._write(type="stage", stage=name, phase=phase, boot_id=boot_id,
                   clock="boot_uptime", t_s=uptime_s)
