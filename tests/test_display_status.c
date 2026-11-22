#define I14_DISPLAY_STATUS_TEST 1
#include "../tools/i14-display-status.c"
#include <assert.h>
static unsigned count;
static uint32_t mmio_read(unsigned offset)
{
    const unsigned wanted[]={0x00c,0x020,0x02c,0x030,0x038,0x044,0xf40,0x050,0x058,0x064,0xf60,0x070,0x078,0x084,0xf80,0x090,0x098,0x0a4,0xfa0};
    assert(count<19 && offset==wanted[count]);
    return ++count;
}
int main(void)
{
    uint32_t values[19];
    display_snapshot(values);
    assert(count==19);
    for(unsigned i=0;i<19;i++) assert(values[i]==i+1);
    uint32_t frame[19]={1,0x064002d0,9};
    frame[15]=0x21ff;frame[16]=0x064002d0;frame[17]=2880;frame[18]=0xbf48e000;
    assert(capture_ready(frame));
    frame[18]=0xbf010000;assert(!capture_ready(frame));frame[18]=0xbf48e000;
    frame[17]=2944;assert(!capture_ready(frame));frame[17]=2880;
    frame[15]=0x10021ff;assert(!capture_ready(frame));frame[15]=0x21ff;
    frame[2]=1;assert(!capture_ready(frame));
    puts("PASS: only 19 allowlisted OVL configuration/status reads, no MMIO store interface");
}
