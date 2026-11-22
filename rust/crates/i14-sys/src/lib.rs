//! Small checked wrappers around the Linux interfaces the I14 software uses.
//!
//! Every `unsafe` call lives here, behind a function that checks its result and
//! returns `io::Result`. Policy code elsewhere stays safe and testable on a host.

use std::ffi::CStr;
use std::fs::File;
use std::io;
use std::os::fd::{AsRawFd, RawFd};

/// Turns a -1 return from libc into the current `errno`.
fn check(ret: libc::c_int) -> io::Result<libc::c_int> {
    if ret < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(ret)
    }
}

pub mod fb {
    //! Linux framebuffer (`/dev/fb0`) geometry.
    use super::*;

    pub const FBIOGET_VSCREENINFO: libc::c_ulong = 0x4600;
    pub const FBIOGET_FSCREENINFO: libc::c_ulong = 0x4602;

    /// The fields of `struct fb_var_screeninfo` the I14 code needs.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct VarInfo {
        pub xres: u32,
        pub yres: u32,
        pub xres_virtual: u32,
        pub yres_virtual: u32,
        pub bits_per_pixel: u32,
    }

    /// `struct fb_var_screeninfo` is 160 bytes of u32 fields.
    pub fn var_info(fd: &File) -> io::Result<VarInfo> {
        let mut raw = [0u32; 40];
        // SAFETY: FBIOGET_VSCREENINFO writes one fb_var_screeninfo (160 bytes) into raw.
        check(unsafe { libc::ioctl(fd.as_raw_fd(), FBIOGET_VSCREENINFO as _, raw.as_mut_ptr()) })?;
        Ok(VarInfo {
            xres: raw[0],
            yres: raw[1],
            xres_virtual: raw[2],
            yres_virtual: raw[3],
            bits_per_pixel: raw[6],
        })
    }

    /// `line_length` and `smem_len` from `struct fb_fix_screeninfo`.
    pub fn fix_info(fd: &File) -> io::Result<(u32, u32)> {
        // 16-byte id, unsigned long smem_start, u32 smem_len, ... u32 line_length at offset 48 (arm64).
        let mut raw = [0u8; 80];
        // SAFETY: FBIOGET_FSCREENINFO writes one fb_fix_screeninfo (68 or 80 bytes) into raw.
        check(unsafe { libc::ioctl(fd.as_raw_fd(), FBIOGET_FSCREENINFO as _, raw.as_mut_ptr()) })?;
        let u32_at = |o: usize| u32::from_ne_bytes(raw[o..o + 4].try_into().unwrap());
        Ok((u32_at(48), u32_at(24)))
    }

    pub const FBIOPAN_DISPLAY: libc::c_ulong = 0x4606;

    /// The raw `struct fb_var_screeninfo`, for handing back to FBIOPAN_DISPLAY.
    pub fn var_raw(fd: &File) -> io::Result<[u32; 40]> {
        let mut raw = [0u32; 40];
        // SAFETY: FBIOGET_VSCREENINFO writes one fb_var_screeninfo (160 bytes) into raw.
        check(unsafe { libc::ioctl(fd.as_raw_fd(), FBIOGET_VSCREENINFO as _, raw.as_mut_ptr()) })?;
        Ok(raw)
    }

    /// Shows the buffer at (xoffset, yoffset) = (0, 0).
    pub fn pan_to_origin(fd: &File, raw: &mut [u32; 40]) -> io::Result<()> {
        raw[4] = 0; // xoffset
        raw[5] = 0; // yoffset
                    // SAFETY: FBIOPAN_DISPLAY reads one fb_var_screeninfo.
        check(unsafe { libc::ioctl(fd.as_raw_fd(), FBIOPAN_DISPLAY as _, raw.as_mut_ptr()) })?;
        Ok(())
    }

    /// A shared read-write mapping of the start of the framebuffer.
    pub struct Map {
        ptr: *mut libc::c_void,
        len: usize,
    }

    impl Map {
        pub fn new(fd: &File, len: usize) -> io::Result<Self> {
            // SAFETY: a fresh shared mapping; checked against MAP_FAILED.
            let ptr = unsafe {
                libc::mmap(
                    std::ptr::null_mut(),
                    len,
                    libc::PROT_READ | libc::PROT_WRITE,
                    libc::MAP_SHARED,
                    fd.as_raw_fd(),
                    0,
                )
            };
            if ptr == libc::MAP_FAILED {
                return Err(io::Error::last_os_error());
            }
            Ok(Self { ptr, len })
        }

        /// Copies `src` to byte `offset` of the mapping (panics past its end).
        /// No `&mut [u8]` is handed out: other mappings of the same framebuffer
        /// (another process, or a second Map) may write it too.
        pub fn write_at(&mut self, offset: usize, src: &[u8]) {
            assert!(
                offset
                    .checked_add(src.len())
                    .is_some_and(|end| end <= self.len),
                "write past the mapping"
            );
            // SAFETY: offset..offset+len is inside the live writable mapping; raw-pointer
            // copy, so no reference asserts exclusive access to the shared memory.
            unsafe {
                std::ptr::copy_nonoverlapping(
                    src.as_ptr(),
                    (self.ptr as *mut u8).add(offset),
                    src.len(),
                )
            };
        }
    }

    impl Drop for Map {
        fn drop(&mut self) {
            // SAFETY: unmapping exactly what new() mapped.
            unsafe { libc::munmap(self.ptr, self.len) };
        }
    }

    /// A read-only mapping of the start of the framebuffer.
    pub struct ReadMap {
        ptr: *mut libc::c_void,
        len: usize,
    }

    impl ReadMap {
        pub fn new(fd: &File, len: usize) -> io::Result<Self> {
            // SAFETY: a fresh shared read-only mapping; checked against MAP_FAILED.
            let ptr = unsafe {
                libc::mmap(
                    std::ptr::null_mut(),
                    len,
                    libc::PROT_READ,
                    libc::MAP_SHARED,
                    fd.as_raw_fd(),
                    0,
                )
            };
            if ptr == libc::MAP_FAILED {
                return Err(io::Error::last_os_error());
            }
            Ok(Self { ptr, len })
        }

        /// A copy of the mapped bytes. Another process (the UI) may draw while we
        /// read, so this uses volatile reads instead of handing out a `&[u8]`.
        pub fn snapshot(&self) -> Vec<u8> {
            let p = self.ptr as *const u8;
            // SAFETY: p..p+len is a live readable mapping until drop; volatile reads
            // make no assumption that the memory stays unchanged.
            (0..self.len)
                .map(|i| unsafe { std::ptr::read_volatile(p.add(i)) })
                .collect()
        }
    }

    impl Drop for ReadMap {
        fn drop(&mut self) {
            // SAFETY: unmapping exactly what new() mapped.
            unsafe { libc::munmap(self.ptr, self.len) };
        }
    }
}

