// SPDX-License-Identifier: GPL-2.0-only
/* Explicit wall trial or allocated-USB pilot. Loading never charges. */
#include <linux/capability.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/ktime.h>
#include <linux/mfd/mt6328/registers.h>
#include <linux/mfd/mt6397/core.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/netdevice.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/reboot.h>
#include <linux/regmap.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/usb/gadget.h>
#include <linux/usb/composite.h>
#include <linux/workqueue.h>
#include <net/net_namespace.h>
#include "i14-battery-sample.h"
#include "i14-charge-bootstrap.h"
#include "i14-charge-session.h"
#include "i14-charger-snapshot.h"

static DEFINE_MUTEX(charger_lock);
static struct platform_device *pmic_device;
static struct device *charger_device;
static struct device *usb_controller;
static struct i14_charge_session session;
static struct i14_charge_state charge = { .phase = "idle", .reason = "" };
static struct i14_adc_result last_adc;
static struct i14_charge_sample latest, first;
static unsigned latest_raw[3], latest_latch[3];
static unsigned latest_valid, first_valid, temperature_valid;
static int temperature_mc, last_error, last_carrier = -ENODATA;
static int last_usb_state = -ENODATA;
static unsigned last_usb_ma;
static int last_usb_suspended = -ENODATA;
static unsigned last_usb_config_ma;
static const char *reason = "initialized-read-only";
static unsigned held_ref, io_failed, stop_requested, shutting_down;
static unsigned long long sequence, armed_ns, started_ns, ended_ns, latched_ns;
static void charger_worker(struct work_struct *work);
static DECLARE_DELAYED_WORK(charger_work, charger_worker);

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

static int usb_carrier(void)
{
    struct net_device *dev = dev_get_by_name(&init_net, "usb0");
    int carrier;
    if (!dev) return -ENODEV;
    carrier = netif_carrier_ok(dev) ? 1 : 0;
    dev_put(dev);
    return carrier;
}

static int gadget_child(struct device *dev, const void *unused)
{
    return dev->bus && !strcmp(dev->bus->name, "gadget");
}

/* Exact-kernel public gadget fields: SET_CONFIGURATION records mA, suspend
 * reduces it to2. Parent/device locks pin controller and bound gadget data. */
static int usb_budget_ready(void)
{
    struct device *child;
    struct usb_gadget *gadget;
    struct usb_composite_dev *composite;
    unsigned long flags;
    int ret = -ENODEV;
    last_usb_state = -ENODEV;
    last_usb_ma = 0;
    last_usb_suspended = -ENODEV;
    last_usb_config_ma = 0;
    if (!usb_controller)
        usb_controller = bus_find_device_by_name(&platform_bus_type, NULL, "musb-hdrc.3.auto");
    if (!usb_controller) return ret;
    device_lock(usb_controller);
    if (!usb_controller->driver || strcmp(usb_controller->driver->name, "musb-hdrc") ||
        !usb_controller->parent || !usb_controller->parent->driver ||
        strcmp(usb_controller->parent->driver->name, "musb-mtk") ||
        !of_device_is_compatible(usb_controller->parent->of_node, "mediatek,mtk-musb"))
        goto unlock_controller;
    child = device_find_child(usb_controller, NULL, gadget_child);
    if (!child) goto unlock_controller;
    device_lock(child);
    gadget = dev_to_usb_gadget(child);
    if (child->driver && !strcmp(child->driver->name, "configfs-gadget.i14") &&
        gadget->dev.parent == usb_controller &&
        gadget->name && !strcmp(gadget->name, "musb-hdrc")) {
        last_usb_state = READ_ONCE(gadget->state);
        last_usb_ma = READ_ONCE(gadget->mA);
        composite = get_gadget_data(gadget);
        if (composite && composite->gadget == gadget) {
            spin_lock_irqsave(&composite->lock, flags);
            last_usb_suspended = composite->suspended;
            if (composite->config) last_usb_config_ma = composite->config->MaxPower;
            spin_unlock_irqrestore(&composite->lock, flags);
        }
        ret = i14_session_usb_budget(last_usb_state == USB_STATE_CONFIGURED &&
                 last_usb_suspended == 0 && last_usb_config_ma == 500, last_usb_ma) ? 0 : -EPERM;
    }
    device_unlock(child);
    put_device(child);
unlock_controller:
    device_unlock(usb_controller);
    return ret;
}

