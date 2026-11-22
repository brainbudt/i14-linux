/* Fixed I14 PWRAP CID reads only. No PMIC writes or recovery operations. */
#define _GNU_SOURCE 1
#include <stdint.h>
#include <stdio.h>
#include <string.h>

enum { MUX_SEL=0x00, WRAP_EN=0x04, WACS_EN=0x98, INIT_DONE=0x9c,
       CMD=0xa0, RDATA=0xa4, VLDCLR=0xa8, MAX_POLLS=4000 };
#define INIT_BIT (1u << 21)
#define SYNC_BIT (1u << 20)
#define REQ_BIT (1u << 19)
#define SECOND_NS UINT64_C(1000000000)
#define PHASE_NS UINT64_C(200000000)

#ifdef I14_PMIC_ID_TEST
static uint32_t mmio_read(unsigned offset);
static void mmio_write(unsigned offset, uint32_t value);
static int monotonic_ns(uint64_t *value);
static int poll_pause(void);
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>
static volatile uint32_t *page;
static uint32_t mmio_read(unsigned offset)
{
    __asm__ volatile ("" ::: "memory");
    uint32_t value = page[offset / sizeof(uint32_t)];
    __asm__ volatile ("dsb sy" ::: "memory");
    return value;
}
static void mmio_write(unsigned offset, uint32_t value)
{
    page[offset / sizeof(uint32_t)] = value;
    __asm__ volatile ("dsb sy" ::: "memory");
}
static int monotonic_ns(uint64_t *value)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    *value = (uint64_t)now.tv_sec * SECOND_NS + (uint64_t)now.tv_nsec;
    return 0;
}
static int poll_pause(void)
{
    const struct timespec delay = { .tv_sec=0, .tv_nsec=50000 };
    return nanosleep(&delay, NULL);
}
#endif

struct transaction { uint64_t deadline; const char *error; };
struct cid_result { uint16_t hw, sw, repeat; };
static int stop(struct transaction *run, const char *reason)
{
    run->error = reason;
    return -1;
}
static int within(struct transaction *run, uint64_t phase)
{
    uint64_t now;
    if (monotonic_ns(&now)) return stop(run, "monotonic clock failed");
    if (now >= run->deadline || (phase && now >= phase)) return stop(run, "deadline expired");
    return 0;
}
static int read_reg(struct transaction *run, unsigned offset, uint32_t *value)
{
    if (within(run, 0)) return -1;
    *value = mmio_read(offset);
    return 0;
}
static int write_reg(struct transaction *run, unsigned offset, uint32_t value)
{
    if (!((offset == CMD && (value == 0x01000000 || value == 0x01010000)) ||
          (offset == VLDCLR && value == 1))) return stop(run, "write outside fixed allowlist");
    if (within(run, 0)) return -1;
    mmio_write(offset, value);
    return 0;
}
static int idle(uint32_t status)
{
    return (status & INIT_BIT) && (status & SYNC_BIT) && !(status & REQ_BIT) &&
           ((status >> 16) & 7) == 0;
}
static int wait_status(struct transaction *run, int want_ready, uint32_t *result)
{
    uint64_t start;
    if (monotonic_ns(&start)) return stop(run, "monotonic clock failed");
    uint64_t phase = start + PHASE_NS;
    for (unsigned poll=0; poll<MAX_POLLS; ++poll) {
        uint32_t status;
        if (within(run, phase) || read_reg(run, RDATA, &status)) return -1;
        unsigned fsm = (status >> 16) & 7;
        if (!(status & INIT_BIT)) return stop(run, "WACS2 initialization lost");
        if ((want_ready && fsm == 6) || (!want_ready && idle(status))) {
            *result = status;
            return 0;
        }
        if (want_ready ? (fsm != 0 && fsm != 2 && fsm != 4) : (fsm != 0 && fsm != 6))
            return stop(run, "unexpected WACS2 state");
        if (poll_pause()) return stop(run, "poll sleep interrupted");
    }
    return stop(run, "poll limit reached");
}
static int read_cid(struct transaction *run, uint16_t address, uint16_t *value)
{
    uint32_t status;
    if (address != 0x0200 && address != 0x0202) return stop(run, "PMIC address outside allowlist");
    if (read_reg(run, RDATA, &status)) return -1;
    if (!idle(status)) return stop(run, "channel not idle before command");
    if (write_reg(run, CMD, (uint32_t)(address >> 1) << 16)) return -1;
    if (wait_status(run, 1, &status)) return -1;
    uint16_t response = status & 0xffff;
    if (write_reg(run, VLDCLR, 1)) return -1;
    if (wait_status(run, 0, &status)) return -1;
    *value = response;
    return 0;
}
static int identify(struct cid_result *result, const char **error)
{
    uint64_t start;
    struct transaction run = { .error="unknown error" };
    uint32_t mux, wrap, enabled, initialized, status;
    if (monotonic_ns(&start) || start > UINT64_MAX - SECOND_NS) {
        *error = "monotonic clock failed";
        return -1;
    }
    run.deadline = start + SECOND_NS;
    if (read_reg(&run, MUX_SEL, &mux) || read_reg(&run, WRAP_EN, &wrap) ||
        read_reg(&run, WACS_EN, &enabled) || read_reg(&run, INIT_DONE, &initialized) ||
        read_reg(&run, RDATA, &status)) goto failed;
    if ((mux & 1) || !(wrap & 1) || enabled != 1 || initialized != 1 || !idle(status)) {
        stop(&run, "initial wrapper/idle/init guard failed");
        goto failed;
    }
    if (read_cid(&run, 0x0200, &result->hw) || read_cid(&run, 0x0202, &result->sw) ||
        read_cid(&run, 0x0200, &result->repeat)) goto failed;
    if (result->hw != result->repeat) { stop(&run, "HWCID repeat mismatch"); goto failed; }
    if (within(&run, 0)) goto failed;
    return 0;
failed:
    *error = run.error;
    return -1;
}

