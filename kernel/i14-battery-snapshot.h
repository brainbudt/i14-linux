/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef I14_BATTERY_SNAPSHOT_H
#define I14_BATTERY_SNAPSHOT_H
struct i14_battery_snapshot {
    unsigned chr_con0, ch0, ch1;
    unsigned auxadc_con0, auxadc_con12, top_clksq;
};
static int i14_battery_read(void *context, int (*read)(void *, unsigned, unsigned *),
                            struct i14_battery_snapshot *result)
{
    struct i14_battery_snapshot sample;
    int ret = read(context, 0x0f48, &sample.chr_con0);
    if (ret) return ret;
    ret = read(context, 0x0e2e, &sample.ch0);
    if (ret) return ret;
    ret = read(context, 0x0e32, &sample.ch1);
    if (ret) return ret;
    ret = read(context, 0x0e94, &sample.auxadc_con0);
    if (ret) return ret;
    ret = read(context, 0x0eb0, &sample.auxadc_con12);
    if (ret) return ret;
    ret = read(context, 0x028a, &sample.top_clksq);
    if (ret) return ret;
    if (sample.chr_con0 > 0xffff || sample.ch0 > 0xffff || sample.ch1 > 0xffff ||
        sample.auxadc_con0 > 0xffff || sample.auxadc_con12 > 0xffff || sample.top_clksq > 0xffff)
        return -EIO;
    *result = sample;
    return 0;
}
static unsigned i14_battery_chgdet(unsigned value) { return (value >> 5) & 1; }
static unsigned i14_battery_chr_en(unsigned value) { return (value >> 4) & 1; }
static unsigned i14_battery_csdac_en(unsigned value) { return (value >> 3) & 1; }
static unsigned i14_battery_ready(unsigned value) { return (value >> 15) & 1; }
static unsigned i14_battery_ck_aon(unsigned value) { return (value >> 15) & 1; }
static unsigned i14_battery_vbuf_en(unsigned value) { return (value >> 7) & 1; }
static unsigned i14_battery_aux_ap_en(unsigned value) { return (value >> 2) & 1; }
static unsigned i14_battery_aux_ap_mode(unsigned value) { return (value >> 8) & 1; }
/* Ready permits scaling a latched result; it does not establish its age. */
static int i14_battery_mv(unsigned value)
{
    return i14_battery_ready(value) ? (int)((value & 0x7fff) * 5400 / 32768) : -1;
}
#endif
