#include <assert.h>
#include <stdio.h>
#include <string.h>
#ifndef EUCLEAN
#define EUCLEAN 117
#endif
#include "../kernel/i14-charge-bootstrap.h"
struct fake { unsigned reads, updates, value; int read_rc, update_rc; unsigned rewind; unsigned long long now; };
static int read_value(void *p,unsigned a,unsigned *v)
{ struct fake *f=p; (void)a; f->reads++; if(f->read_rc)return f->read_rc; *v=f->value; if(f->rewind==2)f->now=1; return 0; }
static int update_value(void *p,unsigned a,unsigned m,unsigned v)
{ struct fake *f=p; (void)a;(void)m;(void)v;f->updates++;if(f->rewind==1)f->now=1;return f->update_rc; }
static unsigned long long now(void *p){return ((struct fake *)p)->now;}
static void pause_noop(void *p){(void)p;assert(0);}
static void historical_reads(void)
{
    struct fake f={.now=1000000000,.value=4}; struct i14_charge_state s={0}; unsigned value;
    struct i14_charge_io io={&f,read_value,update_value,now,pause_noop};
    assert(!s.hist_out.seen && !s.hist_mode.seen);
    assert(!i14_c_begin(&io,&s,I14_CHARGE_IO_NS));
    assert(!i14_c_read(&io,&s,0xf66,&value,"out-read"));
    assert(s.hist_out.seen && s.hist_out.raw==4 && s.hist_out.lo_known && s.hist_out.hi_known);
    assert(f.reads==1 && !f.updates);
    struct i14_charge_seen saved=s.hist_out;
    f.now+=1000; f.value=0x90;
    assert(!i14_c_read(&io,&s,0xf7a,&value,"mode-read"));
    assert(s.hist_mode.seen && s.hist_mode.raw==0x90 && !memcmp(&saved,&s.hist_out,sizeof(saved)));
    f.read_rc=-EIO;
    assert(i14_c_read(&io,&s,0xf66,&value,"out-failed")==-EIO);
    assert(!memcmp(&saved,&s.hist_out,sizeof(saved)) && f.reads==3 && !f.updates);
    memset(&s,0,sizeof(s)); f=(struct fake){.now=1000000000,.value=4,.rewind=2};
    assert(!i14_c_begin(&io,&s,I14_CHARGE_IO_NS));
    assert(i14_c_read(&io,&s,0xf66,&value,"post-bound-failed")==-EIO);
    assert(s.hist_out.seen && s.hist_out.raw==4 && s.hist_out.lo_known && !s.hist_out.hi_known);
    memset(&s,0,sizeof(s)); f=(struct fake){.now=1000000000,.value=0x10000};
    assert(!i14_c_begin(&io,&s,I14_CHARGE_IO_NS));
    assert(i14_c_read(&io,&s,0xf66,&value,"range-failed")==-EIO);
    assert(!s.hist_out.seen);
}
int main(void)
{
    for(unsigned kind=0;kind<8;kind++) {
        struct fake f={.now=1000000000,.value=8};
        struct i14_charge_state s={0};
        struct i14_charge_io io={&f,read_value,update_value,now,pause_noop};
        assert(!i14_c_begin(&io,&s,I14_CHARGE_IO_NS));
        if(kind==1)f.update_rc=-EIO;
        if(kind==2)f.read_rc=-EIO;
        if(kind==3)f.value=0;
        if(kind==4)f.value=0x10000;
        if(kind==5)f.now=1;
        if(kind==6)f.rewind=1;
        if(kind==7)f.rewind=2;
        int ret=i14_c_update(&io,&s,0xf48,8,8,"test-gate");
        assert(s.diag.valid && s.diag.address==0xf48 && s.diag.mask==8 && s.diag.expected==8);
        assert(s.diag.update_attempted==(kind!=5));
        assert(s.diag.update_rc_known==(kind!=5));
        assert(s.diag.read_attempted==(kind!=1 && kind!=5 && kind!=6));
        assert(s.diag.read_rc_known==s.diag.read_attempted);
        assert(s.diag.observed_known==(kind!=1 && kind!=2 && kind!=5 && kind!=6));
        if(kind==0) {
            assert(!ret && s.diag.compare_known && s.diag.compare_match && s.diag.observed==8);
            assert(f.updates==1 && f.reads==1);
        } else {
            assert(ret==-EIO && s.uncertain);
            if(kind==1)assert(!strcmp(s.diag.stage,"update-return") && s.diag.update_rc==-EIO && !f.reads);
            if(kind==2)assert(!strcmp(s.diag.stage,"read-return") && s.diag.read_rc==-EIO);
            if(kind==3)assert(!strcmp(s.diag.stage,"compare") && s.diag.compare_known && !s.diag.compare_match);
            if(kind==4)assert(!strcmp(s.diag.stage,"read-range") && s.diag.observed==0x10000 && !s.diag.range_match);
            if(kind==5)assert(!strcmp(s.diag.stage,"update-pre-bound") && !f.updates && !f.reads);
            if(kind==6)assert(!strcmp(s.diag.stage,"update-post-bound") && !f.reads);
            if(kind==7)assert(!strcmp(s.diag.stage,"read-post-bound") && s.diag.observed==8 && !s.diag.compare_known);
            struct i14_charge_diag saved=s.diag;
            unsigned ops=f.updates+f.reads;
            assert(i14_charge_stop(&io,&s)==-EUCLEAN);
            assert(f.updates+f.reads==ops && !memcmp(&saved,&s.diag,sizeof(saved)));
        }
    }
    historical_reads();
    puts("PASS: cached update/read/mismatch/range/clock stages, historical OUT/MODE validity, exact existing I/O counts, frozen uncertain receipt");
}