pub mod input {
    //! Linux evdev input devices.
    use super::*;

    /// EVIOCGNAME(len) = _IOC(_IOC_READ, 'E', 0x06, len)
    const fn eviocgname(len: u32) -> libc::c_ulong {
        ((2u32 << 30) | (len << 16) | ((b'E' as u32) << 8) | 0x06) as libc::c_ulong
    }

    /// The device name reported by the driver.
    pub fn name(fd: &File) -> io::Result<String> {
        let mut buf = [0u8; 256];
        // SAFETY: EVIOCGNAME(256) writes at most 256 bytes into buf.
        let n =
            check(unsafe { libc::ioctl(fd.as_raw_fd(), eviocgname(256) as _, buf.as_mut_ptr()) })?;
        let s = &buf[..(n as usize).min(buf.len())];
        let s = s.split(|&b| b == 0).next().unwrap_or(&[]);
        Ok(String::from_utf8_lossy(s).into_owned())
    }

    /// EVIOCGKEY(96): the keys that are down now.
    const EVIOCGKEY: libc::c_ulong = 0x8060_4518;
    /// EVIOCSCLOCKID: event timestamps from this clock.
    const EVIOCSCLOCKID: libc::c_ulong = 0x4004_45a0;

    /// Event timestamps from CLOCK_MONOTONIC (errors ignored, as in inputdev.py).
    pub fn use_monotonic_clock(fd: RawFd) {
        let id = libc::CLOCK_MONOTONIC as libc::c_int;
        // SAFETY: EVIOCSCLOCKID reads one int.
        unsafe { libc::ioctl(fd, EVIOCSCLOCKID as _, &id) };
    }

