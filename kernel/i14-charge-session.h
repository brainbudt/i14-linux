/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef I14_CHARGE_SESSION_H
#define I14_CHARGE_SESSION_H
enum i14_session_phase {
    I14_SESSION_IDLE, I14_SESSION_WAIT_DETACH, I14_SESSION_WAIT_WALL,
    I14_SESSION_ACTIVE, I14_SESSION_DONE, I14_SESSION_LATCHED
};
enum i14_session_action { I14_SESSION_WAIT, I14_SESSION_START, I14_SESSION_CANCEL, I14_SESSION_STOP };
struct i14_charge_session {
    enum i14_session_phase phase;
    unsigned mutated, usb_pilot;
    unsigned long long clock_ns, arm_deadline, active_deadline, duration_ns;
};
static inline int i14_session_arm(struct i14_charge_session *s, unsigned long long now,
                                   unsigned seconds, int carrier, unsigned chr)
{
    if (s->phase == I14_SESSION_LATCHED) return -EUCLEAN;
    if (s->phase != I14_SESSION_IDLE && s->phase != I14_SESSION_DONE) return -EBUSY;
    if (seconds != 5 && seconds != 10 && seconds != 600) return -EINVAL;
    if (carrier != 1 || (chr & 0x30) != 0x20) return -EPERM;
    if (now > ~0ULL - 120000000000ULL) return -EOVERFLOW;
    s->usb_pilot = seconds == 5;
    s->phase = s->usb_pilot ? I14_SESSION_WAIT_WALL : I14_SESSION_WAIT_DETACH;
    s->mutated = 0;
    s->clock_ns = now;
    s->arm_deadline = now + (s->usb_pilot ? 10000000000ULL : 120000000000ULL);
    s->active_deadline = 0;
    s->duration_ns = (unsigned long long)seconds * 1000000000ULL;
    return 0;
}
static inline int i14_session_observe(struct i14_charge_session *s, unsigned long long now,
                                       int carrier, unsigned chr)
{
    if (now < s->clock_ns) return -EIO;
    s->clock_ns = now;
    if (s->usb_pilot && s->phase == I14_SESSION_WAIT_WALL)
        return now >= s->arm_deadline || carrier != 1 || (chr & 0x30) != 0x20 ?
               I14_SESSION_CANCEL : I14_SESSION_START;
    if (s->phase == I14_SESSION_WAIT_DETACH || s->phase == I14_SESSION_WAIT_WALL) {
        if (now >= s->arm_deadline || carrier < 0 || (chr & 0x10)) return I14_SESSION_CANCEL;
        if (s->phase == I14_SESSION_WAIT_DETACH) {
            if (!(chr & 0x20)) s->phase = I14_SESSION_WAIT_WALL;
            return I14_SESSION_WAIT;
        }
        if (!(chr & 0x20)) return I14_SESSION_WAIT;
        return carrier == 0 ? I14_SESSION_START : I14_SESSION_CANCEL;
    }
    if (s->phase == I14_SESSION_ACTIVE)
        return now >= s->active_deadline || carrier != (s->usb_pilot ? 1 : 0) || (chr & 0x30) != 0x30 ?
               I14_SESSION_STOP : I14_SESSION_WAIT;
    return -EINVAL;
}
static inline int i14_session_started(struct i14_charge_session *s, unsigned long long now)
{
    if (s->phase != I14_SESSION_WAIT_WALL || now < s->clock_ns || now >= s->arm_deadline)
        return -ETIME;
    if (now > ~0ULL - s->duration_ns) return -EOVERFLOW;
    s->phase = I14_SESSION_ACTIVE;
    s->clock_ns = now;
    s->active_deadline = now + s->duration_ns;
    return 0;
}
static inline int i14_session_finish(struct i14_charge_session *s, unsigned uncertain, unsigned off)
{
    if (s->phase == I14_SESSION_LATCHED) return 0;
    if (uncertain || (s->mutated && !off)) {
        s->phase = I14_SESSION_LATCHED;
        return 0;
    }
    s->phase = I14_SESSION_DONE;
    return 1;
}
static inline int i14_session_usb_budget(int configured, unsigned milliamps)
{
    return configured == 1 && milliamps == 500;
}
static inline int i14_session_observe_usb(struct i14_charge_session *s,
    unsigned long long now, int carrier, unsigned chr, int configured, unsigned milliamps)
{
    int action;
    if (!s->usb_pilot) return -EINVAL;
    action = i14_session_observe(s, now, carrier, chr);
    if (action < 0 || action == I14_SESSION_CANCEL || action == I14_SESSION_STOP) return action;
    if (!i14_session_usb_budget(configured, milliamps))
        return s->phase == I14_SESSION_ACTIVE ? I14_SESSION_STOP : I14_SESSION_CANCEL;
    return action;
}
static inline int i14_session_vbus(unsigned raw, int *mv)
{
    unsigned adc_mv;
    if (raw > 0xfff || !mv) return -ERANGE;
    /* Own stock truncates the 1800-mV ADC stage before the 330/39 divider. */
    adc_mv = raw * 1800U / 4096U;
    *mv = (int)(adc_mv * 369U / 39U);
    return 0;
}
#endif
