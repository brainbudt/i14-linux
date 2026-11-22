// SPDX-License-Identifier: GPL-2.0-only
/* Phase 4 E3: read-only MT6328 state under 7.0 (spec PHASE4-E3E4-SPEC-2026-11-02).
 * debugfs i14-e3/{charger,pmic,adc}, 0400. Reads only even addresses <= 0x0ffe;
 * the only writes are the existing AUXADC enable/request/restore sequence,
 * and only while the 'adc' file is opened. No parameters, no state. */
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/ktime.h>
#include <linux/mfd/mt6328/registers.h>
#include <linux/mfd/mt6397/core.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/regmap.h>
#include <linux/seq_file.h>
#include <linux/utsname.h>
#include "i14-e4-policy.h"

static DEFINE_MUTEX(e3_lock);
static struct platform_device *pmic_device;
static struct dentry *e3_dir;
static unsigned short e3_vals[2048];
static const char *const e3_names[] = { "charger", "pmic", "adc" };

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

/* The only I/O paths; the regmap max_register is 0xffff, so guard here. */
static int e3_read(void *map, unsigned addr, unsigned *value)
{
    int ret;
    if (!i14_e3_range_ok(addr)) return -EPERM;
    ret = regmap_read(map, addr, value);
    return ret ? ret : *value > 0xffff ? -EIO : 0;
}
static int e3_update(void *map, unsigned addr, unsigned mask, unsigned value, int force)
{
    if (force || !i14_e34_adc_write_ok(addr, mask, value)) return -EPERM;
    return regmap_update_bits(map, addr, mask, value);
}
static int e3_write(void *map, unsigned addr, unsigned value)
{
    if (!i14_e34_adc_write_ok(addr, 0xffff, value)) return -EPERM;
    return regmap_write(map, addr, value);
}
static unsigned long long e3_now(void *map) { return ktime_get_ns(); }
static void e3_pause(void *map, unsigned us) { usleep_range(us, 2 * us); }

static void e3_adc_show(struct seq_file *m, const struct i14_e4_rec *r)
{
    unsigned ch;
    seq_printf(m, "t_ns %llu\n", r->t_ns);
    for (ch = 0; ch < r->adc_ch && ch < 4; ch++) {
        seq_printf(m, "ch%u_latch %u\nch%u_raw %u\n", ch, r->latch[ch], ch, r->raw[ch]);
        if (ch == 0) seq_printf(m, "vbat_mv %d\n", r->mv[0]);
        if (ch == 1) seq_printf(m, "isense_mv %d\nisense_minus_vbat_mv %d\n", r->mv[1], r->mv[1] - r->mv[0]);
        if (ch == 2) seq_printf(m, "vbus_mv %d\n", r->mv[2]);
        if (ch == 3) seq_printf(m, "ntc_mv %d\n", r->mv[3]);
    }
    if (r->adc_ch == 4 && r->temp_ok) seq_printf(m, "temp_mc %d\n", r->temp_mc);
    else if (r->adc_ch == 4) seq_puts(m, "temp_mc unknown\n");
    if (r->adc_err) seq_printf(m, "adc_error %d ch%u\n", r->adc_err, r->adc_ch);
    else seq_puts(m, "adc_error 0\n");
}

/* One transaction per open: lock, verified regmap, all reads, unlock, print. */
static int e3_show(struct seq_file *m, void *unused)
{
    unsigned long id = (unsigned long)m->private;
    struct i14_e4_io io = { NULL, e3_read, e3_update, e3_write, e3_now, e3_pause };
    struct i14_e4_rec r;
    struct regmap *map;
    unsigned long long t;
    unsigned bad = 0, i;
    char line[24];
    int ret;

    memset(&r, 0, sizeof(r));
    mutex_lock(&e3_lock);
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    t = ktime_get_ns();
    ret = PTR_ERR_OR_ZERO(map);
    io.ctx = map;
    if (!ret && id == 2) {
        r.t_ns = t;
        i14_e4_adc(&io, &r);
    } else if (!ret) {
        ret = i14_e3_dump(&io, id, e3_vals, &bad);
    }
    device_unlock(&pmic_device->dev);
    if (IS_ERR(map)) {
        dev_err(&pmic_device->dev, "i14-e3: %s: provider check failed rc=%d; no output\n", e3_names[id], ret);
        ret = -EIO;
    } else if (ret < 0) {
        dev_err(&pmic_device->dev, "i14-e3: %s: read 0x%04x rc=%d; no output\n", e3_names[id], bad, ret);
        ret = -EIO;
    } else if (id == 2) {
        if (r.adc_err) dev_warn(&pmic_device->dev, "i14-e3: adc ch%u rc=%d\n", r.adc_ch, r.adc_err);
        e3_adc_show(m, &r);
        ret = 0;
    } else {
        seq_printf(m, "# node=i14-e3/%s kernel=%s t_ns=%llu\n", e3_names[id], init_utsname()->release, t);
        for (i = 0; i < (unsigned)ret; i++)
            seq_write(m, line, i14_e3_line(line, (id ? 0 : 0x0f48) + 2 * i, e3_vals[i]));
        ret = 0;
    }
    mutex_unlock(&e3_lock);
    return ret;
}
static int e3_open(struct inode *inode, struct file *file)
{
    /* Big enough that seq_file never calls show twice (no second register pass). */
    return single_open_size(file, e3_show, inode->i_private,
                            (unsigned long)inode->i_private == 1 ? 32768 : 2048);
}
static const struct file_operations e3_fops = {
    .owner = THIS_MODULE, .open = e3_open, .read = seq_read,
    .llseek = seq_lseek, .release = single_release,
};

static int __init e3_init(void)
{
    struct device_node *node, *selected = NULL;
    struct regmap *map;
    unsigned matches = 0;
    unsigned long i;
    int ret;
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
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    ret = PTR_ERR_OR_ZERO(map);
    device_unlock(&pmic_device->dev);
    if (ret) goto put_pmic;
    e3_dir = debugfs_create_dir("i14-e3", NULL);
    if (IS_ERR(e3_dir)) { ret = PTR_ERR(e3_dir); goto put_pmic; }
    for (i = 0; i < 3; i++)
        debugfs_create_file(e3_names[i], 0400, e3_dir, (void *)i, &e3_fops);
    pr_info("i14-e3: ready (read-only dump <= 0x0ffe, adc on demand)\n");
    return 0;
put_pmic:
    put_device(&pmic_device->dev);
    return ret;
}
static void __exit e3_exit(void)
{
    debugfs_remove(e3_dir);
    put_device(&pmic_device->dev);
}
module_init(e3_init);
module_exit(e3_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("I14 phase 4 E3: read-only MT6328 dump and AUXADC CH0..CH3");
