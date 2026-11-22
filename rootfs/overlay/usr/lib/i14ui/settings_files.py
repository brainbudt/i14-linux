"""Owner and mode checks for persisted I14 settings."""

import os
import stat


def ensure_private_directory(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid():
            raise PermissionError("I14 settings directory has an unexpected owner")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def open_private_settings(path, *, protect_parent=True):
    if protect_parent:
        ensure_private_directory(os.path.dirname(path) or ".")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError("I14 settings file must be a regular file owned by the service user")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


if __name__ == "__main__":
    import sys

    for path, protect_parent in zip(sys.argv[1:], (True, False), strict=True):
        try:
            with open_private_settings(path, protect_parent=protect_parent):
                pass
        except FileNotFoundError:
            pass  # no saved networks yet
        except OSError:
            sys.exit("I14: unsafe Wi-Fi settings path or permissions")
