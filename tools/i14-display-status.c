/* Read only the own-DT-confirmed MT6735 OVL0 configuration. No MMIO stores. */
#define _GNU_SOURCE 1
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#ifdef I14_DISPLAY_STATUS_TEST
static uint32_t mmio_read(unsigned offset);
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/utsname.h>
#include <unistd.h>
#include <errno.h>
static volatile const uint32_t *page;
static uint32_t mmio_read(unsigned offset)
{
    __asm__ volatile ("" ::: "memory");
    uint32_t value=page[offset/4];
    __asm__ volatile ("dsb sy" ::: "memory");
    return value;
}
#endif
static void display_snapshot(uint32_t v[19])
{
    v[0]=mmio_read(0x00c);
    v[1]=mmio_read(0x020);
    v[2]=mmio_read(0x02c);
    for(unsigned i=0;i<4;i++) {
        v[3+i*4]=mmio_read(0x030+i*0x20);
        v[4+i*4]=mmio_read(0x038+i*0x20);
        v[5+i*4]=mmio_read(0x044+i*0x20);
        v[6+i*4]=mmio_read(0xf40+i*0x20);
    }
}
static int capture_ready(const uint32_t v[19])
{
    return (v[0]&1) && v[1]==0x064002d0 && (v[2]&8) &&
        v[15]==0x21ff && v[16]==0x064002d0 &&
        (v[17]&0xffff)==2880 && v[18]==0xbf48e000;
}
#ifndef I14_DISPLAY_STATUS_TEST
static int exact_file(const char *path,const char *expected,size_t size)
{
    char b[256]; int fd=open(path,O_RDONLY|O_CLOEXEC);
    if(fd<0) return -1;
    ssize_t got=read(fd,b,sizeof(b)); int closed=close(fd);
    return got==(ssize_t)size && !closed && !memcmp(b,expected,size) ? 0 : -1;
}
int main(int argc,char **argv)
{
    int capture=argc==2 && !strcmp(argv[1],"--capture");
    static const char model[]="I14 MT6735 initramfs bring-up";
    static const char compatible[]="magcomm,magc6735-65u-1-m0\0mediatek,mt6735";
    struct utsname u;
    if(access("/sys/class/drm/card0",F_OK)==0) {
        fputs("I14_DISPLAY_STOPPED: DRM owns display; raw AP reads are pre-handoff only\n",stderr);return 1;
    }
    if((argc!=1 && !capture) || getuid() || geteuid() || uname(&u) ||
       strcmp(u.release,"7.0.10-i14-bringup") || strcmp(u.machine,"aarch64") ||
       sysconf(_SC_PAGESIZE)!=4096 ||
       exact_file("/proc/device-tree/model",model,sizeof(model)) ||
       exact_file("/proc/device-tree/compatible",compatible,sizeof(compatible))) {
        fputs("I14_DISPLAY_STOPPED: native identity/arguments failed\n",stderr);return 1;
    }
    int fd=open("/dev/mem",O_RDONLY|O_SYNC|O_NOFOLLOW|O_CLOEXEC);
    struct stat st;
    if(fd<0) {perror("I14_DISPLAY_STOPPED: open");return 1;}
    if(fstat(fd,&st) || !S_ISCHR(st.st_mode) || major(st.st_rdev)!=1 || minor(st.st_rdev)!=1) {
        close(fd);fputs("I14_DISPLAY_STOPPED: expected character device 1:1\n",stderr);return 1;
    }
    void *m=mmap(NULL,4096,PROT_READ,MAP_SHARED,fd,(off_t)0x14007000);
    int closed=close(fd);
    if(m==MAP_FAILED) {perror("I14_DISPLAY_STOPPED: mmap");return 1;}
    if(closed) {munmap(m,4096);return 1;}
    page=m;
    uint32_t v[19];display_snapshot(v);
    if(munmap(m,4096)) return 1;
    if(capture) {
        if(!capture_ready(v)) {fputs("I14_DISPLAY_STOPPED: capture configuration changed\n",stderr);return 1;}
        fd=open("/dev/mem",O_RDONLY|O_SYNC|O_NOFOLLOW|O_CLOEXEC);
        if(fd<0) return 1;
        const size_t bytes=2880*1600;
        m=mmap(NULL,bytes,PROT_READ,MAP_SHARED,fd,(off_t)0xbf48e000);
        closed=close(fd);
        if(m==MAP_FAILED) {perror("I14_DISPLAY_STOPPED: capture mmap");return 1;}
        size_t sent=0;
        while(!closed && sent<bytes) {
            ssize_t n=write(STDOUT_FILENO,(const char *)m+sent,bytes-sent);
            if(n<0 && errno==EINTR) continue;
            if(n<=0) break;
            sent+=(size_t)n;
        }
        int unmap=munmap(m,bytes);
        return closed || unmap || sent!=bytes;
    }
    printf("I14_OVL en=%08x roi=%08x src=%08x width=%u height=%u\n",v[0],v[1],v[2],v[1]&0xfff,v[1]>>16);
    for(unsigned i=0;i<4;i++)
        printf("I14_OVL_LAYER n=%u enabled=%u con=%08x size=%08x pitch=%u addr=%08x cfmt=%u swap=%u\n",
               i,(v[2]>>i)&1,v[3+i*4],v[4+i*4],v[5+i*4]&0xffff,v[6+i*4],
               (v[3+i*4]>>12)&15,(v[3+i*4]>>24)&1);
    return 0;
}
#endif
