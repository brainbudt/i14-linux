"""Wait on local supervisor files; stdout completion wakes the parent coordinator.

Journal values are data, never instructions. Only this waiter's lock and PID
metadata are written; AFTER_SEQ remains an explicit decision by the parent.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time


MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 32
POLL_SECONDS = 2
STARTUP_GRACE_SECONDS = 4
MEANINGFUL = {"WINDOW_OPEN", "ACTION_RESERVED", "ACTION_FINISHED", "WINDOW_LOST",
              "INTEGRITY_BLOCK", "RECOVER_UNCERTAIN", "PLAN_UPDATED", "STOP"}
KNOWN_EVENTS = MEANINGFUL | {"WAIT_SOURCE"}
INSTRUCTION = ("I14_AUTONOMOUS_EVENT: read local supervisor policy and journal; "
               "existing supervisor owns phone; "
               "do not start concurrent hardware commands; ")


class LocalFileError(Exception):
    pass


class JournalLimit(LocalFileError):
    pass


def result(status, last_seq, *, events=None, reason=None, detail=None):
    rearm = status in {"EVENT", "HEARTBEAT"}
    if rearm:
        instruction = "re-arm the event waiter after processing"
    elif status == "STOPPED":
        instruction = "STOPPED: do not re-arm the event waiter"
    elif status == "ALREADY_WAITING":
        instruction = "an event waiter already owns wake-up; do not start another waiter"
    else:
        instruction = "do not re-arm the event waiter until this status is resolved"
    output = {"status": status, "last_seq": last_seq, "events": events or [],
              "rearm": rearm, "instruction": INSTRUCTION + instruction}
    if reason is not None:
        output["reason"] = reason
    if detail is not None:
        output["detail"] = detail
    return output


def read_local(path, limit, *, missing=None):
    """Bounded regular-file read without following a registered path's symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return missing
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise LocalFileError()
        if info.st_size > limit:
            raise JournalLimit()
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        raise JournalLimit()
    finally:
        os.close(fd)


def unique_object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError()
        output[key] = value
    return output


def reject_constant(value):
    raise ValueError()


def safe_data(data, state_dir):
    output = {}
    for key in ("status", "verdict", "action", "phase"):
        value = data.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
            output[key] = value
    if type(data.get("window")) is int and data["window"] >= 0:
        output["window"] = data["window"]
    for key in ("out", "path", "evidence_path", "result_path"):
        value = data.get(key)
        if not isinstance(value, str) or len(value) > 512:
            continue
        # Evidence references are only text; never open any path from an event.
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", value):
            continue
        path = PurePosixPath(value)
        if ".." in path.parts:
            continue
        if path.is_absolute():
            try:
                path = path.relative_to(state_dir.absolute())
            except ValueError:
                continue
        if str(path) != ".":
            output[key] = str(path)
    return output


def read_journal(state_dir, after_seq):
    raw = read_local(state_dir / "events.jsonl", MAX_JOURNAL_BYTES, missing=b"")
    # A writer may be between write and newline/fsync. Leave that tail pending.
    complete_lines = raw.split(b"\n")[:-1]
    previous = 0
    selected = []
    for line in complete_lines:
        record = json.loads(line, object_pairs_hook=unique_object,
                            parse_constant=reject_constant)
        if (not isinstance(record, dict) or type(record.get("seq")) is not int
                or record["seq"] <= previous or not isinstance(record.get("type"), str)
                or record["type"] not in KNOWN_EVENTS or not isinstance(record.get("data"), dict)
                or not isinstance(record.get("utc"), str)):
            raise ValueError()
        timestamp = datetime.fromisoformat(record["utc"])
        if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
            raise ValueError()
        previous = record["seq"]
        if previous > after_seq and record["type"] in MEANINGFUL and len(selected) < MAX_EVENTS:
            selected.append({"seq": previous, "type": record["type"],
                             "data": safe_data(record["data"], state_dir)})
    if previous < after_seq:
        raise ValueError()
    return selected, previous