static int fixed_read(void *context, unsigned address, unsigned *value)
{
    int ret;
    if (session.phase == I14_SESSION_LATCHED) return -EUCLEAN;
    switch (address) {
    case 0xf48: case 0xf4a: case 0xf4c: case 0xf4e: case 0xf50:
    case 0xf52: case 0xf54: case 0xf56: case 0xf62: case 0xf66:
    case 0xf68: case 0xf70: case 0xf72: case 0xf74: case 0xf76:
    case 0xf78: case 0xf7a: case 0xf9a: case 0xf9e:
    case 0xe88: case 0xe84: case 0xe86: case 0xe94: case 0xeb0:
    case 0x28a: case 0xe2e: case 0xe04: case 0xe06:
        break;
    default:
        io_failed = 1;
        return -EINVAL;
    }
    ret = regmap_read(context, address, value);
    if (ret || *value > 0xffff) {
        io_failed = 1;
        return ret ? ret : -EIO;
    }
    return 0;
}

static int charge_update(void *context, unsigned address, unsigned mask, unsigned value)
{
    int ret;
    if (!held_ref || session.phase == I14_SESSION_LATCHED ||
        !i14_c_allowed(address, mask, value))
        return -EPERM;
    session.mutated = 1;
    /* Strobes must reach the PMIC even if a read reports their bit already set. */
    if ((address == 0xf62 && mask == 0x100) || (address == 0xf66 && mask == 2))
        ret = regmap_write_bits(context, address, mask, value);
    else
        ret = regmap_update_bits(context, address, mask, value);
    if (ret) io_failed = 1;
    return ret;
}

static int adc_update(void *context, unsigned address, unsigned mask, unsigned value)
{
    int ret;
    if (!held_ref || session.phase == I14_SESSION_LATCHED || (value & ~mask) ||
        !((address == 0xe94 && mask == 0x8000) ||
          (address == 0xeb0 && mask == 0x80) || (address == 0x28a && mask == 0x100)))
        return -EPERM;
    session.mutated = 1;
    ret = regmap_update_bits(context, address, mask, value);
    if (ret) io_failed = 1;
    return ret;
}

static int adc_request(void *context, unsigned address, unsigned value)
{
    int ret;
    if (!held_ref || session.phase == I14_SESSION_LATCHED || address != 0xe8a ||
        (value != 1 && value != 4 && value != 8)) return -EPERM;
    session.mutated = 1;
    ret = regmap_write(context, address, value);
    if (ret) io_failed = 1;
    return ret;
}

static unsigned long long clock_ns(void *context) { return ktime_get_ns(); }
static void adc_pause(void *context) { usleep_range(50, 100); }
static void charge_pause(void *context) { usleep_range(500, 1000); }
static struct i14_charge_io charge_io(struct regmap *map)
{
    return (struct i14_charge_io){ map, fixed_read, charge_update, clock_ns, charge_pause };
}

static void latch_error(const char *why, int error)
{
    reason = why;
    last_error = error < 0 ? error : -EIO;
    latched_ns = ktime_get_ns();
    i14_session_finish(&session, 1, 0);
    dev_err(charger_device, "latched reason=%s error=%d core_uncertain=%x adc_uncertain=%x; no more I/O, ref held, hardware watchdog fallback\n",
            why, last_error, charge.uncertain, last_adc.uncertain);
}

static void finish_session(const char *why, int error)
{
    if (!i14_session_finish(&session, io_failed || charge.uncertain,
                             charge.stop_verified && !charge.running)) {
        latch_error(why, error);
        return;
    }
    reason = why;
    last_error = error;
    ended_ns = ktime_get_ns();
    dev_info(charger_device, "session ended reason=%s error=%d arm=%llu start=%llu end=%llu first_sequence=%llu last_sequence=%llu\n",
             why, error, armed_ns, started_ns, ended_ns,
             first_valid ? first.sequence : 0, latest_valid ? latest.sequence : 0);
    if (held_ref) {
        held_ref = 0;
        module_put(THIS_MODULE);
    }
}

