/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef I14_PMIC_OBSERVER_CORE_H
#define I14_PMIC_OBSERVER_CORE_H

/* Fixed translation-unit profiles, never a runtime selector. Neither is a
 * silicon read-side-effect, stable handoff or physical charging guarantee. */
#ifdef I14_OBSERVER_FIXED_PAIR
#define I14_OBSERVER_PROFILE_NAME "fixed-pair-f48-f7a-v1"
#define I14_OBSERVER_SYSFS_NAME "i14_pmic_observer_pair"
#define I14_OBSERVER_WORDS 2
static const unsigned i14_observer_addresses[I14_OBSERVER_WORDS] = {
    0x0f48, 0x0f7a
};
#else
/* Exact ordinary-word profile.
 * Source profile SHA256: 54edeb8b582f2729ebe3f7e23e7a7964ef0e3f20a2b4612ecefddeaa38a82bc7. */
#define I14_OBSERVER_PROFILE_NAME "default19-v1"
#define I14_OBSERVER_SYSFS_NAME "i14_pmic_observer"
#define I14_OBSERVER_WORDS 19
static const unsigned i14_observer_addresses[I14_OBSERVER_WORDS] = {
    0x0f48, 0x0f4a, 0x0f4c, 0x0f4e, 0x0f50, 0x0f52, 0x0f54,
    0x0f56, 0x0f62, 0x0f66, 0x0f68, 0x0f70, 0x0f72, 0x0f74,
    0x0f76, 0x0f78, 0x0f7a, 0x0f9a, 0x0f9e
};
#endif
#define I14_OBSERVER_BUDGET_NS 1000000000ULL

enum i14_observer_phase {
    I14_OBSERVER_IDLE, I14_OBSERVER_METADATA, I14_OBSERVER_READING,
    I14_OBSERVER_COMPLETE, I14_OBSERVER_METADATA_ERROR,
    I14_OBSERVER_READ_ERROR, I14_OBSERVER_RANGE_ERROR,
    I14_OBSERVER_CLOCK_ERROR, I14_OBSERVER_TIME_BUDGET,
    I14_OBSERVER_ACTIVE_CONFIG
};

struct i14_observer_state {
    enum i14_observer_phase phase;
    unsigned attempted, valid_mask, read_calls, failed_index;
    unsigned failed_raw, failed_raw_valid;
    int rc;
    unsigned long long started_ns, ended_ns;
    unsigned values[I14_OBSERVER_WORDS];
    unsigned long long read_start_ns[I14_OBSERVER_WORDS];
    unsigned long long read_end_ns[I14_OBSERVER_WORDS];
};

struct i14_observer_io {
    void *context;
    int (*claim)(void *);
    int (*prepare)(void *);
    int (*read)(void *, unsigned, unsigned *);
    void (*finish)(void *);
    unsigned long long (*now_ns)(void *);
};

static const char *i14_observer_phase_name(enum i14_observer_phase phase)
{
    switch (phase) {
    case I14_OBSERVER_IDLE: return "not-attempted";
    case I14_OBSERVER_METADATA: return "reserved-metadata";
    case I14_OBSERVER_READING: return "reading";
    case I14_OBSERVER_COMPLETE: return "complete-cached";
    case I14_OBSERVER_METADATA_ERROR: return "latched-metadata";
    case I14_OBSERVER_READ_ERROR: return "latched-read-error";
    case I14_OBSERVER_RANGE_ERROR: return "latched-range-error";
    case I14_OBSERVER_CLOCK_ERROR: return "latched-clock-error";
    case I14_OBSERVER_TIME_BUDGET: return "latched-time-budget";
    case I14_OBSERVER_ACTIVE_CONFIG: return "latched-active-enable-config";
    }
    return "invalid-phase";
}

/* claim() is atomic; losers never touch the receipt. The winning owner and
 * cached readers are serialized by the frontend mutex. No reset operation. */
