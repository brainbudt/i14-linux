#include <assert.h>
#include <stdio.h>
#define I14_REBOOT_BOOTLOADER_TEST
#include "../tools/i14-reboot-bootloader.c"
static int guard_error, prepare_error, receipt_error, syscall_calls, prepare_calls;
static int native_guard(void) { return guard_error; }
static int prepare_target(void) { prepare_calls++; return prepare_error; }
static int verify_target(void) { return receipt_error; }
static int restart_bootloader(void) { syscall_calls++; return -1; }
int main(void)
{
    guard_error = 1;
    assert(reboot_bootloader() != 0 && !prepare_calls && !syscall_calls);
    guard_error = 0; prepare_error = 1;
    assert(reboot_bootloader() != 0 && prepare_calls == 1 && !syscall_calls);
    prepare_error = 0; receipt_error = 1;
    assert(reboot_bootloader() != 0 && prepare_calls == 2 && !syscall_calls);
    receipt_error = 0;
    assert(reboot_bootloader() != 0 && syscall_calls == 1);
    puts("reboot helper: failed guard/prepare/receipt never invokes reset PASS");
}
