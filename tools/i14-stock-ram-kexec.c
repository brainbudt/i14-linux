/* Fixed original-I14 RAM profile. Offline candidate; dispatch requires a separately
 * qualified PMIC/power handoff and one custody-owning host worker. No PMIC I/O. */
#define _GNU_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <time.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

struct asset { unsigned char *data; size_t size; };
struct segment { const void *buf; size_t bufsz; uint64_t mem; size_t memsz; };
static const size_t asset_sizes[4]={4096,16446424,208378,75744};
static const char *const asset_names[4]={"stock-entry.page","Image","stock-charge.cpio.gz","candidate.dtb"};
static const char *const asset_hashes[4]={
 "12e9a3f01a90e891970a46a8c6daad93498d6521a4d7c610626063f4a80e5131",
 "71b449fdf2b61207a0c48ea2e9bb977ae6f704ffe44a63edb2e97e1fe438b4e9",
 "e6f3775a16ea95f48f65446bf66628a0800af805bb39e250a2e186219ba40e1f",
 "c05bb4bdfbd0efc9e71723dfcd446a8cd0db8820bf5cf5bcabe278febb9c69e4"};

static uint32_t ror(uint32_t x,unsigned n) { return (x>>n)|(x<<(32-n)); }
static uint32_t be32(const unsigned char *p) { return (uint32_t)p[0]<<24|(uint32_t)p[1]<<16|(uint32_t)p[2]<<8|p[3]; }
static uint64_t le64(const unsigned char *p) {
    uint64_t v=0;
    for(unsigned i=0;i<8;i++)v|=(uint64_t)p[i]<<(8*i);
    return v;
}
static void sha_block(uint32_t h[8],const unsigned char b[64]) {
    static const uint32_t k[64]={
      0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
      0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
      0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
      0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
      0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
      0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
      0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
      0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
    uint32_t w[64];
    for(unsigned i=0;i<16;i++)w[i]=be32(b+4*i);
    for(unsigned i=16;i<64;i++)w[i]=(ror(w[i-2],17)^ror(w[i-2],19)^(w[i-2]>>10))+w[i-7]+
        (ror(w[i-15],7)^ror(w[i-15],18)^(w[i-15]>>3))+w[i-16];
    uint32_t a=h[0],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],v=h[7],bb=h[1];
    for(unsigned i=0;i<64;i++) {
        uint32_t t=v+(ror(e,6)^ror(e,11)^ror(e,25))+((e&f)^(~e&g))+k[i]+w[i];
        uint32_t u=(ror(a,2)^ror(a,13)^ror(a,22))+((a&bb)^(a&c)^(bb&c));
        v=g;g=f;f=e;e=d+t;d=c;c=bb;bb=a;a=t+u;
    }
    h[0]+=a;h[1]+=bb;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=v;
}
static void digest_hex(const unsigned char *p,size_t n,char out[65]) {
    uint32_t h[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    size_t original=n;
    while(n>=64) {sha_block(h,p);p+=64;n-=64;}
    unsigned char tail[128]={0};memcpy(tail,p,n);tail[n]=0x80;
    size_t total=n<56?64:128;uint64_t bits=(uint64_t)original*8;
    for(unsigned i=0;i<8;i++)tail[total-1-i]=(unsigned char)(bits>>(8*i));
    sha_block(h,tail);if(total==128)sha_block(h,tail+64);
    const char *hex="0123456789abcdef";
    for(unsigned i=0;i<32;i++) {unsigned b=(h[i/4]>>(24-8*(i%4)))&255;out[2*i]=hex[b>>4];out[2*i+1]=hex[b&15];}
    out[64]=0;
}
static int image_header_ok(const unsigned char *p,size_t n) {
    return n==16446424 && le64(p+8)==0x80000 && le64(p+16)==0x1286000 &&
        le64(p+24)==0 && !memcmp(p+56,"ARM\x64",4);
}
static int asset_blob_ok(unsigned index,const unsigned char *p,size_t n) {
    if(index>=4 || n!=asset_sizes[index])return 0;
    if(index==1 && !image_header_ok(p,n))return 0;
    if(index==2 && (p[0]!=0x1f || p[1]!=0x8b))return 0;
    if(index==3 && (be32(p)!=0xd00dfeed || be32(p+4)!=75744))return 0;
    char hash[65];digest_hex(p,n,hash);return !strcmp(hash,asset_hashes[index]);
}
static int layout_ok(const struct segment s[4]) {
    static const uint64_t addresses[4]={0x40000000,0x40080000,0x44000000,0x4e000000};
    static const size_t memory[4]={0x1000,0x1286000,0x33000,0x13000};
    static const uint64_t excluded[6][2]={{0x43000000,0x43030000},{0x43f00000,0x43f10000},
      {0x43f10000,0x43ff0000},{0x43ff0000,0x44000000},{0xbf010000,0xbffc0000},{0xbffc0000,0xc0000000}};
    for(unsigned i=0;i<4;i++) {
        if(s[i].mem!=addresses[i] || s[i].bufsz!=asset_sizes[i] || s[i].memsz!=memory[i] ||
           (s[i].mem&4095) || (s[i].memsz&4095) || s[i].bufsz>s[i].memsz ||
           s[i].mem>UINT64_MAX-s[i].memsz)return 0;
        uint64_t end=s[i].mem+s[i].memsz;
        for(unsigned j=0;j<6;j++)if(s[i].mem<excluded[j][1] && excluded[j][0]<end)return 0;
        for(unsigned j=0;j<i;j++)if(s[i].mem<s[j].mem+s[j].memsz && s[j].mem<end)return 0;
    }
    return 1;
}
static int make_segments(const struct asset a[4],struct segment s[4]) {
    static const uint64_t addr[4]={0x40000000,0x40080000,0x44000000,0x4e000000};
    static const size_t mem[4]={0x1000,0x1286000,0x33000,0x13000};
    for(unsigned i=0;i<4;i++)s[i]=(struct segment){a[i].data,a[i].size,addr[i],mem[i]};
    return layout_ok(s);
}
static int iomem_ok(const char *text,const struct segment s[4]) {
    unsigned covered=0;
    while(*text) {
        const char *end=strchr(text,'\n');size_t n=end?(size_t)(end-text):strlen(text);
        if(n>=160)return 0;
        char line[160],label[48]={0};unsigned long long lo,hi;
        memcpy(line,text,n);line[n]=0;
        if(sscanf(line," %llx-%llx : %47[^\n]",&lo,&hi,label)==3 && !strcmp(label,"System RAM")) {
            if(!lo || hi<lo || hi==UINT64_MAX)return 0;
            for(unsigned i=0;i<4;i++)if(lo<=s[i].mem && s[i].mem+s[i].memsz<=hi+1)covered|=1u<<i;
        }
        if(!end)break;
        text=end+1;
    }
    return covered==15;
}
struct snapshot {
    uid_t uid,euid;long page;
    const char *release,*machine,*online,*disabled,*loaded,*boot,*model,*compatible;
};
static int boot_ok(const char *p) {
    if(strlen(p)!=36)return 0;
    for(unsigned i=0;i<36;i++) {
        if(i==8 || i==13 || i==18 || i==23) {if(p[i]!='-')return 0;}
        else if(!((p[i]>='0' && p[i]<='9') || (p[i]>='a' && p[i]<='f')))return 0;
    }
    return 1;
}
static int snapshot_ok(const struct snapshot *s,int loaded,const char *before) {
    return !s->uid && !s->euid && s->page==4096 && !strcmp(s->release,"7.0.10-i14-bringup") &&
      !strcmp(s->machine,"aarch64") && !strcmp(s->online,"0\n") && !strcmp(s->disabled,"0\n") &&
      !strcmp(s->loaded,loaded?"1\n":"0\n") && boot_ok(s->boot) && (!before || !strcmp(before,s->boot)) &&
      !strcmp(s->model,"9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a") &&
      !strcmp(s->compatible,"867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a");
}
static int storage_index(const char *name) {
    if(!strcmp(name,"mmcblk0"))return 0;
    if(!strcmp(name,"mmcblk0boot0"))return 1;
    if(!strcmp(name,"mmcblk0boot1"))return 2;
    if(strncmp(name,"mmcblk0p",8))return -1;
    char *end;errno=0;unsigned long value=strtoul(name+8,&end,10);
    if(errno || *end || value<1 || value>23)return -1;
    char canonical[32];snprintf(canonical,sizeof(canonical),"mmcblk0p%lu",value);
    return strcmp(canonical,name)?-1:(int)value+2;
}
static int storage_ok(const char *const *names,const int *ro,size_t count,uint64_t sectors) {
    if(count!=26 || sectors!=30535680)return 0;
    uint32_t seen=0;
    for(size_t i=0;i<count;i++) {
        int index=storage_index(names[i]);
        if(index<0 || ro[i]!=1 || (seen&(1u<<index)))return 0;
        seen|=1u<<index;
    }
    return seen==0x3ffffff;
}
static int file_metadata_ok(const struct stat *st,size_t size) {
    return S_ISREG(st->st_mode) && st->st_uid==0 && (st->st_mode&07777)==0600 && st->st_nlink==1 &&
        st->st_size>=0 && (uint64_t)st->st_size==size;
}
static int open_asset_at(int directory,const char *name) {
    return openat(directory,name,O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK);
}
struct session { int phase; char boot[37]; };
struct operations {
    int (*guard)(void *,int,const char *,char[37]);
    int (*nonce)(void *,char[33]);
    int (*load)(void *,const struct segment *);
    int (*announce)(void *,const char *,const char *);
    int (*command)(void *,char *,size_t,uint64_t);
    int (*execute)(void *);
    uint64_t (*millis)(void *);
};
static int run_session(struct session *s,const struct operations *o,void *context,const struct segment seg[4]) {
    if(s->phase)return 0;
    s->phase=1;char token[33],actual[37];
    if(!layout_ok(seg) || !o->guard(context,0,NULL,s->boot) || !o->nonce(context,token))return 0;
    if(o->load(context,seg)) {s->phase=2;return 0;}
    s->phase=3;
    uint64_t now=o->millis(context);
    if(now>UINT64_MAX-5000)return 0;
    uint64_t deadline=now+5000;
    if(!o->announce(context,s->boot,token) || o->millis(context)>=deadline)return 0;
    char received[64]={0},expected[64];snprintf(expected,sizeof(expected),"EXECUTE %s\n",token);
    int n=o->command(context,received,sizeof(received),deadline);
    if(n!=41 || memcmp(received,expected,41) || o->millis(context)>=deadline)return 0;
    if(!o->guard(context,1,s->boot,actual) || strcmp(actual,s->boot) || o->millis(context)>=deadline)return 0;
    s->phase=4;(void)o->execute(context);s->phase=5;
    return 0; /* A returned execute syscall is UNKNOWN, never a success/retry. */
}

static uint64_t native_millis(void *unused) {
    (void)unused;struct timespec t;
    if(clock_gettime(CLOCK_MONOTONIC,&t) || t.tv_sec<0)return UINT64_MAX;
    return (uint64_t)t.tv_sec*1000+(unsigned long)t.tv_nsec/1000000;
}
static int native_command(void *unused,char *out,size_t cap,uint64_t deadline) {
    (void)unused;size_t used=0;
    while(used+1<cap) {
        uint64_t now=native_millis(NULL);
        if(now>=deadline)return -1;
        struct pollfd p={STDIN_FILENO,POLLIN,0};
        int ready=poll(&p,1,(int)(deadline-now));
        if(ready<0 && errno==EINTR)continue;
        if(ready<=0 || !(p.revents&POLLIN))return -1;
        ssize_t n=read(STDIN_FILENO,out+used,1);
        if(n<0 && errno==EINTR)continue;
        if(n<=0)return 0;
        if(out[used++]=='\n') {out[used]=0;return (int)used;}
    }
    return -1;
}

#ifndef I14_STOCK_TEST
#include <dirent.h>
#include <linux/kexec.h>
#include <linux/reboot.h>
#include <sys/random.h>
#include <sys/syscall.h>
#include <sys/utsname.h>

#define ASSET_DIR "/run/i14-stock-trial"
static const struct segment *active_segments;
static int read_small(const char *path,char *out,size_t cap,size_t *length) {
    int fd=open(path,O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK);
    if(fd<0)return 0;
    size_t used=0;int okay=1;
    while(used<cap) {
        ssize_t n=read(fd,out+used,cap-used);
        if(n<0 && errno==EINTR)continue;
        if(n<0) {okay=0;break;}
        if(!n)break;
        used+=(size_t)n;
    }
    if(close(fd) || used==cap)okay=0;
    if(!okay)return 0;
    out[used]=0;if(length)*length=used;return 1;
}
static int same_file(const struct stat *a,const struct stat *b) {
    return a->st_dev==b->st_dev && a->st_ino==b->st_ino && a->st_mode==b->st_mode &&
      a->st_uid==b->st_uid && a->st_nlink==b->st_nlink && a->st_size==b->st_size &&
      a->st_mtim.tv_sec==b->st_mtim.tv_sec && a->st_mtim.tv_nsec==b->st_mtim.tv_nsec &&
      a->st_ctim.tv_sec==b->st_ctim.tv_sec && a->st_ctim.tv_nsec==b->st_ctim.tv_nsec;
}
static int read_asset(int directory,unsigned index,struct asset *asset) {
    int fd=open_asset_at(directory,asset_names[index]);struct stat before,after,named;
    if(fd<0)return 0;
    int okay=0;
    if(fstat(fd,&before) || !file_metadata_ok(&before,asset_sizes[index]))goto done;
    asset->size=asset_sizes[index];asset->data=malloc(asset->size);
    if(!asset->data)goto done;
    size_t used=0;
    while(used<asset->size) {
        ssize_t n=read(fd,asset->data+used,asset->size-used);
        if(n<0 && errno==EINTR)continue;
        if(n<=0)goto done;
        used+=(size_t)n;
    }
    if(fstat(fd,&after) || fstatat(directory,asset_names[index],&named,AT_SYMLINK_NOFOLLOW) ||
       !same_file(&before,&after) || !same_file(&after,&named) || !asset_blob_ok(index,asset->data,asset->size))goto done;
    okay=1;
done:
    if(close(fd))okay=0;
    return okay;
}
static int native_storage(void) {
    char names[27][32];const char *refs[27];int ro[27];size_t count=0;
    DIR *directory=opendir("/sys/class/block");
    if(!directory)return 0;
    struct dirent *entry;int okay=1;
    for(;;) {
        errno=0;entry=readdir(directory);
        if(!entry) {if(errno)okay=0;break;}
        if(strncmp(entry->d_name,"mmcblk0",7) || !strcmp(entry->d_name,"mmcblk0rpmb"))continue;
        if(count==27 || strlen(entry->d_name)>=sizeof(names[0])) {okay=0;break;}
        strcpy(names[count],entry->d_name);refs[count]=names[count];
        char path[128],value[16];snprintf(path,sizeof(path),"/sys/class/block/%s/ro",names[count]);
        if(!read_small(path,value,sizeof(value)-1,NULL)) {okay=0;break;}
        ro[count]=!strcmp(value,"1\n");count++;
    }
    if(closedir(directory))okay=0;
    char size[64];
    if(!okay || !read_small("/sys/class/block/mmcblk0/size",size,sizeof(size)-1,NULL) || strcmp(size,"30535680\n"))return 0;
    return storage_ok(refs,ro,count,30535680);
}
static int native_guard(void *unused,int loaded,const char *before,char boot[37]) {
    (void)unused;
    if(getuid() || geteuid())return 0;
    struct utsname u;char online[32],disabled[16],slot[16],rawboot[64],model[512],compatible[512],mh[65],ch[65];
    size_t mn,cn;
    if(uname(&u) || !read_small("/sys/devices/system/cpu/online",online,sizeof(online)-1,NULL) ||
       !read_small("/proc/sys/kernel/kexec_load_disabled",disabled,sizeof(disabled)-1,NULL) ||
       !read_small("/sys/kernel/kexec/loaded",slot,sizeof(slot)-1,NULL) ||
       !read_small("/proc/sys/kernel/random/boot_id",rawboot,sizeof(rawboot)-1,NULL) ||
       !read_small("/proc/device-tree/model",model,sizeof(model)-1,&mn) ||
       !read_small("/proc/device-tree/compatible",compatible,sizeof(compatible)-1,&cn))return 0;
    if(strlen(rawboot)!=37 || rawboot[36]!='\n')return 0;
    rawboot[36]=0;digest_hex((unsigned char*)model,mn,mh);digest_hex((unsigned char*)compatible,cn,ch);
    struct snapshot s={getuid(),geteuid(),sysconf(_SC_PAGESIZE),u.release,u.machine,online,disabled,slot,rawboot,mh,ch};
    char iomem[131072];
    if(!snapshot_ok(&s,loaded,before) || !native_storage() ||
       !read_small("/proc/iomem",iomem,sizeof(iomem)-1,NULL) || !iomem_ok(iomem,active_segments))return 0;
    memcpy(boot,rawboot,37);return 1;
}
static int native_nonce(void *unused,char out[33]) {
    (void)unused;unsigned char bytes[16];
    if(getrandom(bytes,sizeof(bytes),GRND_NONBLOCK)!=(ssize_t)sizeof(bytes))return 0;
    const char *hex="0123456789abcdef";
    for(unsigned i=0;i<16;i++){out[2*i]=hex[bytes[i]>>4];out[2*i+1]=hex[bytes[i]&15];}
    out[32]=0;return 1;
}
static int native_load(void *unused,const struct segment *seg) {
    (void)unused;struct kexec_segment native[4];
    for(unsigned i=0;i<4;i++)native[i]=(struct kexec_segment){
      .buf=seg[i].buf,.bufsz=seg[i].bufsz,.mem=(void*)(uintptr_t)seg[i].mem,.memsz=seg[i].memsz};
    return syscall(SYS_kexec_load,0x40000000,4,native,KEXEC_ARCH_AARCH64)==0?0:-1;
}
static int native_announce(void *unused,const char *boot,const char *token) {
    (void)unused;
    if(printf("I14_STOCK_LOADED boot=%s token=%s entry=40000000 segments=4 phase=WAIT_EXECUTE charging=UNKNOWN\n",boot,token)<0)return 0;
    return !fflush(stdout);
}
static int native_execute(void *unused) {
    (void)unused;
    return (int)syscall(SYS_reboot,LINUX_REBOOT_MAGIC1,LINUX_REBOOT_MAGIC2,LINUX_REBOOT_CMD_KEXEC,0);
}
int main(int argc,char **argv) {
    if(argc!=2 || (strcmp(argv[1],"--check") && strcmp(argv[1],"--load-and-wait"))) {
        fputs("Usage: i14-stock-ram-kexec --check | --load-and-wait\n",stderr);return 2;
    }
    struct asset assets[4]={{0}};struct segment seg[4];int result=1;
    /* Fixed private directory: no caller-selected image or generic loader. */
    if(getuid() || geteuid())return 1;
    int directory=open(ASSET_DIR,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);struct stat dir_before,dir_after;
    if(directory<0)return 1;
    if(fstat(directory,&dir_before) || !S_ISDIR(dir_before.st_mode) || dir_before.st_uid || (dir_before.st_mode&07777)!=0700)goto done;
    for(unsigned i=0;i<4;i++)if(!read_asset(directory,i,&assets[i]))goto done;
    if(fstatat(AT_FDCWD,ASSET_DIR,&dir_after,AT_SYMLINK_NOFOLLOW) || !same_file(&dir_before,&dir_after) || !make_segments(assets,seg))goto done;
    active_segments=seg;char boot[37];
    if(!native_guard(NULL,0,NULL,boot))goto done;
    if(!strcmp(argv[1],"--check")) {
        puts("I14_STOCK_CHECKED: four exact assets and native guards; NOT LOADED; charging UNKNOWN");result=0;goto done;
    }
    struct operations ops={native_guard,native_nonce,native_load,native_announce,native_command,native_execute,native_millis};
    struct session session={0};(void)run_session(&session,&ops,NULL,seg);
    fprintf(stderr,"I14_STOCK_STOPPED phase=%d; no retry/unload; outcome UNKNOWN\n",session.phase);
done:
    if(close(directory))result=1;
    for(unsigned i=0;i<4;i++)free(assets[i].data);
    return result;
}
#endif
