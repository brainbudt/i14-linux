/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef I14_CHARGER_SNAPSHOT_H
#define I14_CHARGER_SNAPSHOT_H
/* Field proof: artifacts/charging-lk-parameters-20261030/read-only-charger-allowlist.json
 * SHA256 54edeb8b582f2729ebe3f7e23e7a7964ef0e3f20a2b4612ecefddeaa38a82bc7. */
static int i14_charger_capture(void *context,
                               int (*read)(void *, unsigned, unsigned *),
                               char *buffer, size_t capacity)
{
    static const unsigned addresses[] = {
        0x0f48, 0x0f4a, 0x0f4c, 0x0f4e, 0x0f50, 0x0f52, 0x0f54,
        0x0f56, 0x0f62, 0x0f66, 0x0f68, 0x0f70, 0x0f72, 0x0f74,
        0x0f76, 0x0f78, 0x0f7a, 0x0f9a, 0x0f9e
    };
    unsigned values[19];
    char text[1024];
    size_t used = 0;
    int ret;
    unsigned i;

    for (i = 0; i < 19; ++i) {
        ret = read(context, addresses[i], &values[i]);
        if (ret) return ret < 0 ? ret : -EIO;
        if (values[i] > 0xffff) return -EIO;
    }
    for (i = 0; i < 19; ++i) {
        ret = snprintf(text + used, sizeof(text) - used,
                       "raw[%04x]=0x%04x\n", addresses[i], values[i]);
        if (ret < 0) return -EIO;
        if ((size_t)ret >= sizeof(text) - used) return -EOVERFLOW;
        used += ret;
    }
    ret = snprintf(text + used, sizeof(text) - used,
        "cv_code=%u cs_code=%u\n"
        "ov_en=%u ov_code=%u ov_det=%u\n"
        "wdt_td_code=%u wdt_en=%u wdt_wr=%u\n"
        "wdt_int_en=%u wdt_flag_wr=%u wdt_out=%u\n"
        "csdac_mode=%u hwcv_en=%u ulc_det_en=%u\n",
        values[3] & 0x1f, values[4] & 0xf,
        values[6] & 1, (values[6] >> 1) & 7, (values[6] >> 6) & 1,
        values[8] & 0xf, (values[8] >> 4) & 1, (values[8] >> 8) & 1,
        values[9] & 1, (values[9] >> 1) & 1, (values[9] >> 2) & 1,
        (values[16] >> 2) & 1, (values[16] >> 6) & 1, (values[16] >> 7) & 1);
    if (ret < 0) return -EIO;
    if ((size_t)ret >= sizeof(text) - used) return -EOVERFLOW;
    used += ret;
    if (used >= capacity) return -ENOSPC;
    memcpy(buffer, text, used + 1);
    return (int)used;
}
#endif
