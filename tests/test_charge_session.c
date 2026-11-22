#include <assert.h>
#include <errno.h>
#include <stdio.h>
#ifndef EUCLEAN
#define EUCLEAN 117
#endif
#include "../kernel/i14-charge-session.h"

int main(void)
{
    struct i14_charge_session s = {0};
    unsigned long long now = 1000000000ULL;
    int mv = -1;
    assert(i14_session_arm(&s, now, 10, 0, 0x20) == -EPERM);
    assert(i14_session_arm(&s, now, 10, 1, 0x30) == -EPERM);
    assert(i14_session_arm(&s, now, 10, 1, 0) == -EPERM);
    assert(i14_session_arm(&s, now, 7, 1, 0x20) == -EINVAL);
    assert(i14_session_arm(&s, now, 10, 1, 0x20) == 0);
    assert(i14_session_observe(&s, now + 1, 0, 0x20) == I14_SESSION_WAIT);
    assert(s.phase == I14_SESSION_WAIT_DETACH); /* A wall alone is no edge proof. */
    assert(i14_session_observe(&s, now + 2, 0, 0) == I14_SESSION_WAIT);
    assert(s.phase == I14_SESSION_WAIT_WALL);
    assert(i14_session_observe(&s, now + 3, 0, 0x20) == I14_SESSION_START);
    s.mutated = 1;
    assert(i14_session_started(&s, now + 4) == 0);
    assert(i14_session_observe(&s, now + 5, 0, 0x30) == I14_SESSION_WAIT);
    assert(i14_session_observe(&s, s.active_deadline, 0, 0x30) == I14_SESSION_STOP);
    assert(!i14_session_finish(&s, 0, 0) && s.phase == I14_SESSION_LATCHED);
    assert(i14_session_arm(&s, now, 600, 1, 0x20) == -EUCLEAN);
    assert(!i14_session_finish(&s, 0, 1)); /* A latch cannot be cleared by a later stop. */

    s = (struct i14_charge_session){0};
    assert(i14_session_arm(&s, now, 600, 1, 0x20) == 0);
    assert(i14_session_observe(&s, s.arm_deadline, 1, 0x20) == I14_SESSION_CANCEL);
    assert(i14_session_finish(&s, 0, 0) && s.phase == I14_SESSION_DONE);
    assert(i14_session_arm(&s, now, 600, 1, 0x20) == 0);
    assert(i14_session_observe(&s, now + 1, -ENODEV, 0x20) == I14_SESSION_CANCEL);
    assert(i14_session_finish(&s, 0, 0));
    assert(i14_session_arm(&s, now, 600, 1, 0x20) == 0);
    assert(i14_session_observe(&s, now + 1, 0, 0) == I14_SESSION_WAIT);
    assert(i14_session_observe(&s, now + 2, 1, 0x20) == I14_SESSION_CANCEL);
    assert(i14_session_finish(&s, 0, 0));

    for (int cause = 0; cause < 3; cause++) {
        s = (struct i14_charge_session){0};
        assert(i14_session_arm(&s, now, 600, 1, 0x20) == 0);
        assert(i14_session_observe(&s, now + 1, 0, 0) == I14_SESSION_WAIT);
        assert(i14_session_observe(&s, now + 2, 0, 0x20) == I14_SESSION_START);
        s.mutated = 1;
        assert(i14_session_started(&s, now + 3) == 0);
        assert(s.active_deadline == now + 3 + 600000000000ULL);
        assert(i14_session_observe(&s, now + 4, cause == 0 ? 1 : cause == 1 ? -ENODEV : 0,
                                   cause == 2 ? 0 : 0x30) == I14_SESSION_STOP);
        assert(i14_session_finish(&s, 0, 1) && s.phase == I14_SESSION_DONE);
    }
    s = (struct i14_charge_session){0};
    assert(i14_session_arm(&s, now, 10, 1, 0x20) == 0);
    assert(!i14_session_finish(&s, 1, 1) && s.phase == I14_SESSION_LATCHED);
    s = (struct i14_charge_session){0};
    assert(i14_session_arm(&s, ~0ULL - 1, 10, 1, 0x20) == -EOVERFLOW);
    assert(i14_session_arm(&s, now, 10, 1, 0x20) == 0);
    assert(i14_session_observe(&s, now - 1, 0, 0x20) == -EIO);
    assert(i14_session_vbus(1, &mv) == 0 && mv == 0); /* Preserve the first truncation. */
    assert(i14_session_vbus(1202, &mv) == 0 && mv == 4995);
    assert(i14_session_vbus(4095, &mv) == 0 && mv == 17021);
    mv = -1;
    assert(i14_session_vbus(4096, &mv) == -ERANGE && mv == -1);
    s = (struct i14_charge_session){0};
    assert(i14_session_arm(&s, now, 5, 1, 0x20) == 0);
    assert(s.phase == I14_SESSION_WAIT_WALL && s.duration_ns == 5000000000ULL);
    assert(i14_session_observe(&s, now + 1, 1, 0x20) == I14_SESSION_START);
    s.mutated = 1;
    assert(i14_session_started(&s, now + 2) == 0);
    assert(i14_session_observe(&s, now + 3, 1, 0x30) == I14_SESSION_WAIT);
    assert(i14_session_observe(&s, now + 4, 0, 0x30) == I14_SESSION_STOP);
    assert(i14_session_observe(&s, s.active_deadline, 1, 0x30) == I14_SESSION_STOP);
    assert(i14_session_finish(&s, 0, 1));
    assert(i14_session_arm(&s, now, 5, 1, 0x20) == 0);
    assert(i14_session_observe_usb(&s, now + 1, 1, 0x20, 1, 2) == I14_SESSION_CANCEL);
    assert(i14_session_observe_usb(&s, now + 2, 1, 0x20, 0, 500) == I14_SESSION_CANCEL);
    assert(i14_session_observe_usb(&s, now + 3, 1, 0x20, 1, 500) == I14_SESSION_START);
    assert(i14_session_started(&s, now + 4) == 0);
    assert(i14_session_observe_usb(&s, now + 5, 1, 0x30, 1, 500) == I14_SESSION_WAIT);
    assert(i14_session_observe_usb(&s, now + 6, 1, 0x30, 1, 2) == I14_SESSION_STOP);
    assert(i14_session_observe_usb(&s, now + 7, 1, 0x30, 0, 500) == I14_SESSION_STOP);
    assert(i14_session_observe_usb(&s, now + 8, 1, 0x30, 1, 900) == I14_SESSION_STOP);
    assert(i14_session_observe_usb(&s, now + 9, 1, 0x20, 1, 500) == I14_SESSION_STOP);
    assert(i14_session_finish(&s, 0, 1));
    assert(i14_session_arm(&s, now, 10, 1, 0x20) == 0);
    assert(s.phase == I14_SESSION_WAIT_DETACH && !s.usb_pilot);
    assert(i14_session_observe_usb(&s, now + 1, 1, 0x20, 1, 500) == -EINVAL);
    assert(i14_session_observe(&s, now + 2, 1, 0x20) == I14_SESSION_WAIT);
    assert(i14_session_observe(&s, now + 3, 0, 0) == I14_SESSION_WAIT);
    assert(i14_session_observe(&s, now + 4, 0, 0x20) == I14_SESSION_START);
    puts("PASS: real arm/detach/reattach proof, duration/host stops, sticky uncertainty/ref-release policy and stock VBUS staging");
    return 0;
}
