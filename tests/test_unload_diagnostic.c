#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#define I14_UNLOAD_DIAGNOSTIC_TEST
#include "../tools/i14-unload-diagnostic.c"

static int identity_error, sample_present, sample_error, target_error;
static int ref_error, delete_error, guard_calls, delete_calls;
static ssize_t target_count = -1;
static int target_errno = ENODATA;
static const char *refcount = "0\n";
static int native_guard(void) { guard_calls++; return identity_error; }
static int no_sample_interface(void) { return sample_present || sample_error; }
static int no_prepared_receipt(void)
{
    return target_error || !unprepared_read(target_count, target_errno);
}
static int no_module_references(void)
{
    return ref_error || !zero_refcount(refcount, (ssize_t)strlen(refcount));
}
static int delete_diagnostic(const char *name, int flags)
{
    assert(!strcmp(name, "i14_reboot_target"));
    assert(flags == O_NONBLOCK);
    delete_calls++;
    return delete_error;
}

int main(void)
{
    assert(unload_diagnostic(2) == 2 && !guard_calls && !delete_calls);
    identity_error = 1;
    assert(unload_diagnostic(1) != 0 && !delete_calls);
    identity_error = 0; sample_present = 1;
    assert(unload_diagnostic(1) != 0 && !delete_calls);
    sample_present = 0; sample_error = 1;
    assert(unload_diagnostic(1) != 0 && !delete_calls);
    sample_error = 0; target_error = 1;
    assert(unload_diagnostic(1) != 0 && !delete_calls);
    target_error = 0;
    const struct { ssize_t count; int error; } uncertain[] = {
        {32, 0}, {0, 0}, {-1, EIO}, {-1, EACCES}, {-1, ENOENT}, {-1, EINTR},
        {1, ENODATA}, {0, ENODATA}, {-2, ENODATA}
    };
    for (size_t i = 0; i < sizeof(uncertain) / sizeof(uncertain[0]); i++) {
        target_count = uncertain[i].count; target_errno = uncertain[i].error;
        assert(unload_diagnostic(1) != 0 && !delete_calls);
    }
    target_count = -1; target_errno = ENODATA; ref_error = 1;
    assert(unload_diagnostic(1) != 0 && !delete_calls);
    ref_error = 0;
    const char *unsafe_refs[] = {"1\n", "-1\n", "0", "00\n", "0\nextra", "", " 0\n"};
    for (size_t i = 0; i < sizeof(unsafe_refs) / sizeof(unsafe_refs[0]); i++) {
        refcount = unsafe_refs[i];
        assert(unload_diagnostic(1) != 0 && !delete_calls);
    }
    assert(!zero_refcount(NULL, -1));
    refcount = "0\n"; delete_error = -1;
    assert(unload_diagnostic(1) == 1 && delete_calls == 1);
    delete_error = 0;
    assert(unload_diagnostic(1) == 0 && delete_calls == 2);
    puts("PASS: no unload on guards/prepared/uncertain/nonzero refs; fixed name/nonblocking flags; no retry");
    return 0;
}
