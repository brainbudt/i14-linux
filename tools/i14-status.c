#define _POSIX_C_SOURCE 200809L
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static int alpha_supported(unsigned offset,unsigned length,unsigned reversed)
{
    /* Native simpledrm exposes an XRGB shadow for the bootloader ARGB layer. */
    return !reversed && ((offset==24 && length==8) || (offset==0 && length==0));
}
struct surface { unsigned char *pixels; int width,height,stride; };
struct box { int x,y,w,h; };
static const struct box targets[5]={{64,360,144,144},{512,360,144,144},{288,660,144,144},{64,960,144,144},{512,960,144,144}};
enum touch_kind { IX,IY,CONTACT,REPORT,DROPPED };
struct touch {
    int x,y,have_x,have_y,pending_down,down,pressed,discard,inhibit;
    unsigned count;
};
static void touch_resync(struct touch *t,int x,int y,int down)
{
    unsigned count=t->count;
    memset(t,0,sizeof(*t));t->count=count;
    t->x=x;t->y=y;t->have_x=t->have_y=1;
    t->down=t->pending_down=t->inhibit=down!=0;
}
static void rect(struct surface *s,int x,int y,int w,int h,uint32_t rgb)
{
    int64_t right=(int64_t)x+w,bottom=(int64_t)y+h;
    if(w<=0 || h<=0 || right<=0 || bottom<=0) return;
    if(x<0) x=0;
    if(y<0) y=0;
    if(right>s->width) right=s->width;
    if(bottom>s->height) bottom=s->height;
    for(int row=y;row<bottom;row++) for(int col=x;col<right;col++) {
        unsigned char *p=s->pixels+(size_t)row*s->stride+(size_t)col*4;
        p[0]=rgb;p[1]=rgb>>8;p[2]=rgb>>16;p[3]=255;
    }
}
static int map_axis(int v,int min,int max,int pixels)
{
    if(max<=min || v<min || v>max || pixels<=0) return -1;
    return (int)(((int64_t)v-min)*(pixels-1)/(max-min));
}
static int in_box(struct box b,int x,int y)
{
    return x>=b.x && y>=b.y && x<b.x+b.w && y<b.y+b.h;
}
static int hit(int page,int x,int y)
{
    if(!page && in_box((struct box){48,1180,624,140},x,y)) return 1;
    if(in_box((struct box){48,1370,624,140},x,y)) return 2;
    if(page) for(int i=0;i<5;i++) if(in_box(targets[i],x,y)) return i+3;
    return 0;
}
static int touch_event(struct touch *t,enum touch_kind k,int v,int page)
{
    if(k==DROPPED) {
        t->discard=1;t->inhibit=1;t->pressed=0;
        t->down=0;t->pending_down=0;t->have_x=0;t->have_y=0;
        return 0;
    }
    if(t->discard) {if(k==REPORT) t->discard=0;return 0;}
    if(k==IX) {t->x=v;t->have_x=1;}
    if(k==IY) {t->y=v;t->have_y=1;}
    if(k==CONTACT) t->pending_down=v!=0;
    if(k!=REPORT) return 0;
    /* After loss, require an observed idle frame before any fresh contact. */
    if(t->inhibit) {
        if(!t->pending_down) t->inhibit=0;
        t->down=t->pending_down;return 0;
    }
    int area=t->have_x && t->have_y ? hit(page,t->x,t->y):0;
    if(t->pending_down && !t->down) t->pressed=area;
    if(t->pending_down && t->down && area!=t->pressed) t->pressed=0;
    int action=0;
    if(!t->pending_down && t->down) {
        t->count++;
        if(t->pressed && t->pressed==area) action=area;
        t->pressed=0;
    }
    t->down=t->pending_down;
    return action;
}

