/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef I14_BATTERY_TEMPERATURE_H
#define I14_BATTERY_TEMPERATURE_H

/* Stock NTC resistance table and compiled defaults (16.9 kohm to 1800 mV).
 * Returns 0 on conversion; -1 means unknown and leaves *out_mc untouched.
 * Only the calibrated -20..60 C interval is accepted, without extrapolation.
 */
static inline int i14_battery_temperature_mc(int millivolts,int *out_mc)
{
    static const int resistance[]={68237,53650,42506,33892,27219,22021,
        17926,14674,12081,10000,8315,6948,5834,4917,4161,3535,3014};
    long long numerator,denominator;
    int i;

    if(!out_mc || millivolts<=0 || millivolts>=1800)
        return -1;
    /* Keep resistance rational; rounding ohms first loses ADC precision. */
    numerator=16900LL*millivolts;
    denominator=1800LL-millivolts;
    if(numerator>resistance[0]*denominator ||
       numerator<resistance[16]*denominator)
        return -1;
    for(i=0;i<16;i++) {
        if(numerator>=resistance[i+1]*denominator) {
            long long delta=resistance[i]*denominator-numerator;
            long long span=(resistance[i]-resistance[i+1])*denominator;

            *out_mc=-20000+i*5000+(int)(delta*5000LL/span);
            return 0;
        }
    }
    return -1;
}

/* Stateless, conservative probe gate; these are not factory hysteresis limits.
 * -1 unknown, 0 outside 10..40 C, 1 within the inclusive probe interval.
 */
static inline int i14_battery_temperature_probe_guard(int conversion_rc,int temperature_mc)
{
    if(conversion_rc || temperature_mc<-20000 || temperature_mc>60000)
        return -1;
    return temperature_mc>=10000 && temperature_mc<=40000;
}
#endif