/* Caller holds both locks. No cleanup transaction follows uncertain I/O. */
static void stop_session(struct regmap *map, const char *why, int error)
{
    struct i14_charge_io ops = charge_io(map);
    int ret;
    if (session.phase == I14_SESSION_LATCHED) return;
    if (io_failed || charge.uncertain) { latch_error(why, error); return; }
    if (!session.mutated || (charge.stop_verified && !charge.running)) {
        finish_session(why, error);
        return;
    }
    ret = i14_charge_stop(&ops, &charge);
    if (ret || charge.uncertain || !charge.stop_verified || charge.running) {
        latch_error("stop-not-verified", ret ? ret : -EUCLEAN);
        return;
    }
    finish_session(why, error);
}

static int fresh_samples(struct regmap *map, struct i14_charge_sample *sample, int *soft)
{
    static const unsigned channels[] = { 0, 2, 3 };
    struct i14_adc_io ops = { map, fixed_read, adc_update, adc_request, clock_ns, adc_pause };
    struct i14_charge_sample fresh = {0};
    unsigned raw[3], latch[3], i;
    int ret;

    *soft = 0;
    for (i = 0; i < 3; ++i) {
        ret = channels[i] == 0 ? i14_adc_sample(&ops, "sample", &last_adc) :
              i14_adc_sample_channel(&ops, channels[i], "sample", &last_adc);
        if (ret) {
            *soft = ret == -EBUSY && !io_failed && !last_adc.uncertain && !last_adc.request_attempted;
            return ret;
        }
        if (!last_adc.complete || last_adc.channel != channels[i] ||
            last_adc.restored != 7 || last_adc.uncertain || !last_adc.request_attempted || !last_adc.saw_zero)
            return -EPROTO;
        raw[i] = last_adc.raw;
        latch[i] = last_adc.latch;
        if (i == 0) {
            fresh.vbat_mv = last_adc.mv;
            fresh.sampled_ns = last_adc.sampled_ns;
        } else if (i == 1) {
            ret = i14_session_vbus(last_adc.raw, &fresh.vbus_mv);
            if (ret) { *soft = 1; return ret; }
        } else {
            fresh.ntc_mv = last_adc.mv;
        }
        if (last_adc.sampled_ns < fresh.sampled_ns) fresh.sampled_ns = last_adc.sampled_ns;
        fresh.fresh_mask |= 1U << i;
    }
    if (sequence == ~0ULL) { *soft = 1; return -EOVERFLOW; }
    fresh.sequence = ++sequence;
    latest = fresh;
    latest_valid = 1;
    memcpy(latest_raw, raw, sizeof(raw));
    memcpy(latest_latch, latch, sizeof(latch));
    temperature_valid = !i14_battery_temperature_mc(fresh.ntc_mv, &temperature_mc);
    if (!temperature_valid || i14_battery_temperature_probe_guard(0, temperature_mc) != 1) {
        *soft = 1;
        return -ERANGE;
    }
    *sample = fresh;
    return 0;
}

static int observe_power(struct regmap *map)
{
    unsigned chr;
    int ret;
    last_carrier = usb_carrier();
    ret = fixed_read(map, 0xf48, &chr);
    if (ret) return ret;
    if (session.usb_pilot) {
        ret = usb_budget_ready();
        return i14_session_observe_usb(&session, ktime_get_ns(), last_carrier, chr,
                                       ret == 0, last_usb_ma);
    }
    return i14_session_observe(&session, ktime_get_ns(), last_carrier, chr);
}