/* Own 5x7 uppercase bitmap alphabet. Each byte is one top-to-bottom column. */
static const unsigned char font[][5]={
 {0x3e,0x51,0x49,0x45,0x3e},{0,0x42,0x7f,0x40,0},{0x42,0x61,0x51,0x49,0x46},
 {0x21,0x41,0x45,0x4b,0x31},{0x18,0x14,0x12,0x7f,0x10},{0x27,0x45,0x45,0x45,0x39},
 {0x3c,0x4a,0x49,0x49,0x30},{1,0x71,9,5,3},{0x36,0x49,0x49,0x49,0x36},{6,0x49,0x49,0x29,0x1e},
 {0x7e,0x11,0x11,0x11,0x7e},{0x7f,0x49,0x49,0x49,0x36},{0x3e,0x41,0x41,0x41,0x22},
 {0x7f,0x41,0x41,0x22,0x1c},{0x7f,0x49,0x49,0x49,0x41},{0x7f,9,9,9,1},
 {0x3e,0x41,0x49,0x49,0x7a},{0x7f,8,8,8,0x7f},{0,0x41,0x7f,0x41,0},{0x20,0x40,0x41,0x3f,1},
 {0x7f,8,0x14,0x22,0x41},{0x7f,0x40,0x40,0x40,0x40},{0x7f,2,0x0c,2,0x7f},
 {0x7f,4,8,0x10,0x7f},{0x3e,0x41,0x41,0x41,0x3e},{0x7f,9,9,9,6},
 {0x3e,0x41,0x51,0x21,0x5e},{0x7f,9,0x19,0x29,0x46},{0x46,0x49,0x49,0x49,0x31},
 {1,1,0x7f,1,1},{0x3f,0x40,0x40,0x40,0x3f},{0x1f,0x20,0x40,0x20,0x1f},
 {0x3f,0x40,0x38,0x40,0x3f},{0x63,0x14,8,0x14,0x63},{7,8,0x70,8,7},{0x61,0x51,0x49,0x45,0x43}
};
static void text(struct surface *s,int x,int y,int scale,const char *str,uint32_t rgb)
{
    for(;*str;str++,x+=6*scale) {
        const unsigned char *glyph=NULL;
        if(*str>='0' && *str<='9') glyph=font[*str-'0'];
        if(*str>='A' && *str<='Z') glyph=font[10+*str-'A'];
        if(glyph) for(int col=0;col<5;col++) for(int row=0;row<7;row++)
            if(glyph[col]&(1<<row)) rect(s,x+col*scale,y+row*scale,scale,scale,rgb);
        if(*str==':') {rect(s,x+2*scale,y+2*scale,scale,scale,rgb);rect(s,x+2*scale,y+5*scale,scale,scale,rgb);}
        if(*str=='-') rect(s,x,y+3*scale,5*scale,scale,rgb);
        if(*str=='/') for(int row=0;row<7;row++) rect(s,x+(4-row*4/6)*scale,y+row*scale,scale,scale,rgb);
    }
}
struct status { unsigned long uptime; char interface[24]; int carrier,storage,input; };
static void button(struct surface *s,int y,const char *label,int active)
{
    rect(s,48,y,624,140,active?0x6ee7b7:0x163449);
    text(s,48+(624-(int)strlen(label)*24)/2,y+56,4,label,active?0x07131c:0xe8f3ff);
}
static void render(struct surface *s,const struct status *st,const struct touch *t,int page,unsigned target_mask)
{
    const uint32_t fg=0xe8f3ff,muted=0x9db2c5,green=0x6ee7b7;
    char b[64];
    rect(s,0,0,s->width,s->height,0x07131c);
    rect(s,48,64,8,72,green);text(s,80,84,6,"I14 LINUX",fg);
    text(s,48,188,3,page?"TOUCH DIAGNOSE":"NATIVE LINUX STATUS",green);
    rect(s,48,242,624,2,0x294354);
    if(!page) {
        text(s,48,310,3,"UPTIME",muted);
        snprintf(b,sizeof(b),"%lu U %02lu M %02lu S",st->uptime/3600,st->uptime/60%60,st->uptime%60);
        text(s,48,358,4,b,fg);
        text(s,48,460,3,"USB NETWERK",muted);
        snprintf(b,sizeof(b),"INTERFACE: %.20s",st->interface);text(s,48,508,3,b,fg);
        text(s,48,554,4,st->carrier==1?"LINK ACTIEF":st->carrier==0?"GEEN LINK":"LINK ONBEKEND",st->carrier==1?green:fg);
        text(s,48,662,3,"OPSLAG",muted);
        text(s,48,710,4,st->storage?"ALLEEN-LEZEN GEREED":"WACHT OP RO CONTROLE",st->storage?green:fg);
        text(s,48,818,3,"AANRAAKSCHERM",muted);
        text(s,48,866,4,st->input?"KLAAR VOOR AANRAKEN":"GEEN INPUT GEVONDEN",st->input?green:fg);
        text(s,48,1000,3,"USB BEHEER BLIJFT ACTIEF",muted);
        button(s,1180,"VERVERS STATUS",t->pressed==1);
    } else {
        text(s,48,286,3,"RAAK DE VIJF VLAKKEN AAN",muted);
        for(int i=0;i<5;i++) {
            struct box r=targets[i];
            rect(s,r.x,r.y,r.w,r.h,(target_mask&(1u<<i))?green:0x163449);
            b[0]='1'+i;b[1]=0;
            text(s,r.x+57,r.y+51,6,b,(target_mask&(1u<<i))?0x07131c:fg);
        }
        snprintf(b,sizeof(b),"X %d  Y %d",t->x,t->y);text(s,48,1160,4,b,fg);
        snprintf(b,sizeof(b),"AANRAKINGEN %u",t->count);text(s,48,1230,3,b,fg);
        text(s,48,1290,3,st->input?"LOS LATEN BEVESTIGT":"GEEN INPUT GEVONDEN",muted);
    }
    button(s,1370,page?"TERUG":"DIAGNOSE",t->pressed==2);
}

