#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#ifndef EUCLEAN
#define EUCLEAN 117 /* Linux errno, absent on the macOS host-test libc. */
#endif
#include "../kernel/i14-charge-bootstrap.h"
struct op { unsigned addr, mask, value; };
struct fake {
    unsigned reg[0x1000], operations, fail_at, enables, writes, gate_clear, frozen;
    unsigned no_expiry, no_reset_clear;
    unsigned delay_ns, ignore_address;
    unsigned early_gate_loss;
    unsigned block_gate_while_out;
    unsigned expire_on_gate;
    unsigned edge_only, expiry_events;
    unsigned expire_before_gate;
    unsigned require_mode_for_gate, mode_delay_ns;
    struct i14_charge_state *state;
    unsigned long long now, expires, period;
    struct op log[256];
};
static int rd(void *context, unsigned address, unsigned *value)
{
    struct fake *f = context;
    f->now+=f->delay_ns;
    ++f->operations;
    assert(address < 0x1000);
    if (f->period && !f->no_expiry && (f->reg[0xf62] & 0x10) && f->now >= f->expires &&
        (!f->edge_only || !f->expiry_events)) {
        f->expiry_events++;
        f->reg[0xf66] |= 4;
        if (f->gate_clear) f->reg[0xf48] &= ~0x18U;
    }
    if(f->early_gate_loss && (f->reg[0xf62]&16) && f->now+f->period/2>=f->expires)
        f->reg[0xf48]&=~8U;
    if (f->operations == f->fail_at) return -EIO;
    *value = f->reg[address];
    return 0;
}
static int update(void *context, unsigned address, unsigned mask, unsigned value)
{
    struct fake *f = context;
    f->now+=f->delay_ns;
    assert(f->writes < 256 && !(value & ~mask));
    assert((address==0xf48 && (mask==8 || mask==16)) ||
           (address==0xf4a && mask==0xf0 && value==0xa0) ||
           (address==0xf4c && (mask==2 || mask==8)) ||
           (address==0xf4e && mask==31 && value==13) ||
           (address==0xf50 && mask==15 && (value==12 || value==15)) ||
           (address==0xf54 && mask==15 && value==5) ||
           (address==0xf62 && (mask==15 || mask==16 || mask==256)) ||
           (address==0xf66 && (mask==1 || mask==2)) ||
           (address==0xf74 && (mask==7 || mask==0x70)) ||
           (address==0xf76 && (mask==7 || mask==0x70)) ||
           (address==0xf7a && (mask==64 || (mask==4 && value==4))));
    f->log[f->writes++] = (struct op){address,mask,value};
    f->operations++;
    if(f->state && address==0xf7a && mask==4) {
        assert(f->state->initial_stop_completed && !f->state->stop_verified);
        assert(!(f->reg[0xf48]&24) && !(f->reg[0xf7a]&64));
        assert(!(f->reg[0xf62]&16) && !(f->reg[0xf66]&3));
        assert(f->state->deadline==f->state->initial_stop_ns+I14_CHARGE_IO_NS);
    }
    if(address==0xf7a && mask==4)f->now+=f->mode_delay_ns;
    if(f->expire_before_gate && address==0xf48 && mask==8 && value) {
        f->now=f->expires; f->reg[0xf66]|=4; f->reg[0xf48]&=~8U; f->expiry_events=1;
    }
    if(address!=f->ignore_address)f->reg[address] = (f->reg[address] & ~mask) | value;
    if(f->expire_on_gate && address==0xf48 && mask==8 && value) {
        f->now=f->expires; f->reg[0xf66]|=4; f->reg[0xf48]&=~8U;
    }
    if(f->block_gate_while_out && address==0xf48 && mask==8 && value && (f->reg[0xf66]&4))
        f->reg[0xf48]&=~8U;
    /* Hypothesis only: MODE1 is required for the CSDAC gate to accept1. */
    if(f->require_mode_for_gate && address==0xf48 && mask==8 && value && !(f->reg[0xf7a]&4))
        f->reg[0xf48]&=~8U;
    if (address == 0xf62 && mask == 0x10 && value) f->expires = f->now + f->period;
    if (address == 0xf48 && (mask & 0x10) && (value & 0x10)) f->enables++;
    if (address == 0xf62 && mask == 0x100 && value) {
        f->expires = f->now + f->period;
        f->expiry_events=0;
        if (!f->no_reset_clear) f->reg[0xf66] &= ~4U;
    }
    return f->operations == f->fail_at ? -EIO : 0;
}
static unsigned long long tm(void *context) { return ((struct fake *)context)->now; }
static void pause_poll(void *context)
{ struct fake *f = context; if (!f->frozen) f->now += 1000000; }
static struct i14_charge_io io(struct fake *f)
{ return (struct i14_charge_io){ f,rd,update,tm,pause_poll }; }
static void init(struct fake *f)
{
    memset(f,0,sizeof(*f)); f->now=1000000000ULL; f->period=30000000;
    f->gate_clear=1; f->reg[0xf48]=0x8021; f->reg[0xf4a]=0x80f2; f->reg[0xf56]=5;
    f->reg[0xf54]=0x8001; f->reg[0xf7a]=0x8090;
    f->require_mode_for_gate=1;
}
static struct i14_charge_sample sample(struct fake *f, unsigned sequence)
{ return (struct i14_charge_sample){3500,5000,669,7,f->now,sequence,1}; }
static void proved(struct fake *f, struct i14_charge_state *s)
{
    struct i14_charge_io ops;
    init(f); memset(s,0,sizeof(*s)); ops=io(f);
    assert(i14_charge_selftest(&ops,s)==0);
    assert(s->proof_valid && s->proof_gate_clear && !f->enables);
    assert(!(f->reg[0xf48]&0x18) && !(f->reg[0xf62]&0x10));
    f->period=4000000000ULL; f->operations=0; f->writes=0;
}
static void started(struct fake *f,struct i14_charge_state *s,enum i14_charge_profile profile)
{
    struct i14_charge_io ops;
    struct i14_charge_sample v;
    proved(f,s); ops=io(f); v=sample(f,1);
    assert(i14_charge_start(&ops,s,profile,&v)==0);
    f->operations=0; f->writes=0;
}
static void uncertain_stops_all(struct fake *f,struct i14_charge_state *s)
{
    unsigned operations=f->operations;
    struct i14_charge_io ops=io(f);
    struct i14_charge_sample v=sample(f,99);
    assert(s->uncertain && s->error && s->phase && s->address);
    assert(i14_charge_stop(&ops,s)==-EUCLEAN);
    assert(i14_charge_selftest(&ops,s)==-EUCLEAN);
    assert(i14_charge_kick(&ops,s,&v)==-EUCLEAN);
    assert(i14_charge_start(&ops,s,I14_CHARGE_WALL,&v)==-EUCLEAN);
    assert(f->operations==operations);
}
static void failure_points(void)
{
    struct fake f; struct i14_charge_state s;
    struct i14_charge_io ops; struct i14_charge_sample v;
    unsigned count;
    init(&f); memset(&s,0,sizeof(s)); ops=io(&f);
    assert(!i14_charge_selftest(&ops,&s)); count=f.operations;
    for(unsigned at=1;at<=count;at++) {
        init(&f); memset(&s,0,sizeof(s)); ops=io(&f); f.fail_at=at;
        assert(i14_charge_selftest(&ops,&s)<0 && f.operations==at);
        assert(!f.enables); uncertain_stops_all(&f,&s);
    }
    proved(&f,&s); ops=io(&f); v=sample(&f,1);
    assert(!i14_charge_start(&ops,&s,I14_CHARGE_PILOT,&v)); count=f.operations;
    for(unsigned at=1;at<=count;at++) {
        proved(&f,&s); ops=io(&f); v=sample(&f,1); f.fail_at=at;
        assert(i14_charge_start(&ops,&s,I14_CHARGE_PILOT,&v)<0 && f.operations==at);
        uncertain_stops_all(&f,&s);
    }
    started(&f,&s,I14_CHARGE_PILOT); ops=io(&f); f.now+=250000000; v=sample(&f,2);
    assert(!i14_charge_kick(&ops,&s,&v)); count=f.operations;
    for(unsigned at=1;at<=count;at++) {
        started(&f,&s,I14_CHARGE_PILOT); ops=io(&f); f.now+=250000000; v=sample(&f,2); f.fail_at=at;
        assert(i14_charge_kick(&ops,&s,&v)<0 && f.operations==at);
        uncertain_stops_all(&f,&s);
    }
    started(&f,&s,I14_CHARGE_PILOT); ops=io(&f);
    assert(!i14_charge_stop(&ops,&s)); count=f.operations;
    for(unsigned at=1;at<=count;at++) {
        started(&f,&s,I14_CHARGE_PILOT); ops=io(&f); f.fail_at=at;
        assert(i14_charge_stop(&ops,&s)<0 && f.operations==at);
        uncertain_stops_all(&f,&s);
    }
}
static void guards(void)
{
    struct fake f; struct i14_charge_state s; struct i14_charge_io ops;
    struct i14_charge_sample v;
    for(unsigned bad=0;bad<15;bad++) {
        proved(&f,&s); ops=io(&f); v=sample(&f,1);
        switch(bad) {
        case 0:v.fresh_mask=0;break; case 1:v.sequence=0;break;
        case 2:v.vbat_mv=2999;break;case 3:v.vbat_mv=3800;break;
        case 4:v.vbus_mv=4499;break;case 5:v.vbus_mv=5501;break;
        case 6:v.ntc_mv=0;break;case 7:v.ntc_mv=1800;break;
        case 8:v.ntc_mv=1200;break;case 9:v.ntc_mv=300;break;
        case 10:v.sampled_ns=f.now+1;break;
        case 11:f.now+=500000001;break;
        case 12:v.external_power_confirmed=0;break;
        case 13:v.vbat_mv=3299;break;
        case 14:v.sampled_ns=s.proof_ns-1;break;
        }
        assert(i14_charge_start(&ops,&s,I14_CHARGE_WALL,&v)<0 && !f.enables);
        assert(!s.proof_valid);
    }
    for(unsigned bad=0;bad<10;bad++) {
        static const unsigned address[]={0xf48,0xf56,0xf56,0xf9a,0xf54,
                                         0xf54,0xf54,0xf48,0xf48,0xf48};
        proved(&f,&s); ops=io(&f); v=sample(&f,1);
        switch(bad) {
        case 0:f.reg[0xf48]&=~32U;break;
        case 1:f.reg[0xf56]&=~1U;break; case 2:f.reg[0xf56]&=~4U;break;
        case 3:f.reg[0xf9a]|=1;break;case 4:f.reg[0xf54]&=~1U;break;
        case 5:f.reg[0xf54]|=64;break;case 6:f.reg[0xf54]=15;break;
        case 7:f.reg[0xf48]|=16U;break;
        case 8:f.reg[0xf48]&=~1U;break; /* Input overvoltage protection disabled. */
        case 9:f.reg[0xf48]|=128U;break; /* Input overvoltage detected. */
        }
        assert(i14_charge_start(&ops,&s,I14_CHARGE_WALL,&v)<0 && !f.enables);
        assert(s.stop_verified && !s.running && !s.uncertain);
        assert(s.address==address[bad]);
    }
    proved(&f,&s); ops=io(&f); f.now+=2000000001; v=sample(&f,1);
    assert(i14_charge_start(&ops,&s,I14_CHARGE_WALL,&v)<0 && !f.enables && !f.operations);
    proved(&f,&s); ops=io(&f); v=sample(&f,1); v.vbat_mv=3300;
    assert(!i14_charge_start(&ops,&s,I14_CHARGE_WALL,&v) && (f.reg[0xf50]&15)==12);
    proved(&f,&s); ops=io(&f); v=sample(&f,1); v.vbat_mv=3000;
    assert(!i14_charge_start(&ops,&s,I14_CHARGE_PILOT,&v) && (f.reg[0xf50]&15)==15);
    proved(&f,&s); ops=io(&f); v=sample(&f,1); v.external_power_confirmed=0;
    assert(i14_charge_start(&ops,&s,I14_CHARGE_PILOT,&v)==-EPERM && !f.enables);
    for(unsigned i=0;i<3;i++) {
        static const unsigned address[]={0xf4a,0xf54,0xf7a};
        proved(&f,&s); ops=io(&f); v=sample(&f,1); f.ignore_address=address[i];
        assert(i14_charge_start(&ops,&s,I14_CHARGE_WALL,&v)==-EIO && !f.enables);
        uncertain_stops_all(&f,&s);
    }
}
static void watchdog_and_limits(void)
{
    struct fake f; struct i14_charge_state s;struct i14_charge_io ops;
    struct i14_charge_sample v;
    for(unsigned bad=0;bad<5;bad++) {
        init(&f); memset(&s,0,sizeof(s)); ops=io(&f);
        if(bad==0)f.gate_clear=0;
        if(bad==1)f.no_expiry=1;
        if(bad==2){f.no_expiry=1;f.frozen=1;}
        if(bad==3){f.no_reset_clear=1;f.reg[0xf66]=4;}
        if(bad==4)f.early_gate_loss=1;
        assert(i14_charge_selftest(&ops,&s)<0 && !s.proof_valid && !f.enables);
        assert(s.stop_verified && !s.uncertain);
        assert(f.operations<25000);
    }
    for(unsigned bad=0;bad<6;bad++) {
        started(&f,&s,bad==1?I14_CHARGE_WALL:I14_CHARGE_PILOT); ops=io(&f);
        f.now+=bad==0?5000000000ULL:bad==1?600000000000ULL:bad==2?1000000000ULL:250000000ULL;
        v=sample(&f,2);
        if(bad==3)v.vbat_mv=3800;
        if(bad==4)v.sequence=1;
        if(bad==5)f.reg[0xf4a]=0x80f5;
        assert(i14_charge_kick(&ops,&s,&v)<0 && s.stop_verified && !s.running && !s.uncertain);
        assert(!(f.reg[0xf48]&24) && !(f.reg[0xf7a]&64) && !(f.reg[0xf62]&16));
        assert(f.log[0].addr==0xf48 && f.log[0].mask==16 && !f.log[0].value);
        assert(f.log[1].addr==0xf48 && f.log[1].mask==8 && !f.log[1].value);
        assert(f.log[2].addr==0xf7a && f.log[2].mask==64 && !f.log[2].value);
        assert(f.log[3].addr==0xf62 && f.log[3].mask==16 && !f.log[3].value);
    }
    proved(&f,&s); ops=io(&f); v=sample(&f,1); f.delay_ns=5000000;
    assert(i14_charge_start(&ops,&s,I14_CHARGE_PILOT,&v)==-ETIMEDOUT);
    assert(!f.enables && s.stop_verified && !s.uncertain);
    started(&f,&s,I14_CHARGE_PILOT); ops=io(&f); f.now+=950000000; v=sample(&f,2); f.delay_ns=5000000;
    assert(i14_charge_kick(&ops,&s,&v)==-ETIME && s.stop_verified && !s.uncertain);
    assert(f.log[0].addr==0xf48 && f.log[0].mask==16); /* No late watchdog feed. */
    proved(&f,&s); ops=io(&f); v=sample(&f,1); f.ignore_address=0xf4e;
    assert(i14_charge_start(&ops,&s,I14_CHARGE_PILOT,&v)==-EIO && !f.enables);
    uncertain_stops_all(&f,&s);
}
static void inherited_out_order(void)
{
    struct fake f; struct i14_charge_state s={0};
    init(&f); f.reg[0xf66]=4; f.block_gate_while_out=1;
    struct i14_charge_io ops=io(&f);
    /* Hypothetical continuous OUT override: a model, not MT6328 evidence. */
    assert(!i14_charge_selftest(&ops,&s) && s.proof_valid && !f.enables);
    unsigned gate=256,feed=256,flag=256;
    for(unsigned i=0;i<f.writes;i++) {
        struct op *o=&f.log[i];
        if(o->addr==0xf48 && o->mask==8 && o->value==8 && gate==256)gate=i;
        if(o->addr==0xf62 && o->mask==256 && o->value==256 && feed==256)feed=i;
        if(o->addr==0xf66 && o->mask==2 && o->value==2 && flag==256)flag=i;
    }
    assert(feed<gate && flag<gate);
    init(&f); memset(&s,0,sizeof(s)); ops=io(&f);
    f.reg[0xf66]=4; f.edge_only=1;
    assert(!i14_charge_selftest(&ops,&s) && s.proof_valid && !f.enables && f.expiry_events==1);
    init(&f); memset(&s,0,sizeof(s)); ops=io(&f);
    f.reg[0xf66]=4; f.no_reset_clear=1; f.block_gate_while_out=1;
    assert(i14_charge_selftest(&ops,&s)<0 && !s.proof_valid && !f.enables);
    for(unsigned i=0;i<f.writes;i++)
        assert(!(f.log[i].addr==0xf48 && f.log[i].mask==8 && f.log[i].value));
    for(unsigned cause=0;cause<2;cause++) {
        init(&f); memset(&s,0,sizeof(s)); ops=io(&f);
        if(cause==0)f.ignore_address=0xf48;
        else f.expire_on_gate=1;
        assert(i14_charge_selftest(&ops,&s)==-EIO && !s.proof_valid && !f.enables);
        assert(s.hist_out.seen && !(s.hist_out.raw&4) && !strcmp(s.hist_out.phase,"wdt-clear"));
        assert(s.hist_mode.seen && !strcmp(s.hist_mode.phase,"selftest-mode-init"));
        assert((s.hist_mode.raw&4) && s.hist_mode.mask==4 && s.hist_mode.expected==4);
        assert(!strcmp(s.diag.phase,"selftest-gate-on-chr-off"));
        uncertain_stops_all(&f,&s);
    }
    init(&f); memset(&s,0,sizeof(s)); ops=io(&f);
    f.edge_only=1; f.expire_before_gate=1;
    assert(i14_charge_selftest(&ops,&s)==-EPROTO && !s.proof_valid && !f.enables);
    assert(s.stop_verified && !s.uncertain && !strcmp(s.phase,"selftest-expired-gate"));
}
static void mode_init_candidate(void)
{
    struct fake f; struct i14_charge_state s={0};
    init(&f); f.state=&s;
    struct i14_charge_io ops=io(&f);
    /* Reverting MODE init must fail here under the hypothesis model. */
    assert(!i14_charge_selftest(&ops,&s) && s.proof_valid && s.proof_gate_clear);
    assert(s.stop_verified && !s.running && !s.uncertain && !f.enables);
    assert(f.reg[0xf7a]==0x8094 && !(f.reg[0xf48]&24));
    assert(f.writes==19); /* Original two six-write stops, WDT5, gate1, MODE1. */
    assert(f.log[6].addr==0xf7a && f.log[6].mask==4 && f.log[6].value==4);
    assert(f.log[7].addr==0xf62 && f.log[7].mask==15 && !f.log[7].value);
    assert(f.log[11].addr==0xf66 && f.log[11].mask==2 && f.log[11].value==2);
    assert(f.log[12].addr==0xf48 && f.log[12].mask==8 && f.log[12].value==8);
    init(&f); memset(&s,0,sizeof(s)); ops=io(&f); f.state=&s; f.ignore_address=0xf7a;
    assert(i14_charge_selftest(&ops,&s)==-EIO && s.uncertain && !s.proof_valid);
    assert(s.initial_stop_completed && !s.stop_verified && !f.enables && f.writes==7);
    assert(!(f.reg[0xf48]&24) && !(f.reg[0xf62]&16) && !(f.reg[0xf66]&3));
    assert(s.diag.address==0xf7a && s.diag.mask==4 && s.diag.expected==4);
    assert(s.diag.update_rc_known && !s.diag.update_rc && s.diag.read_rc_known && !s.diag.read_rc);
    assert(s.diag.compare_known && !s.diag.compare_match && s.diag.observed==0x8090);
    uncertain_stops_all(&f,&s);
    /* MODE latency consumes the original setup budget; no renewed deadline. */
    init(&f); memset(&s,0,sizeof(s)); ops=io(&f);
    f.delay_ns=1000000; f.mode_delay_ns=90000000;
    assert(i14_charge_selftest(&ops,&s)==-ETIMEDOUT && !s.proof_valid && !f.enables);
    assert(s.stop_verified && !s.uncertain && !strcmp(s.phase,"wdt-flag"));
    for(unsigned i=0;i<f.writes;i++)
        assert(!(f.log[i].addr==0xf48 && f.log[i].mask==8 && f.log[i].value));
}
int main(void)
{
    mode_init_candidate();
    struct fake f; struct i14_charge_state s={0}; struct i14_charge_io ops;
    struct i14_charge_sample observation;
    init(&f); ops=io(&f); observation=sample(&f,1);
    assert(i14_charge_start(&ops,&s,I14_CHARGE_PILOT,&observation)<0 && !f.enables);
    proved(&f,&s); ops=io(&f); observation=sample(&f,1);
    assert(i14_charge_start(&ops,&s,I14_CHARGE_PILOT,&observation)==0);
    assert(s.running && !s.proof_valid && f.enables==1);
    assert((f.reg[0xf4a]&0xff)==0xa2 && (f.reg[0xf7a]&0x94)==0x94);
    assert((f.reg[0xf4e]&0x1f)==13 && (f.reg[0xf50]&0xf)==15);
    assert((f.reg[0xf54]&0xf)==5 && (f.reg[0xf54]&0x8000));
    f.now+=250000000; observation=sample(&f,2);
    assert(i14_charge_kick(&ops,&s,&observation)==0);
    assert(i14_charge_stop(&ops,&s)==0 && !s.running);
    assert(!(f.reg[0xf48]&0x18) && !(f.reg[0xf7a]&0x40) && !(f.reg[0xf62]&0x10));
    assert(i14_charge_start(&ops,&s,I14_CHARGE_PILOT,&observation)<0);
    failure_points(); guards(); watchdog_and_limits(); inherited_out_order();
    puts("charge bootstrap: MODE-init hypothesis/ignored-write latch, proof/gate/guards, fixed setpoints, stop order, all I/O failures and bounds PASS");
}
