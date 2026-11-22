/* Fixed native I14 RAM trial. Load and execution are separate operations. */
#define _GNU_SOURCE 1
#include <stdint.h>
#include <stdio.h>
#include <string.h>
static int layout_valid(size_t outer,uint64_t runtime,size_t dtb,size_t initrd)
{
    return outer==25942024 && runtime==0x1a10000 && dtb>=40 && dtb<=65536 &&
        (initrd==233043 || initrd==302686);
}
#ifndef I14_KEXEC_TEST
#include <errno.h>
#include <fcntl.h>
#include <linux/kexec.h>
#include <linux/reboot.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <unistd.h>
static int exact(const char *path,const char *expected,size_t size)
{
    char data[256];int fd=open(path,O_RDONLY|O_NOFOLLOW|O_CLOEXEC);
    if(fd<0) return 0;
    ssize_t n=read(fd,data,sizeof(data));int closed=close(fd);
    return !closed && n==(ssize_t)size && !memcmp(data,expected,size);
}
static int guard(void)
{
    static const char model[]="I14 MT6735 initramfs bring-up";
    static const char compatible[]="magcomm,magc6735-65u-1-m0\0mediatek,mt6735";
    struct utsname u;
    return !getuid() && !geteuid() && !uname(&u) && !strcmp(u.release,"7.0.10-i14-bringup") &&
        !strcmp(u.machine,"aarch64") && sysconf(_SC_PAGESIZE)==4096 &&
        exact("/proc/device-tree/model",model,sizeof(model)) &&
        exact("/proc/device-tree/compatible",compatible,sizeof(compatible)) &&
        exact("/sys/devices/system/cpu/online","0\n",2) &&
        exact("/proc/sys/kernel/kexec_load_disabled","0\n",2);
}
static void *read_file(const char *path,size_t *size,size_t cap)
{
    int fd=open(path,O_RDONLY|O_NOFOLLOW|O_CLOEXEC);struct stat st;
    if(fd<0) return MAP_FAILED;
    if(fstat(fd,&st) || !S_ISREG(st.st_mode) || st.st_uid || st.st_nlink!=1 ||
       st.st_size<=0 || (uint64_t)st.st_size>cap || (st.st_mode&0022)) {
        close(fd);errno=EINVAL;return MAP_FAILED;
    }
    *size=(size_t)st.st_size;
    void *b=mmap(NULL,*size,PROT_READ,MAP_PRIVATE,fd,0);
    int closed=close(fd);
    if(closed && b!=MAP_FAILED) {munmap(b,*size);return MAP_FAILED;}
    return b;
}
int main(int argc,char **argv)
{
    if(!guard()) {fputs("I14_KEXEC_STOPPED: native identity/CPU/page guard\n",stderr);return 1;}
    if(argc==2 && !strcmp(argv[1],"--execute")) {
        if(!exact("/sys/kernel/kexec/loaded","1\n",2)) return 1;
        puts("I14_KEXEC_EXECUTE: RAM image; installed boot partition unchanged");fflush(stdout);
        long rc=syscall(SYS_reboot,LINUX_REBOOT_MAGIC1,LINUX_REBOOT_MAGIC2,LINUX_REBOOT_CMD_KEXEC,0);
        if(rc<0) perror("I14_KEXEC_STOPPED: execute");
        return 1;
    }
    if(argc==2 && !strcmp(argv[1],"--unload")) {
        if(syscall(SYS_kexec_load,0,0,NULL,KEXEC_ARCH_AARCH64)<0) {perror("unload");return 1;}
        return 0;
    }
    /* The old kexec_loaded name is a symlink on this kernel; keep O_NOFOLLOW. */
    if(argc!=5 || strcmp(argv[1],"--load") || !exact("/sys/kernel/kexec/loaded","0\n",2)) {
        fputs("Usage: i14-ram-kexec --load OUTER DTB INITRD | --execute | --unload\n",stderr);return 2;
    }
    size_t n[3]={0};void *b[3]={MAP_FAILED,MAP_FAILED,MAP_FAILED};int result=1;
    const size_t caps[]={25942024,65536,302686};
    for(unsigned i=0;i<3;i++) {
        b[i]=read_file(argv[i+2],&n[i],caps[i]);
        if(b[i]==MAP_FAILED) {perror("I14_KEXEC_STOPPED: input");goto done;}
    }
    uint64_t runtime;memcpy(&runtime,(const char *)b[0]+16,8);
    static const unsigned char magic[]={0x41,0x52,0x4d,0x64};
    static const unsigned char branch[]={0x10,0,0,0x14};
    static const unsigned char fdt[]={0xd0,0x0d,0xfe,0xed};
    if(!layout_valid(n[0],runtime,n[1],n[2]) || memcmp(b[0],branch,4) ||
       memcmp((const char *)b[0]+56,magic,4) || memcmp(b[1],fdt,4) ||
       memcmp((const char *)b[0]+0x1000,b[1],n[1])) {
        fputs("I14_KEXEC_STOPPED: payload layout/embedded DTB mismatch\n",stderr);goto done;
    }
    struct kexec_segment seg[3]={
        {.buf=b[0],.bufsz=n[0],.mem=(void *)0x40000000,.memsz=0x1a10000},
        {.buf=b[1],.bufsz=n[1],.mem=(void *)0x4e000000,.memsz=(n[1]+4095)&~(size_t)4095},
        {.buf=b[2],.bufsz=n[2],.mem=(void *)0x44000000,.memsz=(n[2]+4095)&~(size_t)4095}
    };
    if(syscall(SYS_kexec_load,0x40000040,3,seg,KEXEC_ARCH_AARCH64)<0) {perror("I14_KEXEC_STOPPED: load");goto done;}
    puts("I14_KEXEC_LOADED: 3 fixed RAM segments; NOT EXECUTED");result=0;
done:
    for(unsigned i=0;i<3;i++) if(b[i]!=MAP_FAILED) munmap(b[i],n[i]);
    return result;
}
#endif
