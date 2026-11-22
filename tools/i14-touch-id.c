/* Own stock driver forces I2C address 0x38. Read identity only; no reset/flash. */
#define _GNU_SOURCE 1
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#ifdef I14_TOUCH_ID_TEST
static int read_register(unsigned char reg,unsigned char *value);
#else
#include <fcntl.h>
#include <linux/i2c.h>
#include <linux/i2c-dev.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/utsname.h>
#include <unistd.h>
static int bus;
static int read_register(unsigned char reg,unsigned char *value)
{
    struct i2c_msg m[2]={{.addr=0x38,.len=1,.buf=&reg},
        {.addr=0x38,.flags=I2C_M_RD,.len=1,.buf=value}};
    struct i2c_rdwr_ioctl_data request={.msgs=m,.nmsgs=2};
    return ioctl(bus,I2C_RDWR,&request)==2 ? 0 : -1;
}
#endif
static int read_identity(unsigned char values[4])
{
    const unsigned char registers[]={0xa3,0x9f,0xa6,0xa8};
    for(unsigned i=0;i<4;i++) if(read_register(registers[i],&values[i])) return -1;
    return 0;
}
#ifndef I14_TOUCH_ID_TEST
static int exact(const char *path,const void *value,size_t n)
{
    char b[256];int fd=open(path,O_RDONLY|O_CLOEXEC);
    if(fd<0) return 0;
    ssize_t got=read(fd,b,sizeof(b));int closed=close(fd);
    return !closed && got==(ssize_t)n && !memcmp(b,value,n);
}
int main(int argc,char **argv)
{
    (void)argv;
    static const char model[]="I14 MT6735 initramfs bring-up";
    static const unsigned char reg[]={0,0,0,0,0x11,0,0x80,0,0,0,0,0,0,0,0,0x70,
        0,0,0,0,0x11,0,2,0,0,0,0,0,0,0,0,0x80};
    struct utsname u;
    if(argc!=1 || getuid() || geteuid() || uname(&u) || strcmp(u.machine,"aarch64") ||
       strcmp(u.release,"7.0.10-i14-bringup") || !exact("/proc/device-tree/model",model,sizeof(model)) ||
       !exact("/sys/class/i2c-dev/i2c-0/device/of_node/reg",reg,sizeof(reg))) {
        fputs("I14_TOUCH_STOPPED: native/bus identity\n",stderr);return 1;
    }
    bus=open("/dev/i2c-0",O_RDWR|O_NOFOLLOW|O_CLOEXEC);struct stat st;
    if(bus<0) {perror("I14_TOUCH_STOPPED: open");return 1;}
    if(fstat(bus,&st) || !S_ISCHR(st.st_mode) || major(st.st_rdev)!=89 || minor(st.st_rdev)!=0) {
        close(bus);return 1;
    }
    unsigned char values[4];int failed=read_identity(values);int closed=close(bus);
    if(failed || closed) {perror("I14_TOUCH_STOPPED: identity read");return 1;}
    printf("I14_TOUCH_ID addr=38 chip_high=%02x chip_low=%02x firmware=%02x vendor=%02x\n",
        values[0],values[1],values[2],values[3]);
    return 0;
}
#endif
