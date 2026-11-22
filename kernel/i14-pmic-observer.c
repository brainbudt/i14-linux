// SPDX-License-Identifier: GPL-2.0-only
/* Offline diagnostic preparation. No first-read or handoff permission. */
#include <linux/atomic.h>
#include <linux/capability.h>
#include <linux/cred.h>
#include <linux/device.h>
#include <linux/errno.h>
#include <linux/kernel.h>
#include <linux/kobject.h>
#include <linux/ktime.h>
#include <linux/mfd/mt6397/core.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/regmap.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/uidgid.h>
#include "i14-pmic-observer-core.h"

/* No approved first-read/access/dispatch contract or native crossbuild exists.
 * This source constant is not a module parameter or runtime switch. */
enum { I14_OBSERVER_READ_QUALIFIED = 0 };
static DEFINE_MUTEX(observer_lock);
static atomic_t consumed = ATOMIC_INIT(0);
static struct i14_observer_state observation;
static struct kobject *observer_kobject;
static unsigned held_ref;
static unsigned last_provider_metadata_match;

struct observer_provider {
    struct platform_device *pmic;
    struct device *parent;
    struct regmap *map;
    unsigned locks;
};
static struct observer_provider provider;

static int claim_once(void *context)
{
    (void)context;
    if (!try_module_get(THIS_MODULE)) return -ENODEV;
    if (atomic_cmpxchg(&consumed, 0, 1)) {
        module_put(THIS_MODULE);
        return -EALREADY;
    }
    held_ref = 1;
    return 0;
}

/* Metadata-only intended binding, derived from the existing board diagnostic.
 * The old checked_regmap() HWCID transaction is deliberately not reproduced.
 * Parent-before-child locks pin driver lifetime, not other regmap clients. */
static int prepare_provider(void *context)
{
    struct observer_provider *p = context;
    struct device_node *node, *other;
    struct device *dev;
    struct mt6397_chip *chip;

    last_provider_metadata_match = 0;
    if (!of_machine_is_compatible("magcomm,magc6735-65u-1-m0")) return -ENODEV;
    node = of_find_compatible_node(NULL, NULL, "mediatek,mt6328");
    if (!node) return -ENODEV;
    other = of_find_compatible_node(of_node_get(node), NULL, "mediatek,mt6328");
    if (other) {
        of_node_put(other);
        of_node_put(node);
        return -ENODEV;
    }
    p->pmic = of_find_device_by_node(node);
    of_node_put(node);
    if (!p->pmic) return -ENODEV;
    dev = &p->pmic->dev;
    p->parent = get_device(dev->parent);
    if (!p->parent) return -ENODEV;
    device_lock(p->parent);
    p->locks = 1;
    device_lock(dev);
    p->locks = 2;
    if (dev->parent != p->parent || !dev->driver ||
        strcmp(dev->driver->name, "mt6397") || !p->parent->driver ||
        strcmp(p->parent->driver->name, "mt-pmic-pwrap") ||
        !of_device_is_compatible(dev->of_node, "mediatek,mt6328") ||
        !of_device_is_compatible(p->parent->of_node, "mediatek,mt6735-pwrap"))
        return -ENODEV;
    chip = dev_get_drvdata(dev);
    if (!chip || chip->dev != dev ||
        chip->chip_id != MT6328_CHIP_ID || !chip->regmap ||
        chip->regmap != dev_get_regmap(p->parent, NULL))
        return -ENODEV;
    p->map = chip->regmap;
    last_provider_metadata_match = 1;
    return I14_OBSERVER_READ_QUALIFIED ? 0 : -EOPNOTSUPP;
}

static void finish_provider(void *context)
{
    struct observer_provider *p = context;

    p->map = NULL;
    if (p->locks == 2) device_unlock(&p->pmic->dev);
    if (p->locks) device_unlock(p->parent);
    p->locks = 0;
    if (p->parent) put_device(p->parent);
    if (p->pmic) put_device(&p->pmic->dev);
    p->parent = NULL;
    p->pmic = NULL;
}

static int read_word(void *context, unsigned address, unsigned *value)
{
    struct observer_provider *p = context;

    return regmap_read(p->map, address, value);
}

static unsigned long long clock_ns(void *context)
{
    (void)context;
    return ktime_get_ns();
}

static ssize_t observe_store(struct kobject *object, struct kobj_attribute *attr,
                             const char *buffer, size_t count)
{
    const struct i14_observer_io io = {
        &provider, claim_once, prepare_provider, read_word,
        finish_provider, clock_ns
    };
    int ret;

    (void)object;
    (void)attr;
    mutex_lock(&observer_lock);
    ret = i14_observer_observe(&observation, &io, buffer, count,
        uid_eq(current_uid(), GLOBAL_ROOT_UID) &&
        uid_eq(current_euid(), GLOBAL_ROOT_UID), capable(CAP_SYS_RAWIO));
    mutex_unlock(&observer_lock);
    return ret ? ret : (ssize_t)count;
}

static ssize_t state_show(struct kobject *object, struct kobj_attribute *attr,
                          char *buffer)
{
    int used, n;

    (void)object;
    (void)attr;
    mutex_lock(&observer_lock);
    used = i14_observer_format(&observation, buffer, PAGE_SIZE);
    if (used >= 0) {
        n = snprintf(buffer + used, PAGE_SIZE - (size_t)used,
            "read_qualified=%u\nlast_provider_metadata_match=%u\nheld_ref=%u\n",
            I14_OBSERVER_READ_QUALIFIED, last_provider_metadata_match, held_ref);
        if (n < 0 || (size_t)n >= PAGE_SIZE - (size_t)used)
            used = -ENOSPC;
        else
            used += n;
    }
    mutex_unlock(&observer_lock);
    return used;
}

static struct kobj_attribute state_attribute = __ATTR(state, 0444, state_show, NULL);
static struct kobj_attribute observe_attribute = __ATTR(observe, 0200, NULL, observe_store);
static struct attribute *observer_attributes[] = {
    &state_attribute.attr, &observe_attribute.attr, NULL
};
static const struct attribute_group observer_group = { .attrs = observer_attributes };

static int __init observer_init(void)
{
    int ret;

    observer_kobject = kobject_create_and_add(I14_OBSERVER_SYSFS_NAME, kernel_kobj);
    if (!observer_kobject) return -ENOMEM;
    ret = sysfs_create_group(observer_kobject, &observer_group);
    if (ret) {
        kobject_put(observer_kobject);
        observer_kobject = NULL;
    }
    return ret;
}

/* An attempted command holds its module reference permanently. This exit is
 * reachable only before an attempt; it performs no cleanup register action. */
static void __exit observer_exit(void)
{
    sysfs_remove_group(observer_kobject, &observer_group);
    kobject_put(observer_kobject);
}

module_init(observer_init);
module_exit(observer_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("I14 offline-prepared one-attempt read-only PMIC observer; "
                   I14_OBSERVER_PROFILE_NAME "; unqualified");
