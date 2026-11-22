"""A login shell in a pseudo-terminal, fed into a VT."""
import errno
import fcntl
import os
import pty
import signal
import struct
import termios

from . import vt

SHELL = ["/bin/sh", "-l"]
_ended = []  # shells that got SIGHUP but were not reaped yet


def reap():
    """Collect ended shells (called from the main loop)."""
    for pid in list(_ended):
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            done = pid
        if done:
            _ended.remove(pid)


class Session:
    def __init__(self, cols, rows, argv=None, env=None):
        self.vt = vt.VT(cols, rows, reply=self.write)
        self.argv = argv or SHELL
        self.env = env
        self.fd = None
        self.pid = None
        self.start()

    def start(self):
        env = dict(self.env if self.env is not None else os.environ)
        env.update({"TERM": "vt102", "HOME": env.get("HOME", "/root"), "LANG": "C.UTF-8"})
        pid, fd = pty.fork()
        if pid == 0:  # child: a fresh session with the pty as controlling terminal
            try:
                os.chdir(env["HOME"])
            except OSError:
                pass
            # the app ignores SIGHUP; an ignored signal survives exec, so give the shell its default back
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGPIPE, signal.SIGCHLD):
                signal.signal(sig, signal.SIG_DFL)
            try:
                os.execvpe(self.argv[0], self.argv, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        self.set_size(self.vt.cols, self.vt.rows)

    def set_size(self, cols, rows):
        self.vt.resize(cols, rows)
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

    @property
    def alive(self):
        return self.fd is not None

    def read(self):
        """Read what is waiting; returns False when the shell has ended."""
        if self.fd is None:
            return False
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return True
        except OSError as e:
            if e.errno != errno.EIO:
                raise
            data = b""
        if not data:
            self.close()
            return False
        self.vt.feed(data)
        return True

    def write(self, data):
        if self.fd is None or not data:
            return
        try:
            while data:
                n = os.write(self.fd, data)
                data = data[n:]
        except BlockingIOError:
            pass  # the shell is not reading; drop the rest rather than block the screen
        except OSError:
            self.close()

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except OSError:
                pass
            _ended.append(self.pid)
            self.pid = None
            reap()
