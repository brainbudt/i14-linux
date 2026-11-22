/* SPDX-License-Identifier: GPL-2.0-only */
/* Phase 4 E3/E4 pure policy (docs/research/PHASE4-E3E4-SPEC-2026-11-02.md).
 * Shared by i14-e3-dump.c, i14-e4-charge.c and the host tests: no kernel
 * types, no allocation. The kernel glue only adds I/O, locks and timers.
 * Fail closed: any doubt ends in W20 (CHR_EN=0, CSDAC_EN=0) and verification. */
#ifndef I14_E4_POLICY_H
#define I14_E4_POLICY_H
#ifdef __KERNEL__
#include <linux/errno.h>
#include <linux/string.h>
#include <asm/barrier.h>
#define I14_MB() smp_mb()
#else
#include <errno.h>
#include <string.h>
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define I14_MB() __atomic_thread_fence(__ATOMIC_SEQ_CST)
#endif
#include "i14-battery-sample.h"
#include "i14-battery-temperature.h"
#include "i14-charge-session.h"

#define I14_MS 1000000ULL
#define I14_E4_NREG 45 /* 0x0f48..0x0fa0 */
#define I14_F(r, a) ((r)->reg[((a) - 0x0f48) / 2])
enum { I14_E4_PASS_MS = 1000, I14_E4_POLL_MS = 20, I14_E4_WINDOW_MS = 6000, I14_E4_CLEAR_MS = 500,
       I14_E4_TIMER_MS = 250, I14_E4_HARD_A_MS = 8000, I14_E4_HARD_B_MS = 61000,
       I14_E4_RUN_B_MS = 60000, I14_E4_HEART_MS = 2500, I14_E4_RETRY_MS = 100,
       I14_E4_PANIC_MS = 2000, I14_E4_DET_MS = 2000, I14_E4_TAIL = 30, I14_E4_FRESH_MS = 500,
       I14_E4_ADC_MISS = 3 };
enum { I14_P_PREFLIGHT, I14_P_SETUP, I14_P_ENABLE, I14_P_CHARGING, I14_P_DEADMAN,
       I14_P_STOPPING, I14_P_DONE };
enum { I14_R_NONE, I14_R_IO, I14_R_HV_DET, I14_R_BATON, I14_R_OV_DET, I14_R_NO_CHRDET,
       I14_R_VBAT, I14_R_TEMP, I14_R_VBUS, I14_R_TIME, I14_R_HEARTBEAT, I14_R_EXIT,
       I14_R_REBOOT, I14_R_USER, I14_R_MISMATCH, I14_R_WDT_OUT, I14_R_DEADMAN, I14_R_PRE_ON };
enum { I14_RES_RUNNING, I14_RES_REFUSED, I14_RES_ABORTED, I14_RES_COMPLETE,
       I14_RES_DM_PASS, I14_RES_DM_FAIL };
enum { I14_DM_OK, I14_DM_WINDOW, I14_DM_SLOW_CLEAR, I14_DM_CLEAR_WITHOUT_OUT };
enum { I14_W_NONE, I14_W_FORCE_STOP, I14_W_PANIC };
enum { I14_S0, I14_S_ARM, I14_KICK, I14_S_EN, I14_S1, I14_S2 };

struct i14_e4_op { unsigned short addr, mask, value, strobe; };
struct i14_e4_io {
    void *ctx;
    int (*read)(void *ctx, unsigned addr, unsigned *value);
    int (*update)(void *ctx, unsigned addr, unsigned mask, unsigned value, int force);
    int (*write)(void *ctx, unsigned addr, unsigned value); /* 0x0e8a requests only */
    unsigned long long (*now)(void *ctx);
    void (*pause)(void *ctx, unsigned us);
};
struct i14_e4_rec {
    unsigned long long t_ns;
    unsigned seq, phase, reason, kicked, full, nreg, log, err_addr, adc_ch;
    int err, adc_err, temp_ok, temp_mc, mv[4]; /* vbat, isense, vbus, ntc */
    unsigned short reg[I14_E4_NREG], fg[2], latch[4], raw[4];
};
/* Shared with the hrtimer. Single words; each has one writer except the
 * req_* flags, which only ever go 0 -> reason. seen* are timer-private. */
struct i14_e4_watch {
    unsigned active, may_be_on, finished, req_work, req_ext, req_timer, seen;
    unsigned long long last_good_ns, limit_ns, seen_ns;
};
struct i14_e4_state {
    unsigned mode, cs, td0_ms, run_s, phase, result, reason, dm_why, tail, reon;
    unsigned stop_verified, det_stuck, det_base, wdt_armed, wdt_off, out_seen, clear_seen;
    unsigned passes, kicks, io_errors, last_addr, adc_miss, adc_misses;
    int last_err;
    unsigned long long t_kick, t_enable, t_w16, t_last_full, t_prev_poll, t_gates_clear;
    unsigned long long t_out_lo, t_out_hi, t_clear_lo, t_clear_hi;
    struct i14_e4_watch w;
};

