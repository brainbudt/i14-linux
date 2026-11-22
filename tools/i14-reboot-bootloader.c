/* Checked MT6328 preparation followed by the real bootloader RESTART2. */
#define _GNU_SOURCE 1
#include <stdio.h>
#include <string.h>

static int native_guard(void);
static int prepare_target(void);
static int verify_target(void);
static int restart_bootloader(void);
static int reboot_bootloader(void)
{
    if (native_guard()) {
        fputs("I14_BOOTLOADER_STOPPED: native identity/root guard failed\n", stderr);
        return 1;
    }
    if (prepare_target() || verify_target()) {
        fputs("I14_BOOTLOADER_STOPPED: preparation/readback failed; no reset issued; RTC flag may remain armed\n", stderr);
        return 1;
    }
    fputs("I14_BOOTLOADER_PREPARED: checked RTC receipt; requesting RESTART2 bootloader\n", stderr);
    (void)restart_bootloader();
    fputs("I14_BOOTLOADER_STOPPED: reboot syscall returned; RTC flag remains armed; do not blindly retry\n", stderr);
    return 1;
}

#ifndef I14_REBOOT_BOOTLOADER_TEST
#include <errno.h>
#include <fcntl.h>
#include <linux/magic.h>
#include <linux/reboot.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <sys/vfs.h>
#include <unistd.h>

static int target_fd = -1;

static int exact_file(const char *path, const char *expected, size_t size)
{
    char buffer[128];
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    ssize_t count;
    int ret;
    if (fd < 0) return -1;
    count = read(fd, buffer, sizeof(buffer));
    ret = close(fd);
    return !ret && count == (ssize_t)size && !memcmp(buffer, expected, size) ? 0 : -1;
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

static int prepare_target(void)
{
    struct stat info;
    struct statfs filesystem;
    static const char request[] = "bootloader\n";
    target_fd = open("/sys/devices/i14-reboot-target/target", O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    if (target_fd < 0) return -1;
    if (fstat(target_fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != 0 ||
        fstatfs(target_fd, &filesystem) || filesystem.f_type != SYSFS_MAGIC ||
        write(target_fd, request, sizeof(request) - 1) != sizeof(request) - 1) {
        close(target_fd);
        target_fd = -1;
        return -1;
    }
    return 0;
}

static int verify_target(void)
{
    char buffer[128], expected[64];
    unsigned int pdn1;
    ssize_t count;
    int ret;
    if (lseek(target_fd, 0, SEEK_SET) != 0) { close(target_fd); target_fd = -1; return -1; }
    count = read(target_fd, buffer, sizeof(buffer) - 1);
    ret = close(target_fd);
    target_fd = -1;
    if (ret || count <= 0) return -1;
    buffer[count] = 0;
    if (sscanf(buffer, "prepared bootloader pdn1=0x%4x", &pdn1) != 1 || !(pdn1 & 0x2000)) return -1;
    ret = snprintf(expected, sizeof(expected), "prepared bootloader pdn1=0x%04x\n", pdn1);
    if (count != ret || memcmp(buffer, expected, count)) return -1;
    fprintf(stderr, "I14_BOOTLOADER_RECEIPT: pdn1=0x%04x\n", pdn1);
    return 0;
}

static int restart_bootloader(void)
{
    sync();
    return syscall(SYS_reboot, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
                   LINUX_REBOOT_CMD_RESTART2, "bootloader");
}

int main(int argc, char **argv)
{
    (void)argv;
    if (argc != 1) { fputs("i14-reboot-bootloader: no arguments accepted\n", stderr); return 2; }
    return reboot_bootloader();
}
#endif