static void charger_worker(struct work_struct *work)
{
    struct regmap *map;
    struct i14_charge_io ops;
    struct i14_charge_sample sample;
    int action, ret, soft;
    unsigned long delay;
    unsigned long long now, deadline;

    mutex_lock(&charger_lock);
    if (!held_ref || session.phase == I14_SESSION_LATCHED) goto unlock;
    if ((stop_requested || shutting_down) && !session.mutated) {
        stop_requested = 0;
        finish_session(shutting_down ? "reboot" : "requested-stop", 0);
        goto unlock;
    }
    io_failed = 0;
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    if (IS_ERR(map)) { latch_error("provider-check", PTR_ERR(map)); goto unlock_device; }
    ops = charge_io(map);
    if (stop_requested || shutting_down) {
        stop_requested = 0;
        stop_session(map, shutting_down ? "reboot" : "requested-stop", 0);
        goto unlock_device;
    }
    action = observe_power(map);
    if (action < 0) { latch_error("power-observation", action); goto unlock_device; }
    if (action == I14_SESSION_CANCEL || action == I14_SESSION_STOP) {
        stop_session(map, action == I14_SESSION_CANCEL ? "arm-cancelled-or-expired" : "time-or-power-guard", 0);
        goto unlock_device;
    }
    if (action == I14_SESSION_START) {
        reason = "watchdog-selftest-chr-off";
        ret = i14_charge_selftest(&ops, &charge);
        if (ret) { stop_session(map, "selftest-failed", ret); goto unlock_device; }
    } else if (session.phase != I14_SESSION_ACTIVE) {
        reason = session.phase == I14_SESSION_WAIT_WALL ? "awaiting-wall-attach" : "awaiting-mac-detach";
        goto unlock_device;
    }
    ret = fresh_samples(map, &sample, &soft);
    if (ret) {
        if (soft) stop_session(map, "sensor-soft-guard", ret);
        else latch_error("adc-not-certain", ret);
        goto unlock_device;
    }
    /* Recabling during selftest/ADC acquisition must not reuse the old edge. */
    action = observe_power(map);
    if (action < 0) { latch_error("power-recheck", action); goto unlock_device; }
    if (action == I14_SESSION_CANCEL || action == I14_SESSION_STOP) {
        stop_session(map, "post-sample-time-or-power-guard", 0);
        goto unlock_device;
    }
    sample.external_power_confirmed = 1;
    latest.external_power_confirmed = 1;
    if (session.phase == I14_SESSION_WAIT_WALL) {
        if (action != I14_SESSION_START) { stop_session(map, "wall-edge-lost", -EPERM); goto unlock_device; }
        /* USB always uses70mA/max5s; lower-VBAT wall trials also use it. */
        ret = i14_charge_start(&ops, &charge,
                               session.usb_pilot || sample.vbat_mv < 3300 ? I14_CHARGE_PILOT : I14_CHARGE_WALL,
                               &sample);
        if (ret) { stop_session(map, "start-failed", ret); goto unlock_device; }
        started_ns = charge.started_ns;
        first = sample;
        first_valid = 1;
        ret = i14_session_started(&session, started_ns);
        if (ret) { stop_session(map, "start-deadline", ret); goto unlock_device; }
        dev_info(charger_device, "session started source=%s arm=%llu start=%llu limit=%llu first_sequence=%llu first_vbat_mv=%d first_ntc_mv=%d\n",
                 session.usb_pilot ? "configured-usb" : "wall", armed_ns, started_ns,
                 session.active_deadline, first.sequence, first.vbat_mv, first.ntc_mv);
    } else {
        ret = i14_charge_kick(&ops, &charge, &sample);
        if (ret) { stop_session(map, "kick-stopped", ret); goto unlock_device; }
    }
    reason = session.usb_pilot ? "usb-pilot-active" : "wall-session-active";
    last_error = 0;
unlock_device:
    device_unlock(&pmic_device->dev);
    if (held_ref && session.phase != I14_SESSION_LATCHED && !shutting_down) {
        now = ktime_get_ns();
        deadline = session.phase == I14_SESSION_ACTIVE ? session.active_deadline : session.arm_deadline;
        delay = msecs_to_jiffies(250);
        if (deadline <= now) delay = 0;
        else delay = min(delay, max(1UL, nsecs_to_jiffies(deadline - now)));
        mod_delayed_work(system_wq, &charger_work, delay);
    }
unlock:
    mutex_unlock(&charger_lock);
}

static const char *session_phase(void)
{
    switch (session.phase) {
    case I14_SESSION_IDLE: return "idle";
    case I14_SESSION_WAIT_DETACH: return "armed-wait-detach";
    case I14_SESSION_WAIT_WALL: return session.usb_pilot ? "usb-await-selftest" : "armed-wait-wall";
    case I14_SESSION_ACTIVE: return "active";
    case I14_SESSION_DONE: return "done";
    case I14_SESSION_LATCHED: return "latched-no-more-io";
    }
    return "unknown";
}