    /// Whether key `code` is held down now; false if the device cannot say.
    pub fn key_down(fd: RawFd, code: u16) -> bool {
        let mut bits = [0u8; 96];
        // SAFETY: EVIOCGKEY(96) writes at most 96 bytes into bits.
        if unsafe { libc::ioctl(fd, EVIOCGKEY as _, bits.as_mut_ptr()) } < 0 {
            return false;
        }
        bits.get(code as usize / 8)
            .is_some_and(|b| b >> (code % 8) & 1 == 1)
    }

    #[cfg(test)]
    mod tests {
        #[test]
        fn eviocgname_matches_kernel_macro() {
            // EVIOCGNAME(256) on Linux is 0x81004506.
            assert_eq!(super::eviocgname(256), 0x8100_4506);
        }
    }
}

#[cfg(target_os = "linux")]
pub mod pty {
    //! Pseudo-terminals.
    use super::*;
    use std::os::fd::{FromRawFd, OwnedFd};

    /// Opens a new pty master and returns it with the slave's path.
    pub fn open() -> io::Result<(OwnedFd, String)> {
        // SAFETY: posix_openpt returns a new fd or -1.
        let fd =
            check(unsafe { libc::posix_openpt(libc::O_RDWR | libc::O_NOCTTY | libc::O_CLOEXEC) })?;
        // SAFETY: fd is a fresh, owned descriptor.
        let master = unsafe { OwnedFd::from_raw_fd(fd) };
        // SAFETY: plain calls on a valid pty master.
        check(unsafe { libc::grantpt(fd) })?;
        check(unsafe { libc::unlockpt(fd) })?;
        let mut buf = [0 as libc::c_char; 128];
        // SAFETY: ptsname_r writes a NUL-terminated path of at most buf.len() bytes.
        let r = unsafe { libc::ptsname_r(fd, buf.as_mut_ptr(), buf.len()) };
        if r != 0 {
            return Err(io::Error::from_raw_os_error(r));
        }
        // SAFETY: ptsname_r succeeded, so buf holds a NUL-terminated string.
        let path = unsafe { CStr::from_ptr(buf.as_ptr()) }
            .to_string_lossy()
            .into_owned();
        Ok((master, path))
    }

    /// Sets the terminal size (TIOCSWINSZ).
    pub fn set_size(fd: RawFd, cols: u16, rows: u16) -> io::Result<()> {
        let ws = libc::winsize {
            ws_row: rows,
            ws_col: cols,
            ws_xpixel: 0,
            ws_ypixel: 0,
        };
        // SAFETY: TIOCSWINSZ reads one winsize.
        check(unsafe { libc::ioctl(fd, libc::TIOCSWINSZ as _, &ws) })?;
        Ok(())
    }

    /// For a child before exec: new session with `fd` as controlling terminal.
    /// Only async-signal-safe calls, so it may run in `pre_exec`.
    pub fn make_controlling(fd: RawFd) -> io::Result<()> {
        // SAFETY: setsid and TIOCSCTTY are async-signal-safe syscalls.
        check(unsafe { libc::setsid() })?;
        check(unsafe { libc::ioctl(fd, libc::TIOCSCTTY as _, 0) })?;
        Ok(())
    }
}

#[cfg(target_os = "linux")]
pub mod random {
    //! Kernel randomness.
    use super::*;

