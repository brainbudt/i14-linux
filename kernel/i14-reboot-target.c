// SPDX-License-Identifier: GPL-2.0-only
/* Own-LK RTC flag only; see docs/NATIVE-REBOOT-2026-10-30.md. */
#include <linux/capability.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/ktime.h>
#include <linux/mfd/mt6328/registers.h>
#include <linux/mfd/mt6397/core.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/reboot.h>
#include <linux/regmap.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include "i14-reboot-rtc.h"
#include "i14-battery-snapshot.h"
#include "i14-battery-sample.h"
#include "i14-charger-snapshot.h"

static DEFINE_MUTEX(target_lock);
static struct platform_device *pmic_device;
static struct device *target_device;
static unsigned int receipt;
static bool prepared;
static struct i14_adc_result last_sample;
static bool sample_valid;

/* Caller holds the bound MFD device lock throughout the transaction. The MFD
 * gets its real regmap from PWRAP; never manufacture an MMIO regmap here. */
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

static int rtc_read(void *context, unsigned int address, unsigned int *value)
{
    return regmap_read(context, address, value);
}

static int rtc_write(void *context, unsigned int address, unsigned int value)
{
    return regmap_write(context, address, value);
}

static int adc_update(void *context, unsigned int address, unsigned int mask, unsigned int value)
{
    return regmap_update_bits(context, address, mask, value);
}

static unsigned long long rtc_now(void *context)
{
    return ktime_get_ns();
}

static void rtc_pause(void *context)
{
    usleep_range(50, 100);
}

static int verify_receipt(struct regmap *map)
{
    unsigned int observed;
    int ret;

    if (!prepared) return -ENODATA;
    ret = regmap_read(map, I14_RTC_PDN1, &observed);
    if (ret) return ret;
    return observed == receipt && (observed & I14_FAST_BOOT) ? 0 : -EIO;
}

static ssize_t target_store(struct device *dev, struct device_attribute *attr,
                            const char *buf, size_t count)
{
    struct regmap *map;
    struct i14_rtc_io io;
    int ret;

    if (!capable(CAP_SYS_BOOT)) return -EPERM;
    if (count != sizeof("bootloader\n") - 1 ||
        memcmp(buf, "bootloader\n", sizeof("bootloader\n") - 1)) return -EINVAL;
    mutex_lock(&target_lock);
    device_lock(&pmic_device->dev);
    prepared = false;
    map = checked_regmap();
    ret = PTR_ERR_OR_ZERO(map);
    if (!ret) {
        io = (struct i14_rtc_io){ map, rtc_read, rtc_write, rtc_now, rtc_pause };
        ret = i14_rtc_prepare(&io, "bootloader", &receipt);
        prepared = !ret;
    }
    device_unlock(&pmic_device->dev);
    mutex_unlock(&target_lock);
    if (ret) dev_err(dev, "RTC preparation failed: %d; flag may be armed, no reset issued\n", ret);
    return ret ? ret : count;
}

static ssize_t target_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct regmap *map;
    int ret;
    ssize_t length;

    mutex_lock(&target_lock);
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    ret = PTR_ERR_OR_ZERO(map);
    if (!ret) ret = verify_receipt(map);
    length = ret ? ret : sysfs_emit(buf, "prepared bootloader pdn1=0x%04x\n", receipt);
    device_unlock(&pmic_device->dev);
    mutex_unlock(&target_lock);
    return length;
}
static DEVICE_ATTR(target, 0600, target_show, target_store);