static ssize_t state_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    unsigned long long now, age;
    int length;
    mutex_lock(&charger_lock);
    now = ktime_get_ns();
    length = sysfs_emit(buf,
        "phase=%s reason=%s error=%d held_ref=%u mutated=%u usb0_carrier=%d usb_pilot=%u usb_state=%d usb_budget_ma=%u usb_suspended=%d usb_config_ma=%u\n"
        "arm_ns=%llu arm_deadline_ns=%llu start_ns=%llu active_deadline_ns=%llu end_ns=%llu latched_ns=%llu\n"
        "core_phase=%s core_reason=%s core_error=%d core_uncertain=0x%x core_running=%u stop_verified=%u address=0x%04x profile=%u\n"
        "adc_phase=%s adc_error=%d adc_uncertain=0x%x adc_channel=%u adc_request_attempted=%u\n",
        session_phase(), reason, last_error, held_ref, session.mutated, last_carrier,
        session.usb_pilot, last_usb_state, last_usb_ma, last_usb_suspended, last_usb_config_ma,
        armed_ns, session.arm_deadline, started_ns, session.active_deadline, ended_ns, latched_ns,
        charge.phase ? charge.phase : "none", charge.reason ? charge.reason : "none",
        charge.error, charge.uncertain, charge.running, charge.stop_verified, charge.address, (unsigned)charge.profile,
        last_adc.phase ? last_adc.phase : "none", last_adc.error, last_adc.uncertain,
        last_adc.channel, last_adc.request_attempted);
    /* Last existing operation, frozen on uncertainty; never fetch PMIC data here. */
    length += sysfs_emit_at(buf,length,
        "last_io_valid=%u last_io_operation=%s last_io_phase=%s last_io_stage=%s last_io_address=0x%04x last_io_mask=0x%x last_io_expected=0x%x\n"
        "last_io_update_attempted=%u last_io_update_rc_known=%u last_io_update_rc=%d last_io_read_attempted=%u last_io_read_rc_known=%u last_io_read_rc=%d io_failed=%u\n"
        "last_io_observed_known=%u last_io_observed=0x%x last_io_range_known=%u last_io_range_match=%u last_io_compare_known=%u last_io_compare_match=%u\n"
        "clock_previous_known=%u clock_previous_ns=%llu clock_observed_known=%u clock_observed_ns=%llu deadline_known=%u deadline_ns=%llu\n"
        "historical_initial_stop_completed=%u historical_initial_stop_ns=%llu\n",
        charge.diag.valid,charge.diag.operation ? charge.diag.operation : "none",
        charge.diag.phase ? charge.diag.phase : "none",charge.diag.stage ? charge.diag.stage : "none",
        charge.diag.address,charge.diag.mask,charge.diag.expected,
        charge.diag.update_attempted,charge.diag.update_rc_known,charge.diag.update_rc,
        charge.diag.read_attempted,charge.diag.read_rc_known,charge.diag.read_rc,(unsigned)io_failed,
        charge.diag.observed_known,charge.diag.observed,charge.diag.range_known,charge.diag.range_match,
        charge.diag.compare_known,charge.diag.compare_match,
        charge.clock_previous_known,charge.clock_previous,charge.clock_observed_known,charge.clock_observed,
        charge.deadline_known,charge.deadline,charge.initial_stop_completed,charge.initial_stop_ns);
    {
        unsigned i;
        const struct i14_charge_seen *seen[]={&charge.hist_out,&charge.hist_mode};
        const char *name[]={"f66","f7a"};
        for(i=0;i<2;i++)length+=sysfs_emit_at(buf,length,
            "hist_%s seen=%u raw=0x%04x phase=%.32s op=%.6s mask=0x%04x expected=0x%04x lo=%llu hi=%llu known=%u:%u\n",
            name[i],seen[i]->seen,seen[i]->raw,seen[i]->phase ? seen[i]->phase : "none",
            seen[i]->operation ? seen[i]->operation : "none",seen[i]->mask,seen[i]->expected,
            seen[i]->lo,seen[i]->hi,seen[i]->lo_known,seen[i]->hi_known);
    }
    if (latest_valid) {
        age = now >= latest.sampled_ns ? now - latest.sampled_ns : ~0ULL;
        length += sysfs_emit_at(buf, length,
            "last_sample_ns=%llu age_ns=%llu sequence=%llu channels=0x%x fresh_now=%u vbat_mv=%d vbus_mv=%d ntc_mv=%d\n"
            "raw_ch0=%u raw_ch2=%u raw_ch3=%u latch_ch0=0x%04x latch_ch2=0x%04x latch_ch3=0x%04x\n",
            latest.sampled_ns, age, latest.sequence, latest.fresh_mask,
            (unsigned)(age <= I14_CHARGE_FRESH_NS && latest.fresh_mask == 7),
            latest.vbat_mv, latest.vbus_mv, latest.ntc_mv,
            latest_raw[0], latest_raw[1], latest_raw[2], latest_latch[0], latest_latch[1], latest_latch[2]);
        if (temperature_valid)
            length += sysfs_emit_at(buf, length, "temperature_mc=%d\n", temperature_mc);
        else
            length += sysfs_emit_at(buf, length, "temperature=unknown\n");
    } else {
        length += sysfs_emit_at(buf, length, "samples=unknown\n");
    }
    if (first_valid)
        length += sysfs_emit_at(buf, length, "first_sample_ns=%llu first_sequence=%llu first_vbat_mv=%d first_ntc_mv=%d\n",
                                first.sampled_ns, first.sequence, first.vbat_mv, first.ntc_mv);
    mutex_unlock(&charger_lock);
    return length;
}
static DEVICE_ATTR(state, 0400, state_show, NULL);

