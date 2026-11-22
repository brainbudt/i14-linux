/* insmod that passes module parameters, for the RAM-booted 7.0 trial initramfs.
 * Its busybox is built without CONFIG_FEATURE_CMDLINE_MODULE_OPTIONS, so busybox insmod
 * hands the kernel "" as options (E4a attempt 2, 2026-11-03: "i14-e4: refused: mode=(none)").
 *   insmod FILE.ko [name=value]...
 * Options are joined with spaces, as busybox does. finit_module only, one attempt.
 * Exit status: 0, or the errno of the failed open/finit_module (as busybox insmod). */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    char opts[1024];
    size_t n = 0;
    int fd, e;

    if (argc < 2) {
        fputs("usage: insmod FILE.ko [name=value]...\n", stderr);
        return 2;
    }
    opts[0] = 0;
    for (int i = 2; i < argc; i++) {
        size_t l = strlen(argv[i]);
        if (n + l + 2 > sizeof opts) {
            fputs("insmod: options too long\n", stderr);
            return 2;
        }
        memcpy(opts + n, argv[i], l);
        n += l;
        opts[n++] = ' ';
        opts[n] = 0;
    }
    fd = open(argv[1], O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        e = errno;
        fprintf(stderr, "insmod: can't open '%s': %s\n", argv[1], strerror(e));
        return e & 0xff ? e & 0xff : 1;
    }
    if (syscall(SYS_finit_module, fd, opts, 0) != 0) {
        e = errno;
        fprintf(stderr, "insmod: can't insert '%s': %s\n", argv[1], strerror(e));
        return e & 0xff ? e & 0xff : 1;
    }
    return 0;
}
