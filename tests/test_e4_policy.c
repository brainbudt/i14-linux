/* Host test for kernel/i14-e4-policy.h against a fake MT6328 charger block.
 * Model sources: [HW,E1] measured 2026-11-02; [unproven] runs in both variants. */
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#ifndef EUCLEAN
#define EUCLEAN 117
#endif
#include "../kernel/i14-e4-policy.h"

#define MS 1000000ULL
#define SEC (1000 * MS)
#define NEVER (~0ULL)
struct fake {
    unsigned reg[0x1000], adc[4], adc_state[4];
    unsigned long long now, td0, kick_at, fail_t, ev_at;
    int mode_sticks;    /* [unproven] MODE/HWCV writes stick while CHR_EN=0 */
    int wdt_clears;     /* [unproven] variant A: WDT expiry clears CHR_EN (and so CSDAC/MODE/HWCV) */
    unsigned ops, fail_at, fail_from, ignore_chr_off, ignore_kick, det_stuck, det_rest, ev_a, ev_m, ev_v, flip_a, flip_m;
    unsigned long long out_at, dly_at, dly_ns; /* dly: one read of dly_a at >= dly_at takes dly_ns */
    unsigned dly_a, dly_nl, req_a, req_m, req_v, req_ph, req_why, req_nl; /* req: stop request inside an op */
    unsigned nows, req_now, req_mbo, req_at_ph; /* req_now: reboot request at the n-th now() */
};
struct wlog { unsigned short a, m, v; char k; unsigned long long t; };
static struct fake F;
static struct wlog L[1 << 14];
static unsigned NL;
static struct i14_e4_state S;
static struct i14_e4_rec R;

/* The fake's own write allowlist, typed apart from the policy (spec 3.2). */
static const struct { unsigned short reg, bits, val; } allowed[] = {
    { 0xf4a, 0xf0, 0xa0 }, { 0xf4e, 0x1f, 13 }, { 0xf50, 0x0f, 15 }, { 0xf50, 0x0f, 14 }, { 0xf50, 0x0f, 13 },
    { 0xf54, 0x0f, 5 }, { 0xf76, 0x07, 4 }, { 0xf76, 0x70, 0x10 }, { 0xf74, 0x07, 1 }, { 0xf74, 0x70, 0x20 },
    { 0xf4c, 0x08, 8 }, { 0xf4c, 0x02, 2 }, { 0xf62, 0x0f, 0 }, { 0xf66, 0x01, 1 }, { 0xf66, 0x01, 0 },
    { 0xf62, 0x10, 0x10 }, { 0xf62, 0x10, 0 }, { 0xf62, 0x100, 0x100 }, { 0xf66, 0x02, 2 },
    { 0xf48, 0x10, 0x10 }, { 0xf48, 0x08, 8 }, { 0xf7a, 0x04, 4 }, { 0xf7a, 0x40, 0x40 }, { 0xf48, 0x18, 0 },
    { 0xe94, 0x8000, 0x8000 }, { 0xe94, 0x8000, 0 }, { 0xeb0, 0x80, 0x80 }, { 0xeb0, 0x80, 0 },
    { 0x28a, 0x100, 0x100 }, { 0x28a, 0x100, 0 }, { 0xe8a, 0xffff, 1 }, { 0xe8a, 0xffff, 2 },
    { 0xe8a, 0xffff, 4 }, { 0xe8a, 0xffff, 8 } };
