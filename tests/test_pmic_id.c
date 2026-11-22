#define I14_PMIC_ID_TEST 1
#include "../tools/i14-pmic-id.c"
#include <assert.h>

static uint64_t now, step;
static unsigned writes, transaction, phase, fault;
static uint32_t trace_offset[8], trace_value[8];
static int monotonic_ns(uint64_t *value) { *value=now; now+=step; return 0; }
static int poll_pause(void) { return 0; }
static uint32_t mmio_read(unsigned offset)
{
    assert(offset==MUX_SEL || offset==WRAP_EN || offset==WACS_EN || offset==INIT_DONE || offset==RDATA);
    if (offset==MUX_SEL) return 0;
    if (offset==WRAP_EN || offset==WACS_EN) return 1;
    if (offset==INIT_DONE) return fault==2 ? 0 : 1;
    if (fault==1 && !writes) return INIT_BIT|SYNC_BIT|(6u<<16);
    if (fault==2) return SYNC_BIT;
    if (fault==3 && phase==1) return INIT_BIT|(2u<<16);
    if (phase==1) {
        uint16_t value=transaction==2 ? 0x1234 : 0x0030;
        if (fault==4 && transaction==3) value=0x0031;
        return INIT_BIT|(6u<<16)|value;
    }
    return INIT_BIT|SYNC_BIT;
}
static void mmio_write(unsigned offset, uint32_t value)
{
    assert(writes<8);
    trace_offset[writes]=offset; trace_value[writes++]=value;
    if (offset==CMD) {
        assert(!(value&(1u<<31)) && !(value&0xffff));
        ++transaction; phase=1;
    } else { assert(offset==VLDCLR && value==1 && phase==1); phase=0; }
}
static int run(unsigned mode)
{
    now=0; step=mode==3 ? 1000000 : 1000;
    writes=transaction=phase=0; fault=mode;
    struct cid_result result={0}; const char *error=NULL;
    int status=identify(&result,&error);
    if (!status) assert(result.hw==0x30 && result.sw==0x1234 && result.repeat==0x30);
    else assert(error);
    return status;
}
int main(void)
{
    assert(run(0)==0 && writes==6);
    const uint32_t commands[]={0x01000000,1,0x01010000,1,0x01000000,1};
    for (unsigned i=0;i<6;++i) {
        assert(trace_offset[i]==(i%2 ? VLDCLR : CMD));
        assert(trace_value[i]==commands[i]);
    }
    assert(run(1)<0 && writes==0);
    assert(run(2)<0 && writes==0);
    assert(run(3)<0 && writes==1); /* no acknowledge or retry after timeout */
    assert(run(4)<0 && writes==6); /* mismatch after completed third transaction */
    puts("PASS: fixed HW/SW/HW protocol, read commands, idle/init refusal, timeout/no retry, repeat mismatch");
    return 0;
}