#ifndef I14_STATUS_TEST
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <poll.h>
#include <signal.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stopping;
static void stop(int sig) {(void)sig;stopping=1;}
static int read_value(const char *path,char *out,size_t size)
{
    FILE *f=fopen(path,"r");if(!f) return -1;
    char *got=fgets(out,(int)size,f);int error=ferror(f);fclose(f);
    if(!got || error) return -1;
    out[strcspn(out,"\r\n")]=0;return 0;
}
static int identity(void)
{
    struct utsname u;char model[96];
    return !uname(&u) && !strcmp(u.machine,"aarch64") &&
        !strcmp(u.release,"7.0.10-i14-bringup") &&
        !read_value("/proc/device-tree/model",model,sizeof(model)) &&
        !strcmp(model,"I14 MT6735 initramfs bring-up");
}
static void snapshot(struct status *s)
{
    char b[128];double up=0;
    if(!read_value("/proc/uptime",b,sizeof(b))) sscanf(b,"%lf",&up);
    s->uptime=up>0?(unsigned long)up:0;
    strcpy(s->interface,"ONBEKEND");
    if(!read_value("/run/usb-interface",b,sizeof(b))) {
        size_t n=strlen(b);int valid=n>0 && n<sizeof(s->interface);
        for(size_t i=0;i<n;i++) if(!((b[i]>='a' && b[i]<='z') || (b[i]>='A' && b[i]<='Z') || (b[i]>='0' && b[i]<='9') || b[i]=='-' || b[i]=='_')) valid=0;
        if(valid) {for(size_t i=0;i<n;i++) if(b[i]>='a' && b[i]<='z') b[i]-=32;strcpy(s->interface,b);}
    }
    s->carrier=-1;
    if(!read_value("/sys/class/net/usb0/carrier",b,sizeof(b))) {
        if(!strcmp(b,"1")) s->carrier=1;
        if(!strcmp(b,"0")) s->carrier=0;
    }
    s->storage=access("/run/i14-storage-ro.ready",F_OK)==0;
}
static int field(struct fb_bitfield f,int offset)
{
    return f.offset==(unsigned)offset && f.length==8 && !f.msb_right;
}
static int framebuffer(struct surface *s,size_t *mapped)
{
    int fd=open("/dev/fb0",O_RDWR|O_CLOEXEC|O_NOFOLLOW);
    struct fb_var_screeninfo v;struct fb_fix_screeninfo f;struct stat st;
    if(fd<0) return -1;
    if(fstat(fd,&st) || !S_ISCHR(st.st_mode) || ioctl(fd,FBIOGET_VSCREENINFO,&v) || ioctl(fd,FBIOGET_FSCREENINFO,&f) ||
       v.xres!=720 || v.yres!=1600 || v.xres_virtual!=720 || v.yres_virtual!=1600 ||
       v.xoffset || v.yoffset || v.bits_per_pixel!=32 || v.grayscale || v.nonstd ||
       f.type!=FB_TYPE_PACKED_PIXELS || f.visual!=FB_VISUAL_TRUECOLOR || f.line_length!=2880 ||
       f.smem_len<2880u*1600u || !field(v.blue,0) || !field(v.green,8) || !field(v.red,16) ||
       !alpha_supported(v.transp.offset,v.transp.length,v.transp.msb_right)) {
        close(fd);errno=EINVAL;return -1;
    }
    /* fbcon may have blanked its DRM client before this userspace starts. */
    if(ioctl(fd,FBIOBLANK,FB_BLANK_UNBLANK)) {close(fd);return -1;}
    *mapped=2880u*1600u;
    void *m=mmap(NULL,*mapped,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);
    if(m==MAP_FAILED) return -1;
    *s=(struct surface){m,720,1600,2880};return 0;
}
#define BITS_PER_WORD (sizeof(unsigned long)*8)
#define WORDS(n) (((n)+BITS_PER_WORD-1)/BITS_PER_WORD)
static int bit(const unsigned long *bits,unsigned n) {return (bits[n/BITS_PER_WORD]>>(n%BITS_PER_WORD))&1;}
static int resync_input(int fd,struct touch *touch)
{
    struct input_absinfo x,y;unsigned long state[WORDS(KEY_CNT)]={0};
    if(ioctl(fd,EVIOCGABS(ABS_X),&x) || ioctl(fd,EVIOCGABS(ABS_Y),&y) ||
       ioctl(fd,EVIOCGKEY(sizeof(state)),state)<0 ||
       x.minimum!=0 || x.maximum!=719 || y.minimum!=0 || y.maximum!=1599) return -1;
    touch_resync(touch,map_axis(x.value,0,719,720),map_axis(y.value,0,1599,1600),bit(state,BTN_TOUCH));
    return 0;
}
static int find_input(struct touch *touch)
{
    glob_t paths={0};int result=-1;
    if(glob("/dev/input/event*",0,NULL,&paths)) {globfree(&paths);return -1;}
    for(size_t i=0;i<paths.gl_pathc;i++) {
        int fd=open(paths.gl_pathv[i],O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);
        struct input_id id;struct input_absinfo x,y;struct stat st;
        unsigned long keys[WORDS(KEY_CNT)]={0},axes[WORDS(ABS_CNT)]={0};
        if(fd<0) continue;
        if(!fstat(fd,&st) && S_ISCHR(st.st_mode) && !ioctl(fd,EVIOCGID,&id) && id.bustype==BUS_I2C &&
           ioctl(fd,EVIOCGBIT(EV_ABS,sizeof(axes)),axes)>=0 && bit(axes,ABS_X) && bit(axes,ABS_Y) &&
           ioctl(fd,EVIOCGBIT(EV_KEY,sizeof(keys)),keys)>=0 && bit(keys,BTN_TOUCH) &&
           !ioctl(fd,EVIOCGABS(ABS_X),&x) && !ioctl(fd,EVIOCGABS(ABS_Y),&y) &&
           x.minimum==0 && x.maximum==719 && y.minimum==0 && y.maximum==1599 &&
           !resync_input(fd,touch)) {
            fprintf(stderr,"I14_STATUS input=%s I2C XY=0..719/0..1599\n",paths.gl_pathv[i]);
            result=fd;break;
        }
        close(fd);
    }
    globfree(&paths);return result;
}
int main(int argc,char **argv)
{
    (void)argv;
    if(argc!=1 || !identity()) {fputs("I14_STATUS: identity/arguments rejected\n",stderr);return 1;}
    struct surface s;size_t mapped;
    if(framebuffer(&s,&mapped)) {perror("I14_STATUS framebuffer");return 1;}
    struct sigaction sa={0};sa.sa_handler=stop;sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM,&sa,NULL);sigaction(SIGINT,&sa,NULL);
    struct status status={0};struct touch touch={0};int input=-1,page=0,dirty=1;
    unsigned mask=0;time_t updated=(time_t)-1,retry=(time_t)-1;
    while(!stopping) {
        time_t now=time(NULL);
        if(now!=updated) {snapshot(&status);updated=now;dirty=1;}
        if(input<0 && now!=retry) {
            input=find_input(&touch);retry=now;status.input=input>=0;
            dirty=1;
        }
        if(dirty) {render(&s,&status,&touch,page,mask);dirty=0;}
        struct pollfd p={input,POLLIN,0};int ready=poll(&p,1,200);
        if(ready<0) {if(errno==EINTR) continue;perror("I14_STATUS poll");break;}
        if(ready<=0) continue;
        if(p.revents&(POLLERR|POLLHUP|POLLNVAL)) {close(input);input=-1;status.input=0;memset(&touch,0,sizeof(touch));dirty=1;continue;}
        struct input_event events[32];ssize_t n=read(input,events,sizeof(events));
        if(n<0 && (errno==EAGAIN || errno==EINTR)) continue;
        if(n<=0 || n%(ssize_t)sizeof(events[0])) {close(input);input=-1;status.input=0;memset(&touch,0,sizeof(touch));dirty=1;continue;}
        for(size_t i=0;i<(size_t)n/sizeof(events[0]);i++) {
            struct input_event e=events[i];enum touch_kind kind;int value=e.value;
            if(e.type==EV_ABS && e.code==ABS_X) {kind=IX;value=map_axis(value,0,719,720);}
            else if(e.type==EV_ABS && e.code==ABS_Y) {kind=IY;value=map_axis(value,0,1599,1600);}
            else if(e.type==EV_KEY && e.code==BTN_TOUCH) kind=CONTACT;
            else if(e.type==EV_SYN && e.code==SYN_REPORT) kind=REPORT;
            else if(e.type==EV_SYN && e.code==SYN_DROPPED) kind=DROPPED;
            else continue;
            int recovering=touch.discard && kind==REPORT;
            int action=touch_event(&touch,kind,value,page);
            if(recovering && resync_input(input,&touch)) {
                close(input);input=-1;status.input=0;memset(&touch,0,sizeof(touch));dirty=1;break;
            }
            if(kind==REPORT) dirty=1;
            if(action==1) {snapshot(&status);dirty=1;}
            if(action==2) {page=!page;dirty=1;}
            if(action>=3) {mask|=1u<<(action-3);dirty=1;}
        }
    }
    if(input>=0) close(input);
    munmap(s.pixels,mapped);return 0;
}
#endif