static int fake_allowed(unsigned a, unsigned m, unsigned v, unsigned cs)
{
    unsigned i;
    if (a >= 0x1000 || (a >= 0xc00 && a <= 0xcff) || a == 0xf68 || a == 0xf70 || a == 0xf5c) return 0;
    for (i = 0; i < sizeof(allowed) / sizeof(allowed[0]); i++)
        if (allowed[i].reg == a && allowed[i].bits == m && allowed[i].val == v)
            return a != 0xf50 || v == cs;
    return 0;
}
static void gates_off(void) /* [HW,E1] CHR_EN=0 clears CSDAC (f48 b3) and MODE/HWCV (f7a b2/b6) */
{ F.reg[0xf48] &= ~0x18u; F.reg[0xf7a] &= ~0x44u; }
static void tick(void)
{
    F.now += 20000;
    if (F.ev_at && F.now >= F.ev_at) {
        F.ev_at = 0;
        if (F.ev_a == 0x2000) F.ignore_kick = 1;
        else if (F.ev_a >= 0x1000) F.adc[F.ev_a - 0x1000] = F.ev_v;
        else F.reg[F.ev_a] = (F.reg[F.ev_a] & ~F.ev_m) | F.ev_v;
        if (F.ev_a == 0xf48 && !(F.reg[0xf48] & 0x10)) gates_off();
    }
    /* [unproven] WDT runs while EN (f62 b4); expiry sets OUT (f66 b2) */
    if ((F.reg[0xf62] & 0x10) && F.td0 && !(F.reg[0xf66] & 4) && F.now - F.kick_at >= F.td0) {
        F.reg[0xf66] |= 4; F.out_at = F.now;
        if (F.wdt_clears) gates_off();
    }
    /* [model] CS/CC_DET follow charging; det_stuck keeps them after a stop.
     * [HW, E3 + E4a 2026-11-03] at rest with VBUS 0xf4c reads 0x84: CC_DET (b7) stays 1. */
    if ((F.reg[0xf48] & 0x18) == 0x18 && (F.reg[0xf7a] & 4)) F.reg[0xf4c] |= 0xa0;
    else if (!F.det_stuck) F.reg[0xf4c] = (F.reg[0xf4c] & ~0xe0u) | F.det_rest;
}
static int failing(void)
{
    ++F.ops;
    if (F.fail_t && F.now >= F.fail_t) { F.fail_t = 0; return 1; }
    return (F.fail_at && F.ops == F.fail_at) || (F.fail_from && F.ops >= F.fail_from);
}
static void inject(unsigned a, unsigned m, unsigned v) /* a stop request arriving during this op */
{
    if (F.req_why && a == F.req_a && m == F.req_m && v == F.req_v && S.phase >= F.req_ph)
    { S.w.req_ext = F.req_why; F.req_why = 0; F.req_nl = NL; }
}
static void wlog(unsigned a, unsigned m, unsigned v, char k)
{ if (NL < sizeof(L) / sizeof(L[0])) L[NL++] = (struct wlog){ (unsigned short)a, (unsigned short)m, (unsigned short)v, k, F.now }; }
static int f_read(void *c, unsigned a, unsigned *v)
{
    unsigned ch = a == 0xe2e ? 0 : a == 0xe32 ? 1 : a == 0xe04 ? 2 : a == 0xe06 ? 3 : 4;
    (void)c;
    if (F.dly_ns && a == F.dly_a && F.now >= F.dly_at) { F.now += F.dly_ns; F.dly_ns = 0; F.dly_nl = NL; }
    tick();
    assert((a >= 0xf48 && a <= 0xfa0 && !(a & 1)) || a == 0xcb2 || a == 0xccc || ch < 4 ||
           a == 0xe84 || a == 0xe86 || a == 0xe88 || a == 0xe94 || a == 0xeb0 || a == 0x28a);
    if (failing()) return -EIO;
    if (ch == 4) { *v = F.reg[a] ^ (a == F.flip_a ? F.flip_m : 0); return 0; } /* flip: readback mismatch */
    *v = F.adc[ch] | (F.adc_state[ch] == 1 ? 0 : 0x8000); /* RDY 0 once after a request */
    F.adc_state[ch] = F.adc_state[ch] == 1 ? 2 : 0;
    return 0;
}
static int f_write(void *c, unsigned a, unsigned v)
{
    int fail;
    (void)c; tick(); inject(a, 0xffff, v);
    assert(fake_allowed(a, 0xffff, v, 0));
    fail = failing();
    if (fail && F.fail_from) return -EIO;
    wlog(a, 0xffff, v, 'W');
    F.adc_state[v == 1 ? 0 : v == 2 ? 1 : v == 4 ? 2 : 3] = 1;
    return fail ? -EIO : 0;
}
static int f_update(void *c, unsigned a, unsigned m, unsigned v, int force)
{
    unsigned nv = (F.reg[a] & ~m) | v, old = F.reg[a];
    int fail;
    (void)c; tick(); inject(a, m, v);
    assert(fake_allowed(a, m, v, S.cs) && (!force || a == 0xf62 || a == 0xf66 || a == 0xf48));
    fail = failing();
    if (fail && F.fail_from) return -EIO; /* dead transport: nothing applied */
    wlog(a, m, v, force ? 'F' : 'U');     /* a single failure may still have applied */
    if (!force && nv == old) return fail ? -EIO : 0;
    if (a == 0xf48) {
        if (F.ignore_chr_off && (old & 0x10) && !(nv & 0x10)) return fail ? -EIO : 0; /* injected */
        nv = (old & ~0x19u) | (nv & 0x19);            /* RW mask 0x19 */
        if (!(nv & 0x10)) nv &= ~8u;                  /* [HW,E1] CSDAC=1 ignored while CHR_EN=0 */
        F.reg[0xf48] = nv;
        if ((old & 0x10) && !(nv & 0x10)) gates_off(); /* [HW,E1] */
    } else if (a == 0xf7a) {
        if (!(F.reg[0xf48] & 0x10) && !F.mode_sticks) nv = (nv & ~0x44u) | (old & 0x44);
        F.reg[0xf7a] = nv;
    } else if (a == 0xf62) {
        if ((nv & 0x100) && !F.ignore_kick) F.kick_at = F.now; /* WR restarts the WDT */
        F.reg[0xf62] = nv & ~0x100u;
    } else if (a == 0xf66) {
        nv = (nv & ~4u) | (old & 4);                  /* OUT is read-only ... */
        if (nv & 2) nv &= ~4u;                        /* ... FLAG_WR clears it */
        F.reg[0xf66] = nv & ~2u;
    } else F.reg[a] = nv;
    return fail ? -EIO : 0;
}
static unsigned long long f_now(void *c)
{
    (void)c;
    if (F.req_now && ++F.nows == F.req_now) { /* e4_reboot: request; mb; read may_be_on */
        S.w.req_ext = I14_R_REBOOT; F.req_mbo = S.w.may_be_on; F.req_nl = NL; F.req_at_ph = S.phase; F.req_now = 0;
    }
    return F.now;
}
static void f_pause(void *c, unsigned us) { (void)c; F.now += us * 1000ULL; }
static struct i14_e4_io IO = { 0, f_read, f_update, f_write, f_now, f_pause };

static unsigned raw15(int mv) { return (unsigned)((mv * 32768 + 5399) / 5400); }
/* LK handover state (PHASE4-STATE section 4) with the stock ADC values */
static int start(unsigned mode, unsigned cs, unsigned long long td0, int wdt_clears, int sticks)
{
    static const unsigned short init[][2] = { { 0xf48, 0x63 }, { 0xf4a, 0xf2 }, { 0xf4c, 0x84 },
        { 0xf4e, 0xcd }, { 0xf50, 0x0c }, { 0xf54, 0x01 }, { 0xf56, 0x05 }, { 0xf62, 0x13 }, { 0xf66, 0x05 },
        { 0xf68, 0x04 }, { 0xf70, 0x03 }, { 0xf74, 0x22 }, { 0xf76, 0x24 }, { 0xf78, 0x44 }, { 0xf7a, 0x90 },
        { 0xe94, 0x8014 }, { 0x28a, 0x8002 }, { 0xcb2, 0x1234 }, { 0xccc, 0x42 } };
    unsigned i, keep[3] = { F.fail_at, F.fail_from, F.ignore_chr_off };
    F = (struct fake){ .now = SEC, .td0 = td0, .wdt_clears = wdt_clears, .mode_sticks = sticks, .det_rest = 0x80,
                       .adc = { 23386, 23626, 1199, 1418 }, .fail_at = keep[0], .fail_from = keep[1],
                       .ignore_chr_off = keep[2] };
    for (i = 0; i < sizeof(init) / sizeof(init[0]); i++) F.reg[init[i][0]] = init[i][1];
    NL = 0;
    S = (struct i14_e4_state){ .mode = mode, .cs = cs };
    i14_e4_arm(&S, F.now);
    return i14_e4_init_run(&IO, &S, &R);
}
struct sim { unsigned long long stuck_from, stuck_to, ext_at, force_ns, good_at_force, panic_ns;
             unsigned ext, until_force, stop_at_done, stop_ops, done_ops, early_ops; };
