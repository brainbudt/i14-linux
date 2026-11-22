/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef I14_BATTERY_SAMPLE_H
#define I14_BATTERY_SAMPLE_H
struct i14_adc_io {
    void *context;
    int (*read)(void *, unsigned, unsigned *);
    int (*update)(void *, unsigned, unsigned, unsigned);
    int (*write)(void *, unsigned, unsigned);
    unsigned long long (*now_ns)(void *);
    void (*pause)(void *);
};
struct i14_adc_result {
    unsigned original[3], originals_valid, uncertain, restored, request_attempted, saw_zero, latch;
    unsigned complete, address;
    const char *phase;
    unsigned long long deadline, sampled_ns;
    int error;
    unsigned channel, raw;
    int mv;
};
static int i14_adc_deadline(struct i14_adc_io *io, struct i14_adc_result *run)
{
    return io->now_ns(io->context) >= run->deadline ? -ETIMEDOUT : 0;
}
static int i14_adc_read(struct i14_adc_io *io, struct i14_adc_result *run,
                         unsigned address, unsigned *value, const char *phase)
{
    int ret;
    run->address = address; run->phase = phase;
    ret = i14_adc_deadline(io, run);
    if (ret) return ret;
    ret = io->read(io->context, address, value);
    if (ret) return ret;
    if (*value > 0xffff) return -EIO;
    return i14_adc_deadline(io, run);
}
static int i14_adc_idle(struct i14_adc_io *io, struct i14_adc_result *run, unsigned request_bit)
{
    unsigned value;
    int ret = i14_adc_read(io, run, 0x0e88, &value, "guard-request");
    if (ret) return ret;
    if (value) return -EBUSY;
    ret = i14_adc_read(io, run, 0x0e84, &value,
                       request_bit == 1 ? "guard-ch0-busy" : "guard-channel-busy");
    if (ret) return ret;
    if (value & request_bit) return -EBUSY;
    ret = i14_adc_read(io, run, 0x0e86, &value, "guard-share-busy");
    return ret ? ret : (value & 0x80) ? -EBUSY : 0;
}
static int i14_adc_update(struct i14_adc_io *io, struct i14_adc_result *run,
                           unsigned index, unsigned address, unsigned mask,
                           unsigned value, const char *phase)
{
    int ret;
    run->address = address; run->phase = phase;
    ret = i14_adc_deadline(io, run);
    if (ret) return ret;
    run->uncertain |= 1U << index;
    ret = io->update(io->context, address, mask, value);
    return ret ? ret : i14_adc_deadline(io, run);
}
static int i14_adc_sample_channel(struct i14_adc_io *io, unsigned channel,
                                  const char *command, struct i14_adc_result *run)
{
    static const unsigned addresses[] = { 0x0e94, 0x0eb0, 0x028a };
    static const unsigned masks[] = { 0x8000, 0x0080, 0x0100 };
    unsigned i, value, poll, latch_address, data_mask, scale, divisor, request_bit;
    int ret;

    memset(run, 0, sizeof(*run));
    run->phase = "command";
    if (!command || strcmp(command, "sample")) { ret = -EINVAL; goto failed; }
    run->phase = "channel";
    switch (channel) {
    case 0:
        latch_address = 0x0e2e; data_mask = 0x7fff; scale = 5400; divisor = 32768;
        break;
    case 2: case 3:
        latch_address = channel == 2 ? 0x0e04 : 0x0e06;
        data_mask = 0xfff; scale = 1800; divisor = 4096;
        break;
    default:
        ret = -EINVAL; goto failed;
    }
    run->channel = channel;
    request_bit = 1U << channel;
    run->deadline = io->now_ns(io->context) + 100000000ULL;
    ret = i14_adc_idle(io, run, request_bit);
    if (ret) goto failed;
    for (i = 0; i < 3; i++) {
        ret = i14_adc_read(io, run, addresses[i], &run->original[i], "original");
        if (ret) goto failed;
        run->originals_valid |= 1U << i;
    }
    for (i = 0; i < 3; i++) {
        ret = i14_adc_idle(io, run, request_bit);
        if (ret) goto failed;
        ret = i14_adc_update(io, run, i, addresses[i], masks[i], masks[i], "enable");
        if (ret) goto failed;
        ret = i14_adc_read(io, run, addresses[i], &value, "verify-enable");
        if (ret) goto failed;
        if ((value & masks[i]) != masks[i]) { ret = -EIO; goto failed; }
    }
    ret = i14_adc_idle(io, run, request_bit);
    if (ret) goto failed;
    run->phase = "request"; run->address = 0x0e8a;
    ret = i14_adc_deadline(io, run);
    if (ret) goto failed;
    run->request_attempted = 1;
    ret = io->write(io->context, 0x0e8a, request_bit);
    if (ret) goto failed;
    ret = i14_adc_deadline(io, run);
    if (ret) goto failed;
    for (poll = 0; poll < 2000; poll++) {
        ret = i14_adc_read(io, run, latch_address, &value,
                             run->saw_zero ? "wait-ready-one" : "wait-ready-zero");
        if (ret) goto failed;
        if (data_mask == 0xfff && (value & 0x7000)) { ret = -EIO; goto failed; }
        if (!(value & 0x8000)) run->saw_zero = 1;
        else if (run->saw_zero) break;
        io->pause(io->context);
    }
    if (poll == 2000) { ret = -ETIMEDOUT; goto failed; }
    run->latch = value;
    run->sampled_ns = io->now_ns(io->context);
    ret = i14_adc_idle(io, run, request_bit);
    if (ret) goto failed;
    /* Restore only the three touched bits, in reverse setup order. A failed
     * transaction stops here: do not add speculative cleanup or a new request. */
    for (i = 3; i-- > 0;) {
        ret = i14_adc_update(io, run, i, addresses[i], masks[i],
                                run->original[i] & masks[i], "restore");
        if (ret) goto failed;
        ret = i14_adc_read(io, run, addresses[i], &value, "verify-restore");
        if (ret) goto failed;
        if ((value & masks[i]) != (run->original[i] & masks[i])) { ret = -EIO; goto failed; }
        run->restored |= 1U << i;
        run->uncertain &= ~(1U << i);
    }
    run->phase = "complete";
    run->raw = run->latch & data_mask;
    run->mv = (int)(run->raw * scale / divisor);
    run->complete = 1;
    return 0;
failed:
    run->error = ret;
    return ret;
}
static int i14_adc_sample(struct i14_adc_io *io, const char *command, struct i14_adc_result *run)
{
    return i14_adc_sample_channel(io, 0, command, run);
}
#endif