    /// Fills buf with the getrandom(2) syscall (Linux 3.17+).
    pub fn getrandom(buf: &mut [u8]) -> io::Result<usize> {
        // SAFETY: the kernel writes at most buf.len() bytes into buf.
        let n = unsafe { libc::syscall(libc::SYS_getrandom, buf.as_mut_ptr(), buf.len(), 0) };
        if n < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(n as usize)
        }
    }
}

/// `uname -srm` without spawning a process.
pub fn uname() -> io::Result<String> {
    // SAFETY: utsname is plain data; uname fills it.
    let mut u: libc::utsname = unsafe { std::mem::zeroed() };
    check(unsafe { libc::uname(&mut u) })?;
    let s = |f: &[libc::c_char]| {
        // SAFETY: uname fields are NUL-terminated.
        unsafe { CStr::from_ptr(f.as_ptr()) }
            .to_string_lossy()
            .into_owned()
    };
    Ok(format!(
        "{} {} {}",
        s(&u.sysname),
        s(&u.release),
        s(&u.machine)
    ))
}

/// Free and total bytes of the filesystem holding `path` (for an unprivileged user, like `df`).
pub fn disk_space(path: &str) -> io::Result<(u64, u64)> {
    let c =
        std::ffi::CString::new(path).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
    // SAFETY: statvfs is plain data that the call fills in.
    let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
    check(unsafe { libc::statvfs(c.as_ptr(), &mut s) })?;
    let frsize = s.f_frsize as u64;
    Ok((s.f_bavail as u64 * frsize, s.f_blocks as u64 * frsize))
}

/// Broken-down local time (TZ / /etc/localtime), as Python's time.localtime().
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LocalTime {
    pub year: i32,
    /// 1-12
    pub month: u32,
    pub day: u32,
    pub hour: u32,
    pub minute: u32,
    /// Monday = 0, as Python's tm_wday.
    pub weekday: u32,
}

#[allow(deprecated)] // time_t: 64-bit on aarch64 musl already
pub fn local_time(unix_secs: i64) -> LocalTime {
    let t = unix_secs as libc::time_t;
    // SAFETY: localtime_r fills tm from t; tm is plain data.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    unsafe { libc::localtime_r(&t, &mut tm) };
    LocalTime {
        year: tm.tm_year + 1900,
        month: (tm.tm_mon + 1) as u32,
        day: tm.tm_mday as u32,
        hour: tm.tm_hour as u32,
        minute: tm.tm_min as u32,
        weekday: ((tm.tm_wday + 6) % 7) as u32,
    }
}

/// CLOCK_MONOTONIC in seconds, the clock Python's time.monotonic() reads, so
/// timestamps in state files written by either language compare.
pub fn monotonic() -> f64 {
    // SAFETY: clock_gettime fills ts; CLOCK_MONOTONIC always exists on Linux and macOS.
    let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    ts.tv_sec as f64 + ts.tv_nsec as f64 / 1e9
}

pub mod process {
    //! Signals and descriptors for supervised child programs.
    use super::*;

    pub fn set_nonblocking(fd: RawFd) -> io::Result<()> {
        // SAFETY: plain fcntl calls on a descriptor the caller owns.
        let fl = check(unsafe { libc::fcntl(fd, libc::F_GETFL) })?;
        check(unsafe { libc::fcntl(fd, libc::F_SETFL, fl | libc::O_NONBLOCK) })?;
        Ok(())
    }

    pub fn kill(pid: u32, sig: libc::c_int) -> io::Result<()> {
        // SAFETY: kill has no memory effects.
        check(unsafe { libc::kill(pid as libc::pid_t, sig) })?;
        Ok(())
    }

    /// Ignores a signal in this process (as `signal(sig, SIG_IGN)`).
    pub fn ignore(sig: libc::c_int) {
        // SAFETY: setting a disposition to SIG_IGN installs no handler code.
        unsafe { libc::signal(sig, libc::SIG_IGN) };
    }