#ifndef I14_PMIC_ID_TEST
static int exact_file(const char *path, const char *expected, size_t size)
{
    char data[256];
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    ssize_t got = read(fd, data, sizeof(data));
    int closed = close(fd);
    return got == (ssize_t)size && !closed && !memcmp(data, expected, size) ? 0 : -1;
}
static int no_driver(const char *path)
{
    struct stat info;
    if (lstat(path, &info) == 0) return -1;
    return errno == ENOENT ? 0 : -1;
}
static int native_guard(void)
{
    static const char model[] = "I14 MT6735 initramfs bring-up";
    static const char compatible[] = "magcomm,magc6735-65u-1-m0\0mediatek,mt6735";
    static const char disabled[] = "disabled";
    struct utsname name;
    if (getuid() || geteuid() || uname(&name) || strcmp(name.release, "7.0.10-i14-bringup") ||
        strcmp(name.machine, "aarch64") || sysconf(_SC_PAGESIZE) != 4096) return -1;
    if (exact_file("/proc/device-tree/model", model, sizeof(model)) ||
        exact_file("/proc/device-tree/compatible", compatible, sizeof(compatible)) ||
        exact_file("/proc/device-tree/soc/pwrap@10001000/status", disabled, sizeof(disabled)) ||
        no_driver("/sys/bus/platform/devices/10001000.pwrap/driver") ||
        no_driver("/sys/bus/platform/drivers/mt-pmic-pwrap/10001000.pwrap")) return -1;
    return 0;
}
int main(int argc, char **argv)
{
    (void)argv;
    if (argc != 1) { fputs("i14-pmic-id: no arguments accepted\n", stderr); return 2; }
    if (native_guard()) { fputs("I14_PMIC_STOPPED: native identity/PWRAP guard failed\n", stderr); return 1; }
    int fd = open("/dev/mem", O_RDWR | O_SYNC | O_NOFOLLOW | O_CLOEXEC);
    struct stat info;
    if (fd < 0) { perror("I14_PMIC_STOPPED: open /dev/mem"); return 1; }
    if (fstat(fd, &info) || !S_ISCHR(info.st_mode) || major(info.st_rdev) != 1 || minor(info.st_rdev) != 1) {
        fputs("I14_PMIC_STOPPED: /dev/mem must be character device 1:1\n", stderr);
        close(fd);
        return 1;
    }
    void *mapping = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (off_t)0x10001000);
    if (close(fd) || mapping == MAP_FAILED) { fputs("I14_PMIC_STOPPED: mapping/close failed\n", stderr); return 1; }
    page = mapping;
    struct cid_result result = {0};
    const char *error = "unknown";
    int failed = identify(&result, &error);
    if (munmap(mapping, 4096)) { fputs("I14_PMIC_STOPPED: unmap failed\n", stderr); return 1; }
    if (failed) { fprintf(stderr, "I14_PMIC_STOPPED: %s\n", error); return 1; }
    printf("I14_PMIC_READS_COMPLETE HWCID[0200]=0x%04x SWCID[0202]=0x%04x HWCID_REPEAT[0200]=0x%04x\n",
           result.hw, result.sw, result.repeat);
    return 0;
}
#endif
