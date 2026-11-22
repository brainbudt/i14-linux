#define I14_PWRAP_STATUS_TEST 1
#include "../tools/i14-pwrap-status.c"
#include <assert.h>
static unsigned reads, pauses, fail_pause;
static uint32_t mmio_read(unsigned offset)
{
    const unsigned allowed[] = {0xf4,0xc4,0xc8,0xf0,0xec,0xe4};
    assert(reads<18 && offset==allowed[reads%6]);
    return ++reads;
}
static int pause_snapshot(void) { ++pauses; return fail_pause ? -1 : 0; }
int main(void)
{
    uint32_t values[3][6];
    assert(snapshots(values)==0 && reads==18 && pauses==2);
    for (unsigned i=0;i<18;++i) assert(values[i/6][i%6]==i+1);
    reads=pauses=0; fail_pause=1;
    assert(snapshots(values)<0 && reads==6 && pauses==1);
    puts("PASS: exactly six allowlisted AP reads per snapshot, three snapshots/two pauses, no MMIO-write interface, pause failure stops");
    return 0;
}