static inline const char *i14_e4_name(unsigned kind, unsigned v)
{
    static const char *const t[4][18] = {
        { "none", "io", "hv_det", "baton_undet", "ov_det", "no_chrdet", "vbat", "temp", "vbus",
          "time", "heartbeat", "exit", "reboot", "user", "mismatch", "wdt_out", "deadman", "preflight_on" },
        { "preflight", "setup", "enable", "charging", "deadman", "stopping", "done" },
        { "running", "refused", "aborted", "complete", "deadman_pass", "deadman_fail" },
        { "ok", "window", "slow_clear", "clear_without_out" } };
    return kind < 4 && v < 18 && t[kind][v] ? t[kind][v] : "?";
}

/* ---- allowlists (spec 2.2, 3.2) ---- */
static inline int i14_e3_range_ok(unsigned a) { return !(a & 1) && a <= 0x0ffe; }
static inline int i14_e34_adc_write_ok(unsigned a, unsigned m, unsigned v)
{
    if (a == 0x0e8a) return m == 0xffff && (v == 1 || v == 2 || v == 4 || v == 8);
    return ((a == 0x0e94 && m == 0x8000) || (a == 0x0eb0 && m == 0x0080) ||
            (a == 0x028a && m == 0x0100)) && (v == 0 || v == m);
}
static inline int i14_e4_read_ok(unsigned a)
{
    static const unsigned short x[] = { 0x0cb2, 0x0ccc, 0x0e2e, 0x0e32, 0x0e04, 0x0e06, 0x0e84,
                                        0x0e86, 0x0e88, 0x0e94, 0x0eb0, 0x028a, 0x0200 };
    unsigned i;
    if (a >= 0x0f48 && a <= 0x0fa0 && !(a & 1)) return 1;
    for (i = 0; i < sizeof(x) / sizeof(x[0]); i++)
        if (a == x[i]) return 1;
    return 0;
}
static inline int i14_e4_write_ok(unsigned a, unsigned m, unsigned v, unsigned cs)
{
    static const unsigned short t[][3] = { /* W1,W2,W4..W19 (both W12/W13 values), W20 */
        { 0xf4a, 0xf0, 0xa0 }, { 0xf4e, 0x1f, 13 }, { 0xf54, 0x0f, 5 }, { 0xf76, 7, 4 },
        { 0xf76, 0x70, 0x10 }, { 0xf74, 7, 1 }, { 0xf74, 0x70, 0x20 }, { 0xf4c, 8, 8 },
        { 0xf4c, 2, 2 }, { 0xf62, 0x0f, 0 }, { 0xf66, 1, 1 }, { 0xf66, 1, 0 },
        { 0xf62, 0x10, 0x10 }, { 0xf62, 0x10, 0 }, { 0xf62, 0x100, 0x100 }, { 0xf66, 2, 2 },
        { 0xf48, 0x10, 0x10 }, { 0xf48, 8, 8 }, { 0xf7a, 4, 4 }, { 0xf7a, 0x40, 0x40 },
        { 0xf48, 0x18, 0 } };
    unsigned i;
    if (a == 0xf50) return m == 0x0f && v == cs && cs >= 13 && cs <= 15; /* W3 */
    for (i = 0; i < sizeof(t) / sizeof(t[0]); i++)
        if (a == t[i][0] && m == t[i][1] && v == t[i][2]) return 1;
    return i14_e34_adc_write_ok(a, m, v);
}
/* 0 = e4a, 1 = e4b (60 s), 2 = e5 (run_s 300..1800, phase 4 E5); run_s only for e5. */
static inline int i14_e4_cfg_ok(const char *mode, unsigned cs, unsigned td0_ms, unsigned run_s)
{
    if (!mode || cs < 13 || cs > 15) return -EINVAL;
    if (!strcmp(mode, "e4a")) return cs == 15 && !td0_ms && !run_s ? 0 : -EINVAL;
    if (cs != 14 || td0_ms < 2000 || td0_ms > 6000) return -EINVAL;
    if (!strcmp(mode, "e4b")) return !run_s ? 1 : -EINVAL;
    if (!strcmp(mode, "e5")) return run_s >= 300 && run_s <= 1800 ? 2 : -EINVAL;
    return -EINVAL;
}
/* Charging time for e4b/e5. */
static inline unsigned long long i14_e4_run_ms(unsigned mode, unsigned run_s)
{ return mode == 2 ? run_s * 1000ULL : I14_E4_RUN_B_MS; }
/* Sequences (spec 3.3) into a caller buffer of >= 10 ops; W3 carries cs. */
static inline unsigned i14_e4_seq(unsigned id, unsigned cs, struct i14_e4_op *o)
{
    static const struct i14_e4_op s[] = {
        { 0xf4a, 0xf0, 0xa0, 0 }, { 0xf4e, 0x1f, 13, 0 }, { 0xf50, 0x0f, 0, 0 }, { 0xf54, 0x0f, 5, 0 },
        { 0xf76, 7, 4, 0 }, { 0xf76, 0x70, 0x10, 0 }, { 0xf74, 7, 1, 0 }, { 0xf74, 0x70, 0x20, 0 },
        { 0xf4c, 8, 8, 0 }, { 0xf4c, 2, 2, 0 },                                  /* S0: W1..W10 */
        { 0xf62, 0x0f, 0, 0 }, { 0xf66, 1, 1, 0 }, { 0xf62, 0x10, 0x10, 0 },     /* S_ARM */
        { 0xf62, 0x100, 0x100, 1 }, { 0xf66, 2, 2, 1 },                          /* KICK */
        { 0xf48, 0x10, 0x10, 0 }, { 0xf48, 8, 8, 0 }, { 0xf7a, 4, 4, 0 }, { 0xf7a, 0x40, 0x40, 0 },
        { 0xf48, 0x18, 0, 1 },                                                   /* S1: W20 */
        { 0xf62, 0x10, 0, 0 }, { 0xf66, 1, 0, 0 } };                             /* S2 */
    static const unsigned char first[] = { 0, 10, 13, 15, 19, 20, 22 };
    unsigned i;
    if (id > I14_S2) return 0;
    for (i = first[id]; i < first[id + 1]; i++) {
        o[i - first[id]] = s[i];
        if (s[i].addr == 0xf50) o[i - first[id]].value = (unsigned short)cs;
    }
    return first[id + 1] - first[id];
}