/* Loop/stop work plus the 250 ms hrtimer, as in i14-e4-charge.c. */
static void run(struct sim *s, unsigned long long until)
{
    unsigned long long work = F.now, timer = F.now + 250 * MS;
    unsigned pre;
    int next;
    while (F.now < until && (work != NEVER || timer != NEVER)) {
        if (s->ext_at && !S.w.req_ext && s->ext_at <= work && s->ext_at <= timer) {
            if (s->ext_at > F.now) F.now = s->ext_at;
            S.w.req_ext = s->ext; work = F.now; /* debugfs stop / exit / reboot queue the stop work */
            continue;
        }
        if (timer <= work) {
            if (timer > F.now) F.now = timer;
            next = (int)i14_e4_watch(&S.w, F.now);
            if (next == I14_W_PANIC) { s->panic_ns = F.now; return; }
            if (next == I14_W_FORCE_STOP) {
                if (!s->force_ns) { s->force_ns = F.now; s->good_at_force = S.w.last_good_ns; }
                if (s->until_force) s->stuck_to = F.now;
                if (work > F.now) work = F.now;
            }
            timer = S.w.finished ? NEVER : timer + 250 * MS;
            continue;
        }
        if (work > F.now) F.now = work;
        if (s->stuck_from && F.now >= s->stuck_from && F.now < s->stuck_to) { work = s->stuck_to; continue; }
        pre = F.ops;
        next = i14_e4_step(&IO, &S, &R);
        if (!s->early_ops && S.passes >= 5) s->early_ops = F.ops;
        if (S.phase == I14_P_DONE && !s->done_ops) { s->done_ops = F.ops; s->stop_ops = pre; if (s->stop_at_done) return; }
        work = next < 0 ? NEVER : F.now + (unsigned)next * MS;
    }
}
static int find(unsigned from, unsigned a, unsigned m, unsigned v)
{
    for (; from < NL; from++) if (L[from].a == a && L[from].m == m && L[from].v == v) return (int)from;
    return -1;
}
static int chg(unsigned i) { return L[i].a >= 0xf48 && L[i].a <= 0xfa0; }
static int kick_after(unsigned from) { return find(from, 0xf62, 0x100, 0x100) >= 0; }
/* Shutdown order: the first charger write after the last kick is W20; after
 * it only W20 retries and then S2 (only when S1 verified). */
static void stop_order(int expect_s2)
{
    int w = find(0, 0xf48, 0x18, 0), s2 = -1;
    unsigned i;
    assert(w >= 0 && L[w].k == 'F' && S.phase == I14_P_DONE && !S.w.may_be_on && !(F.reg[0xf48] & 0x18));
    for (i = (unsigned)w; i-- > 0 && !(L[i].a == 0xf66 && L[i].m == 2) && !(L[i].a == 0xf7a && L[i].m == 0x40);)
        assert(!chg(i)); /* back to the last kick or W19 */
    for (i = (unsigned)w; i < NL; i++) {
        if (!chg(i) || (L[i].a == 0xf48 && L[i].m == 0x18)) continue;
        assert(expect_s2 && s2 < 0 && L[i].a == 0xf62 && L[i].m == 0x10 && !L[i].v && S.stop_verified);
        s2 = (int)i; i++;
        assert(L[i].a == 0xf66 && L[i].m == 1 && !L[i].v);
    }
    assert((s2 > w) == !!expect_s2 && S.wdt_off == !!expect_s2 && !(F.reg[0xf62] & 0x10) == !!expect_s2);
}
static void reason_case(unsigned why, unsigned ev_a, unsigned ev_m, unsigned ev_v, unsigned ext)
{
    struct sim s = { .stop_at_done = 1 };
    unsigned long long t0 = F.now;
    assert(!start(1, 14, 4 * SEC, 1, 0));
    t0 = F.now;
    if (ext) { s.ext = ext; s.ext_at = t0 + 10 * SEC; }
    else if (why == I14_R_IO) F.fail_t = t0 + 10 * SEC;
    else if (why == I14_R_HEARTBEAT) { s.stuck_from = t0 + 10 * SEC; s.until_force = 1; s.stuck_to = NEVER; }
    else if (why == I14_R_WDT_OUT) { F.ev_at = t0 + 10 * SEC; F.ev_a = 0x2000; } /* WR stops restarting the WDT */
    else { F.ev_at = t0 + 10 * SEC; F.ev_a = ev_a; F.ev_m = ev_m; F.ev_v = ev_v; }
    run(&s, t0 + 70 * SEC);
    assert(!s.panic_ns && S.reason == why && S.result == (why == I14_R_TIME ? I14_RES_COMPLETE : I14_RES_ABORTED));
    stop_order(1);
}

