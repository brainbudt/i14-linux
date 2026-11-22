#define I14_STATUS_TEST 1
#include "../tools/i14-status.c"
#include <assert.h>

static void contact(struct touch *t,int x,int y,int down,int page)
{
    touch_event(t,IX,x,page);touch_event(t,IY,y,page);
    touch_event(t,CONTACT,down,page);
}
int main(int argc,char **argv)
{
    assert(argc==2);
    if(!strcmp(argv[1],"pixels")) {
        assert(alpha_supported(24,8,0));
        assert(alpha_supported(0,0,0));
        assert(!alpha_supported(0,8,0));
        assert(!alpha_supported(24,8,1));
        unsigned char bytes[48];memset(bytes,0x5a,sizeof(bytes));
        struct surface s={bytes+4,3,2,16};
        rect(&s,-2,-1,4,3,0x123456);
        assert(bytes[4]==0x56 && bytes[5]==0x34 && bytes[6]==0x12 && bytes[7]==0xff);
        assert(bytes[4+16]==0x56 && bytes[4+16+7]==0xff);
        assert(bytes[3]==0x5a && bytes[4+8]==0x5a && bytes[4+12]==0x5a);
        assert(bytes[36]==0x5a);
        rect(&s,3,2,100,100,0);assert(bytes[36]==0x5a);
    } else if(!strcmp(argv[1],"mapping")) {
        assert(map_axis(0,0,719,720)==0);
        assert(map_axis(719,0,719,720)==719);
        assert(map_axis(-1,0,719,720)==-1);
        assert(map_axis(720,0,719,720)==-1);
        assert(map_axis(5,5,5,720)==-1);
        assert(hit(0,48,1180)==1 && hit(0,671,1319)==1);
        assert(hit(0,47,1180)==0 && hit(0,672,1180)==0);
        assert(hit(0,60,1370)==2 && hit(0,60,1510)==0);
        for(int i=0;i<5;i++) assert(hit(1,targets[i].x+70,targets[i].y+70)==i+3);
    } else if(!strcmp(argv[1],"touch")) {
        struct touch t={0};
        contact(&t,100,1200,1,0);assert(touch_event(&t,REPORT,0,0)==0);
        for(int i=0;i<100;i++) assert(touch_event(&t,REPORT,0,0)==0);
        contact(&t,100,1200,0,0);assert(touch_event(&t,REPORT,0,0)==1);
        assert(touch_event(&t,REPORT,0,0)==0 && t.count==1);
        contact(&t,100,1200,1,0);touch_event(&t,REPORT,0,0);
        contact(&t,100,1400,1,0);touch_event(&t,REPORT,0,0);
        contact(&t,100,1200,0,0);assert(touch_event(&t,REPORT,0,0)==0);
        contact(&t,100,1400,1,0);touch_event(&t,REPORT,0,0);
        contact(&t,100,1400,0,0);assert(touch_event(&t,REPORT,0,0)==2);
    } else if(!strcmp(argv[1],"loss")) {
        struct touch t={0};contact(&t,100,1200,1,0);touch_event(&t,REPORT,0,0);
        touch_event(&t,DROPPED,0,0);
        contact(&t,100,1200,0,0);assert(touch_event(&t,REPORT,0,0)==0);
        contact(&t,100,1200,1,0);assert(touch_event(&t,REPORT,0,0)==0);
        contact(&t,100,1200,0,0);assert(touch_event(&t,REPORT,0,0)==0);
        contact(&t,100,1200,1,0);touch_event(&t,REPORT,0,0);
        contact(&t,100,1200,0,0);assert(touch_event(&t,REPORT,0,0)==1);
    } else if(!strcmp(argv[1],"resync")) {
        struct touch t={.count=8,.discard=1,.inhibit=1};
        touch_resync(&t,100,1200,0);
        touch_event(&t,CONTACT,1,0);touch_event(&t,REPORT,0,0);
        touch_event(&t,CONTACT,0,0);assert(touch_event(&t,REPORT,0,0)==1);
        assert(t.count==9);
        touch_resync(&t,100,1400,1);
        touch_event(&t,CONTACT,0,0);assert(touch_event(&t,REPORT,0,0)==0);
        touch_event(&t,CONTACT,1,0);touch_event(&t,REPORT,0,0);
        touch_event(&t,CONTACT,0,0);assert(touch_event(&t,REPORT,0,0)==2);
    } else if(!strcmp(argv[1],"fixture") || !strcmp(argv[1],"fixture-diagnose")) {
        unsigned char *pixels=malloc(720u*1600u*4u);assert(pixels);
        struct surface s={pixels,720,1600,2880};
        struct status st={3723,"USB0",1,1,1};
        struct touch t={.x=360,.y=732,.count=5};
        render(&s,&st,&t,!strcmp(argv[1],"fixture-diagnose"),31);
        FILE *f=fopen(!strcmp(argv[1],"fixture")?"artifacts/status-ui-20261030/status.ppm":"artifacts/status-ui-20261030/diagnose.ppm","wb");assert(f);
        assert(fprintf(f,"P6\n720 1600\n255\n")>0);
        for(int i=0;i<720*1600;i++) {
            unsigned char rgb[]={pixels[i*4+2],pixels[i*4+1],pixels[i*4]};
            assert(pixels[i*4+3]==255 && fwrite(rgb,1,3,f)==3);
        }
        assert(!fclose(f));free(pixels);
    } else return 2;
    printf("PASS %s\n",argv[1]);return 0;
}
