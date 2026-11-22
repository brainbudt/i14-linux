// SPDX-License-Identifier: GPL-2.0-only
/* Phase 4 E4 (spec PHASE4-E3E4-SPEC-2026-11-02): one bounded charge session per
 * load. mode=e4a cs=15: dead-man proof; mode=e4b cs=14 td0_ms=<E4a PASS>: 60 s.
 * Every decision lives in i14-e4-policy.h; this file only does I/O, locks and
 * timers. Every exit path ends in W20 (CHR_EN=0 + CSDAC_EN=0) with readback.
 * An hrtimer panics (-> reset -> LK sets CHR_EN=0) if CHR_EN is not read back
 * as 0 two seconds after any stop request. */
#include <linux/atomic.h>
#include <linux/crash_core.h>
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/hrtimer.h>
#include <linux/ktime.h>
#include <linux/mfd/mt6328/registers.h>
#include <linux/mfd/mt6397/core.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/panic.h>
#include <linux/platform_device.h>
#include <linux/reboot.h>
#include <linux/regmap.h>
#include <linux/seq_file.h>
#include <linux/spinlock.h>
#include <linux/suspend.h>
#include <linux/workqueue.h>
#include "i14-e4-policy.h"

static char *mode;
module_param(mode, charp, 0444);
MODULE_PARM_DESC(mode, "e4a, e4b or e5 (required)");
static unsigned int cs = 15;
module_param(cs, uint, 0444);
MODULE_PARM_DESC(cs, "CS_VTH code: e4a 15, e4b 14 (13 needs a later build, spec 3.8)");
static unsigned int td0_ms;
module_param(td0_ms, uint, 0444);
MODULE_PARM_DESC(td0_ms, "e4b/e5 only: td0_out_ms of an E4a PASS, 2000..6000");
static unsigned int run_s;
module_param(run_s, uint, 0444);
MODULE_PARM_DESC(run_s, "e5 only: charging time in s, 300..1800");

#define E4_RING 256
static DEFINE_MUTEX(e4_lock); /* one PMIC transaction at a time */
static DEFINE_SPINLOCK(e4_ring_lock);
static struct platform_device *pmic_device;
static struct i14_e4_state st;
static struct i14_e4_rec e4_ring[E4_RING], e4_cur;
static unsigned int e4_count, e4_down;
static struct dentry *e4_dir;
static struct hrtimer e4_timer;
static void e4_run(struct work_struct *work);
static DECLARE_DELAYED_WORK(e4_loop, e4_run);
static DECLARE_WORK(e4_stop_work, e4_run);

/* Exact provider checks shared with the existing RTC diagnostic. Caller holds
 * the bound MFD device lock; never manufacture an MMIO regmap. */
static struct regmap *checked_regmap(void)
{
    struct device *dev = &pmic_device->dev;
    struct mt6397_chip *chip;
    unsigned int id;

    if (!of_machine_is_compatible("magcomm,magc6735-65u-1-m0") ||
        !dev->driver || strcmp(dev->driver->name, "mt6397") ||
        !dev->parent || !dev->parent->driver ||
        strcmp(dev->parent->driver->name, "mt-pmic-pwrap") ||
        !of_device_is_compatible(dev->of_node, "mediatek,mt6328") ||
        !of_device_is_compatible(dev->parent->of_node, "mediatek,mt6735-pwrap"))
        return ERR_PTR(-ENODEV);
    chip = dev_get_drvdata(dev);
    if (!chip || chip->dev != dev || chip->chip_id != MT6328_CHIP_ID ||
        !chip->regmap || chip->regmap != dev_get_regmap(dev->parent, NULL))
        return ERR_PTR(-ENODEV);
    if (regmap_read(chip->regmap, MT6328_HWCID, &id) ||
        (id & 0xff) != MT6328_CHIP_ID)
        return ERR_PTR(-ENODEV);
    return chip->regmap;
}