/* ---- E3 helpers ---- */
static inline unsigned i14_e3_line(char *b, unsigned addr, unsigned value) /* b >= 18 bytes */
{
    char d[10];
    unsigned n = 0, k = 0, i;
    b[n++] = '0'; b[n++] = 'x';
    for (i = 4; i-- > 0;) b[n++] = "0123456789abcdef"[(addr >> (4 * i)) & 15];
    b[n++] = ' ';
    do { d[k++] = (char)('0' + value % 10); value /= 10; } while (value);
    while (k) b[n++] = d[--k];
    b[n++] = '\n';
    return n;
}
/* id 0 'charger' 0x0f48..0x0fa0, id 1 'pmic' 0x0000..0x0ffe. All or nothing. */
static inline int i14_e3_dump(struct i14_e4_io *io, unsigned id, unsigned short *vals, unsigned *bad)
{
    unsigned a = id ? 0 : 0x0f48, last = id ? 0x0ffe : 0x0fa0, n = 0, v = 0;
    int ret;
    if (id > 1) return -EINVAL;
    for (; a <= last; a += 2) {
        ret = i14_e3_range_ok(a) ? io->read(io->ctx, a, &v) : -EPERM;
        if (!ret && v > 0xffff) ret = -EIO;
        if (ret) { *bad = a; return ret; }
        vals[n++] = (unsigned short)v;
    }
    return (int)n;
}

/* ---- AUXADC (spec 1.2): unchanged CH0/2/3 path plus the CH1 io-shim ---- */
static inline int i14_e4_decode(unsigned ch, unsigned latch, int *mv)
{
    unsigned raw = latch & (ch < 2 ? 0x7fff : 0x0fff);
    if (ch > 3) return -ERANGE;
    if (ch == 2) return i14_session_vbus(raw, mv) ? -ERANGE : (int)raw;
    *mv = (int)(raw * (ch < 2 ? 5400U : 1800U) / (ch < 2 ? 32768U : 4096U));
    return (int)raw;
}
static inline int i14_e4_a_rd(void *c, unsigned a, unsigned *v)
{ struct i14_e4_io *io = c; return io->read(io->ctx, a, v); }
static inline int i14_e4_a_up(void *c, unsigned a, unsigned m, unsigned v)
{ struct i14_e4_io *io = c; return io->update(io->ctx, a, m, v, 0); }
static inline int i14_e4_a_wr(void *c, unsigned a, unsigned v)
{ struct i14_e4_io *io = c; return io->write(io->ctx, a, v); }
static inline unsigned long long i14_e4_a_now(void *c) { struct i14_e4_io *io = c; return io->now(io->ctx); }
static inline void i14_e4_a_pause(void *c) { struct i14_e4_io *io = c; io->pause(io->ctx, 50); }
/* Exactly three translations; everything else passes through unchanged. */
static inline int i14_ch1_rd(void *c, unsigned a, unsigned *v)
{
    struct i14_adc_io *in = c;
    int ret = in->read(in->context, a == 0x0e2e ? 0x0e32 : a, v);
    if (!ret && a == 0x0e84) *v |= (*v >> 1) & 1; /* CH1 busy counts as busy */
    return ret;
}
static inline int i14_ch1_up(void *c, unsigned a, unsigned m, unsigned v)
{ struct i14_adc_io *in = c; return in->update(in->context, a, m, v); }
static inline int i14_ch1_wr(void *c, unsigned a, unsigned v)
{ struct i14_adc_io *in = c; return a == 0x0e8a && v == 1 ? in->write(in->context, a, 2) : -EPERM; }
static inline unsigned long long i14_ch1_now(void *c) { struct i14_adc_io *in = c; return in->now_ns(in->context); }
static inline void i14_ch1_pause(void *c) { struct i14_adc_io *in = c; in->pause(in->context); }
static inline int i14_ch1_sample(struct i14_adc_io *inner, struct i14_adc_result *res)
{
    struct i14_adc_io shim = { inner, i14_ch1_rd, i14_ch1_up, i14_ch1_wr, i14_ch1_now, i14_ch1_pause };
    return i14_adc_sample(&shim, "sample", res);
}
/* CH0..CH3 one after another; stops at the first failure (no extra cleanup). */
static inline int i14_e4_adc(struct i14_e4_io *io, struct i14_e4_rec *r)
{
    struct i14_adc_io a = { io, i14_e4_a_rd, i14_e4_a_up, i14_e4_a_wr, i14_e4_a_now, i14_e4_a_pause };
    struct i14_adc_result res;
    unsigned ch;
    int ret, raw;
    for (ch = 0; ch < 4; ch++) {
        ret = ch == 0 ? i14_adc_sample(&a, "sample", &res) : ch == 1 ? i14_ch1_sample(&a, &res) :
              i14_adc_sample_channel(&a, ch, "sample", &res);
        if (!ret && (!res.complete || res.restored != 7 || res.uncertain ||
                     !res.request_attempted || !res.saw_zero)) ret = -EPROTO;
        raw = ret ? 0 : i14_e4_decode(ch, res.latch, &r->mv[ch]);
        if (!ret && raw < 0) ret = raw;
        if (ret) { r->adc_err = ret; r->adc_ch = ch; return ret; }
        r->latch[ch] = (unsigned short)res.latch;
        r->raw[ch] = (unsigned short)raw;
    }
    r->adc_ch = 4;
    r->temp_ok = !i14_battery_temperature_mc(r->mv[3], &r->temp_mc);
    return 0;
}