int main(void)
{
    struct sim s;
    struct i14_e4_op op[10], bad[] = { { 0xf68, 0x0c, 4, 0 }, { 0x0c0c, 0xffff, 1, 0 }, { 0x4000, 1, 1, 0 },
        { 0xf50, 0x0f, 12, 0 }, { 0xf5c, 0x10, 0x10, 0 }, { 0xf48, 0x18, 0x18, 1 }, { 0xf62, 0x0f, 3, 0 } };
    struct i14_e4_rec r;
    unsigned a, i, j, k, n, persist, mode, w16;
    unsigned long long t0;
    int mv, rc;

    /* 8. configuration */
    assert(i14_e4_cfg_ok("e4a", 15, 0, 0) == 0 && i14_e4_cfg_ok("e4a", 14, 0, 0) < 0 && i14_e4_cfg_ok("e4a", 15, 3000, 0) < 0);
    assert(i14_e4_cfg_ok("e4b", 14, 2000, 0) == 1 && i14_e4_cfg_ok("e4b", 14, 6000, 0) == 1 && i14_e4_cfg_ok("e4b", 13, 6000, 0) < 0 && i14_e4_cfg_ok("e4b", 14, 0, 0) < 0);
    assert(i14_e4_cfg_ok("e4b", 14, 1999, 0) < 0 && i14_e4_cfg_ok("e4b", 14, 6001, 0) < 0 && i14_e4_cfg_ok("e4b", 12, 3000, 0) < 0);
    assert(i14_e4_cfg_ok("e4b", 16, 3000, 0) < 0 && i14_e4_cfg_ok(NULL, 15, 0, 0) < 0 && i14_e4_cfg_ok("e4c", 15, 0, 0) < 0);
    /* e5: cs 14, td0 of an E4a PASS, run_s 300..1800; run_s only for e5 */
    assert(i14_e4_cfg_ok("e5", 14, 4000, 300) == 2 && i14_e4_cfg_ok("e5", 14, 4000, 1800) == 2);
    assert(i14_e4_cfg_ok("e5", 14, 4000, 299) < 0 && i14_e4_cfg_ok("e5", 14, 4000, 1801) < 0 && i14_e4_cfg_ok("e5", 14, 4000, 0) < 0);
    assert(i14_e4_cfg_ok("e5", 15, 4000, 600) < 0 && i14_e4_cfg_ok("e5", 14, 0, 600) < 0 && i14_e4_cfg_ok("e5", 14, 6001, 600) < 0);
    assert(i14_e4_cfg_ok("e4b", 14, 4000, 600) < 0 && i14_e4_cfg_ok("e4a", 15, 0, 600) < 0);
    /* 2. bounds: policy allowlist == independent fake allowlist, everywhere */
    for (a = 0; a < 0x10000; a++)
        for (i = 0; i < sizeof(allowed) / sizeof(allowed[0]) + 2; i++)
            for (k = 13; k <= 15; k++) {
                unsigned m = i < sizeof(allowed) / sizeof(allowed[0]) ? allowed[i].bits : 0xffff;
                unsigned v = i < sizeof(allowed) / sizeof(allowed[0]) ? allowed[i].val : i & 1 ? 0 : 0x18;
                assert(i14_e4_write_ok(a, m, v, k) == fake_allowed(a, m, v, k));
            }
    assert(!start(1, 14, 4 * SEC, 1, 0));
    for (i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        n = NL;
        assert(i14_e4_run(&IO, &S, &R, &bad[i], 1) == -EPERM && NL == n && S.last_addr == bad[i].addr);
    }
    /* 1. order: S0, arm and kick before W16; W16 before W17/W18/W19 */
    s = (struct sim){ 0 };
    run(&s, F.now + 3 * SEC);
    w16 = (unsigned)find(0, 0xf48, 0x10, 0x10);
    for (j = I14_S0; j <= I14_KICK; j++)
        for (i = 0, n = i14_e4_seq(j, 14, op); i < n; i++)
            assert((rc = find(0, op[i].addr, op[i].mask, op[i].value)) >= 0 && rc < (int)w16);
    assert(find(0, 0xf48, 8, 8) > (int)w16 && find(0, 0xf7a, 4, 4) > find(0, 0xf48, 8, 8));
    assert(find(0, 0xf7a, 0x40, 0x40) > find(0, 0xf7a, 4, 4) && S.phase == I14_P_CHARGING);
    /* 3. edges of check() on a good charging record */
    r = (struct i14_e4_rec){ .full = 1, .temp_ok = 1, .temp_mc = 28129, .mv = { 3853, 3893, 4976, 623 } };
    I14_F(&r, 0xf48) = 0x7b; I14_F(&r, 0xf4a) = 0xa2; I14_F(&r, 0xf4c) = 0xae; I14_F(&r, 0xf4e) = 0xcd;
    I14_F(&r, 0xf50) = 14; I14_F(&r, 0xf54) = 5; I14_F(&r, 0xf56) = 5; I14_F(&r, 0xf62) = 0x10;
    I14_F(&r, 0xf66) = 1; I14_F(&r, 0xf7a) = 0xd4;
    S = (struct i14_e4_state){ .phase = I14_P_CHARGING, .cs = 14, .mode = 1 };
    {
        static const int v[][3] = { { 0, 3599, I14_R_VBAT }, { 0, 3600, 0 }, { 0, 4250, 0 }, { 0, 4251, I14_R_VBAT },
            { 2, 4499, I14_R_VBUS }, { 2, 4500, 0 }, { 2, 5500, 0 }, { 2, 5501, I14_R_VBUS },
            { 9, 9999, I14_R_TEMP }, { 9, 10000, 0 }, { 9, 40000, 0 }, { 9, 40001, I14_R_TEMP } };
        for (i = 0; i < sizeof(v) / sizeof(v[0]); i++) {
            struct i14_e4_rec x = r;
            if (v[i][0] == 9) x.temp_mc = v[i][1]; else x.mv[v[i][0]] = v[i][1];
            assert(i14_e4_check(&S, &x) == (unsigned)v[i][2]);
        }
        static const unsigned short sp[][3] = { { 0xf50, 0x0f, 12 }, { 0xf54, 0x0f, 1 }, { 0xf4c, 2, 0 },
            { 0xf4c, 8, 0 }, { 0xf4a, 0xf0, 0x90 }, { 0xf62, 0x1f, 0x13 }, { 0xf62, 0x10, 0 }, { 0xf48, 1, 0 },
            { 0xf48, 0x18, 0x10 }, { 0xf48, 0x18, 0x08 }, { 0xf56, 5, 4 }, { 0xf4e, 0x1f, 12 }, { 0xf7a, 0x44, 0x40 },
            { 0xf7a, 0x44, 0x04 }, { 0xf66, 1, 0 } }; /* each setpoint/gate term drifts once: stop */
        assert(i14_e4_check(&S, &r) == I14_R_NONE);
        for (i = 0; i < sizeof(sp) / sizeof(sp[0]); i++) {
            struct i14_e4_rec x = r;
            I14_F(&x, sp[i][0]) = (unsigned short)((I14_F(&x, sp[i][0]) & ~sp[i][1]) | sp[i][2]);
            assert(i14_e4_check(&S, &x) == I14_R_MISMATCH);
        }
        r.temp_ok = 0; assert(i14_e4_check(&S, &r) == I14_R_TEMP); r.temp_ok = 1;
        I14_F(&r, 0xf66) = 5; assert(i14_e4_check(&S, &r) == I14_R_WDT_OUT);
        S.phase = I14_P_ENABLE; assert(i14_e4_check(&S, &r) == I14_R_MISMATCH); I14_F(&r, 0xf66) = 1;
        I14_F(&r, 0xf7a) = 0x90; assert(i14_e4_check(&S, &r) == I14_R_MISMATCH);
    }
    /* 3. every stop reason: first W20, verified S1, then S2 */
    assert(i14_e4_decode(0, raw15(4251), &mv) >= 0 && mv == 4251 && i14_e4_decode(0, raw15(3599), &mv) >= 0 && mv == 3599);
    reason_case(I14_R_HV_DET, 0xf48, 0x80, 0x80, 0);
    reason_case(I14_R_BATON, 0xf9a, 1, 1, 0);
    reason_case(I14_R_OV_DET, 0xf54, 0x40, 0x40, 0);
    reason_case(I14_R_NO_CHRDET, 0xf48, 0x20, 0, 0);
    reason_case(I14_R_VBAT, 0x1000, 0, raw15(4251), 0);
    reason_case(I14_R_VBAT, 0x1000, 0, raw15(3599), 0);
    reason_case(I14_R_TEMP, 0x1003, 0, 0, 0);       /* 0 mV: unknown temperature */
    reason_case(I14_R_TEMP, 0x1003, 0, 910, 0);     /* ~400 mV: about 45 C */
    reason_case(I14_R_VBUS, 0x1002, 0, 1400, 0);    /* 5818 mV */
    reason_case(I14_R_MISMATCH, 0xf4e, 0x1f, 22, 0);
    reason_case(I14_R_MISMATCH, 0xf7a, 0x40, 0, 0);
    reason_case(I14_R_IO, 0, 0, 0, 0);
    reason_case(I14_R_WDT_OUT, 0, 0, 0, 0);
    reason_case(I14_R_HEARTBEAT, 0, 0, 0, 0);
    reason_case(I14_R_USER, 0, 0, 0, I14_R_USER);
    reason_case(I14_R_EXIT, 0, 0, 0, I14_R_EXIT);
    reason_case(I14_R_REBOOT, 0, 0, 0, I14_R_REBOOT);
    /* 7. e4b: 60 s, every kick follows a fully good pass */
    reason_case(I14_R_TIME, 0, 0, 0, 0);
    assert(S.kicks == S.passes - 1 && S.result == I14_RES_COMPLETE);
    assert(L[find(0, 0xf48, 0x18, 0)].t - S.t_w16 >= 60 * SEC && L[find(0, 0xf48, 0x18, 0)].t - S.t_w16 < 61 * SEC);
    for (i = n = 0; i < NL; i++) n += L[i].a == 0xf62 && L[i].m == 0x100;
    assert(n == S.kicks);
    /* AUXADC deadline miss [HW E3, E4b]: one per pass is skipped (no kick), the session goes on */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    F.dly_a = 0xe88; F.dly_at = F.now + 10 * SEC; F.dly_ns = 150 * MS;
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 70 * SEC);
    assert(F.dly_nl && S.result == I14_RES_COMPLETE && S.reason == I14_R_TIME && S.adc_misses == 1);
    assert(S.io_errors == 1 && S.last_err == -ETIMEDOUT && S.kicks == S.passes - 1 && S.stop_verified);
    /* same with the miss during the conversion (CH2 latch poll, after the request): the next pass samples again */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    F.dly_a = 0xe04; F.dly_at = F.now + 10 * SEC; F.dly_ns = 150 * MS;
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 70 * SEC);
    assert(F.dly_nl && S.result == I14_RES_COMPLETE && S.adc_misses == 1 && S.kicks == S.passes - 1);
    /* two misses in a row: stop (io) */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    F.dly_a = 0xe88; F.dly_at = F.now + 10 * SEC; F.dly_ns = 150 * MS;
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.dly_at + 500 * MS);
    assert(F.dly_nl && S.phase == I14_P_CHARGING && S.adc_misses == 1);
    F.dly_at = F.now; F.dly_ns = 150 * MS; F.dly_nl = 0;
    run(&s, F.now + 70 * SEC);
    assert(F.dly_nl && S.result == I14_RES_ABORTED && S.reason == I14_R_IO && S.adc_misses == 1 && S.stop_verified);
    /* more than I14_E4_ADC_MISS misses in one session: stop (io) */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    s = (struct sim){ .stop_at_done = 1 };
    for (k = 0; k <= I14_E4_ADC_MISS; k++) {
        F.dly_a = 0xe88; F.dly_at = F.now + 3 * SEC; F.dly_ns = 150 * MS; F.dly_nl = 0;
        run(&s, F.dly_at + 500 * MS);
        assert(F.dly_nl);
    }
    run(&s, F.now + 70 * SEC);
    assert(S.result == I14_RES_ABORTED && S.reason == I14_R_IO && S.adc_misses == I14_E4_ADC_MISS);
    /* e5: run_s of charging, hard limit run_s + 1 s, ADC misses may grow with the passes (never two in a row) */
    assert(!start(2, 14, 4 * SEC, 1, 0));
    S.run_s = 300;
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 20 * SEC);
    assert(S.phase == I14_P_CHARGING && S.w.limit_ns == S.t_w16 + 301 * SEC);
    for (k = 0; k < 6; k++) {
        F.dly_a = 0xe88; F.dly_at = F.now + 20 * SEC; F.dly_ns = 150 * MS; F.dly_nl = 0;
        run(&s, F.dly_at + 500 * MS);
        assert(F.dly_nl && S.phase == I14_P_CHARGING);
    }
    run(&s, F.now + 400 * SEC);
    assert(S.result == I14_RES_COMPLETE && S.reason == I14_R_TIME && S.adc_misses == 6 && S.stop_verified);
    i = find(0, 0xf48, 0x18, 0);
    assert(i >= 0 && L[i].t - S.t_w16 >= 300 * SEC && L[i].t - S.t_w16 < 301 * SEC);
    assert(S.kicks == S.passes - 1);
    /* e5 early: 4 misses within the first 16 passes still stop (io) */
    assert(!start(2, 14, 4 * SEC, 1, 0));
    S.run_s = 300;
    s = (struct sim){ .stop_at_done = 1 };
    for (k = 0; k <= I14_E4_ADC_MISS; k++) {
        F.dly_a = 0xe88; F.dly_at = F.now + 2 * SEC; F.dly_ns = 150 * MS; F.dly_nl = 0;
        run(&s, F.dly_at + 500 * MS);
    }
    run(&s, F.now + 400 * SEC);
    assert(S.result == I14_RES_ABORTED && S.reason == I14_R_IO);
    /* a register hazard in the missed pass still stops in that pass */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    F.dly_a = 0xe88; F.dly_at = F.now + 10 * SEC; F.dly_ns = 150 * MS;
    F.ev_a = 0xf54; F.ev_m = 0x40; F.ev_v = 0x40; F.ev_at = F.dly_at;
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 70 * SEC);
    assert(F.dly_nl && S.reason == I14_R_OV_DET && S.adc_misses == 1);
    i = find(0, 0xf48, 0x18, 0);
    assert(i >= 0 && L[i].t < F.dly_at + 500 * MS);
    /* det bits stuck: no S2, WDT stays armed and unkicked */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    F.det_stuck = 1;
    s = (struct sim){ .stop_at_done = 1, .ext = I14_R_USER, .ext_at = F.now + 5 * SEC };
    run(&s, F.now + 20 * SEC);
    assert(S.det_stuck && !S.stop_verified && !S.w.may_be_on && S.t_gates_clear && F.now - S.t_gates_clear >= 2 * SEC);
    stop_order(0);
    /* readback mismatch during S0: stop before any W16 */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    F.flip_a = 0xf4a; F.flip_m = 0x40;
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 5 * SEC);
    assert(S.reason == I14_R_MISMATCH && find(0, 0xf48, 0x10, 0x10) < 0 && !S.wdt_armed && !S.w.may_be_on);
    /* a DONE tail pass that finds the gates on again stops again (fail closed) */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    s = (struct sim){ .stop_at_done = 1, .ext = I14_R_USER, .ext_at = F.now + 3 * SEC };
    run(&s, F.now + 10 * SEC);
    F.reg[0xf48] |= 0x18; F.reg[0xf7a] |= 0x44; n = NL;
    assert(i14_e4_step(&IO, &S, &R) == I14_E4_PASS_MS && S.phase == I14_P_DONE && find(n, 0xf48, 0x18, 0) >= (int)n);
    assert(!S.w.may_be_on && S.stop_verified && !(F.reg[0xf48] & 0x18) && S.tail == 1 && S.reon);
    S.tail = I14_E4_TAIL; /* after a re-enable the 1 Hz tail never goes silent */
    assert(i14_e4_step(&IO, &S, &R) == I14_E4_PASS_MS && !S.w.finished);
    S.reon = 0; S.tail = I14_E4_TAIL;
    assert(i14_e4_step(&IO, &S, &R) == -1 && S.w.finished);
    /* 6. e4a dead-man, both MODE variants */
    for (k = 0; k < 2; k++) {
        unsigned long long td[2] = { 4 * SEC, 1500 * MS };
        for (j = 0; j < 2; j++) {
            assert(!start(0, 15, td[j], 1, (int)k));
            s = (struct sim){ .stop_at_done = 1 };
            run(&s, F.now + 20 * SEC);
            assert(S.result == I14_RES_DM_PASS && S.out_seen && S.clear_seen && S.t_clear_hi == S.t_out_hi);
            assert(S.t_out_hi - S.t_kick + 100000 >= td[j] && S.t_out_hi - S.t_kick <= td[j] + 20 * MS);
            assert(S.t_out_lo <= F.out_at && F.out_at <= S.t_out_hi && S.t_clear_lo <= F.out_at);
            stop_order(1);
        }
        assert(!start(0, 15, 4 * SEC, 0, (int)k)); /* variant B: OUT without clear */
        s = (struct sim){ .stop_at_done = 1 };
        run(&s, F.now + 20 * SEC);
        assert(S.result == I14_RES_DM_FAIL && S.dm_why == I14_DM_SLOW_CLEAR);
        /* stop at the first poll past OUT+500 ms, not at the 6 s window (spec 3.7, Sol delta review) */
        assert(F.now <= F.out_at + 1 * SEC && F.now < S.t_kick + 6 * SEC);
        stop_order(1);
        assert(!start(0, 15, 0, 0, (int)k));       /* WDT never fires: FAIL at the 6 s window */
        s = (struct sim){ .stop_at_done = 1 };
        run(&s, F.now + 20 * SEC);
        assert(S.result == I14_RES_DM_FAIL && S.dm_why == I14_DM_WINDOW);
        assert(L[find(0, 0xf48, 0x18, 0)].t - S.t_kick >= 6 * SEC && L[find(0, 0xf48, 0x18, 0)].t - S.t_kick < 6 * SEC + 40 * MS);
        stop_order(1);
        assert(!start(0, 15, 4 * SEC, 1, (int)k)); /* gates clear while OUT=0 */
        F.ev_at = F.now + 2 * SEC; F.ev_a = 0xf48; F.ev_m = 0x10; F.ev_v = 0;
        s = (struct sim){ .stop_at_done = 1 };
        run(&s, F.now + 20 * SEC);
        assert(S.result == I14_RES_DM_FAIL && S.dm_why == I14_DM_CLEAR_WITHOUT_OUT);
        stop_order(1);
    }
    /* review fixes: delayed dead-man reads across the 6 s window and the 500 ms clear limit */
    assert(!start(0, 15, 6050 * MS, 1, 0));                     /* OUT+clear at kick+6.05 s */
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 100 * MS);
    F.dly_a = 0xf48; F.dly_at = F.kick_at + 5950 * MS; F.dly_ns = 200 * MS; /* read lands after 6 s */
    run(&s, F.now + 20 * SEC);
    assert(F.dly_nl && S.result == I14_RES_DM_FAIL && S.dm_why == I14_DM_WINDOW);
    stop_order(1);
    assert(!start(0, 15, 4 * SEC, 0, 0));                       /* OUT at 4 s, gates clear 600 ms later */
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 100 * MS);
    F.ev_at = F.kick_at + 4600 * MS; F.ev_a = 0xf48; F.ev_m = 0x10; F.ev_v = 0;
    F.dly_a = 0xf48; F.dly_at = F.kick_at + 4450 * MS; F.dly_ns = 200 * MS;
    run(&s, F.now + 20 * SEC);
    assert(F.dly_nl && S.result == I14_RES_DM_FAIL && S.dm_why == I14_DM_SLOW_CLEAR && S.t_clear_hi - S.t_out_lo > 500 * MS);
    stop_order(1);
    assert(!start(0, 15, 4 * SEC, 0, 0)); /* one 520 ms poll sees OUT and clear: unprovable <= 500 ms */
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 100 * MS);
    F.ev_at = F.kick_at + 4300 * MS; F.ev_a = 0xf48; F.ev_m = 0x10; F.ev_v = 0;
    F.dly_a = 0xf48; F.dly_at = F.kick_at + 3990 * MS; F.dly_ns = 520 * MS;
    run(&s, F.now + 20 * SEC);
    assert(F.dly_nl && S.t_clear_hi == S.t_out_hi && S.t_clear_hi - S.t_out_lo > 500 * MS);
    assert(S.result == I14_RES_DM_FAIL && S.dm_why == I14_DM_SLOW_CLEAR);
    stop_order(1);
    /* stop request inside S_ARM (W13): never W16; inside a CHARGING pass (CH3 request): no kick */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    F.req_a = 0xf62; F.req_m = 0x10; F.req_v = 0x10; F.req_why = I14_R_USER; F.req_ph = 0;
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 10 * SEC);
    assert(F.req_nl && find(0, 0xf48, 0x10, 0x10) < 0 && S.reason == I14_R_USER && !S.w.may_be_on);
    stop_order(1);
    assert(!start(1, 14, 4 * SEC, 1, 0));
    F.req_a = 0xe8a; F.req_m = 0xffff; F.req_v = 8; F.req_why = I14_R_REBOOT; F.req_ph = I14_P_CHARGING;
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 10 * SEC);
    assert(F.req_nl && !kick_after(F.req_nl) && S.reason == I14_R_REBOOT && S.result == I14_RES_ABORTED);
    stop_order(1);
    /* stale pass (600 ms read delay): no kick, heartbeat stop */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 3 * SEC);
    F.dly_a = 0xccc; F.dly_at = F.now; F.dly_ns = 600 * MS;
    run(&s, F.now + 10 * SEC);
    assert(F.dly_nl && !kick_after(F.dly_nl) && S.reason == I14_R_HEARTBEAT);
    stop_order(1);
    /* an all-zero 0xf48 read after W20 is not "stopped": retry, then PANIC */
    assert(!start(1, 14, 4 * SEC, 1, 0));
    F.flip_a = 0xf48; F.flip_m = 0x63;
    s = (struct sim){ 0 };
    run(&s, F.now + 10 * SEC);
    assert(s.panic_ns && S.w.may_be_on && !S.stop_verified);
    F.flip_a = F.flip_m = 0;
    /* panic clock starts when "request and may_be_on" first holds */
    {
        struct i14_e4_watch w = { .active = 1, .req_ext = I14_R_USER };
        assert(i14_e4_watch(&w, 1 * SEC) == I14_W_NONE && i14_e4_watch(&w, 4 * SEC) == I14_W_NONE);
        w.may_be_on = 1;
        assert(i14_e4_watch(&w, 5 * SEC) == I14_W_FORCE_STOP && i14_e4_watch(&w, 6999 * MS) == I14_W_FORCE_STOP);
        assert(i14_e4_watch(&w, 7 * SEC) == I14_W_PANIC);
    }
    {   /* hard limit with a fresh heartbeat */
        struct i14_e4_watch w = { .active = 1, .limit_ns = 10 * SEC, .last_good_ns = 9900 * MS };
        assert(i14_e4_watch(&w, 9999 * MS) == I14_W_NONE && !w.req_timer);
        assert(i14_e4_watch(&w, 10 * SEC) == I14_W_FORCE_STOP && w.req_timer == I14_R_TIME);
    }
    {   /* stalled discovery after i14_e4_arm(): gates unknown, heartbeat stop, then PANIC */
        struct i14_e4_state x = { 0 };
        i14_e4_arm(&x, SEC);
        assert(x.w.may_be_on && i14_e4_watch(&x.w, 3499 * MS) == I14_W_NONE);
        assert(i14_e4_watch(&x.w, 3500 * MS) == I14_W_FORCE_STOP && x.w.req_timer == I14_R_HEARTBEAT);
        assert(i14_e4_watch(&x.w, 5499 * MS) == I14_W_FORCE_STOP && i14_e4_watch(&x.w, 5500 * MS) == I14_W_PANIC);
    }
    /* 5. CHR_EN=0 ignored (WDT variant B, so no hardware rescue): loop stop at 60 s, PANIC 2 s later */
    F.ignore_chr_off = 1;
    assert(!start(1, 14, 4 * SEC, 0, 0));
    s = (struct sim){ 0 };
    run(&s, F.now + 70 * SEC);
    rc = find(0, 0xf48, 0x18, 0);
    assert(rc >= 0 && s.panic_ns >= L[rc].t + 2 * SEC && s.panic_ns <= L[rc].t + 2300 * MS && S.w.may_be_on);
    /* stuck loop: FORCE_STOP after 2.5 s, then the stop is ignored: PANIC 2 s later */
    assert(!start(1, 14, 4 * SEC, 0, 0));
    s = (struct sim){ .stuck_from = F.now + 10 * SEC, .stuck_to = NEVER, .until_force = 1 };
    run(&s, F.now + 70 * SEC);
    assert(s.force_ns - s.good_at_force >= 2500 * MS && s.force_ns - s.good_at_force < 2750 * MS);
    assert(s.panic_ns >= s.force_ns + 2 * SEC && s.panic_ns <= s.force_ns + 2300 * MS);
    /* stuck forever: FORCE_STOP then PANIC without any step */
    F.ignore_chr_off = 0;
    assert(!start(1, 14, 4 * SEC, 1, 0));
    s = (struct sim){ .stuck_from = F.now + 10 * SEC, .stuck_to = NEVER };
    run(&s, F.now + 70 * SEC);
    assert(s.force_ns && s.panic_ns >= s.force_ns + 2 * SEC && s.panic_ns <= s.force_ns + 2300 * MS);
    /* 9. preflight: CHR_EN=1 -> S1 first, then refuse; other refusals write nothing */
    F.fail_at = 0;
    start(1, 14, 4 * SEC, 1, 0);
    F.reg[0xf48] = 0x7b; F.reg[0xf7a] = 0xd4; F.reg[0xf4c] = 0xae; NL = 0; t0 = F.now;
    F.det_rest = 0; /* no preflight baseline here: S1 needs all det bits 0 */
    S = (struct i14_e4_state){ .mode = 1, .cs = 14 }; i14_e4_arm(&S, F.now);
    assert(i14_e4_init_run(&IO, &S, &R) == -EPERM && S.reason == I14_R_PRE_ON && S.result == I14_RES_REFUSED);
    /* W20 is op 2: one read of 0x0f48, then S1, before any other I/O (review: stalled preflight) */
    assert(NL && L[0].a == 0xf48 && L[0].m == 0x18 && L[0].k == 'F' && L[0].t == t0 + 2 * 20000);
    assert(!S.w.may_be_on && !(F.reg[0xf48] & 0x18) && S.stop_verified);
    /* same with CC_DET 1 at rest [HW]: gates verified off (no panic), but never stop_verified */
    F.det_rest = 0x80; F.reg[0xf48] = 0x7b; F.reg[0xf7a] = 0xd4; F.reg[0xf4c] = 0xae;
    S = (struct i14_e4_state){ .mode = 1, .cs = 14 }; i14_e4_arm(&S, F.now);
    assert(i14_e4_init_run(&IO, &S, &R) == -EPERM && S.reason == I14_R_PRE_ON);
    assert(!S.w.may_be_on && !(F.reg[0xf48] & 0x18) && !S.stop_verified && !S.wdt_off);
    /* CS_DET set at preflight (gates off): refused, nothing written */
    start(1, 14, 4 * SEC, 1, 0);
    F.reg[0xf4c] = 0xa4; F.det_stuck = 1; NL = 0; /* det_stuck: the fake keeps the det bits as set */
    S = (struct i14_e4_state){ .mode = 1, .cs = 14 }; i14_e4_arm(&S, F.now);
    assert(i14_e4_init_run(&IO, &S, &R) == -EPERM && S.reason == I14_R_MISMATCH && !S.w.may_be_on);
    for (i = 0; i < NL; i++) assert(!chg(i));
    start(1, 14, 4 * SEC, 1, 0);
    F.fail_from = F.ops + 2; /* dead transport right after the 0x0f48 read: S1 never verifies, caller panics */
    F.reg[0xf48] = 0x7b; F.reg[0xf7a] = 0xd4;
    S = (struct i14_e4_state){ .mode = 1, .cs = 14 }; i14_e4_arm(&S, F.now);
    assert(i14_e4_init_run(&IO, &S, &R) == -EPERM && S.reason == I14_R_PRE_ON && S.w.may_be_on);
    F.fail_from = 0;
    for (i = 0; i < NL; i++) assert(!chg(i) || (L[i].a == 0xf48 && L[i].m == 0x18));
    {
        static const unsigned pre[][4] = { { 0x1000, 0, 0, I14_R_VBAT }, { 0x1002, 0, 1000, I14_R_VBUS },
            { 0x1003, 0, 0, I14_R_TEMP }, { 0xf48, 0x20, 0, I14_R_NO_CHRDET }, { 0xf68, 0x0c, 0x08, I14_R_MISMATCH },
            { 0xf9a, 1, 1, I14_R_BATON }, { 0xf54, 0x40, 0x40, I14_R_OV_DET }, { 0xf48, 0x80, 0x80, I14_R_HV_DET } };
        for (i = 0; i < sizeof(pre) / sizeof(pre[0]); i++) {
            start(1, 14, 4 * SEC, 1, 0);
            if (pre[i][0] >= 0x1000) F.adc[pre[i][0] - 0x1000] = pre[i][0] == 0x1000 ? raw15(3699) : pre[i][2];
            else F.reg[pre[i][0]] = (F.reg[pre[i][0]] & ~pre[i][1]) | pre[i][2];
            NL = 0; S = (struct i14_e4_state){ .mode = 1, .cs = 14 }; i14_e4_arm(&S, F.now);
            assert(i14_e4_init_run(&IO, &S, &R) == -EPERM && S.reason == pre[i][3]);
            for (j = 0; j < NL; j++) assert(!chg(j));
        }
    }
    /* failed first 0x0f48 read: gates unknown, W20 next and verified; dead transport: may_be_on stays 1 */
    F.fail_at = 1; t0 = SEC;
    /* no preflight baseline: with CC_DET 1 at rest [HW] the stop is never stop_verified (no S2) */
    assert(start(1, 14, 4 * SEC, 1, 0) == -EPERM && S.reason == I14_R_IO && !S.stop_verified && !S.w.may_be_on);
    assert(NL >= 1 && L[0].a == 0xf48 && L[0].m == 0x18 && L[0].k == 'F' && L[0].t == t0 + 2 * 20000);
    for (i = 0; i < NL; i++) assert(L[i].a == 0xf48 && L[i].m == 0x18 && L[i].v == 0); /* W20 retries only */
    F.fail_at = 0; F.fail_from = 1;
    assert(start(1, 14, 4 * SEC, 1, 0) == -EPERM && S.reason == I14_R_IO && S.w.may_be_on && NL == 0);
    F.fail_from = 0;
    /* reboot request at every now() through setup into CHARGING: either the step sees
     * it before W16, or the notifier sees may_be_on=1 (and panics) */
    for (k = 1, n = 0; ; k++) {
        assert(!start(1, 14, 4 * SEC, 1, 0));
        F.req_now = k;
        s = (struct sim){ .stop_at_done = 1 };
        run(&s, F.now + 10 * SEC);
        assert(!F.req_now && !s.panic_ns && S.phase == I14_P_DONE && !S.w.may_be_on && !(F.reg[0xf48] & 0x18));
        if (find(F.req_nl, 0xf48, 0x10, 0x10) >= 0) { assert(F.req_mbo); n++; }
        if (F.req_at_ph == I14_P_CHARGING) break;
    }
    printf("e4 policy: reboot request at now() 1..%u, %u after the last check (all saw may_be_on=1)\n", k, n);
    /* e4a: user stop during DEADMAN is honoured at once (no hrtimer panic) */
    assert(!start(0, 15, 4 * SEC, 1, 0));
    s = (struct sim){ .stop_at_done = 1 };
    run(&s, F.now + 100 * MS);
    assert(S.phase == I14_P_DEADMAN);
    s.ext = I14_R_USER; s.ext_at = S.t_kick + 1200 * MS;
    run(&s, F.now + 20 * SEC);
    rc = find(0, 0xf48, 0x18, 0);
    assert(!s.panic_ns && S.reason == I14_R_USER && S.result == I14_RES_ABORTED && rc >= 0);
    assert(L[rc].t >= s.ext_at && L[rc].t - s.ext_at <= 40 * MS);
    stop_order(1);
    /* 4. one failing op at every position (transient) and a dead transport from
     * every position: always a verified stop or PANIC, never silent with CHR_EN=1 */
    for (mode = 0; mode < 2; mode++) {
        unsigned lo2, hi2, early, runs = 0;
        F.fail_at = F.fail_from = 0;
        assert(!start(mode, mode ? 14 : 15, 4 * SEC, 1, 0));
        s = (struct sim){ .stop_at_done = 1 };
        run(&s, F.now + 70 * SEC);
        lo2 = s.stop_ops > 300 ? s.stop_ops - 300 : 1; hi2 = s.done_ops + 3; early = s.early_ops;
        for (persist = 0; persist < 2; persist++)
            for (k = 1; k <= hi2; k++) {
                if (mode && k > early && k < lo2) k = lo2; /* identical 1 s passes in between */
                F.fail_at = persist ? 0 : k; F.fail_from = persist ? k : 0;
                rc = start(mode, mode ? 14 : 15, 4 * SEC, 1, 0);
                runs++;
                if (rc) { /* only a failed 0x0f48 read (op 1) leads to W20; dead from op 1: caller panics */
                    for (i = 0; i < NL; i++) assert(!chg(i) || (k == 1 && L[i].a == 0xf48 && L[i].m == 0x18));
                    assert(S.w.may_be_on == (persist && k == 1) && !(F.reg[0xf48] & 0x18));
                    continue;
                }
                s = (struct sim){ .stop_at_done = 1 };
                run(&s, F.now + 75 * SEC);
                assert(!(S.phase == I14_P_DONE && (F.reg[0xf48] & 0x18)));
                if (s.panic_ns) assert(S.w.may_be_on && persist);
                else assert(!S.w.may_be_on && !(F.reg[0xf48] & 0x18));
                if (!persist) assert(S.phase == I14_P_DONE);
            }
        printf("e4 policy: %s fault sweep %u runs over ops 1..%u", mode ? "e4b" : "e4a", runs, hi2);
        printf(mode ? " (skipping identical passes %u..%u)\n" : "\n", early, lo2);
    }
    puts("e4 policy: order, allowlist, stop reasons+edges, shutdown order, fault sweep, stuck/panic, e4a, e4b, cfg, preflight PASS");
    return 0;
}
