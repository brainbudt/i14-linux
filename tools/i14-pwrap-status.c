/* Fixed read-only AP-wrapper snapshots. No PMIC transaction or MMIO store. */
#define _GNU_SOURCE 1
#include <stdint.h>
#include <stdio.h>
#include <string.h>
static const unsigned offsets[6] = {0xf4, 0xc4, 0xc8, 0xf0, 0xec, 0xe4};
#ifdef I14_PWRAP_STATUS_TEST
static uint32_t mmio_read(unsigned offset);
static int pause_snapshot(void);
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>
static volatile const uint32_t *page;
static uint32_t mmio_read(unsigned offset)
{
    __asm__ volatile ("" ::: "memory");
    uint32_t value = page[offset / sizeof(uint32_t)];
    __asm__ volatile ("dsb sy" ::: "memory");
    return value;
}
static int pause_snapshot(void)
{
    const struct timespec delay = { .tv_sec=0, .tv_nsec=100000000 };
    return nanosleep(&delay, NULL);
}
#endif
static int snapshots(uint32_t values[3][6])
{
    for (unsigned sample=0; sample<3; ++sample) {
        for (unsigned field=0; field<6; ++field)
            values[sample][field] = mmio_read(offsets[field]);
        if (sample<2 && pause_snapshot()) return -1;
    }
    return 0;
}
#ifndef I14_PWRAP_STATUS_TEST
static int exact_file(const char *path, const char *expected, size_t size)
{
    char data[256];
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    ssize_t got = read(fd, data, sizeof(data));
    int closed = close(fd);
    return got == (ssize_t)size && !closed && !memcmp(data, expected, size) ? 0 : -1;
}
static int bound_driver(void)
{
    const char *path = "/sys/bus/platform/devices/10001000.pwrap/driver";
    char target[256];
    struct stat info;
    ssize_t size = readlink(path, target, sizeof(target)-1);
    if (size<=0 || size>=(ssize_t)sizeof(target)-1 || stat(path, &info) || !S_ISDIR(info.st_mode)) return -1;
    target[size] = '\0';
    const char *name = strrchr(target, '/');
    return name && !strcmp(name+1, "mt-pmic-pwrap") ? 0 : -1;
}
static int native_guard(void)
{
    static const char model[] = "I14 MT6735 initramfs bring-up";
    static const char compatible[] = "magcomm,magc6735-65u-1-m0\0mediatek,mt6735";
    static const char okay[] = "okay";
    struct utsname name;
    if (getuid() || geteuid() || uname(&name) || strcmp(name.release, "7.0.10-i14-bringup") ||
        strcmp(name.machine, "aarch64") || sysconf(_SC_PAGESIZE)!=4096) return -1;
    return exact_file("/proc/device-tree/model", model, sizeof(model)) ||
           exact_file("/proc/device-tree/compatible", compatible, sizeof(compatible)) ||
           exact_file("/proc/device-tree/soc/pwrap@10001000/status", okay, sizeof(okay)) || bound_driver();
}
int main(int argc, char **argv)
{
    (void)argv;
    if (argc!=1) { fputs("i14-pwrap-status: no arguments accepted\n", stderr); return 2; }
    if (native_guard()) { fputs("I14_PWRAP_STOPPED: native identity/enabled-driver guard failed\n", stderr); return 1; }
    int fd = open("/dev/mem", O_RDONLY | O_SYNC | O_NOFOLLOW | O_CLOEXEC);
    struct stat info;
    if (fd<0) { perror("I14_PWRAP_STOPPED: open /dev/mem"); return 1; }
    if (fstat(fd, &info) || !S_ISCHR(info.st_mode) || major(info.st_rdev)!=1 || minor(info.st_rdev)!=1) {
        fputs("I14_PWRAP_STOPPED: /dev/mem must be character device 1:1\n", stderr);
        close(fd); return 1;
    }
    void *mapping = mmap(NULL, 4096, PROT_READ, MAP_SHARED, fd, (off_t)0x10001000);
    if (close(fd) || mapping==MAP_FAILED) { fputs("I14_PWRAP_STOPPED: mapping/close failed\n", stderr); return 1; }
    page = mapping;
    uint32_t values[3][6];
    int failed = snapshots(values);
    if (munmap(mapping, 4096) || failed) { fputs("I14_PWRAP_STOPPED: snapshot/unmap failed\n", stderr); return 1; }
    for (unsigned sample=0; sample<3; ++sample)
        printf("I14_PWRAP_STATUS sample=%u WDT_FLG=%08x INT_RAW=%08x INT_FLG=%08x WDT_SRC=%08x WDT_UNIT=%08x TIMER_EN=%08x\n",
               sample, values[sample][0], values[sample][1], values[sample][2],
               values[sample][3], values[sample][4], values[sample][5]);
    return 0;
}
#endif