/* ---- E4 I/O helpers ---- */
static inline int i14_e4_fail(struct i14_e4_state *st, struct i14_e4_rec *r, int ret, unsigned a)
{
    st->io_errors++; st->last_err = ret; st->last_addr = a;
    r->err = ret; r->err_addr = a;
    return ret;
}
static inline int i14_e4_rd(struct i14_e4_io *io, struct i14_e4_state *st, struct i14_e4_rec *r,
                            unsigned a, unsigned *v)
{
    int ret = io->read(io->ctx, a, v);
    if (!ret && *v > 0xffff) ret = -EIO;
    if (ret) return i14_e4_fail(st, r, ret, a);
    if (a >= 0x0f48 && a <= 0x0fa0) I14_F(r, a) = (unsigned short)*v;
    return 0;
}
/* update (+ readback unless strobe); -EPERM outside the table, -EPROTO mismatch */
static inline int i14_e4_run(struct i14_e4_io *io, struct i14_e4_state *st, struct i14_e4_rec *r,
                             const struct i14_e4_op *op, unsigned n)
{
    unsigned i, v;
    int ret;
    for (i = 0; i < n; i++) {
        ret = i14_e4_write_ok(op[i].addr, op[i].mask, op[i].value, st->cs) ?
              io->update(io->ctx, op[i].addr, op[i].mask, op[i].value, op[i].strobe) : -EPERM;
        if (ret) return i14_e4_fail(st, r, ret, op[i].addr);
        if (op[i].strobe) continue;
        ret = i14_e4_rd(io, st, r, op[i].addr, &v);
        if (ret) return ret;
        if ((v & op[i].mask) != op[i].value) return i14_e4_fail(st, r, -EPROTO, op[i].addr);
    }
    return 0;
}
static inline unsigned i14_e4_why(int ret)
{ return ret == -EPROTO || ret == -ETIMEDOUT ? I14_R_MISMATCH : I14_R_IO; }
/* Full pass: 45 charger registers, FG 0x0cb2/0x0ccc, CH0..CH3. */
static inline int i14_e4_pass(struct i14_e4_io *io, struct i14_e4_state *st, struct i14_e4_rec *r)
{
    unsigned i, v;
    int ret = 0;
    r->t_ns = io->now(io->ctx);
    for (i = 0; i < I14_E4_NREG && !ret; i++) {
        ret = i14_e4_rd(io, st, r, 0x0f48 + 2 * i, &v);
        r->nreg = ret ? i : i + 1;
    }
    for (i = 0; i < 2 && !ret; i++) {
        ret = i14_e4_rd(io, st, r, i ? 0x0ccc : 0x0cb2, &v);
        r->fg[i] = (unsigned short)v;
    }
    if (!ret && (ret = i14_e4_adc(io, r))) i14_e4_fail(st, r, ret, 0x0e8a);
    if (!ret) { r->full = 1; st->passes++; }
    return ret;
}