    /// For a child before exec: default dispositions for `sigs` (an ignored signal
    /// survives exec) and `dir` as working directory if possible. Only
    /// async-signal-safe calls, so it may run in `pre_exec`.
    pub fn child_defaults(sigs: &[libc::c_int], dir: &CStr) {
        for &s in sigs {
            // SAFETY: async-signal-safe; SIG_DFL installs no handler code.
            unsafe { libc::signal(s, libc::SIG_DFL) };
        }
        // SAFETY: chdir with a valid C string; a failure is ignored, as in Python's term.py.
        unsafe { libc::chdir(dir.as_ptr()) };
    }
}

/// Creates a character device node (static /dev on this phone: no devtmpfs).
pub fn mknod_char(path: &str, major: u32, minor: u32, mode: u32) -> io::Result<()> {
    let c =
        std::ffi::CString::new(path).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
    let dev = libc::makedev(major as _, minor as _);
    // SAFETY: mknod with a valid C string.
    check(unsafe { libc::mknod(c.as_ptr(), libc::S_IFCHR | mode as libc::mode_t, dev) })?;
    Ok(())
}

/// The descriptors of `fds` that are readable within `timeout_ms` (-1: wait forever).
/// An interrupting signal returns an empty list.
pub fn poll_readable(fds: &[RawFd], timeout_ms: i32) -> io::Result<Vec<RawFd>> {
    let mut p: Vec<libc::pollfd> = fds
        .iter()
        .map(|&fd| libc::pollfd {
            fd,
            events: libc::POLLIN,
            revents: 0,
        })
        .collect();
    // SAFETY: p is a valid array of p.len() pollfd structs.
    let n = unsafe { libc::poll(p.as_mut_ptr(), p.len() as libc::nfds_t, timeout_ms) };
    if n < 0 {
        let e = io::Error::last_os_error();
        return if e.kind() == io::ErrorKind::Interrupted {
            Ok(Vec::new())
        } else {
            Err(e)
        };
    }
    // POLLHUP/POLLERR count as readable: the read then reports the end or the error, as select does.
    let ready = libc::POLLIN | libc::POLLHUP | libc::POLLERR | libc::POLLNVAL;
    Ok(p.iter()
        .filter(|x| x.revents & ready != 0)
        .map(|x| x.fd)
        .collect())
}

pub mod term_signal {
    //! SIGTERM and SIGINT as a flag the main loop checks, so it can clean up and exit.
    use std::sync::atomic::{AtomicBool, Ordering};

    static REQUESTED: AtomicBool = AtomicBool::new(false);

    extern "C" fn on_term(_: libc::c_int) {
        REQUESTED.store(true, Ordering::SeqCst); // async-signal-safe
    }

    pub fn install() {
        for sig in [libc::SIGTERM, libc::SIGINT] {
            // SAFETY: the handler only stores to an atomic.
            unsafe { libc::signal(sig, on_term as *const () as libc::sighandler_t) };
        }
    }

    pub fn requested() -> bool {
        REQUESTED.load(Ordering::SeqCst)
    }
}

/// Restarts into the bootloader's fastboot mode: reboot(2) with
/// LINUX_REBOOT_CMD_RESTART2 and "bootloader" (BusyBox `reboot` cannot pass it).
/// Returns only on failure. Call `sync` first.
#[cfg(target_os = "linux")]
pub fn reboot_bootloader() -> io::Error {
    const MAGIC1: libc::c_long = 0xfee1_dead;
    const MAGIC2: libc::c_long = 672_274_793;
    const RESTART2: libc::c_long = 0xa1b2_c3d4;
    // SAFETY: the argument is a valid NUL-terminated string; on success the call does not return.
    unsafe {
        libc::syscall(
            libc::SYS_reboot,
            MAGIC1,
            MAGIC2,
            RESTART2,
            c"bootloader".as_ptr(),
        )
    };
    io::Error::last_os_error()
}