static ssize_t charger_state_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct regmap *map;
    int ret;
    mutex_lock(&charger_lock);
    if (session.phase == I14_SESSION_LATCHED) { ret = -EUCLEAN; goto unlock; }
    io_failed = 0;
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    ret = IS_ERR(map) ? PTR_ERR(map) : i14_charger_capture(map, fixed_read, buf, PAGE_SIZE);
    if (ret < 0 && held_ref) latch_error("charger-snapshot-failed", ret);
    device_unlock(&pmic_device->dev);
unlock:
    mutex_unlock(&charger_lock);
    return ret;
}
static DEVICE_ATTR(charger_state, 0400, charger_state_show, NULL);

static ssize_t control_store(struct device *dev, struct device_attribute *attr,
                              const char *buf, size_t count)
{
    struct regmap *map;
    unsigned chr, seconds;
    int ret, carrier;
    if (!capable(CAP_SYS_RAWIO)) return -EPERM;
    if (count == sizeof("stop\n") - 1 && !memcmp(buf, "stop\n", count)) seconds = 0;
    else if (count == sizeof("arm-wall-test\n") - 1 && !memcmp(buf, "arm-wall-test\n", count)) seconds = 10;
    else if (count == sizeof("arm-wall\n") - 1 && !memcmp(buf, "arm-wall\n", count)) seconds = 600;
    else if (count == sizeof("usb-pilot-500\n") - 1 && !memcmp(buf, "usb-pilot-500\n", count)) seconds = 5;
    else return -EINVAL;
    mutex_lock(&charger_lock);
    if (shutting_down || session.phase == I14_SESSION_LATCHED || charge.uncertain) {
        ret = -EUCLEAN; goto unlock;
    }
    if (!seconds) {
        if (held_ref) {
            stop_requested = 1;
            mod_delayed_work(system_wq, &charger_work, 0);
        }
        ret = count; goto unlock;
    }
    if (held_ref || (session.phase != I14_SESSION_IDLE && session.phase != I14_SESSION_DONE)) {
        ret = -EBUSY; goto unlock;
    }
    carrier = usb_carrier();
    if (carrier != 1) { ret = carrier < 0 ? carrier : -EPERM; goto unlock; }
    if (seconds == 5) {
        ret = usb_budget_ready();
        if (ret) goto unlock;
    }
    if (!try_module_get(THIS_MODULE)) { ret = -ENODEV; goto unlock; }
    held_ref = 1;
    io_failed = 0;
    device_lock(&pmic_device->dev);
    map = checked_regmap();
    if (IS_ERR(map)) { ret = PTR_ERR(map); latch_error("arm-provider-check", ret); goto unlock_device; }
    ret = fixed_read(map, 0xf48, &chr);
    if (ret) { latch_error("arm-power-read", ret); goto unlock_device; }
    ret = i14_session_arm(&session, ktime_get_ns(), seconds, carrier, chr);
    if (ret) {
        held_ref = 0;
        module_put(THIS_MODULE);
        goto unlock_device;
    }
    memset(&charge, 0, sizeof(charge));
    charge.phase = "armed";
    memset(&last_adc, 0, sizeof(last_adc));
    armed_ns = session.clock_ns;
    started_ns = ended_ns = latched_ns = 0;
    first_valid = latest_valid = temperature_valid = 0;
    last_carrier = carrier;
    last_error = 0;
    stop_requested = 0;
    reason = seconds == 5 ? "usb-pilot-pending-selftest" : "awaiting-mac-detach";
    mod_delayed_work(system_wq, &charger_work, 0);
    ret = count;
unlock_device:
    device_unlock(&pmic_device->dev);
unlock:
    mutex_unlock(&charger_lock);
    return ret;
}
static DEVICE_ATTR(control, 0200, NULL, control_store);