/* ---- pure decisions ---- */
static inline unsigned i14_e4_preflight(const struct i14_e4_rec *r)
{
    unsigned f48 = I14_F(r, 0x0f48);
    if (!r->nreg) return I14_R_IO;
    if (f48 & 0x18) return I14_R_PRE_ON;
    if (r->err) return I14_R_IO;
    if (!(f48 & 0x20)) return I14_R_NO_CHRDET;
    if (f48 & 0x80) return I14_R_HV_DET;
    if (I14_F(r, 0x0f54) & 0x40) return I14_R_OV_DET;
    if (I14_F(r, 0x0f9a) & 1) return I14_R_BATON;
    if (!(f48 & 1) || (I14_F(r, 0x0f4c) & 0x20) || (I14_F(r, 0x0f56) & 5) != 5 || (I14_F(r, 0x0f68) & 0x0c) != 4 ||
        (I14_F(r, 0x0f70) & 3) != 3) return I14_R_MISMATCH;
    if (!r->full) return I14_R_IO;
    if (r->mv[0] < 3700 || r->mv[0] > 4100) return I14_R_VBAT;
    if (i14_battery_temperature_probe_guard(r->temp_ok ? 0 : -1, r->temp_mc) != 1) return I14_R_TEMP;
    if (r->mv[2] < 4500 || r->mv[2] > 5500) return I14_R_VBUS;
    return I14_R_NONE;
}
/* First hit of spec 3.5 A (always) and B (ENABLE/CHARGING/DEADMAN, full passes). */
static inline unsigned i14_e4_check(const struct i14_e4_state *st, const struct i14_e4_rec *r)
{
    unsigned f48 = I14_F(r, 0x0f48), f66 = I14_F(r, 0x0f66), ph = st->phase;
    int dm = ph == I14_P_DEADMAN;
    if (r->err) return I14_R_IO;
    if (f48 & 0x80) return I14_R_HV_DET;
    if (I14_F(r, 0x0f9a) & 1) return I14_R_BATON;
    if (I14_F(r, 0x0f54) & 0x40) return I14_R_OV_DET;
    if (!(f48 & 0x20)) return I14_R_NO_CHRDET;
    if (!r->full) return I14_R_NONE;
    if (r->mv[0] > 4250 || r->mv[0] < 3600) return I14_R_VBAT;
    if (i14_battery_temperature_probe_guard(r->temp_ok ? 0 : -1, r->temp_mc) != 1) return I14_R_TEMP;
    if (r->mv[2] > 5500 || r->mv[2] < 4500) return I14_R_VBUS;
    /* After a dead-man event the WDT owns the gates; only A stays (spec 3.7). */
    if ((ph != I14_P_ENABLE && ph != I14_P_CHARGING && !dm) || (dm && (st->out_seen || st->clear_seen)))
        return I14_R_NONE;
    if (ph == I14_P_CHARGING && (f66 & 4)) return I14_R_WDT_OUT;
    if ((!dm && (f48 & 0x18) != 0x18) || !(f48 & 1) || (I14_F(r, 0x0f7a) & 0x44) != 0x44 ||
        (I14_F(r, 0x0f4a) & 0xf0) != 0xa0 || (I14_F(r, 0x0f4e) & 0x1f) != 13 ||
        (I14_F(r, 0x0f50) & 0x0f) != st->cs || (I14_F(r, 0x0f54) & 0x0f) != 5 ||
        (I14_F(r, 0x0f4c) & 0x0a) != 0x0a || (I14_F(r, 0x0f56) & 5) != 5 ||
        (I14_F(r, 0x0f62) & 0x1f) != 0x10 || (f66 & (dm ? 1 : 5)) != 1)
        return I14_R_MISMATCH;
    return I14_R_NONE;
}
/* E4a: first OUT=1 and first gates==0, each as (start of the previous poll's
 * reads, end of this poll's reads]. Acceptance uses the conservative ends
 * (hi <= window, clear_hi - out_lo <= 500 ms). Returns 1 once decided;
 * dm_why stays I14_DM_OK only for a PASS. */
