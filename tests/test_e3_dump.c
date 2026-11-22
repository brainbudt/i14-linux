/* Host test for the E3 helpers in kernel/i14-e4-policy.h: line format against
 * the stock dump, the read range, decode vectors and the CH1 shim. */
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#ifndef EUCLEAN
#define EUCLEAN 117
#endif
#include "../kernel/i14-e4-policy.h"

/* --- 1. range: every address the dump may read --- */
static unsigned max_read, reads, odd_reads;
static int dump_rd(void *c, unsigned a, unsigned *v)
{
    (void)c;
    assert(a < 0x1000 && !(a & 1)); /* never the RTC/other blocks */
    reads++; odd_reads += a & 1;
    if (a > max_read) max_read = a;
    *v = a ^ 0x5a5a;
    return 0;
}
static int no_up(void *c, unsigned a, unsigned m, unsigned v, int f)
{ (void)c; (void)a; (void)m; (void)v; (void)f; assert(!"E3 dump must not write"); return -EPERM; }
static int no_wr(void *c, unsigned a, unsigned v) { (void)c; (void)a; (void)v; assert(!"E3 dump must not write"); return -EPERM; }
static unsigned long long zero_now(void *c) { (void)c; return 0; }
static void no_pause(void *c, unsigned us) { (void)c; (void)us; }

/* --- 2. CH1 shim, fake built like tests/test_battery_sample.c --- */
struct fake { unsigned config[3], updates, requests, latch_reads, other_reads, busy, ops; unsigned long long now; };
static int rd(void *context, unsigned address, unsigned *value)
{
    struct fake *f = context;
    f->ops++; f->now += 1000;
    switch (address) {
    case 0xe88: *value = 0; break;
    case 0xe84: *value = f->busy; break;
    case 0xe86: *value = 0; break;
    case 0xe94: *value = f->config[0]; break;
    case 0xeb0: *value = f->config[1]; break;
    case 0x28a: *value = f->config[2]; break;
    case 0xe32:
        assert(f->requests == 1);
        f->latch_reads++;
        *value = f->latch_reads == 1 ? 0x0001 : 0x8000 | 23626; /* stock CH1 raw */
        break;
    default: f->other_reads++; assert(!"read outside CH1 path (0x0e2e must become 0x0e32)");
    }
    return 0;
}
static int update(void *context, unsigned address, unsigned mask, unsigned value)
{
    struct fake *f = context;
    unsigned index = address == 0xe94 ? 0 : address == 0xeb0 ? 1 : 2;
    assert((address == 0xe94 && mask == 0x8000) || (address == 0xeb0 && mask == 0x80) ||
           (address == 0x28a && mask == 0x100));
    f->ops++; f->updates++;
    f->config[index] = (f->config[index] & ~mask) | value;
    return 0;
}
static int wr(void *context, unsigned address, unsigned value)
{
    struct fake *f = context;
    assert(address == 0xe8a && value == 2 && !f->requests); /* RQST_CH1 only */
    f->ops++; f->requests++;
    return 0;
}
static unsigned long long tm(void *context) { return ((struct fake *)context)->now; }
static void pause_poll(void *context) { ((struct fake *)context)->now += 100000; }

static void ch1_shim(void)
{
    struct fake f = { .config = { 0x8014, 0, 0x8002 } };
    struct i14_adc_io in = { &f, rd, update, wr, tm, pause_poll };
    struct i14_adc_result res;
    unsigned v;
    assert(i14_ch1_sample(&in, &res) == 0);
    assert(res.complete && res.restored == 7 && res.saw_zero && !res.uncertain);
    assert(res.raw == 23626 && res.mv == 3893 && f.requests == 1 && f.latch_reads == 2);
    assert(f.updates == 6 && f.config[0] == 0x8014 && f.config[1] == 0 && f.config[2] == 0x8002);
    /* CH1 busy (bit 1) blocks exactly like CH0 busy: no enables, no request. */
    f = (struct fake){ .config = { 0x8014, 0, 0x8002 }, .busy = 2 };
    assert(i14_ch1_sample(&in, &res) == -EBUSY && !f.updates && !f.requests);
    assert(i14_ch1_rd(&in, 0x0e84, &v) == 0 && v == 3); /* v | ((v >> 1) & 1) */
    /* Every other write is refused before reaching the inner io. */
    f = (struct fake){ 0 };
    assert(i14_ch1_wr(&in, 0x0e8a, 2) == -EPERM && i14_ch1_wr(&in, 0x0e8a, 4) == -EPERM);
    assert(i14_ch1_wr(&in, 0x0e8c, 1) == -EPERM && i14_ch1_wr(&in, 0x0f48, 1) == -EPERM && !f.ops);
}

