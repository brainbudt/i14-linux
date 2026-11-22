/* Exercise the production read/format flow with no PMIC access. */
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include "../kernel/i14-charger-snapshot.h"

static const unsigned expected_addresses[] = {
    0x0f48, 0x0f4a, 0x0f4c, 0x0f4e, 0x0f50, 0x0f52, 0x0f54,
    0x0f56, 0x0f62, 0x0f66, 0x0f68, 0x0f70, 0x0f72, 0x0f74,
    0x0f76, 0x0f78, 0x0f7a, 0x0f9a, 0x0f9e
};
struct fake {
    unsigned calls, fail_at, overflow_at, values[19];
    int error;
};
static int read_register(void *context, unsigned address, unsigned *value)
{
    struct fake *f = context;
    assert(f->calls < 19 && address == expected_addresses[f->calls]);
    ++f->calls;
    if (f->calls == f->fail_at) return f->error;
    *value = f->calls == f->overflow_at ? 0x10000 : f->values[f->calls - 1];
    return 0;
}

static void check_zero_decodes(const char *text)
{
    assert(strstr(text, "cv_code=0 cs_code=0\n"));
    assert(strstr(text, "ov_en=0 ov_code=0 ov_det=0\n"));
    assert(strstr(text, "wdt_td_code=0 wdt_en=0 wdt_wr=0\n"));
    assert(strstr(text, "wdt_int_en=0 wdt_flag_wr=0 wdt_out=0\n"));
    assert(strstr(text, "csdac_mode=0 hwcv_en=0 ulc_det_en=0\n"));
}

int main(void)
{
    struct fake f = {0};
    char output[4096], untouched[4096], line[64];
    int length = i14_charger_capture(&f, read_register, output, sizeof(output));
    assert(length > 0 && length < 1024 && length < 4096 && f.calls == 19);
    assert((size_t)length == strlen(output));
    for (unsigned i = 0; i < 19; ++i) {
        snprintf(line, sizeof(line), "raw[%04x]=0x0000\n", expected_addresses[i]);
        assert(strstr(output, line));
    }
    check_zero_decodes(output);
    assert(!strstr(output, "mV") && !strstr(output, "mA"));
    for (unsigned i = 0; i < 19; ++i) f.values[i] = 0xffff;
    f.calls = 0;
    length = i14_charger_capture(&f, read_register, output, sizeof(output));
    assert(length > 0 && length < 1024 && f.calls == 19);
    assert(strstr(output, "cv_code=31 cs_code=15\n"));
    assert(strstr(output, "ov_en=1 ov_code=7 ov_det=1\n"));
    assert(strstr(output, "wdt_td_code=15 wdt_en=1 wdt_wr=1\n"));
    assert(strstr(output, "wdt_int_en=1 wdt_flag_wr=1 wdt_out=1\n"));
    assert(strstr(output, "csdac_mode=1 hwcv_en=1 ulc_det_en=1\n"));
    f.values[3] = 0xffe0; f.values[4] = 0xfff0;
    f.values[6] = 0xffb0; f.values[8] = 0xfee0;
    f.values[9] = 0xfff8; f.values[16] = 0xff3b;
    f.calls = 0;
    assert(i14_charger_capture(&f, read_register, output, sizeof(output)) > 0);
    check_zero_decodes(output);

    memset(untouched, 0xa5, sizeof(untouched));
    for (unsigned position = 1; position <= 19; ++position) {
        for (unsigned kind = 0; kind < 3; ++kind) {
            f = (struct fake){0};
            if (kind == 0) { f.fail_at = position; f.error = -EIO; }
            if (kind == 1) { f.overflow_at = position; }
            if (kind == 2) { f.fail_at = position; f.error = 1; }
            memcpy(output, untouched, sizeof(output));
            assert(i14_charger_capture(&f, read_register, output, sizeof(output)) == -EIO);
            assert(f.calls == position && !memcmp(output, untouched, sizeof(output)));
        }
    }
    f = (struct fake){0};
    length = i14_charger_capture(&f, read_register, output, sizeof(output));
    for (unsigned capacity = 0; capacity <= (unsigned)length; ++capacity) {
        f.calls = 0;
        memcpy(output, untouched, sizeof(output));
        assert(i14_charger_capture(&f, read_register, output, capacity) == -ENOSPC);
        assert(f.calls == 19 && !memcmp(output, untouched, sizeof(output)));
    }
    f.calls = 0;
    memcpy(output, untouched, sizeof(output));
    assert(i14_charger_capture(&f, read_register, output, (size_t)length + 1) == length);
    assert(output[length] == 0 && (unsigned char)output[length + 1] == 0xa5);
    puts("PASS: fixed 19 read-only registers; atomic failure/16-bit guards; proven code/bit decodes; page-bounded output");
    return 0;
}
