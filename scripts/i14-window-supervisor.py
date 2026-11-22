"""Durable host dispatcher; the fixed, pinned collector owns all device access.

Run: python3 -I -B scripts/i14-window-supervisor.py STATE_DIR PACKAGE_MANIFEST
Stop: create STATE_DIR/STOP or SIGTERM this supervisor. No service is installed.
"""
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ["power_evidence", "cached_baseline"]
POLL_SECONDS = 2
WORKER_WAIT_SECONDS = 5
WORKER_TIMEOUT_SECONDS = 35  # 5s wait, bounded 20s transport, metadata/exit margin.


class IntegrityError(Exception):
    """Host package/state is inconsistent; no device action is authorized."""


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise IntegrityError("unreadable JSON: " + str(path)) from error


def package_at(path):
    package = read_json(path)
    required = {"version", "reviewed", "worker_sha256", "actions"}
    if not isinstance(package, dict) or not required <= set(package) \
            or set(package) - required - {"source_sha256"} \
            or type(package["version"]) is not int or package["version"] != 1 \
            or package["reviewed"] is not True or package["actions"] != ACTIONS:
        raise IntegrityError("manifest must authorize only the reviewed version-1 read-only actions")
    pins = package.get("source_sha256", {})
    if not isinstance(pins, dict):
        raise IntegrityError("source_sha256 must be an object")
    for name, sha in {"scripts/wait-native-cached-once.py": package["worker_sha256"], **pins}.items():
        if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts \
                or not name or not (ROOT / name).resolve().is_relative_to(ROOT.resolve()) \
                or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise IntegrityError("invalid source pin")
    if "scripts/wait-native-cached-once.py" in pins \
            and pins["scripts/wait-native-cached-once.py"] != package["worker_sha256"]:
        raise IntegrityError("conflicting worker pins")
    return package, digest(encoded(package))


def receipt_at(out):
    try:
        result = json.loads((out / "result.json").read_bytes())
    except (OSError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def valid_receipt(result):
    return (isinstance(result, dict) and type(result.get("attempted")) is bool
            and type(result.get("capture_complete")) is bool and type(result.get("exit_code")) is int
            and isinstance(result.get("verdict"), str) and result.get("charging_blocked") is True
            and result.get("ready_for_charging") is False)


def complete_receipt(result, action, rc):
    if not valid_receipt(result):
        return False
    identity, storage, boot, cached = (result.get(name) for name in ("identity", "storage", "boot", "cached_diagnostics"))
    verdict = "COMPLETE_POWER_EVIDENCE" if action == "power_evidence" else "COMPLETE_CACHED_BASELINE"
    return (rc == 0 and result["exit_code"] == 0 and result["verdict"] == verdict
            and result["attempted"] is True and result["capture_complete"] is True
            and isinstance(identity, dict) and identity.get("ok") is True
            and isinstance(storage, dict) and all(storage.get(k) is True for k in ("capacity_ok", "names_ok", "all_ro"))
            and isinstance(boot, dict) and isinstance(boot.get("boot_id"), str)
            and re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot["boot_id"]) is not None
            and isinstance(cached, dict) and cached.get("historical_only") is True
            and all(isinstance(cached.get(k), dict) and cached[k].get("status") in {"PRESENT", "ABSENT", "UNKNOWN"}
                    for k in ("charger_state", "battery_sample"))
            and result.get("failure_kind") != "integrity" and result.get("integrity_ok") is not False
            and (action != "power_evidence" or (isinstance(result.get("power_evidence"), dict)
                 and isinstance(result["power_evidence"].get("kernel_log"), dict)
                 and result["power_evidence"]["kernel_log"].get("status") in {"PRESENT", "ABSENT", "UNREADABLE"}
                 and boot.get("same_boot") is True and boot.get("end_boot_id") == boot["boot_id"])))