static ssize_t battery_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct i14_battery_snapshot sample;
    struct regmap *map;
    char ch0_mv[16], ch1_mv[16];
    int ret, mv;
    ssize_t length;

    mutex_lock(&target_lock);
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    ret = PTR_ERR_OR_ZERO(map);
    if (!ret) ret = i14_battery_read(map, rtc_read, &sample);
    device_unlock(&pmic_device->dev);
    mutex_unlock(&target_lock);
    if (ret) return ret;
    mv = i14_battery_mv(sample.ch0);
    if (mv < 0) strscpy(ch0_mv, "unknown", sizeof(ch0_mv));
    else snprintf(ch0_mv, sizeof(ch0_mv), "%d", mv);
    mv = i14_battery_mv(sample.ch1);
    if (mv < 0) strscpy(ch1_mv, "unknown", sizeof(ch1_mv));
    else snprintf(ch1_mv, sizeof(ch1_mv), "%d", mv);
    length = sysfs_emit(buf,
        "source=cached-pmic-latches freshness=unknown adc_requested=0\n"
        "chr_con0[0f48]=0x%04x chgdet=%u chr_en=%u csdac_en=%u\n"
        "ch0_batsns[0e2e]=0x%04x raw=%u ready=%u cached_mv=%s\n"
        "ch1_isense[0e32]=0x%04x raw=%u ready=%u cached_mv=%s\n"
        "auxadc_con0[0e94]=0x%04x ck_aon=%u\n"
        "auxadc_con12[0eb0]=0x%04x vbuf_en=%u\n"
        "top_clksq[028a]=0x%04x aux_ap_en=%u aux_ap_mode=%u\n",
        sample.chr_con0, i14_battery_chgdet(sample.chr_con0),
        i14_battery_chr_en(sample.chr_con0), i14_battery_csdac_en(sample.chr_con0),
        sample.ch0, sample.ch0 & 0x7fff, i14_battery_ready(sample.ch0), ch0_mv,
        sample.ch1, sample.ch1 & 0x7fff, i14_battery_ready(sample.ch1), ch1_mv,
        sample.auxadc_con0, i14_battery_ck_aon(sample.auxadc_con0),
        sample.auxadc_con12, i14_battery_vbuf_en(sample.auxadc_con12),
        sample.top_clksq, i14_battery_aux_ap_en(sample.top_clksq),
        i14_battery_aux_ap_mode(sample.top_clksq));
    return length;
}
static DEVICE_ATTR(battery, 0400, battery_show, NULL);

static ssize_t charger_state_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct regmap *map;
    int ret;

    mutex_lock(&target_lock);
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    ret = PTR_ERR_OR_ZERO(map);
    if (!ret) ret = i14_charger_capture(map, rtc_read, buf, PAGE_SIZE);
    device_unlock(&pmic_device->dev);
    mutex_unlock(&target_lock);
    return ret;
}
static DEVICE_ATTR(charger_state, 0400, charger_state_show, NULL);

static ssize_t battery_sample_store(struct device *dev, struct device_attribute *attr,
                                    const char *buf, size_t count)
{
    struct regmap *map;
    struct i14_adc_io io;
    int ret;

    if (!capable(CAP_SYS_RAWIO)) return -EPERM;
    if (count != sizeof("sample\n") - 1 || memcmp(buf, "sample\n", sizeof("sample\n") - 1))
        return -EINVAL;
    mutex_lock(&target_lock);
    if (last_sample.error && (last_sample.uncertain || last_sample.request_attempted)) {
        dev_err(dev, "ADC sample blocked after uncertain prior attempt; no additional I/O issued\n");
        mutex_unlock(&target_lock);
        return -EUCLEAN;
    }
    sample_valid = false;
    memset(&last_sample, 0, sizeof(last_sample));
    last_sample.phase = "provider";
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    ret = PTR_ERR_OR_ZERO(map);
    if (!ret) {
        io = (struct i14_adc_io){ map, rtc_read, adc_update, rtc_write, rtc_now, rtc_pause };
        ret = i14_adc_sample(&io, "sample", &last_sample);
        sample_valid = !ret;
    }
    last_sample.error = ret;
    device_unlock(&pmic_device->dev);
    if (ret)
        dev_err(dev, "ADC sample failed %d phase=%s address=%04x uncertain_bits=%x restored_bits=%x request_attempted=%u saw_ready_zero=%u originals_valid=%x originals=%04x,%04x,%04x; no fresh receipt, setup may remain changed, no retry/cleanup\n",
                ret, IS_ERR(map) ? "provider" : last_sample.phase, last_sample.address,
                last_sample.uncertain, last_sample.restored, last_sample.request_attempted,
                last_sample.saw_zero, last_sample.originals_valid, last_sample.original[0],
                last_sample.original[1], last_sample.original[2]);
    mutex_unlock(&target_lock);
    return ret ? ret : count;
}

