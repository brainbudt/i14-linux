#define I14_BACKLIGHT_STATUS_TEST 1
#include "../tools/i14-backlight-status.c"
#include <assert.h>
static unsigned count;
static uint32_t mmio_read(unsigned offset)
{
    const unsigned wanted[]={0x00,0x08,0x10,0x14};
    assert(count<4 && offset==wanted[count]);
    return ++count;
}
int main(void)
{
    uint32_t v[4];
    backlight_snapshot(v);
    assert(count==4);
    for(unsigned i=0;i<4;i++) assert(v[i]==i+1);
    struct backlight_counts c=backlight_decode((uint32_t[]){1,0,0,0x040003ff});
    assert(c.enabled==1 && c.divisor==1 && c.period_ticks==1024 && c.high_ticks==1024);
    c=backlight_decode((uint32_t[]){0,1,0x03ff0000,0x1fff0fff});
    assert(c.enabled==0 && c.divisor==1024 && c.period_ticks==4096 && c.high_ticks==8191);
    c=backlight_decode((uint32_t[]){2,0,0xfc00ffff,0xe000f000});
    assert(c.enabled==0 && c.divisor==1 && c.period_ticks==1 && c.high_ticks==0);
    puts("PASS: four allowlisted PWM reads; divider, period+1, enable and duty masks");
}