static int i14_observer_observe(struct i14_observer_state *state,
                               const struct i14_observer_io *io,
                               const char *command, size_t count,
                               int root, int rawio)
{
    unsigned i;
    int ret;
    unsigned long long last;

    if (!root || !rawio) return -EPERM;
    if (count != 8 || memcmp(command, "observe\n", 8)) return -EINVAL;
    ret = io->claim(io->context);
    if (ret) return ret < 0 ? ret : -EIO;

    memset(state, 0, sizeof(*state));
    state->attempted = 1;
    state->failed_index = I14_OBSERVER_WORDS;
    state->phase = I14_OBSERVER_METADATA;
    state->started_ns = state->ended_ns = io->now_ns(io->context);
    ret = io->prepare(io->context);
    if (ret) {
        state->phase = I14_OBSERVER_METADATA_ERROR;
        state->rc = ret < 0 ? ret : -EIO;
        state->ended_ns = io->now_ns(io->context);
        goto end;
    }
    state->phase = I14_OBSERVER_READING;
    last = state->started_ns;
    for (i = 0; i < I14_OBSERVER_WORDS; ++i) {
        unsigned value = 0;
        unsigned long long begin = io->now_ns(io->context), finish;

        state->failed_index = i;
        state->read_start_ns[i] = state->ended_ns = begin;
        if (begin < last) {
            state->phase = I14_OBSERVER_CLOCK_ERROR;
            state->rc = -EIO;
            goto end;
        }
        if (begin - state->started_ns >= I14_OBSERVER_BUDGET_NS) {
            state->phase = I14_OBSERVER_TIME_BUDGET;
            state->rc = -ETIMEDOUT;
            goto end;
        }
        ++state->read_calls;
        ret = io->read(io->context, i14_observer_addresses[i], &value);
        finish = io->now_ns(io->context);
        state->read_end_ns[i] = state->ended_ns = finish;
        if (ret) {
            state->phase = I14_OBSERVER_READ_ERROR;
            state->rc = ret < 0 ? ret : -EIO;
            goto end;
        }
        if (value > 0xffff) {
            state->failed_raw = value;
            state->failed_raw_valid = 1;
            state->phase = I14_OBSERVER_RANGE_ERROR;
            state->rc = -ERANGE;
            goto end;
        }
        if (finish < begin) {
            state->phase = I14_OBSERVER_CLOCK_ERROR;
            state->rc = -EIO;
            goto end;
        }
        if (finish - state->started_ns >= I14_OBSERVER_BUDGET_NS) {
            state->phase = I14_OBSERVER_TIME_BUDGET;
            state->rc = -ETIMEDOUT;
            goto end;
        }
        state->values[i] = value;
        state->valid_mask |= 1u << i;
        /* This observed configuration is not a physical-current or stable
         * ownership proof; active enables conservatively stop the batch. */
        if (i == 0 && (value & 0x1c)) {
            state->phase = I14_OBSERVER_ACTIVE_CONFIG;
            state->rc = -EBUSY;
            goto end;
        }
        last = finish;
    }
    state->failed_index = I14_OBSERVER_WORDS;
    state->phase = I14_OBSERVER_COMPLETE;
end:
    io->finish(io->context);
    return state->rc;
}

static int i14_observer_format(const struct i14_observer_state *state,
                              char *buffer, size_t capacity)
{
    size_t used = 0;
    int n;
    unsigned i;

#define I14_OBSERVER_APPEND(...) do { \
    if (used >= capacity) return -ENOSPC; \
    n = snprintf(buffer + used, capacity - used, __VA_ARGS__); \
    if (n < 0) return -EIO; \
    if ((size_t)n >= capacity - used) return -ENOSPC; \
    used += (size_t)n; \
} while (0)
    I14_OBSERVER_APPEND("kind=historical-read-only-observation\n"
        "profile=%s\nword_count=%u\n"
        "phase=%s\nattempted=%u\nread_calls=%u\nknown_valid=0x%05x\n"
        "last_rc=%d\nstart_ns=%llu\nend_ns=%llu\n"
        "charging_status=UNKNOWN\nhandoff_qualified=0\n"
        "read_side_effects=UNKNOWN\nhardware_go=0\n",
        I14_OBSERVER_PROFILE_NAME, (unsigned)I14_OBSERVER_WORDS,
        i14_observer_phase_name(state->phase), state->attempted,
        state->read_calls, state->valid_mask, state->rc,
        state->started_ns, state->ended_ns);
    if (state->attempted && state->failed_index < I14_OBSERVER_WORDS)
        I14_OBSERVER_APPEND("failed_address=0x%04x\n",
                            i14_observer_addresses[state->failed_index]);
    else
        I14_OBSERVER_APPEND("failed_address=NONE\n");
    if (state->failed_raw_valid)
        I14_OBSERVER_APPEND("failed_raw=0x%x invalid_word=1\n", state->failed_raw);
    for (i = 0; i < I14_OBSERVER_WORDS; ++i) {
        if (state->valid_mask & (1u << i))
            I14_OBSERVER_APPEND("raw[%04x]=0x%04x known=1 start_ns=%llu end_ns=%llu\n",
                i14_observer_addresses[i], state->values[i],
                state->read_start_ns[i], state->read_end_ns[i]);
        else
            I14_OBSERVER_APPEND("raw[%04x]=UNKNOWN known=0 start_ns=%llu end_ns=%llu\n",
                i14_observer_addresses[i], state->read_start_ns[i], state->read_end_ns[i]);
    }
#undef I14_OBSERVER_APPEND
    return (int)used;
}
#endif
