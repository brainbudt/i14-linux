/* Remove only the idle cached-only diagnostic; never reset sampling state. */
#define _GNU_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>

static int native_guard(void);
static int no_sample_interface(void);
static int no_prepared_receipt(void);
static int no_module_references(void);
static int delete_diagnostic(const char *name, int flags);

static int unprepared_read(ssize_t count, int error)
{
    return count == -1 && error == ENODATA;
}

static int zero_refcount(const char *data, ssize_t count)
{
    return count == 2 && data && !memcmp(data, "0\n", 2);
}

static int unload_diagnostic(int argc)
{
    if (argc != 1) {
        fputs("i14-unload-diagnostic: no arguments accepted\n", stderr);
        return 2;
    }
    if (native_guard() || no_sample_interface() || no_prepared_receipt() || no_module_references()) {
        fputs("I14_UNLOAD_STOPPED: identity/cached-only/RTC/refcount guard; no unload issued\n", stderr);
        return 1;
    }
    if (delete_diagnostic("i14_reboot_target", O_NONBLOCK)) {
        fputs("I14_UNLOAD_STOPPED: single nonblocking unload failed; no retry\n", stderr);
        return 1;
    }
    puts("I14_UNLOAD_COMPLETE: cached-only i14_reboot_target removed");
    return 0;
}

#ifndef I14_UNLOAD_DIAGNOSTIC_TEST
#include <linux/magic.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <sys/vfs.h>
#include <unistd.h>

static int checked_fd(int fd, int directory)
{
    struct stat info;
    struct statfs filesystem;
    if (fd < 0) return -1;
    if (fstat(fd, &info) || info.st_uid != 0 ||
        (directory ? !S_ISDIR(info.st_mode) : !S_ISREG(info.st_mode)) ||
        (info.st_mode & 0022) || fstatfs(fd, &filesystem) || filesystem.f_type != SYSFS_MAGIC) {
        close(fd);
        return -1;
    }
    return fd;
}

static int checked_directory(const char *path)
{
    return checked_fd(open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC), 1);
}

static int checked_attribute(const char *directory, const char *name)
{
    int parent = checked_directory(directory);
    int fd;
    if (parent < 0) return -1;
    fd = checked_fd(openat(parent, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC), 0);
    if (close(parent)) { if (fd >= 0) close(fd); return -1; }
    return fd;
}

static int exact_file(const char *path, const char *expected, size_t size)
{
    char buffer[128];
    int fd = checked_fd(open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC), 0);
    ssize_t count;
    int closed;
    if (fd < 0) return -1;
    count = read(fd, buffer, sizeof(buffer));
    closed = close(fd);
    return !closed && count == (ssize_t)size && !memcmp(buffer, expected, size) ? 0 : -1;
}

static int native_guard(void)
{
    struct utsname name;
    static const char model[] = "I14 MT6735 initramfs bring-up";
    static const char compatible[] = "magcomm,magc6735-65u-1-m0\0mediatek,mt6735";
    if (getuid() || geteuid() || uname(&name) ||
        strcmp(name.machine, "aarch64") || strcmp(name.release, "7.0.10-i14-bringup")) return -1;
    return exact_file("/proc/device-tree/model", model, sizeof(model)) ||
           exact_file("/proc/device-tree/compatible", compatible, sizeof(compatible));
}

static int no_sample_interface(void)
{
    struct stat info;
    int fd = checked_directory("/sys/devices/i14-reboot-target");
    int result, error, closed;
    if (fd < 0) return -1;
    result = fstatat(fd, "battery_sample", &info, AT_SYMLINK_NOFOLLOW);
    error = errno;
    closed = close(fd);
    return !closed && result == -1 && error == ENOENT ? 0 : -1;
}

static int no_prepared_receipt(void)
{
    char buffer[128];
    int fd = checked_attribute("/sys/devices/i14-reboot-target", "target");
    ssize_t count;
    int error, closed;
    if (fd < 0) return -1;
    count = read(fd, buffer, sizeof(buffer));
    error = errno;
    closed = close(fd);
    return !closed && unprepared_read(count, error) ? 0 : -1;
}

static int no_module_references(void)
{
    char buffer[16];
    int fd = checked_attribute("/sys/module/i14_reboot_target", "refcnt");
    ssize_t count;
    int closed;
    if (fd < 0) return -1;
    count = read(fd, buffer, sizeof(buffer));
    closed = close(fd);
    return !closed && zero_refcount(buffer, count) ? 0 : -1;
}

static int delete_diagnostic(const char *name, int flags)
{
    if (strcmp(name, "i14_reboot_target") || flags != O_NONBLOCK) return -1;
    return syscall(SYS_delete_module, "i14_reboot_target", O_NONBLOCK);
}

int main(int argc, char **argv)
{
    (void)argv;
    return unload_diagnostic(argc);
}
#endif
