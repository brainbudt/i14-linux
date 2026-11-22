/* Offline production snapshot/decoder test; no PMIC accesses. */
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include "../kernel/i14-battery-snapshot.h"
struct fake { unsigned calls, fail_at; };
static int read_latch(void *context, unsigned address, unsigned *value)
{
    struct fake *f = context;
    static const unsigned addresses[] = { 0x0f48, 0x0e2e, 0x0e32, 0x0e94, 0x0eb0, 0x028a };
    static const unsigned values[] = { 0x0038, 0xded1, 0x5ed1, 0x8000, 0x0080, 0x0104 };
    assert(f->calls < 6 && address == addresses[f->calls]);
    ++f->calls;
    if (f->calls == f->fail_at) return -EIO;
    *value = values[f->calls - 1];
    return 0;
}
int main(void)
{
    struct fake f = {0};
    struct i14_battery_snapshot result = {0};
    assert(i14_battery_read(&f, read_latch, &result) == 0);
    assert(f.calls == 6 && result.chr_con0 == 0x38);
    assert(result.auxadc_con0 == 0x8000 && result.auxadc_con12 == 0x80 && result.top_clksq == 0x104);
    assert(i14_battery_ck_aon(result.auxadc_con0) && !i14_battery_ck_aon(0x7fff));
    assert(i14_battery_vbuf_en(result.auxadc_con12) && !i14_battery_vbuf_en(0xff7f));
    assert(i14_battery_aux_ap_en(result.top_clksq) && !i14_battery_aux_ap_en(0xfffb));
    assert(i14_battery_aux_ap_mode(result.top_clksq) && !i14_battery_aux_ap_mode(0xfeff));
    assert(i14_battery_chgdet(result.chr_con0) == 1);
    assert(i14_battery_chr_en(result.chr_con0) == 1);
    assert(i14_battery_csdac_en(result.chr_con0) == 1);
    assert(!i14_battery_chgdet(0) && !i14_battery_chr_en(0) && !i14_battery_csdac_en(0));
    assert(i14_battery_ready(result.ch0) == 1 && !i14_battery_ready(result.ch1));
    assert(i14_battery_mv(result.ch0) == 4000);
    assert(i14_battery_mv(result.ch1) == -1);
    assert(i14_battery_mv(0x8000) == 0 && i14_battery_mv(0xffff) == 5399);
    for (unsigned fail = 1; fail <= 6; ++fail) {
        f = (struct fake){ .fail_at = fail };
        result = (struct i14_battery_snapshot){ .chr_con0 = 0x1234, .ch0 = 0x5678, .ch1 = 0x9abc };
        assert(i14_battery_read(&f, read_latch, &result) == -EIO);
        assert(f.calls == fail && result.chr_con0 == 0x1234 && result.ch0 == 0x5678 && result.ch1 == 0x9abc);
    }
    f = (struct fake){0};
    result = (struct i14_battery_snapshot){0};
    puts("battery snapshot: fixed read-only addresses, error-stop, charger bits, readiness and mV PASS");
}
