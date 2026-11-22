#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include "../kernel/i14-battery-sample.h"
struct fake {
    unsigned config[3], reads, updates, requests, initial_busy, initial_req;
    unsigned stale_only, never_ready, latch_reads, failure_at, operations;
    unsigned freeze, corrupt_restore, ignore_enable, busy_after_setup, delay_ns;
    unsigned request_stays_set;
    unsigned channel, ready_latch, reserved_at, reserved_bits, wide_at, now_calls, restore_delay_ns;
    unsigned long long now;
};
static int rd(void *context, unsigned address, unsigned *value)
{
    struct fake *f = context;
    f->now += f->delay_ns;
    if (++f->operations == f->failure_at) return -EIO;
    f->reads++;
    switch (address) {
    case 0xe88: *value = f->initial_req | ((f->requests && f->request_stays_set) ? 1U << f->channel : 0); break;
    case 0xe84: *value = (f->initial_busy & 0x7f) | ((f->busy_after_setup && f->updates) ? 1U << f->channel : 0); break;
    case 0xe86: *value = f->initial_busy & 0x80; break;
    case 0xe94: *value = f->config[0]; break;
    case 0xeb0: *value = f->config[1]; break;
    case 0x28a: *value = f->config[2]; break;
    case 0xe2e: case 0xe04: case 0xe06:
        assert(address == (f->channel == 2 ? 0xe04U : f->channel == 3 ? 0xe06U : 0xe2eU));
        assert(f->requests == 1);
        f->latch_reads++;
        *value = f->stale_only ? (f->channel ? 0x82f8 : 0xd2f8) :
                 f->never_ready ? 0x0001 :
                 f->latch_reads == 1 ? (f->channel ? 0x82f8 : 0xd2f8) :
                 f->latch_reads == 2 ? 0x0001 :
                 f->ready_latch ? f->ready_latch : f->channel ? 0x8abc : 0xe001;
        if (f->latch_reads == f->reserved_at) *value |= f->reserved_bits;
        if (f->latch_reads == f->wide_at) *value = 0x10000;
        break;
    default: assert(!"read outside ADC allowlist");
    }
    return 0;
}
static int update(void *context, unsigned address, unsigned mask, unsigned value)
{
    struct fake *f = context;
    f->now += f->delay_ns;
    unsigned index;
    assert((address == 0xe94 && mask == 0x8000) ||
           (address == 0xeb0 && mask == 0x80) ||
           (address == 0x28a && mask == 0x100));
    assert(!(value & ~mask));
    f->updates++;
    if (f->updates > 3) f->now += f->restore_delay_ns;
    index = address == 0xe94 ? 0 : address == 0xeb0 ? 1 : 2;
    /* A failed transport can already have applied its write. */
    if (!(f->corrupt_restore && f->updates > 3) && !(f->ignore_enable && f->updates <= 3))
        f->config[index] = (f->config[index] & ~mask) | value;
    return ++f->operations == f->failure_at ? -EIO : 0;
}
static int wr(void *context, unsigned address, unsigned value)
{
    struct fake *f = context;
    f->now += f->delay_ns;
    assert(address == 0xe8a && value == (1U << f->channel) && !f->requests);
    f->requests++;
    f->config[0] ^= 2; f->config[1] ^= 0x100; f->config[2] ^= 0x40;
    return ++f->operations == f->failure_at ? -EIO : 0;
}
static unsigned long long tm(void *context)
{
    struct fake *f = context;
    f->now_calls++;
    return f->now;
}
static void pause_poll(void *context)
{
    struct fake *f = context;
    if (!f->freeze) f->now += 100000;
}
static struct i14_adc_io io(struct fake *f)
{
    return (struct i14_adc_io){ f, rd, update, wr, tm, pause_poll };
}
static void channel_tests(void)
{
    struct fake f;
    struct i14_adc_io ops;
    struct i14_adc_result result;
    const unsigned invalid[] = { 1, 4, 7, 31, 32, ~0U };
    for (unsigned i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        f = (struct fake){0}; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, invalid[i], "sample", &result) == -EINVAL);
        assert(!f.operations && !f.now_calls && !f.updates && !f.requests);
        assert(!result.complete && !result.request_attempted && result.error == -EINVAL);
    }
    for (unsigned raw = 0; raw <= 0x7fff; raw += 0x7fff) {
        f = (struct fake){ .ready_latch = 0x8000 | raw }; ops = io(&f);
        assert(i14_adc_sample(&ops, "sample", &result) == 0);
        assert(result.channel == 0 && result.raw == raw && result.mv == (raw ? 5399 : 0));
    }
    for (unsigned channel = 2; channel <= 3; ++channel) {
        f = (struct fake){ .channel = channel, .config = { 0x8014, 0x1234, 0x8002 } }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "other", &result) == -EINVAL);
        assert(!f.operations && !f.now_calls);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == 0);
        assert(result.complete && result.channel == channel && result.latch == 0x8abc);
        assert(result.raw == 0xabc && result.mv == 1207 && result.saw_zero && result.restored == 7);
        assert(f.updates == 6 && f.requests == 1 && f.latch_reads == 3);
        assert(f.config[0] == (0x8014 ^ 2) && f.config[1] == (0x1234 ^ 0x100) && f.config[2] == (0x8002 ^ 0x40));
        unsigned successful_operations = f.operations;
        for (unsigned failure = 1; failure <= successful_operations; ++failure) {
            f = (struct fake){ .channel = channel, .failure_at = failure }; ops = io(&f);
            assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -EIO);
            assert(f.operations == failure && !result.complete);
        }
        for (unsigned raw = 0; raw <= 0xfff; raw += 0xfff) {
            f = (struct fake){ .channel = channel, .ready_latch = 0x8000 | raw }; ops = io(&f);
            assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == 0);
            assert(result.raw == raw && result.mv == (raw ? 1799 : 0));
        }
        for (unsigned bits = 0x1000; bits <= 0x4000; bits <<= 1) {
            for (unsigned position = 1; position <= 3; ++position) {
                f = (struct fake){ .channel = channel, .reserved_at = position, .reserved_bits = bits }; ops = io(&f);
                assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -EIO);
                assert(!result.complete && !result.restored && result.uncertain == 7);
                assert(f.latch_reads == position && f.requests == 1 && f.updates == 3);
            }
        }
        f = (struct fake){ .channel = channel, .wide_at = 3 }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -EIO && !result.complete);
        f = (struct fake){ .channel = channel, .initial_busy = 1U << channel }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -EBUSY && !f.updates && !f.requests);
        f = (struct fake){ .channel = channel, .initial_busy = 1 }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == 0);
        f = (struct fake){ .channel = channel, .initial_busy = 0x80 }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -EBUSY && !f.updates && !f.requests);
        for (unsigned request = 1; request <= 0x8000; request <<= 1) {
            f = (struct fake){ .channel = channel, .initial_req = request }; ops = io(&f);
            assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -EBUSY && !f.updates && !f.requests);
        }
        for (unsigned stale = 0; stale <= 1; ++stale) {
            f = (struct fake){ .channel = channel, .stale_only = stale, .never_ready = !stale }; ops = io(&f);
            assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -ETIMEDOUT);
            assert(!result.complete && f.requests == 1 && f.updates == 3 && f.now <= 100000000);
            assert(result.saw_zero == !stale);
        }
        f = (struct fake){ .channel = channel, .stale_only = 1, .freeze = 1 }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -ETIMEDOUT);
        assert(f.latch_reads == 2000 && f.requests == 1 && !result.complete);
        f = (struct fake){ .channel = channel, .delay_ns = 10000000 }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -ETIMEDOUT);
        assert(!result.complete && !f.requests && f.updates == 1 && f.now == 100000000);
        f = (struct fake){ .channel = channel, .corrupt_restore = 1 }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -EIO);
        assert(!result.complete && !strcmp(result.phase, "verify-restore") && result.uncertain == 7);
        f = (struct fake){ .channel = channel, .restore_delay_ns = 100000000 }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -ETIMEDOUT);
        assert(!result.complete && !strcmp(result.phase, "restore") && result.uncertain == 7 && !result.restored);
        assert(f.requests == 1 && f.updates == 4 && f.now >= 100000000);
        f = (struct fake){ .channel = channel, .busy_after_setup = 1 }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -EBUSY);
        assert(!result.complete && !f.requests && f.updates == 1 && result.uncertain == 1);
        f = (struct fake){ .channel = channel, .request_stays_set = 1 }; ops = io(&f);
        assert(i14_adc_sample_channel(&ops, channel, "sample", &result) == -EBUSY);
        assert(result.saw_zero && !result.complete && f.requests == 1 && f.updates == 3);
    }
}
int main(void)
{
    struct fake f = { .config = { 0x8014, 0x1234, 0x8002 } };
    struct i14_adc_io ops = io(&f);
    struct i14_adc_result result;
    assert(i14_adc_sample(&ops, "other", &result) == -EINVAL && !f.operations);
    assert(i14_adc_sample(&ops, "sample", &result) == 0);
    assert(result.saw_zero && result.restored == 7 && result.latch == 0xe001);
    assert(result.channel == 0 && result.raw == 0x6001 && result.mv == 4050);
    assert(f.updates == 6 && f.requests == 1 && f.latch_reads == 3);
    assert(f.config[0] == (0x8014 ^ 2) && f.config[1] == (0x1234 ^ 0x100) && f.config[2] == (0x8002 ^ 0x40));
    unsigned successful_operations = f.operations;
    for (unsigned failure = 1; failure <= successful_operations; failure++) {
        f = (struct fake){ .config = { 0x8014, 0x1234, 0x8002 }, .failure_at = failure }; ops = io(&f);
        assert(i14_adc_sample(&ops, "sample", &result) == -EIO);
        assert(f.operations == failure); /* No writes/reads after transport uncertainty. */
        assert(!result.complete);
    }
    for (unsigned busy = 1; busy <= 0x80; busy <<= 7) {
        f = (struct fake){ .initial_busy = busy }; ops = io(&f);
        assert(i14_adc_sample(&ops, "sample", &result) == -EBUSY);
        assert(!f.updates && !f.requests && !result.complete);
    }
    f = (struct fake){ .initial_req = 1 }; ops = io(&f);
    assert(i14_adc_sample(&ops, "sample", &result) == -EBUSY && !f.updates && !f.requests);
    f = (struct fake){ .stale_only = 1 }; ops = io(&f);
    assert(i14_adc_sample(&ops, "sample", &result) == -ETIMEDOUT);
    assert(!result.saw_zero && !result.complete && f.requests == 1 && f.now <= 100000000);
    f = (struct fake){ .never_ready = 1 }; ops = io(&f);
    assert(i14_adc_sample(&ops, "sample", &result) == -ETIMEDOUT);
    assert(result.saw_zero && !result.complete && f.requests == 1 && f.now <= 100000000);
    f = (struct fake){ .stale_only = 1, .freeze = 1 }; ops = io(&f);
    assert(i14_adc_sample(&ops, "sample", &result) == -ETIMEDOUT);
    assert(!result.complete && f.latch_reads == 2000 && f.requests == 1);
    f = (struct fake){ .corrupt_restore = 1 }; ops = io(&f);
    assert(i14_adc_sample(&ops, "sample", &result) == -EIO);
    assert(!result.complete && !strcmp(result.phase, "verify-restore") && result.uncertain == 7);
    f = (struct fake){ .ignore_enable = 1 }; ops = io(&f);
    assert(i14_adc_sample(&ops, "sample", &result) == -EIO);
    assert(!result.complete && !f.requests && f.updates == 1);
    f = (struct fake){ .busy_after_setup = 1 }; ops = io(&f);
    assert(i14_adc_sample(&ops, "sample", &result) == -EBUSY);
    assert(!result.complete && !f.requests && f.updates == 1 && result.uncertain == 1);
    f = (struct fake){ .delay_ns = 10000000 }; ops = io(&f);
    assert(i14_adc_sample(&ops, "sample", &result) == -ETIMEDOUT);
    assert(!result.complete && !f.requests && f.updates == 1 && f.now == 100000000);
    f = (struct fake){ .request_stays_set = 1 }; ops = io(&f);
    assert(i14_adc_sample(&ops, "sample", &result) == -EBUSY);
    assert(result.saw_zero && !result.complete && f.requests == 1 && f.updates == 3);
    channel_tests();
    puts("battery sample: fixed CH0/2/3 requests/registers/scaling, reserved-bit rejection, RDY0->1, deadlines/restoration and error-stop PASS");
}
