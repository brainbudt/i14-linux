/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef I14_REBOOT_RTC_H
#define I14_REBOOT_RTC_H
enum { I14_RTC_BBPU = 0x4000, I14_RTC_PDN1 = 0x402c };
#define I14_FAST_BOOT 0x2000U
struct i14_rtc_io {
    void *context;
    int (*read)(void *, unsigned, unsigned *);
    int (*write)(void *, unsigned, unsigned);
    unsigned long long (*now_ns)(void *);
    void (*pause)(void *);
};
/* Every access is deadline checked; the underlying regmap transport must also
 * provide bounded I/O (the pinned kernel PWRAP implementation does). */
static int i14_rtc_access(struct i14_rtc_io *io, unsigned long long deadline,
                          unsigned address, unsigned *value, int write)
{
    int ret;
    if (io->now_ns(io->context) >= deadline) return -ETIMEDOUT;
    ret = write ? io->write(io->context, address, *value) :
                  io->read(io->context, address, value);
    if (ret) return ret;
    return io->now_ns(io->context) >= deadline ? -ETIMEDOUT : 0;
}
static int i14_rtc_idle(struct i14_rtc_io *io, unsigned long long deadline)
{
    unsigned long long phase = io->now_ns(io->context) + 200000000ULL;
    unsigned value, poll;
    int ret;
    if (phase > deadline) phase = deadline;
    for (poll = 0; poll < 4000; poll++) {
        ret = i14_rtc_access(io, phase, I14_RTC_BBPU, &value, 0);
        if (ret) return ret;
        if (!(value & 64)) return 0;
        io->pause(io->context);
    }
    return -ETIMEDOUT;
}
static int i14_rtc_commit(struct i14_rtc_io *io, unsigned long long deadline)
{
    unsigned value = 1;
    int ret = i14_rtc_access(io, deadline, 0x403c, &value, 1);
    return ret ? ret : i14_rtc_idle(io, deadline);
}
static int i14_rtc_prepare(struct i14_rtc_io *io, const char *command, unsigned *receipt)
{
    unsigned value, expected, observed;
    unsigned long long deadline;
    int ret;
    if (!command || strcmp(command, "bootloader")) return -EINVAL;
    deadline = io->now_ns(io->context) + 1000000000ULL;
    ret = i14_rtc_idle(io, deadline);
    if (ret) return ret;
    value = 0x586a;
    ret = i14_rtc_access(io, deadline, 0x4036, &value, 1);
    if (ret) return ret;
    ret = i14_rtc_commit(io, deadline);
    if (ret) return ret;
    value = 0x9136;
    ret = i14_rtc_access(io, deadline, 0x4036, &value, 1);
    if (ret) return ret;
    ret = i14_rtc_commit(io, deadline);
    if (ret) return ret;
    ret = i14_rtc_access(io, deadline, I14_RTC_PDN1, &value, 0);
    if (ret) return ret;
    if (value > 0xffff) return -EIO;
    expected = value | I14_FAST_BOOT;
    ret = i14_rtc_access(io, deadline, I14_RTC_PDN1, &expected, 1);
    if (ret) return ret;
    ret = i14_rtc_commit(io, deadline);
    if (ret) return ret;
    ret = i14_rtc_access(io, deadline, I14_RTC_PDN1, &observed, 0);
    if (ret) return ret;
    if (observed != expected) return -EIO;
    *receipt = observed;
    return 0;
}
#endif