static ssize_t battery_sample_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    ssize_t length;

    mutex_lock(&target_lock);
    if (!sample_valid) length = -ENODATA;
    else length = sysfs_emit(buf,
        "sample_receipt ch0_batsns[0e2e]=0x%04x raw=%u mv=%d observed_ready=0->1 restored_bits=0x%x sampled_monotonic_ns=%llu age_ns=%llu\n",
        last_sample.latch, last_sample.latch & 0x7fff, i14_battery_mv(last_sample.latch),
        last_sample.restored, last_sample.sampled_ns, ktime_get_ns() - last_sample.sampled_ns);
    mutex_unlock(&target_lock);
    return length;
}
static DEVICE_ATTR(battery_sample, 0600, battery_sample_show, battery_sample_store);

static int target_reboot(struct notifier_block *nb, unsigned long event, void *command)
{
    struct regmap *map;
    int ret;

    if (event != SYS_RESTART || !command || strcmp(command, "bootloader"))
        return NOTIFY_DONE;
    mutex_lock(&target_lock);
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    ret = PTR_ERR_OR_ZERO(map);
    if (!ret) ret = verify_receipt(map);
    device_unlock(&pmic_device->dev);
    mutex_unlock(&target_lock);
    if (ret)
        pr_err("i14-reboot-target: FAILED target verification %d; reboot chain cannot be vetoed\n", ret);
    else
        pr_info("i14-reboot-target: RTC bootloader request verified (not Fastboot enumeration)\n");
    return NOTIFY_DONE;
}

static struct notifier_block target_notifier = { .notifier_call = target_reboot };

static int __init target_init(void)
{
    struct device_node *node, *selected = NULL;
    struct regmap *map;
    unsigned int matches = 0;
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
    target_device = root_device_register("i14-reboot-target");
    if (IS_ERR(target_device)) { ret = PTR_ERR(target_device); goto put_pmic; }
    ret = register_reboot_notifier(&target_notifier);
    if (ret) goto unregister_device;
    ret = device_create_file(target_device, &dev_attr_target);
    if (!ret) {
        ret = device_create_file(target_device, &dev_attr_battery);
        if (!ret) {
            ret = device_create_file(target_device, &dev_attr_battery_sample);
            if (!ret) {
                ret = device_create_file(target_device, &dev_attr_charger_state);
                if (!ret) return 0;
                device_remove_file(target_device, &dev_attr_battery_sample);
            }
            device_remove_file(target_device, &dev_attr_battery);
        }
        device_remove_file(target_device, &dev_attr_target);
    }
    unregister_reboot_notifier(&target_notifier);
unregister_device:
    root_device_unregister(target_device);
put_pmic:
    put_device(&pmic_device->dev);
    return ret;
}

static void __exit target_exit(void)
{
    device_remove_file(target_device, &dev_attr_charger_state);
    device_remove_file(target_device, &dev_attr_battery_sample);
    device_remove_file(target_device, &dev_attr_battery);
    device_remove_file(target_device, &dev_attr_target);
    unregister_reboot_notifier(&target_notifier);
    root_device_unregister(target_device);
    put_device(&pmic_device->dev);
    /* Do not attempt a second RTC transaction on error/unload. */
}

module_init(target_init);
module_exit(target_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("I14 exact-board checked MT6328 LK Fastboot request");
