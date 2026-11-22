/* Host-only fake PMIC; runs the production transaction without hardware. */
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "../kernel/i14-reboot-rtc.h"

struct fake {
    unsigned pdn, writes, reads, busy, busy_after, wrong, fail_at, write_fail_at;
    unsigned freeze, transfer_ns;
    unsigned address[8], value[8];
    uint64_t now;
};
static int rd(void *arg, unsigned address, unsigned *value)
{
    struct fake *f = arg;
    f->now += f->transfer_ns;
    if (++f->reads == f->fail_at) return -EIO;
    if (address == I14_RTC_BBPU)
        *value = f->busy || (f->busy_after && f->writes >= f->busy_after) ? 64 : 0;
    else { assert(address == I14_RTC_PDN1); *value = f->pdn ^ (f->wrong && f->writes == 6 ? 1 : 0); }
    return 0;
}
static int wr(void *arg, unsigned address, unsigned value)
{
    struct fake *f = arg;
    f->now += f->transfer_ns;
    assert(f->writes < 8);
    f->address[f->writes] = address;
    f->value[f->writes++] = value;
    if (f->writes == f->write_fail_at) return -EIO;
    if (address == I14_RTC_PDN1) f->pdn = value;
    return 0;
}
static uint64_t tm(void *arg) { return ((struct fake *)arg)->now; }
static void pause_poll(void *arg)
{
    struct fake *f = arg;
    if (!f->freeze) f->now += 100000;
}
static struct i14_rtc_io io(struct fake *f)
{
    return (struct i14_rtc_io){ f, rd, wr, tm, pause_poll };
}
int main(void)
{
    struct fake f = { .pdn = 0x95af };
    struct i14_rtc_io ops = io(&f);
    unsigned receipt = 0;
    assert(i14_rtc_prepare(&ops, "recovery", &receipt) == -EINVAL);
    assert(i14_rtc_prepare(&ops, NULL, &receipt) == -EINVAL);
    assert(!f.writes && !f.reads);
    assert(i14_rtc_prepare(&ops, "bootloader", &receipt) == 0);
    assert(receipt == 0xb5af && f.pdn == 0xb5af && f.writes == 6);
    unsigned addresses[] = { 0x4036, 0x403c, 0x4036, 0x403c, 0x402c, 0x403c };
    unsigned values[] = { 0x586a, 1, 0x9136, 1, 0xb5af, 1 };
    assert(!memcmp(f.address, addresses, sizeof(addresses)));
    assert(!memcmp(f.value, values, sizeof(values)));

    /* Cover every possible unrelated-bit combination, including already armed. */
    for (unsigned original = 0; original <= 0xffff; original++) {
        f = (struct fake){ .pdn = original }; ops = io(&f);
        assert(i14_rtc_prepare(&ops, "bootloader", &receipt) == 0);
        assert(receipt == (original | 0x2000));
    }

    f = (struct fake){ .pdn = 0x95af, .busy = 1 }; ops = io(&f);
    assert(i14_rtc_prepare(&ops, "bootloader", &receipt) == -ETIMEDOUT);
    assert(!f.writes && f.reads <= 4000 && f.now <= 1000000000);
    f = (struct fake){ .pdn = 0x95af, .busy_after = 2 }; ops = io(&f);
    assert(i14_rtc_prepare(&ops, "bootloader", &receipt) == -ETIMEDOUT);
    assert(f.writes == 2 && f.reads <= 4001 && f.now <= 1000000000);
    f = (struct fake){ .busy = 1, .freeze = 1 }; ops = io(&f);
    assert(i14_rtc_prepare(&ops, "bootloader", &receipt) == -ETIMEDOUT);
    assert(!f.writes && f.reads == 4000);
    f = (struct fake){ .pdn = 0x95af, .transfer_ns = 100000000 }; ops = io(&f);
    receipt = 0x1234;
    assert(i14_rtc_prepare(&ops, "bootloader", &receipt) == -ETIMEDOUT);
    assert(f.writes == 6 && f.reads == 4 && receipt == 0x1234);
    f = (struct fake){ .pdn = 0x95af, .wrong = 1 }; ops = io(&f);
    assert(i14_rtc_prepare(&ops, "bootloader", &receipt) == -EIO);
    assert(f.writes == 6);
    f = (struct fake){ .pdn = 0x95af, .fail_at = 2 }; ops = io(&f);
    assert(i14_rtc_prepare(&ops, "bootloader", &receipt) == -EIO);
    assert(f.writes == 2); /* Failed first commit: never send second unlock. */
    f = (struct fake){ .pdn = 0x95af, .write_fail_at = 1 }; ops = io(&f);
    assert(i14_rtc_prepare(&ops, "bootloader", &receipt) == -EIO);
    assert(f.writes == 1 && f.reads == 1);
    puts("reboot target: command guard, preservation, exact sequence, bounded busy and I/O/readback failure PASS");
}