/* The only I/O paths: fixed allowlists, then the verified regmap. */
static int e4_read(void *map, unsigned addr, unsigned *value)
{
    int ret;
    if (!map) return -ENODEV;
    if (!i14_e4_read_ok(addr)) return -EPERM;
    ret = regmap_read(map, addr, value);
    return ret ? ret : *value > 0xffff ? -EIO : 0;
}
static int e4_update(void *map, unsigned addr, unsigned mask, unsigned value, int force)
{
    if (!map) return -ENODEV;
    if (!i14_e4_write_ok(addr, mask, value, st.cs)) return -EPERM;
    return force ? regmap_write_bits(map, addr, mask, value) : regmap_update_bits(map, addr, mask, value);
}
static int e4_write(void *map, unsigned addr, unsigned value)
{
    if (!map) return -ENODEV;
    if (!i14_e4_write_ok(addr, 0xffff, value, st.cs)) return -EPERM;
    return regmap_write(map, addr, value);
}
static unsigned long long e4_now(void *map) { return ktime_get_ns(); }
static void e4_pause(void *map, unsigned us)
{
    if (us >= 10000) msleep(us / 1000);
    else usleep_range(us, 2 * us);
}

static void e4_log(struct i14_e4_rec *r)
{
    char temp[16] = "-";
    if (!r->log) return;
    spin_lock(&e4_ring_lock);
    r->seq = e4_count;
    e4_ring[e4_count++ % E4_RING] = *r;
    spin_unlock(&e4_ring_lock);
    if (r->full && r->temp_ok) snprintf(temp, sizeof(temp), "%d", r->temp_mc);
    pr_info("I14E4 %u %s %04x %04x %04x %04x %04x %04x %d %d %d %s %u %s err=%d@%04x\n", r->seq,
            i14_e4_name(1, r->phase), I14_F(r, 0x0f48), I14_F(r, 0x0f4c), I14_F(r, 0x0f50),
            I14_F(r, 0x0f62), I14_F(r, 0x0f66), I14_F(r, 0x0f7a), r->mv[0], r->mv[1], r->mv[2],
            temp, r->kicked, i14_e4_name(0, r->reason), r->err, r->err_addr);
}
/* One transaction; caller holds e4_lock. A failed provider check leaves
 * io.ctx NULL: every I/O fails, the stop retries and the hrtimer decides. */
static int e4_once(void)
{
    struct i14_e4_io io = { NULL, e4_read, e4_update, e4_write, e4_now, e4_pause };
    struct regmap *map;
    int next;
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    if (!IS_ERR(map)) io.ctx = map;
    next = i14_e4_step(&io, &st, &e4_cur);
    device_unlock(&pmic_device->dev);
    e4_log(&e4_cur);
    return next;
}
static void e4_run(struct work_struct *work) /* loop work and stop work */
{
    int next;
    mutex_lock(&e4_lock);
    if (!e4_down) {
        next = e4_once();
        if (next >= 0) mod_delayed_work(system_highpri_wq, &e4_loop, msecs_to_jiffies(next));
    }
    mutex_unlock(&e4_lock);
}
static void e4_sync_stop(unsigned int limit_ms) /* caller holds e4_lock */
{
    unsigned long long t0 = ktime_get_ns();
    while (st.phase < I14_P_DONE) {
        e4_once();
        if (st.phase >= I14_P_DONE || ktime_get_ns() - t0 >= limit_ms * NSEC_PER_MSEC) break;
        msleep(I14_E4_RETRY_MS);
    }
}
static enum hrtimer_restart e4_tick(struct hrtimer *timer)
{
    unsigned int act = i14_e4_watch(&st.w, ktime_get_ns());
    if (act == I14_W_PANIC) panic("i14-e4: CHR_EN not cleared");
    if (act == I14_W_FORCE_STOP) queue_work(system_highpri_wq, &e4_stop_work);
    if (READ_ONCE(st.w.finished)) return HRTIMER_NORESTART;
    hrtimer_forward_now(timer, ms_to_ktime(I14_E4_TIMER_MS));
    return HRTIMER_RESTART;
}
static int e4_reboot(struct notifier_block *nb, unsigned long event, void *cmd)
{
    unsigned int i;
    (void)cmpxchg(&st.w.req_ext, 0, I14_R_REBOOT);
    smp_mb(); /* pairs with I14_MB() in i14_e4_setup(): request, then may_be_on */
    /* Bounded wait for a running step: never hang the reboot; the running
     * loop sees the request itself and the hrtimer stays armed. */
    for (i = 0; i < 200 && !mutex_trylock(&e4_lock); i++) msleep(10);
    if (i < 200) {
        e4_down = 1;
        cancel_delayed_work(&e4_loop);
        e4_sync_stop(1000);
        pr_info("i14-e4: reboot: phase=%s may_be_on=%u stop_verified=%u wdt_off=%u\n", i14_e4_name(1, st.phase),
                READ_ONCE(st.w.may_be_on), st.stop_verified, st.wdt_off);
        mutex_unlock(&e4_lock);
    }
    /* halt/poweroff stop the SoC WDT and spin with IRQs off: no hrtimer, no LK.
     * Panic -> restart handler -> LK clears CHR_EN. */
    if (READ_ONCE(st.w.may_be_on)) panic("i14-e4: CHR_EN not cleared at reboot (step %s)", i < 200 ? "done" : "busy");
    return NOTIFY_DONE;
}
static struct notifier_block e4_nb = { .notifier_call = e4_reboot };
/* Suspend freezes CLOCK_MONOTONIC (hrtimer, heartbeat, limits): refuse it until the session is over. */
static int e4_pm(struct notifier_block *nb, unsigned long event, void *unused)
{
    if ((event == PM_SUSPEND_PREPARE || event == PM_HIBERNATION_PREPARE) && !READ_ONCE(st.w.finished))
        return notifier_from_errno(-EBUSY);
    return NOTIFY_DONE;
}
static struct notifier_block e4_pm_nb = { .notifier_call = e4_pm };
/* Teardown after e4_down=1 (and, on exit, the final stop). */
static void e4_unwind(void)
{
    hrtimer_cancel(&e4_timer);
    debugfs_remove(e4_dir); /* no new stop writes after this */
    unregister_reboot_notifier(&e4_nb);
    unregister_pm_notifier(&e4_pm_nb);
    cancel_work_sync(&e4_stop_work); /* the timer or a writer may have queued it once more */
    cancel_delayed_work_sync(&e4_loop);
    put_device(&pmic_device->dev);
}