class Supervisor:
    def __init__(self, state_dir, manifest_path, *, worker=None, clock=time.monotonic, popen=subprocess.Popen):
        self.directory = Path(state_dir).resolve()
        self.manifest_path = Path(manifest_path).resolve()
        self.worker_path = ROOT / "scripts/wait-native-cached-once.py"
        self.clock, self.popen = clock, popen
        self.process = None
        self.streams = []
        self.started_at = None
        self.last_scan = None
        self.stop_requested = False
        self.closed = False
        self.lock_fd = None
        self.directory.mkdir(parents=True, exist_ok=True)
        sync_directory(self.directory.parent)
        try:
            self.lock_fd = os.open(self.directory / "supervisor.lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.package, self.package_sha = package_at(self.manifest_path)
            self.check_pins()
            pin_path = self.directory / "package-pin.json"
            state_path = self.directory / "state.json"
            if pin_path.exists():
                if read_json(pin_path) != {"version": 1, "package_sha256": self.package_sha,
                                           "manifest": self.package}:
                    raise IntegrityError("package changed; use an explicitly reviewed new state epoch")
            elif state_path.exists():
                raise IntegrityError("state exists without its package pin")
            else:
                atomic_write(pin_path, encoded({"version": 1, "package_sha256": self.package_sha,
                                               "manifest": self.package}))
            if state_path.exists():
                self.state = read_json(state_path)
                self.validate_state()
            else:
                journal = self.directory / "events.jsonl"
                if journal.exists() and journal.stat().st_size:
                    raise IntegrityError("journal exists without state; preserve epoch for parent reconciliation")
                self.state = {"version": 1, "poll_seconds": POLL_SECONDS, "window_seq": 0,
                              "current_window": None, "attachment_open": False, "absent_streak": 0,
                              "reserved": None, "inflight": None, "results": [], "last_boot": None,
                              "phase": "WAIT_DEVICE", "event_seq": 0, "package_sha256": self.package_sha,
                              "global_block": None, "wait_reason": None, "recovery": None,
                              "worker_active_at_stop": False}
            self.reconcile_journal()
            # A host restart breaks continuity; require three fresh absent observations.
            self.state["absent_streak"] = 0
            self.save()
            # Hash verification precedes import, including its top-level code.
            self.check_pins()
            if worker is None:
                spec = importlib.util.spec_from_file_location("i14_reviewed_collector", self.worker_path)
                worker = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(worker)
            self.worker = worker
            self.recover()
            atomic_write(self.directory / "pid", (str(os.getpid()) + "\n").encode())
            if self.state["phase"] == "STOPPED":
                window = self.state["current_window"]
                self.state["phase"] = "INTEGRITY_BLOCK" if self.state["global_block"] else \
                    "BLOCKED_WINDOW" if self.state["recovery"] or (window and window["blocked"]) else \
                    "WAIT_DEVICE" if not self.state["attachment_open"] else \
                    "COMPLETE" if any(a["status"] == "COMPLETE" for a in window["actions"].values()) else "READY"
                self.save()
        except BaseException:
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
            raise

    def validate_state(self):
        state = self.state
        required = {"version", "poll_seconds", "window_seq", "current_window", "attachment_open",
                    "absent_streak", "reserved", "inflight", "results", "last_boot", "phase", "event_seq",
                    "package_sha256", "global_block", "wait_reason", "recovery", "worker_active_at_stop"}
        if not isinstance(state, dict) or not required <= state.keys() \
                or type(state["version"]) is not int or state["version"] != 1 \
                or state["package_sha256"] != self.package_sha or state["poll_seconds"] != POLL_SECONDS \
                or type(state["attachment_open"]) is not bool or type(state["window_seq"]) is not int \
                or state["window_seq"] < 0 or type(state["absent_streak"]) is not int \
                or not 0 <= state["absent_streak"] <= 3 or not isinstance(state["results"], list) \
                or type(state["event_seq"]) is not int or state["event_seq"] < 0:
            raise IntegrityError("invalid persistent state")
        window = state["current_window"]
        if window is not None and (not isinstance(window, dict) or type(window.get("id")) is not int
                                   or window["id"] != state["window_seq"] or window["id"] <= 0
                                   or not isinstance(window.get("actions"), dict)
                                   or type(window.get("blocked")) is not bool):
            raise IntegrityError("invalid current window")
        if window is not None:
            for action, info in window["actions"].items():
                if action not in ACTIONS or not isinstance(info, dict) \
                        or info.get("status") not in {"RESERVED", "UNCERTAIN", "COMPLETE", "SAFE_NO_COMMAND", "BLOCKED"}:
                    raise IntegrityError("invalid durable action state")
        if state["attachment_open"] and window is None:
            raise IntegrityError("open attachment without a window")
        for pending in (state["reserved"], state["inflight"]):
            if pending is not None and (not isinstance(pending, dict) or pending.get("action") not in ACTIONS
                                       or type(pending.get("window")) is not int
                                       or pending["window"] != state["window_seq"]
                                       or pending.get("out") != str(self.action_path(pending["window"], pending["action"]))):
                raise IntegrityError("invalid pending reservation")

    def check_pins(self):
        package, sha = package_at(self.manifest_path)
        if sha != self.package_sha or package != self.package:
            raise IntegrityError("manifest changed during epoch")
        pins = {**package.get("source_sha256", {}),
                "scripts/wait-native-cached-once.py": package["worker_sha256"]}
        for name, expected in pins.items():
            try:
                actual = digest((ROOT / name).read_bytes())
            except OSError as error:
                raise IntegrityError("source unavailable: " + name) from error
            if actual != expected:
                raise IntegrityError("source pin mismatch: " + name)

    def reconcile_journal(self):
        path = self.directory / "events.jsonl"
        seq = 0
        if path.exists():
            for line in path.read_bytes().splitlines():
                try:
                    item = json.loads(line)
                    if type(item["seq"]) is not int or item["seq"] != seq + 1 \
                            or not isinstance(item["type"], str) or not isinstance(item["data"], dict):
                        raise ValueError("event ordering")
                    seq = item["seq"]
                except (ValueError, KeyError, TypeError) as error:
                    raise IntegrityError("journal incomplete or inconsistent; preserve it for review") from error
        if self.state["event_seq"] > seq:
            raise IntegrityError("state refers to missing journal events")
        self.state["event_seq"] = seq

    def save(self):
        atomic_write(self.directory / "state.json", encoded(self.state))

    def emit(self, kind, **data):
        event = {"seq": self.state["event_seq"] + 1, "utc": utc(), "type": kind, "data": data}
        with (self.directory / "events.jsonl").open("ab") as stream:
            stream.write((json.dumps(event, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.directory)
        self.state["event_seq"] = event["seq"]
        self.save()

    def action_path(self, window, action):
        return self.directory / "windows" / ("window-%06d" % window) / action

    def block_integrity(self, reason):
        if self.state["global_block"] is None:
            self.state["global_block"] = reason
            self.state["phase"] = "INTEGRITY_BLOCK"
            self.save()
            self.emit("INTEGRITY_BLOCK", reason=reason)
            self.plan("Integrity/identity failure; device actions blocked until parent repair")

    def recover(self):
        pending = self.state["inflight"] or self.state["reserved"]
        if pending is None:
            return
        result = receipt_at(Path(pending["out"]))
        record = {**pending, "status": "UNCERTAIN", "receipt": result,
                  "returncode": None, "recovered_utc": utc()}
        self.state["results"].append(record)
        self.state["recovery"] = {**pending, "status": "UNCERTAIN", "process_status": "unknown; never signalled"}
        self.state["current_window"]["blocked"] = True
        self.state["current_window"]["actions"][pending["action"]] = {"status": "UNCERTAIN"}
        self.state["reserved"] = self.state["inflight"] = None
        self.state["phase"] = "INTEGRITY_BLOCK" if self.state["global_block"] else "BLOCKED_WINDOW"
        self.save()
        self.emit("RECOVER_UNCERTAIN", window=pending["window"], action=pending["action"],
                  out=pending["out"], pid=pending.get("pid"), receipt_available=result is not None)
        self.inspect_failure(result)
        self.plan("Recovered reservation; result/process completion uncertain; never redispatch this window")

    def inspect_failure(self, result):
        if not isinstance(result, dict):
            return
        identity = result.get("identity")
        reasons = result.get("stop_reasons", [])
        framing = result.get("framing")
        explicit_refusal = (result.get("verdict") in ("STOP_IDENTITY", "STOP_CONTRADICTION")
                            or (isinstance(framing, dict)
                                and type(framing.get("I14_STOP_IDENTITY")) is int
                                and framing["I14_STOP_IDENTITY"] > 0))
        if explicit_refusal or (isinstance(identity, dict) and identity.get("ok") is False):
            self.block_integrity("worker identity refused")
        elif result.get("failure_kind") == "integrity" or result.get("integrity_ok") is False \
                or (isinstance(reasons, list) and any(isinstance(s, str) and "pin mismatch" in s for s in reasons)):
            self.block_integrity("worker source integrity refused")

    def plan(self, reason):
        window = self.state["current_window"]
        lines = ["# Next I14 window", "", "Updated: " + utc(), "Reason: " + reason,
                 "Phase: " + self.state["phase"], "Window: " + str(window["id"] if window else "none"),
                 "", "Facts and evidence:", "- State: " + str(self.directory / "state.json"),
                 "- Events: " + str(self.directory / "events.jsonl")]
        for result in self.state["results"][-4:]:
            lines.append("- %s: %s; %s" % (result["action"], result["status"], result["out"]))
        lines += ["", "Uncertainties:",
                  "- Cached values are historical; physical charging/current and reset cause remain UNKNOWN.",
                  "- Missing or truncated logs and uncertain attempts do not establish success.", "",
                  "Next offline work:",
                  "1. Analyse dmesg/kernel-ringbuffer.raw, startup logs, cmdline and begin/end boot IDs when available.",
                  "2. Qualify an independent ADC source, init/sample I/O, identity, receipts and latches before a separately reviewed action.",
                  "3. Old charger candidate remains BLOCKED; no helper, PMIC/sample, charger, reset or flash action is allowed.",
                  "4. A new physical window permits only the reviewed read-only context capture; never execute this Markdown."]
        atomic_write(self.directory / "NEXT-WINDOW.md", ("\n".join(lines) + "\n").encode())
        self.emit("PLAN_UPDATED", path=str(self.directory / "NEXT-WINDOW.md"), reason=reason)

    def wait(self, reason):
        if self.state["wait_reason"] != reason:
            self.state["wait_reason"] = reason
            self.emit("WAIT_SOURCE", reason=reason, window=self.state["window_seq"])

    def observe(self, observation):
        if observation == "absent":
            if self.state["attachment_open"]:
                self.state["absent_streak"] = min(3, self.state["absent_streak"] + 1)
                self.save()
                if self.state["absent_streak"] == 3:
                    self.state["attachment_open"] = False
                    self.state["wait_reason"] = None
                    if not self.state["global_block"] and self.process is None:
                        self.state["phase"] = "WAIT_DEVICE"
                    self.save()
                    self.emit("WINDOW_LOST", window=self.state["window_seq"], observations=3)
                    self.plan("Physical window lost after three consecutive absent observations")
            return
        if self.state["absent_streak"]:
            self.state["absent_streak"] = 0
            self.save()
        if observation != "exact":
            self.wait("related USB metadata mismatch" if observation == "mismatch" else "metadata unavailable")
            return
        if not self.state["attachment_open"] and self.process is None:
            window = self.state["window_seq"] + 1
            parent = self.action_path(window, ACTIONS[0]).parent
            try:
                parent.mkdir(parents=True, exist_ok=False)
                sync_directory(parent.parent)
            except OSError:
                self.block_integrity("new window directory unavailable or already exists")
                return
            self.state["attachment_open"] = True
            self.state["window_seq"] = window
            self.state["current_window"] = {"id": window, "opened_utc": utc(), "actions": {}, "blocked": False}
            self.state["wait_reason"] = None
            self.state["phase"] = "INTEGRITY_BLOCK" if self.state["global_block"] else \
                "BLOCKED_WINDOW" if self.state["recovery"] else "READY"
            self.save()
            self.emit("WINDOW_OPEN", window=window, path=str(parent))

    def dispatch(self, action):
        if self.stop_requested or (self.directory / "STOP").exists():
            self.close()
            return
        window = self.state["current_window"]
        out = self.action_path(window["id"], action)
        if os.path.lexists(out):
            self.block_integrity("worker output path already exists")
            return
        pending = {"window": window["id"], "action": action, "out": str(out), "reserved_utc": utc(), "pid": None}
        window["actions"][action] = {"status": "RESERVED", "out": str(out)}
        self.state["reserved"] = pending
        self.state["phase"] = "RESERVED"
        self.save()  # Custody fence must be durable before Popen or its second pin check.
        self.emit("ACTION_RESERVED", **pending)
        try:
            self.check_pins()
        except IntegrityError as error:
            self.block_integrity(str(error))
            return
        if self.stop_requested or (self.directory / "STOP").exists():
            self.close()
            return
        args = [sys.executable, "-I", "-B", str(self.worker_path), str(out), str(WORKER_WAIT_SECONDS)]
        if action == "power_evidence":
            args.append("--power-evidence")
        try:
            for suffix in (".stdout", ".stderr"):
                self.streams.append(open(out.parent / (action + suffix), "xb"))
            sync_directory(out.parent)
            self.process = self.popen(args, stdout=self.streams[0], stderr=self.streams[1], close_fds=True, shell=False)
        except Exception as error:
            self.state["current_window"]["blocked"] = True
            self.state["phase"] = "BLOCKED_WINDOW"
            self.state["launch_error"] = type(error).__name__
            self.save()
            self.emit("ACTION_FINISHED", **pending, status="UNCERTAIN", reason="launch failed")
            self.plan("Launch outcome uncertain; reservation remains terminal")
            self.close_streams()
            return
        self.started_at = self.clock()
        self.state["inflight"] = {**pending, "pid": self.process.pid, "started_utc": utc(),
                                  "timeout_seconds": WORKER_TIMEOUT_SECONDS, "term_sent": False}
        self.state["phase"] = "INFLIGHT"
        self.save()

    def close_streams(self):
        for stream in self.streams:
            stream.close()
        self.streams = []

    def reap(self):
        if self.process is None:
            return
        rc = self.process.poll()
        pending = self.state["inflight"]
        if rc is None:
            if self.clock() - self.started_at >= WORKER_TIMEOUT_SECONDS and not pending["term_sent"]:
                pending["term_sent"] = True
                pending["timeout_utc"] = utc()
                self.state["current_window"]["blocked"] = True
                self.state["phase"] = "BLOCKED_WINDOW"
                self.save()
                try:
                    self.process.terminate()  # Only this Popen; never a recovered PID or process group.
                except ProcessLookupError:
                    pass  # It exited between poll and signal; reap on the next tick.
                self.plan("Owned collector exceeded host deadline; SIGTERM sent; outcome uncertain")
            return
        self.process.wait(timeout=0)
        self.process = None
        self.close_streams()
        out = Path(pending["out"])
        result = receipt_at(out)
        valid = valid_receipt(result)
        complete = (not pending["term_sent"] and os.path.lexists(out / "attempt.json")
                    and complete_receipt(result, pending["action"], rc))
        safe = (not pending["term_sent"] and pending["action"] == "power_evidence" and valid
                and rc == 3 and result["exit_code"] == 3 and result["verdict"] == "TIMEOUT_NO_COMMAND"
                and result["attempted"] is False and result["capture_complete"] is False
                and not os.path.lexists(out / "attempt.json"))
        status = "COMPLETE" if complete else "SAFE_NO_COMMAND" if safe else "BLOCKED"
        record = {**pending, "finished_utc": utc(), "returncode": rc, "status": status, "receipt": result}
        self.state["results"].append(record)
        window = self.state["current_window"]
        window["actions"][pending["action"]] = {"status": status, "out": str(out)}
        if complete:
            self.state["last_boot"] = result["boot"]["boot_id"]
        elif not safe:
            window["blocked"] = True
        # Keep the durable dispatch fence until failure classification is saved.
        self.inspect_failure(result)
        self.state["reserved"] = self.state["inflight"] = None
        self.state["phase"] = "COMPLETE" if complete else "READY" if safe else "BLOCKED_WINDOW"
        self.save()
        self.emit("ACTION_FINISHED", window=pending["window"], action=pending["action"], out=str(out),
                  status=status, returncode=rc, verdict=result.get("verdict") if result else None)
        if self.state["global_block"]:
            self.state["phase"] = "INTEGRITY_BLOCK"
            self.save()
        self.plan("Action finished: " + status + "; no uncertain action is retried")

    def tick(self):
        if self.closed:
            return False
        if self.stop_requested or (self.directory / "STOP").exists():
            self.close()
            return False
        self.reap()
        if self.last_scan is not None and self.clock() - self.last_scan < POLL_SECONDS:
            return True
        self.last_scan = self.clock()
        try:
            self.check_pins()
        except IntegrityError as error:
            self.block_integrity(str(error))
            return True
        try:
            observation = self.worker.classify(self.worker.scan())
        except Exception:
            observation = "unknown"
        self.observe(observation)
        window = self.state["current_window"]
        if observation != "exact" or not self.state["attachment_open"] or window is None \
                or self.process is not None or self.state["reserved"] is not None \
                or window["blocked"] or self.state["global_block"] or self.state["recovery"]:
            return True
        actions = window["actions"]
        action = "power_evidence" if "power_evidence" not in actions else \
            "cached_baseline" if actions["power_evidence"]["status"] == "SAFE_NO_COMMAND" \
            and "cached_baseline" not in actions else None
        if action is None:
            return True
        try:
            ready = self.worker.source_ready()
        except Exception:
            ready = False
        if not ready:
            self.wait("source address not bindable")
            return True
        self.state["wait_reason"] = None
        self.dispatch(action)
        return not self.closed

    def request_stop(self, signum=None, frame=None):
        self.stop_requested = True

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.process is not None:
                pending = self.state["inflight"]
                if self.process.poll() is None:
                    if not pending["term_sent"]:
                        pending["term_sent"] = True
                        self.save()
                        try:
                            self.process.terminate()
                        except ProcessLookupError:
                            pass  # It exited between poll and signal; still reap below.
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.state["worker_active_at_stop"] = True
                if self.process.poll() is not None:
                    self.reap()
            self.state["phase"] = "STOPPED"
            self.save()
            self.emit("STOP", owned_worker_active=self.state["worker_active_at_stop"],
                      owned_pid=self.state["inflight"].get("pid") if self.state["inflight"] else None,
                      recovered_pid=self.state["recovery"].get("pid") if self.state["recovery"] else None)
        finally:
            self.close_streams()
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None


def main(argv):
    if len(argv) != 2:
        print("usage: i14-window-supervisor.py STATE_DIR PACKAGE_MANIFEST", file=sys.stderr)
        return 5
    supervisor = None
    try:
        supervisor = Supervisor(argv[0], argv[1])
        signal.signal(signal.SIGTERM, supervisor.request_stop)
        signal.signal(signal.SIGINT, supervisor.request_stop)
        while supervisor.tick():
            time.sleep(POLL_SECONDS)
        return 0
    except (IntegrityError, OSError) as error:
        print("REFUSED: " + str(error), file=sys.stderr)
        return 5
    finally:
        if supervisor is not None:
            supervisor.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