static int charger_reboot(struct notifier_block *nb, unsigned long event, void *command)
{
    struct regmap *map;
    mutex_lock(&charger_lock);
    shutting_down = 1;
    mutex_unlock(&charger_lock);
    cancel_delayed_work_sync(&charger_work);
    mutex_lock(&charger_lock);
    if (held_ref && session.phase != I14_SESSION_LATCHED) {
        if (!session.mutated) {
            finish_session("reboot-before-mutation", 0);
        } else {
            io_failed = 0;
            device_lock(&pmic_device->dev);
            map = checked_regmap();
            if (IS_ERR(map)) latch_error("reboot-provider-check", PTR_ERR(map));
            else stop_session(map, "reboot", 0);
            device_unlock(&pmic_device->dev);
        }
    }
    if (session.phase == I14_SESSION_LATCHED)
        dev_err(charger_device, "reboot with latched uncertainty; no retry, hardware watchdog fallback only\n");
    mutex_unlock(&charger_lock);
    return NOTIFY_DONE;
}
static struct notifier_block charger_notifier = { .notifier_call = charger_reboot };

static int __init charger_init(void)
{
    struct device_node *node, *selected = NULL;
    struct regmap *map;
    unsigned matches = 0;
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
    charger_device = root_device_register("i14-charger");
    if (IS_ERR(charger_device)) { ret = PTR_ERR(charger_device); goto put_pmic; }
    ret = register_reboot_notifier(&charger_notifier);
    if (ret) goto unregister_device;
    ret = device_create_file(charger_device, &dev_attr_state);
    if (ret) goto unregister_notifier;
    ret = device_create_file(charger_device, &dev_attr_charger_state);
    if (ret) goto remove_state;
    ret = device_create_file(charger_device, &dev_attr_control);
    if (!ret) return 0;
    device_remove_file(charger_device, &dev_attr_charger_state);
remove_state:
    device_remove_file(charger_device, &dev_attr_state);
unregister_notifier:
    unregister_reboot_notifier(&charger_notifier);
unregister_device:
    root_device_unregister(charger_device);
put_pmic:
    put_device(&pmic_device->dev);
    return ret;
}

static void __exit charger_exit(void)
{
    /* Normal unload cannot occur while the explicit session holds its ref. */
    mutex_lock(&charger_lock);
    shutting_down = 1;
    mutex_unlock(&charger_lock);
    cancel_delayed_work_sync(&charger_work);
    unregister_reboot_notifier(&charger_notifier);
    device_remove_file(charger_device, &dev_attr_control);
    device_remove_file(charger_device, &dev_attr_charger_state);
    device_remove_file(charger_device, &dev_attr_state);
    root_device_unregister(charger_device);
    if (usb_controller) put_device(usb_controller);
    put_device(&pmic_device->dev);
}
module_init(charger_init);
module_exit(charger_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("I14 exact-board bounded wall trial and allocated-USB pilot");