static inline unsigned i14_e4_deadman(struct i14_e4_state *st, unsigned f48, unsigned f66,
                                      unsigned long long t0, unsigned long long t1)
{
    unsigned long long lo = st->t_prev_poll, end = st->t_kick + I14_E4_WINDOW_MS * I14_MS;
    st->t_prev_poll = t0;
    if (!st->out_seen && (f66 & 4)) { st->out_seen = 1; st->t_out_lo = lo; st->t_out_hi = t1; }
    if (!st->clear_seen && !(f48 & 0x18)) {
        st->clear_seen = 1; st->t_clear_lo = lo; st->t_clear_hi = t1;
        st->dm_why = !st->out_seen ? I14_DM_CLEAR_WITHOUT_OUT : t1 > end ? I14_DM_WINDOW :
                     t1 - st->t_out_lo > I14_E4_CLEAR_MS * I14_MS ? I14_DM_SLOW_CLEAR : I14_DM_OK;
        return 1;
    }
    if (st->out_seen && t1 - st->t_out_lo > I14_E4_CLEAR_MS * I14_MS) st->dm_why = I14_DM_SLOW_CLEAR;
    else if (t1 > end) st->dm_why = I14_DM_WINDOW;
    else return 0;
    return 1;
}
/* hrtimer backstop, independent of the loop. */
static inline unsigned i14_e4_watch(struct i14_e4_watch *w, unsigned long long now)
{
    unsigned long long limit = READ_ONCE(w->limit_ns), good = READ_ONCE(w->last_good_ns);
    unsigned on = READ_ONCE(w->may_be_on), fresh = 0;
    if (!READ_ONCE(w->req_work) && !READ_ONCE(w->req_ext) && !READ_ONCE(w->req_timer)) {
        if (!READ_ONCE(w->active)) return I14_W_NONE;
        if (limit && now >= limit) WRITE_ONCE(w->req_timer, I14_R_TIME);
        else if (now > good && now - good >= I14_E4_HEART_MS * I14_MS) WRITE_ONCE(w->req_timer, I14_R_HEARTBEAT);
        else return I14_W_NONE;
        fresh = 1;
    }
    if (!on) w->seen = 0; /* the 2 s clock starts when "request and on" first holds */
    else if (!w->seen) { w->seen = 1; w->seen_ns = now; }
    if (on && now - w->seen_ns >= I14_E4_PANIC_MS * I14_MS) return I14_W_PANIC;
    return on || fresh ? I14_W_FORCE_STOP : I14_W_NONE;
}

/* ---- sequences with I/O ---- */
static inline int i14_e4_kick(struct i14_e4_io *io, struct i14_e4_state *st, struct i14_e4_rec *r,
                              unsigned max_ms)
{
    struct i14_e4_op op[2];
    unsigned long long t0 = io->now(io->ctx);
    unsigned f62, f66;
    int ret = i14_e4_run(io, st, r, op, i14_e4_seq(I14_KICK, st->cs, op));
    while (!ret) {
        ret = i14_e4_rd(io, st, r, 0x0f62, &f62);
        if (!ret) ret = i14_e4_rd(io, st, r, 0x0f66, &f66);
        if (ret) break;
        if ((f62 & 0x1f) == 0x10 && (f66 & 5) == 1) {
            st->t_kick = t0; st->kicks++; r->kicked = 1;
            return 0;
        }
        if (io->now(io->ctx) - t0 >= max_ms * I14_MS) ret = i14_e4_fail(st, r, -ETIMEDOUT, 0x0f66);
        else io->pause(io->ctx, 1000);
    }
    return ret;
}
/* One S1 attempt (W20 force, verify); S2 only after full verification.
 * 0 = final (verified or det_stuck), else retry later. */