static int e4_state_show(struct seq_file *m, void *v)
{
    struct i14_e4_state s;
    if (mutex_lock_interruptible(&e4_lock)) return -EINTR;
    s = st;
    mutex_unlock(&e4_lock);
    seq_printf(m, "mode %s\ncs %u\ntd0_param_ms %u\nrun_s %u\nphase %s\nresult %s",
               s.mode == 2 ? "e5" : s.mode ? "e4b" : "e4a", s.cs, s.td0_ms, s.run_s,
               i14_e4_name(1, s.phase), i14_e4_name(2, s.result));
    if (s.result == I14_RES_REFUSED || s.result == I14_RES_ABORTED) seq_printf(m, " %s", i14_e4_name(0, s.reason));
    if (s.result == I14_RES_DM_FAIL) seq_printf(m, " %s", i14_e4_name(3, s.dm_why));
    seq_printf(m, "\nstop_reason %s\nmay_be_on %u\nstop_verified %u\ndet_stuck %u\nwdt_off %u\nadc_misses %u\n",
               i14_e4_name(0, s.reason), s.w.may_be_on, s.stop_verified, s.det_stuck, s.wdt_off, s.adc_misses);
    seq_printf(m, "t_kick_ns %llu\nt_enable_ns %llu\nt_out_lo_ns %llu\nt_out_hi_ns %llu\nt_clear_lo_ns %llu\nt_clear_hi_ns %llu\n",
               s.t_kick, s.t_enable, s.t_out_lo, s.t_out_hi, s.t_clear_lo, s.t_clear_hi);
    if (s.out_seen) seq_printf(m, "td0_out_ms %llu\ntd0_out_lo_ms %llu\n", (s.t_out_hi - s.t_kick) / I14_MS,
                               (s.t_out_lo - s.t_kick) / I14_MS);
    else seq_puts(m, "td0_out_ms unknown\ntd0_out_lo_ms unknown\n");
    if (s.clear_seen) seq_printf(m, "td0_clear_ms %llu\n", (s.t_clear_hi - s.t_kick) / I14_MS);
    else seq_puts(m, "td0_clear_ms unknown\n");
    seq_printf(m, "passes %u\nkicks %u\nio_errors %u\nlast_err %d\nlast_addr 0x%04x\n", s.passes, s.kicks,
               s.io_errors, s.last_err, s.last_addr);
    return 0;
}
static int e4_log_show(struct seq_file *m, void *v)
{
    struct i14_e4_rec r;
    unsigned int i, k, n;
    seq_puts(m, "# seq t_ns phase reason kicked full err");
    for (i = 0; i < I14_E4_NREG; i++) seq_printf(m, " %04x", 0x0f48 + 2 * i);
    seq_puts(m, " fg_0cb2 fg_0ccc raw0 raw1 raw2 raw3 vbat_mv isense_mv vbus_mv ntc_mv temp_ok temp_mc adc_err\n");
    spin_lock(&e4_ring_lock);
    n = e4_count;
    spin_unlock(&e4_ring_lock);
    for (k = n > E4_RING ? n - E4_RING : 0; k < n; k++) {
        spin_lock(&e4_ring_lock);
        r = e4_ring[k % E4_RING];
        spin_unlock(&e4_ring_lock);
        if (r.seq != k) continue; /* overwritten while reading */
        seq_printf(m, "%u %llu %s %s %u %u %d", r.seq, r.t_ns, i14_e4_name(1, r.phase),
                   i14_e4_name(0, r.reason), r.kicked, r.full, r.err);
        for (i = 0; i < I14_E4_NREG; i++) seq_printf(m, " %04x", r.reg[i]);
        seq_printf(m, " %u %u %u %u %u %u %d %d %d %d %d %d %d\n", r.fg[0], r.fg[1], r.raw[0], r.raw[1],
                   r.raw[2], r.raw[3], r.mv[0], r.mv[1], r.mv[2], r.mv[3], r.temp_ok, r.temp_mc, r.adc_err);
    }
    return 0;
}
static int e4_state_open(struct inode *inode, struct file *file) { return single_open(file, e4_state_show, NULL); }
static int e4_log_open(struct inode *inode, struct file *file)
{ return single_open_size(file, e4_log_show, NULL, 128 * 1024); }
static ssize_t e4_stop_write(struct file *file, const char __user *buf, size_t len, loff_t *ppos)
{
    (void)cmpxchg(&st.w.req_ext, 0, I14_R_USER); /* lock-free: works even if a step hangs */
    queue_work(system_highpri_wq, &e4_stop_work);
    return len;
}
static const struct file_operations e4_state_fops = { .owner = THIS_MODULE, .open = e4_state_open,
    .read = seq_read, .llseek = seq_lseek, .release = single_release };