def probe_pid(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def poll_events(state_dir, after_seq, timeout, clock, sleep, pid_alive):
    started = clock()
    deadline = started + timeout
    expected_pid = None
    known_last_seq = after_seq
    while True:
        if os.path.lexists(state_dir / "STOP"):
            return result("STOPPED", after_seq)
        try:
            events, last_seq = read_journal(state_dir, after_seq)
            if last_seq < known_last_seq:
                raise ValueError()
            known_last_seq = last_seq
        except JournalLimit:
            return result("JOURNAL_LIMIT", after_seq, reason="UNKNOWN", detail="journal_size_limit")
        except (OSError, LocalFileError, ValueError, RecursionError):
            return result("JOURNAL_ERROR", after_seq, reason="UNKNOWN", detail="journal_invalid")
        if any(item["type"] == "STOP" for item in events):
            return result("STOPPED", events[-1]["seq"], events=events)
        try:
            raw_pid = read_local(state_dir / "pid", 32)
            if raw_pid is None:
                initial = (expected_pid is None and not os.path.lexists(state_dir / "state.json")
                           and not os.path.lexists(state_dir / "events.jsonl"))
                grace = min(STARTUP_GRACE_SECONDS, timeout)
                if not initial or clock() - started >= grace:
                    return result("UNKNOWN", after_seq, reason="UNKNOWN", detail="supervisor_pid_missing")
                sleep(min(POLL_SECONDS, grace - (clock() - started)))
                continue
            if not re.fullmatch(rb"[1-9][0-9]{0,9}\n?", raw_pid):
                raise LocalFileError()
            pid = int(raw_pid)
            if pid > 2147483647:
                raise LocalFileError()
            if expected_pid is not None and pid != expected_pid:
                return result("UNKNOWN", after_seq, reason="UNKNOWN", detail="supervisor_pid_changed")
            expected_pid = pid
            if not pid_alive(pid):
                return result("SUPERVISOR_GONE", after_seq, reason="UNKNOWN")
        except (OSError, LocalFileError, ValueError):
            return result("UNKNOWN", after_seq, reason="UNKNOWN", detail="supervisor_pid_unverifiable")
        if events:
            return result("EVENT", events[-1]["seq"], events=events)
        remaining = deadline - clock()
        if remaining <= 0:
            return result("HEARTBEAT", known_last_seq)
        sleep(min(POLL_SECONDS, remaining))


def open_metadata(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise LocalFileError()
    return fd


def wait_events(state_dir, after_seq, timeout=300, *, clock=time.monotonic,
                sleep=time.sleep, pid_alive=probe_pid):
    if type(after_seq) is not int or after_seq < 0 or type(timeout) is not int or not 1 <= timeout <= 3600:
        return result("UNKNOWN", max(0, after_seq) if type(after_seq) is int else 0,
                      reason="UNKNOWN", detail="invalid_arguments")
    state_dir = Path(state_dir)
    lock_fd = None
    own_pid_info = None
    try:
        lock_fd = open_metadata(state_dir / "event-waiter.lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return result("ALREADY_WAITING", after_seq)
        pid_fd = open_metadata(state_dir / "event-waiter.pid")
        try:
            os.ftruncate(pid_fd, 0)
            os.write(pid_fd, (str(os.getpid()) + "\n").encode("ascii"))
            os.fsync(pid_fd)
            own_pid_info = os.fstat(pid_fd)
        finally:
            os.close(pid_fd)
        return poll_events(state_dir, after_seq, timeout, clock, sleep, pid_alive)
    except (OSError, LocalFileError):
        return result("UNKNOWN", after_seq, reason="UNKNOWN", detail="waiter_metadata_unavailable")
    finally:
        if own_pid_info is not None:
            try:
                info = os.lstat(state_dir / "event-waiter.pid")
                if (info.st_dev, info.st_ino) == (own_pid_info.st_dev, own_pid_info.st_ino):
                    os.unlink(state_dir / "event-waiter.pid")
            except OSError:
                pass
        if lock_fd is not None:
            os.close(lock_fd)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) not in (2, 3):
            raise ValueError()
        after_seq = int(argv[1])
        timeout = int(argv[2]) if len(argv) == 3 else 300
        if after_seq < 0 or not 1 <= timeout <= 3600:
            raise ValueError()
    except ValueError:
        print(json.dumps(result("UNKNOWN", 0, reason="UNKNOWN", detail="invalid_arguments"), sort_keys=True))
        return 2
    print(json.dumps(wait_events(Path(argv[0]), after_seq, timeout), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