int main(int argc, char **argv)
{
    const char *path = argc > 1 ? argv[1] : "artifacts/phase1-run-20261102/pmic-dump-charging.txt";
    struct i14_e4_io io = { 0, dump_rd, no_up, no_wr, zero_now, no_pause };
    static unsigned short vals[2048];
    char line[64], mine[24];
    unsigned a, v, bad = 0, n = 0, expect = 0;
    int mv;
    FILE *fp;

    for (a = 0; a < 0x20000; a++)
        assert(i14_e3_range_ok(a) == (!(a & 1) && a < 0x1000));
    assert(i14_e3_dump(&io, 0, vals, &bad) == 45 && reads == 45 && max_read == 0x0fa0);
    assert(vals[0] == (0x0f48 ^ 0x5a5a) && vals[44] == (0x0fa0 ^ 0x5a5a));
    reads = max_read = 0;
    assert(i14_e3_dump(&io, 1, vals, &bad) == 2048 && reads == 2048 && max_read == 0x0ffe && !odd_reads);
    reads = 0;
    assert(i14_e3_dump(&io, 2, vals, &bad) == -EINVAL && !reads);
    /* AUXADC write allowlist of E3 (spec 2.2) */
    assert(i14_e34_adc_write_ok(0x0e8a, 0xffff, 2) && !i14_e34_adc_write_ok(0x0e8a, 0xffff, 3));
    assert(!i14_e34_adc_write_ok(0x0e8a, 0xffff, 16) && !i14_e34_adc_write_ok(0x0f48, 0x10, 0x10));
    assert(i14_e34_adc_write_ok(0x0e94, 0x8000, 0) && !i14_e34_adc_write_ok(0x0e94, 0x8000, 0x4000));

    /* decode vectors (spec 1.2) */
    assert(i14_e4_decode(0, 56154, &mv) == 23386 && mv == 3853);
    assert(i14_e4_decode(1, 56394, &mv) == 23626 && mv == 3893);
    assert(i14_e4_decode(2, 33967, &mv) == 1199 && mv == 4976);
    assert(i14_e4_decode(3, 34186, &mv) == 1418 && mv == 623);
    assert(!i14_battery_temperature_mc(mv, &mv) && mv == 28129);
    assert(i14_e4_decode(4, 1, &mv) == -ERANGE);

    ch1_shim();

    assert(i14_e3_line(mine, 0x0f48, 123) == 11 && !memcmp(mine, "0x0f48 123\n", 11));
    assert(i14_e3_line(mine, 0x0ffe, 65535) == 13 && !memcmp(mine, "0x0ffe 65535\n", 13));
    fp = fopen(path, "r");
    if (!fp) {
        printf("e3 dump: SKIPPED stock-fixture comparison (%s missing)\n", path);
    } else {
        while (fgets(line, sizeof(line), fp)) {
            if (strncmp(line, "0x", 2)) continue;
            assert(sscanf(line, "0x%x %u", &a, &v) == 2 && a == expect);
            assert(i14_e3_line(mine, a, v) == strlen(line) && !memcmp(mine, line, strlen(line)));
            expect += 2; n++;
        }
        fclose(fp);
        assert(n == 2048);
        printf("e3 dump: %u stock lines byte-identical\n", n);
    }
    puts("e3 dump: range (<0x1000, even), 45/2048 reads, line format, decode vectors, CH1 shim PASS");
    return 0;
}