static const struct file_operations e4_log_fops = { .owner = THIS_MODULE, .open = e4_log_open,
    .read = seq_read, .llseek = seq_lseek, .release = single_release };
static const struct file_operations e4_stop_fops = { .owner = THIS_MODULE, .write = e4_stop_write,
    .llseek = noop_llseek };

static int __init e4_init(void)
{
    struct i14_e4_io io = { NULL, e4_read, e4_update, e4_write, e4_now, e4_pause };
    struct device_node *node, *selected = NULL;
    struct regmap *map;
    struct dentry *stop;
    unsigned matches = 0;
    int ret, m = i14_e4_cfg_ok(mode, cs, td0_ms, run_s), pt = READ_ONCE(panic_timeout);

    if (m < 0) {
        pr_err("i14-e4: refused: mode=%s cs=%u td0_ms=%u run_s=%u\n", mode ? mode : "(none)", cs, td0_ms, run_s);
        return -EINVAL;
    }
    if (pt < 1 || pt > 10 || kexec_crash_loaded()) { /* the hrtimer panic must end in a reset, not kdump */
        pr_err("i14-e4: refused: panic_timeout=%d (need 1..10) kexec_crash_loaded=%d\n", pt, kexec_crash_loaded());
        return -EPERM;
    }
    if (!of_machine_is_compatible("magcomm,magc6735-65u-1-m0")) return -ENODEV;
    for_each_compatible_node(node, NULL, "mediatek,mt6328") {
        if (!of_device_is_available(node)) continue;
        matches++;
        if (!selected) selected = of_node_get(node);
    }
    if (matches != 1) { of_node_put(selected); return -ENODEV; }
    pmic_device = of_find_device_by_node(selected);
    of_node_put(selected);
    if (!pmic_device) return -ENODEV;
    /* No PMIC read before the backstop: the provider check (HWCID read) runs
     * below, after i14_e4_arm(). Its failure counts as unknown gates. */
    st.mode = m; st.cs = cs; st.td0_ms = td0_ms; st.run_s = run_s;
    e4_dir = debugfs_create_dir("i14-e4", NULL);
    if (IS_ERR(e4_dir)) { ret = PTR_ERR(e4_dir); goto put_pmic; } /* no stop file: no session */
    debugfs_create_file("state", 0400, e4_dir, NULL, &e4_state_fops);
    debugfs_create_file("log", 0400, e4_dir, NULL, &e4_log_fops);
    stop = debugfs_create_file("stop", 0200, e4_dir, NULL, &e4_stop_fops);
    if (IS_ERR(stop)) { ret = PTR_ERR(stop); goto rm_dir; }
    ret = register_pm_notifier(&e4_pm_nb);
    if (ret) goto rm_dir;
    ret = register_reboot_notifier(&e4_nb);
    if (ret) goto rm_pm;
    /* The independent backstop is armed before the first PMIC read, with the
     * gates unknown (may_be_on=1) until 0x0f48 reads them off. */
    i14_e4_arm(&st, ktime_get_ns());
    hrtimer_setup(&e4_timer, e4_tick, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
    hrtimer_start(&e4_timer, ms_to_ktime(I14_E4_TIMER_MS), HRTIMER_MODE_REL);
    /* Preflight: 0x0f48 first; if CHR_EN/CSDAC read 1, S1 before any other I/O.
     * Otherwise reads and the AUXADC sequence only. */
    mutex_lock(&e4_lock);
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    if (!IS_ERR(map)) io.ctx = map;
    else pr_err("i14-e4: provider check failed rc=%ld: gates unknown, S1 or panic\n", PTR_ERR(map));
    ret = i14_e4_init_run(&io, &st, &e4_cur);
    device_unlock(&pmic_device->dev);
    e4_log(&e4_cur);
    if (READ_ONCE(st.w.may_be_on)) panic("i14-e4: CHR_EN on or unknown at preflight and not cleared");
    if (ret) e4_down = 1;
    mutex_unlock(&e4_lock);
    if (ret) {
        pr_err("i14-e4: refused: preflight %s\n", i14_e4_name(0, st.reason));
        e4_unwind();
        return ret;
    }
    queue_delayed_work(system_highpri_wq, &e4_loop, 0);
    pr_info("i14-e4: session %s cs=%u td0_ms=%u run_s=%u panic_timeout=%d started\n", mode, cs, td0_ms, run_s, pt);
    return 0;
rm_pm:
    unregister_pm_notifier(&e4_pm_nb);
rm_dir:
    debugfs_remove(e4_dir);
put_pmic:
    put_device(&pmic_device->dev);
    return ret;
}
static void __exit e4_exit(void)
{
    (void)cmpxchg(&st.w.req_ext, 0, I14_R_EXIT);
    mutex_lock(&e4_lock);
    e4_down = 1;
    mutex_unlock(&e4_lock);
    cancel_delayed_work_sync(&e4_loop);
    cancel_work_sync(&e4_stop_work);
    mutex_lock(&e4_lock);
    e4_sync_stop(I14_E4_DET_MS + 500); /* covers the det-bit window, so S2 can run */
    if (READ_ONCE(st.w.may_be_on)) panic("i14-e4: CHR_EN not cleared at module exit");
    if (st.phase < I14_P_DONE) pr_warn("i14-e4: exit: phase=%s stop_verified=%u det_stuck=%u\n",
                                       i14_e4_name(1, st.phase), st.stop_verified, st.det_stuck);
    mutex_unlock(&e4_lock);
    e4_unwind();
}
module_init(e4_init);
module_exit(e4_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("I14 phase 4 E4: bounded, fail-closed MT6328 charge enable (e4a dead-man, e4b 60 s)");
