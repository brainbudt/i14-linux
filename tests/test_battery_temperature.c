#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include "../kernel/i14-battery-temperature.h"

/* Independent stock fixture: ntc-table.json, not the implementation table. */
static const int resistance[]={68237,53650,42506,33892,27219,22021,17926,
    14674,12081,10000,8315,6948,5834,4917,4161,3535,3014};

int main(void)
{
    int output=123456;
    assert(i14_battery_temperature_mc(669,&output)==0);
    assert(output>=24900 && output<=25100);
    for(int i=0;i<17;i++) {
        long long numerator=1800LL*resistance[i],denominator=16900+resistance[i];
        int mv=(int)((numerator+denominator/2)/denominator);
        /* Endpoint samples round inward: extrapolation remains an error. */
        if(i==0) mv=(int)(numerator/denominator);
        if(i==16) mv=(int)((numerator+denominator-1)/denominator);
        assert(i14_battery_temperature_mc(mv,&output)==0);
        int error=output-(-20000+i*5000);
        assert(error>=-150 && error<=150);
    }
    int previous=INT_MAX,valid=0;
    for(int mv=1;mv<1800;mv++) {
        output=123456;
        int rc=i14_battery_temperature_mc(mv,&output);
        long double ohms=16900.L*mv/(1800-mv);
        if(ohms>resistance[0] || ohms<resistance[16]) {
            assert(rc==-1 && output==123456);continue;
        }
        assert(rc==0 && output<=previous && output>=-20000 && output<=60000);
        previous=output;valid++;
        for(int i=0;i<16;i++) if(ohms<=resistance[i] && ohms>=resistance[i+1]) {
            long double reference=-20000+i*5000+
                (resistance[i]-ohms)*5000/(resistance[i]-resistance[i+1]);
            assert(reference-output>-0.001L && reference-output<1.001L);
            break;
        }
    }
    assert(valid==1170); /* Exactly 273..1442 mV, no endpoint clamping. */
    const int bad[]={INT_MIN,-1,0,1,200,272,1443,1600,1799,1800,1801,INT_MAX};
    for(unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);i++) {
        output=123456;assert(i14_battery_temperature_mc(bad[i],&output)==-1);
        assert(output==123456);
    }
    assert(i14_battery_temperature_mc(669,NULL)==-1);
    assert(i14_battery_temperature_mc(1300,&output)==0 && output<0);
    assert(i14_battery_temperature_mc(350,&output)==0 && output>40000);
    assert(i14_battery_temperature_probe_guard(0,9999)==0);
    assert(i14_battery_temperature_probe_guard(0,10000)==1);
    assert(i14_battery_temperature_probe_guard(0,40000)==1);
    assert(i14_battery_temperature_probe_guard(0,40001)==0);
    assert(i14_battery_temperature_probe_guard(-1,25000)==-1);
    assert(i14_battery_temperature_probe_guard(1,25000)==-1);
    assert(i14_battery_temperature_probe_guard(0,INT_MIN)==-1);
    assert(i14_battery_temperature_probe_guard(0,INT_MAX)==-1);
    puts("PASS stock NTC: 17 calibration points, exhaustive mV sweep, faults and 10..40 C guard");
    return 0;
}