static inline int i14_e4_stop(struct i14_e4_io *io, struct i14_e4_state *st, struct i14_e4_rec *r)
{
    struct i14_e4_op op[2];
    unsigned long long now;
    unsigned f48, f4c;
    int ret = i14_e4_run(io, st, r, op, i14_e4_seq(I14_S1, st->cs, op));
    if (!ret) ret = i14_e4_rd(io, st, r, 0x0f48, &f48);
    if (ret) return ret;
    if ((f48 & 0x18) || !(f48 & 1)) { WRITE_ONCE(st->w.may_be_on, 1); st->t_gates_clear = 0; return -EAGAIN; }
    WRITE_ONCE(st->w.may_be_on, 0);
    now = io->now(io->ctx);
    if (!st->t_gates_clear) st->t_gates_clear = now;
    ret = i14_e4_rd(io, st, r, 0x0f4c, &f4c);
    if (ret) return ret;
    /* [HW, E3 + E4a 2026-11-03] CC_DET (b7) reads 1 at rest with VBUS: the det bits must
     * return to their preflight value (CS_DET 0 there), not to 0. */
    if ((f4c & 0xe0) != st->det_base) {
        if (now - st->t_gates_clear < I14_E4_DET_MS * I14_MS) return -EAGAIN;
        st->det_stuck = 1; /* WDT stays armed and unkicked: no S2 */
        return 0;
    }
    st->stop_verified = 1;
    if (st->wdt_armed && !i14_e4_run(io, st, r, op, i14_e4_seq(I14_S2, st->cs, op))) st->wdt_off = 1;
    return 0;
}
/* A pending external/timer stop request (user, reboot, exit, time, heartbeat). */
static inline unsigned i14_e4_req(const struct i14_e4_state *st)
{
    unsigned x = READ_ONCE(st->w.req_ext);
    return x ? x : READ_ONCE(st->w.req_timer);
}
static inline unsigned i14_e4_setup(struct i14_e4_io *io, struct i14_e4_state *st, struct i14_e4_rec *r)
{
    struct i14_e4_op op[10];
    unsigned hit;
    int ret = i14_e4_run(io, st, r, op, i14_e4_seq(I14_S0, st->cs, op));
    if (!ret) { st->wdt_armed = 1; ret = i14_e4_run(io, st, r, op, i14_e4_seq(I14_S_ARM, st->cs, op)); }
    if (!ret) ret = i14_e4_kick(io, st, r, 100); /* LK leaves OUT=1 */
    if (ret) return i14_e4_why(ret);
    if (!st->mode) WRITE_ONCE(st->w.limit_ns, st->t_kick + I14_E4_HARD_A_MS * I14_MS);
    st->phase = I14_P_ENABLE;
    /* Publish the enable intent, then the last request check (Dekker with
     * e4_reboot: request; mb; read may_be_on). A request before this check
     * means no W16; one after it finds may_be_on=1 (reboot panics). */
    WRITE_ONCE(st->w.may_be_on, 1);
    I14_MB();
    hit = i14_e4_req(st);
    if (hit) return hit;
    st->t_w16 = io->now(io->ctx);
    if (st->mode) /* hard limit: run time + 1 s (e4b: 61 s) */
        WRITE_ONCE(st->w.limit_ns, st->t_w16 + (i14_e4_run_ms(st->mode, st->run_s) + I14_E4_HARD_B_MS -
                                                 I14_E4_RUN_B_MS) * I14_MS);
    ret = i14_e4_run(io, st, r, op, i14_e4_seq(I14_S_EN, st->cs, op)); /* CHR_EN first (E1) */
    if (ret) return i14_e4_why(ret);
    st->t_enable = io->now(io->ctx);
    if (i14_e4_pass(io, st, r)) return I14_R_IO;
    hit = i14_e4_check(st, r); /* the confirmation: all four gates 1 */
    if (hit) return hit;
    st->t_prev_poll = st->t_last_full = r->t_ns;
    WRITE_ONCE(st->w.last_good_ns, io->now(io->ctx));
    return I14_R_NONE;
}
static inline unsigned i14_e4_chg(struct i14_e4_io *io, struct i14_e4_state *st, struct i14_e4_rec *r)
{
    unsigned hit;
    int ret;
    if (i14_e4_pass(io, st, r)) {
        /* [HW, E3 5/127 and E4b 2026-11-03] an AUXADC sample now and then misses its 100 ms
         * deadline. Registers and FG were read; only the ADC part is missing. Such a pass gets
         * no kick (td0 and the heartbeat bound it) and the next pass retries; a second miss in
         * a row, or more than I14_E4_ADC_MISS per session, stops as before. */
        ret = r->err;
        if (ret != -ETIMEDOUT || r->adc_err != ret || r->nreg != I14_E4_NREG || st->adc_miss ||
            st->adc_misses >= I14_E4_ADC_MISS + (st->mode == 2 ? st->passes / 16 : 0)) return I14_R_IO;
        st->adc_miss = 1; st->adc_misses++;
        r->err = 0;
        hit = i14_e4_check(st, r); /* not full: the register checks only */
        r->err = ret;
        return hit ? hit : i14_e4_req(st);
    }
    st->adc_miss = 0;
    hit = i14_e4_check(st, r);
    if (!hit) hit = i14_e4_req(st); /* a request that arrived during the pass: no kick */
    if (hit) return hit;
    if (io->now(io->ctx) - r->t_ns > I14_E4_FRESH_MS * I14_MS) return I14_R_HEARTBEAT; /* stale pass */
    ret = i14_e4_kick(io, st, r, 10); /* only after a fully good, fresh pass */
    if (ret) return i14_e4_why(ret);
    WRITE_ONCE(st->w.last_good_ns, io->now(io->ctx));
    return I14_R_NONE;
}
static inline unsigned i14_e4_dm(struct i14_e4_io *io, struct i14_e4_state *st, struct i14_e4_rec *r)
{
    static const unsigned short poll[] = { 0x0f48, 0x0f54, 0x0f66, 0x0f9a };
    unsigned long long t = io->now(io->ctx);
    unsigned i, v, hit, decided, events = st->out_seen + st->clear_seen;
    if (t - st->t_last_full >= I14_E4_PASS_MS * I14_MS) {
        if (i14_e4_pass(io, st, r)) return I14_R_IO;
        st->t_last_full = t;
    } else {
        for (i = 0; i < 4; i++)
            if (i14_e4_rd(io, st, r, poll[i], &v)) return I14_R_IO;
    }
    decided = i14_e4_deadman(st, I14_F(r, 0x0f48), I14_F(r, 0x0f66), t, io->now(io->ctx));
    r->log = r->full || decided || st->out_seen + st->clear_seen != events;
    hit = i14_e4_check(st, r);
    if (hit) return hit;
    if (decided) return I14_R_DEADMAN;
    WRITE_ONCE(st->w.last_good_ns, io->now(io->ctx));
    return I14_R_NONE;
}
static inline void i14_e4_result(struct i14_e4_state *st)
{
    if (st->reason == I14_R_DEADMAN) st->result = st->dm_why ? I14_RES_DM_FAIL : I14_RES_DM_PASS;
    else if (st->mode && st->reason == I14_R_TIME && READ_ONCE(st->w.req_work) == I14_R_TIME)
        st->result = I14_RES_COMPLETE;
    else st->result = I14_RES_ABORTED;
}
/* One loop/stop-work invocation under the transaction lock; returns the
 * delay in ms until the next one, or -1 when the session is silent for good. */
