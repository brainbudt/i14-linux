/* Own-DT-confirmed MT6735 DISP_PWM0; configuration reads only, no MMIO stores.
 * LK ddp_reg.h blob cce608a59b21e19e3f0432ba1e270d1d924a6f15:1138-1141.
 * LK ddp_pwm.c blob e32cc6ce421c18a0487efb32778ab657288bc62a:155-169
 * reads these registers without acknowledgement or read-to-clear operations.
 */
#define _GNU_SOURCE 1
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#ifdef I14_BACKLIGHT_STATUS_TEST
static uint32_t mmio_read(unsigned offset);
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/utsname.h>
#include <unistd.h>
static volatile const uint32_t *page;
static uint32_t mmio_read(unsigned offset)
{
    __asm__ volatile ("" ::: "memory");
    uint32_t value=page[offset/4];
    __asm__ volatile ("dsb sy" ::: "memory");
    return value;
}
#endif
static void backlight_snapshot(uint32_t v[4])
{
    v[0]=mmio_read(0x00);
    v[1]=mmio_read(0x08);
    v[2]=mmio_read(0x10);
    v[3]=mmio_read(0x14);
}
struct backlight_counts { unsigned enabled,divisor,period_ticks,high_ticks; };
static struct backlight_counts backlight_decode(const uint32_t v[4])
{
    return (struct backlight_counts){v[0]&1,((v[2]>>16)&0x3ff)+1,
                                    (v[3]&0xfff)+1,(v[3]>>16)&0x1fff};
}
#ifndef I14_BACKLIGHT_STATUS_TEST
static int exact_file(const char *path,const char *expected,size_t size)
{
    char b[256]; int fd=open(path,O_RDONLY|O_CLOEXEC);
    if(fd<0) return -1;
    ssize_t got=read(fd,b,sizeof(b)); int closed=close(fd);
    return got==(ssize_t)size && !closed && !memcmp(b,expected,size) ? 0 : -1;
}
int main(int argc,char **argv)
{
    (void)argv;
    static const char model[]="I14 MT6735 initramfs bring-up";
    static const char compatible[]="magcomm,magc6735-65u-1-m0\0mediatek,mt6735";
    struct utsname u;
    if(access("/sys/class/drm/card0",F_OK)==0) {
        fputs("I14_BACKLIGHT_STOPPED: DRM active; use the bound PWM/backlight interface\n",stderr);return 1;
    }
    if(argc!=1 || getuid() || geteuid() || uname(&u) ||
       strcmp(u.release,"7.0.10-i14-bringup") || strcmp(u.machine,"aarch64") ||
       sysconf(_SC_PAGESIZE)!=4096 ||
       exact_file("/proc/device-tree/model",model,sizeof(model)) ||
       exact_file("/proc/device-tree/compatible",compatible,sizeof(compatible))) {
        fputs("I14_BACKLIGHT_STOPPED: native identity/arguments failed\n",stderr);return 1;
    }
    int fd=open("/dev/mem",O_RDONLY|O_SYNC|O_NOFOLLOW|O_CLOEXEC);
    struct stat st;
    if(fd<0) {perror("I14_BACKLIGHT_STOPPED: open");return 1;}
    if(fstat(fd,&st) || !S_ISCHR(st.st_mode) || major(st.st_rdev)!=1 || minor(st.st_rdev)!=1) {
        close(fd);fputs("I14_BACKLIGHT_STOPPED: expected character device 1:1\n",stderr);return 1;
    }
    void *m=mmap(NULL,4096,PROT_READ,MAP_SHARED,fd,(off_t)0x1100e000);
    int closed=close(fd);
    if(m==MAP_FAILED) {perror("I14_BACKLIGHT_STOPPED: mmap");return 1;}
    if(closed) {munmap(m,4096);return 1;}
    page=m;
    uint32_t v[4];backlight_snapshot(v);
    if(munmap(m,4096)) return 1;
    struct backlight_counts c=backlight_decode(v);
    printf("I14_DISP_PWM0 en=%08x commit=%08x con0=%08x con1=%08x\n",v[0],v[1],v[2],v[3]);
    printf("I14_DISP_PWM0_COUNTS enabled=%u divisor=%u period_ticks=%u high_ticks=%u clock_rate=UNKNOWN\n",
           c.enabled,c.divisor,c.period_ticks,c.high_ticks);
    return 0;
}
#endif