static inline int i14_e4_step(struct i14_e4_io *io, struct i14_e4_state *st, struct i14_e4_rec *r)
{
    unsigned long long now = io->now(io->ctx);
    unsigned hit = I14_R_NONE, ext = i14_e4_req(st);
    int next = I14_E4_PASS_MS;
    memset(r, 0, sizeof(*r));
    r->t_ns = now; r->log = 1;
    if (st->phase == I14_P_DONE) {
        if (st->tail >= I14_E4_TAIL && !st->reon) { WRITE_ONCE(st->w.finished, 1); r->log = 0; return -1; }
        st->tail++;
        i14_e4_pass(io, st, r);
        if (r->nreg && (I14_F(r, 0x0f48) & 0x18)) { /* re-enabled behind our back: 1 Hz tail until unload */
            st->reon = 1;
            WRITE_ONCE(st->w.may_be_on, 1);
            st->stop_verified = 0;
            st->phase = I14_P_STOPPING;
        }
    } else if (ext && st->phase < I14_P_STOPPING) {
        if (!st->reason) st->reason = ext;
        st->phase = I14_P_STOPPING;
    } else if (st->phase == I14_P_SETUP) {
        hit = i14_e4_setup(io, st, r);
        if (!hit) st->phase = st->mode ? I14_P_CHARGING : I14_P_DEADMAN;
        next = st->mode ? I14_E4_PASS_MS : I14_E4_POLL_MS;
    } else if (st->phase == I14_P_CHARGING) {
        hit = now - st->t_w16 >= i14_e4_run_ms(st->mode, st->run_s) * I14_MS ? I14_R_TIME : i14_e4_chg(io, st, r);
    } else if (st->phase == I14_P_DEADMAN) {
        hit = i14_e4_dm(io, st, r);
        next = I14_E4_POLL_MS;
    }
    if (hit) {
        if (!st->reason) st->reason = hit;
        if (!READ_ONCE(st->w.req_work) && !i14_e4_req(st)) WRITE_ONCE(st->w.req_work, hit);
        st->phase = I14_P_STOPPING;
    }
    if (st->phase == I14_P_STOPPING) {
        next = I14_E4_RETRY_MS;
        if (!i14_e4_stop(io, st, r)) { st->phase = I14_P_DONE; i14_e4_result(st); next = I14_E4_PASS_MS; }
    }
    r->phase = st->phase; r->reason = st->reason;
    return next;
}
/* Before the hrtimer starts and before any PMIC read (including the provider
 * check's HWCID read): the gates are unknown, so may_be_on=1 and the heartbeat
 * runs. A stalled discovery ends in FORCE_STOP at 2.5 s and PANIC 2 s later. */
static inline void i14_e4_arm(struct i14_e4_state *st, unsigned long long now)
{
    WRITE_ONCE(st->w.may_be_on, 1);
    WRITE_ONCE(st->w.last_good_ns, now);
    WRITE_ONCE(st->w.active, 1);
}
/* insmod-time preflight after i14_e4_arm(): reads + AUXADC only. 0 = go.
 * 0x0f48 is read first; may_be_on drops only when it reads CHR_EN=CSDAC=0.
 * On CHR_EN/CSDAC=1 or a failed read (unknown), S1 runs before any other I/O
 * (<= 2 s); the caller must panic if w.may_be_on is still 1. */
static inline int i14_e4_init_run(struct i14_e4_io *io, struct i14_e4_state *st, struct i14_e4_rec *r)
{
    unsigned long long t0 = io->now(io->ctx);
    unsigned f48 = 0;
    memset(r, 0, sizeof(*r));
    r->log = 1;
    if (i14_e4_rd(io, st, r, 0x0f48, &f48)) st->reason = I14_R_IO;
    else if (f48 & 0x18) st->reason = I14_R_PRE_ON;
    else {
        WRITE_ONCE(st->w.may_be_on, 0); /* read back off; nothing below writes the gates */
        i14_e4_pass(io, st, r);
        st->reason = i14_e4_preflight(r);
        st->det_base = I14_F(r, 0x0f4c) & 0xe0; /* used by S1 only after a passed preflight */
    }
    if (!st->reason) {
        st->phase = I14_P_SETUP;
        WRITE_ONCE(st->w.last_good_ns, io->now(io->ctx));
        WRITE_ONCE(st->w.active, 1);
        return 0;
    }
    st->result = I14_RES_REFUSED;
    if (READ_ONCE(st->w.may_be_on)) { /* on, or unknown */
        WRITE_ONCE(st->w.req_work, st->reason);
        while (i14_e4_stop(io, st, r) && io->now(io->ctx) - t0 < I14_E4_PANIC_MS * I14_MS)
            io->pause(io->ctx, I14_E4_RETRY_MS * 1000);
    }
    st->phase = I14_P_DONE;
    r->phase = st->phase; r->reason = st->reason;
    return -EPERM;
}
#endif
