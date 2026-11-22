/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef I14_CHARGE_BOOTSTRAP_H
#define I14_CHARGE_BOOTSTRAP_H
#ifdef __KERNEL__
#include <linux/errno.h>
#else
#include <errno.h>
#endif
#include "i14-battery-temperature.h"

enum i14_charge_profile { I14_CHARGE_PILOT, I14_CHARGE_WALL };
struct i14_charge_io {
    void *context;
    int (*read)(void *, unsigned, unsigned *);
    int (*update)(void *, unsigned, unsigned, unsigned);
    unsigned long long (*now_ns)(void *);
    void (*pause)(void *);
};
struct i14_charge_sample {
    int vbat_mv, vbus_mv, ntc_mv;
    unsigned fresh_mask;
    unsigned long long sampled_ns, sequence;
    int external_power_confirmed;
};
struct i14_charge_diag {
    const char *operation, *phase, *stage;
    unsigned valid, address, mask, expected;
    unsigned update_attempted, update_rc_known, read_attempted, read_rc_known;
    int update_rc, read_rc;
    unsigned observed, observed_known, range_known, range_match, compare_known, compare_match;
};
struct i14_charge_seen {
    const char *phase, *operation;
    unsigned seen, raw, mask, expected, lo_known, hi_known;
    unsigned long long lo, hi;
};
struct i14_charge_state {
    const char *phase, *reason;
    unsigned address, dirty, uncertain, running, proof_valid, proof_gate_clear, stop_verified;
    int error;
    enum i14_charge_profile profile;
    void *proof_context;
    unsigned long long proof_ns, started_ns, last_feed_ns, last_sequence;
    unsigned long long deadline, clock_ns;
    struct i14_charge_diag diag;
    unsigned clock_known, clock_observed_known, clock_previous_known, deadline_known;
    unsigned long long clock_previous, clock_observed;
    unsigned initial_stop_completed;
    unsigned long long initial_stop_ns;
    struct i14_charge_seen hist_out, hist_mode;
};
/* Own-stock register proof: charging-lk-parameters-20261030 JSON; ramp values
 * charging_hw_pmic.c charging_enable, matched own charging_enable binary.
 * Caller serializes the verified provider and supplies genuinely fresh ADCs.
 * No hardware duration/current/pack-rating claim follows from these constants. */
#define I14_CHARGE_IO_NS 100000000ULL
#define I14_CHARGE_PROOF_NS 2000000000ULL
#define I14_CHARGE_FRESH_NS 500000000ULL

static inline int i14_c_error(struct i14_charge_state *s, int ret, unsigned uncertain)
{
    s->error = ret < 0 ? ret : -EIO;
    s->uncertain |= uncertain;
    s->proof_valid = 0;
    return s->error;
}
static inline int i14_c_now(struct i14_charge_io *io, struct i14_charge_state *s,
                            unsigned long long *now)
{
    if (s->uncertain) return -EUCLEAN;
    s->clock_previous=s->clock_ns; s->clock_previous_known=s->clock_known;
    *now = io->now_ns(io->context);
    s->clock_observed=*now; s->clock_observed_known=1;
    if (*now < s->clock_ns) return i14_c_error(s, -EIO, 1);
    s->clock_ns = *now;
    s->clock_known=1;
    return 0;
}
static inline int i14_c_begin(struct i14_charge_io *io, struct i14_charge_state *s,
                              unsigned long long duration)
{
    unsigned long long now;
    int ret = i14_c_now(io,s,&now);
    if (ret) return ret;
    if (now > ~0ULL-duration) return i14_c_error(s,-EOVERFLOW,1);
    s->deadline=now+duration; s->deadline_known=1; s->error=0;
    return 0;
}
static inline int i14_c_bound(struct i14_charge_io *io, struct i14_charge_state *s)
{
    unsigned long long now;
    int ret=i14_c_now(io,s,&now);
    if (ret) return ret;
    return now>=s->deadline ? i14_c_error(s,-ETIMEDOUT,0) : 0;
}
static inline int i14_c_slot(unsigned address)
{
    switch(address) {
    case 0xf48:return 0; case 0xf4c:return 1; case 0xf4e:return 2;
    case 0xf50:return 3; case 0xf54:return 4; case 0xf62:return 5;
    case 0xf66:return 6; case 0xf74:return 7; case 0xf76:return 8;
    case 0xf7a:return 9; case 0xf4a:return 10; default:return -1;
    }
}
static inline void i14_c_diag(struct i14_charge_state *s,const char *operation,
    const char *phase,unsigned address,unsigned mask,unsigned expected)
{
    if(!s->uncertain)s->diag=(struct i14_charge_diag){.operation=operation,
        .phase=phase,.stage="validation",.valid=1,.address=address,.mask=mask,.expected=expected};
}
static inline int i14_c_read_existing(struct i14_charge_io *io, struct i14_charge_state *s,
                              unsigned address, unsigned *value, const char *phase)
{
    int ret;
    struct i14_charge_seen *seen=NULL;
    unsigned long long before;
    unsigned before_known;
    s->phase=phase; s->address=address;
    if(i14_c_slot(address)<0 && address!=0xf4a && address!=0xf56 && address!=0xf9a)
        return i14_c_error(s,-EINVAL,1);
    if(!s->uncertain)s->diag.stage="read-pre-bound";
    ret=i14_c_bound(io,s); if(ret)return ret;
    before=s->clock_ns; before_known=s->clock_known;
    s->diag.stage="read-return"; s->diag.read_attempted=1;
    ret=io->read(io->context,address,value);
    s->diag.read_rc=ret; s->diag.read_rc_known=1;
    if(ret)return i14_c_error(s,ret,1);
    s->diag.observed=*value; s->diag.observed_known=1;
    s->diag.stage="read-range"; s->diag.range_known=1; s->diag.range_match=*value<=0xffff;
    if(*value>0xffff)return i14_c_error(s,-EIO,1);
    if(address==0xf66)seen=&s->hist_out;
    else if(address==0xf7a)seen=&s->hist_mode;
    if(seen)*seen=(struct i14_charge_seen){.phase=phase,.operation=s->diag.operation,
        .seen=1,.raw=*value,.mask=s->diag.mask,.expected=s->diag.expected,
        .lo_known=before_known,.lo=before};
    s->diag.stage="read-post-bound";
    ret=i14_c_bound(io,s); if(ret)return ret;
    if(seen){seen->hi=s->clock_ns;seen->hi_known=s->clock_known;}
    s->diag.stage="read-complete";
    return 0;
}
static inline int i14_c_read(struct i14_charge_io *io,struct i14_charge_state *s,
    unsigned address,unsigned *value,const char *phase)
{
    i14_c_diag(s,"read",phase,address,0,0);
    return i14_c_read_existing(io,s,address,value,phase);
}
static inline int i14_c_allowed(unsigned a,unsigned m,unsigned v)
{
    if(v&~m)return 0;
    switch(a) {
    case 0xf48:return (m==8 || m==16) && (v==0 || v==m);
    case 0xf4a:return m==0xf0 && v==0xa0;
    case 0xf4c:return (m==2 || m==8) && v==m;
    case 0xf4e:return m==31 && v==13;
    case 0xf50:return m==15 && (v==15 || v==12);
    case 0xf54:return m==15 && v==5; /* OV enabled, proven non-HV code2. */
    case 0xf62:return (m==15 && v==0) || (m==16 && (v==0 || v==16)) || (m==256 && v==256);
    case 0xf66:return (m==1 || m==2) && (v==0 || v==m);
    case 0xf74:return (m==7 && v==1) || (m==0x70 && v==0x20);
    case 0xf76:return (m==7 && v==4) || (m==0x70 && v==0x10);
    case 0xf7a:return (m==4 && v==4) || (m==0x40 && (v==0 || v==0x40));
    default:return 0;
    }
}
static inline int i14_c_update(struct i14_charge_io *io, struct i14_charge_state *s,
                                unsigned address,unsigned mask,unsigned value,const char *phase)
{
    unsigned observed;
    int ret;
    i14_c_diag(s,"update",phase,address,mask,value);
    s->phase=phase; s->address=address;
    if(!i14_c_allowed(address,mask,value))return i14_c_error(s,-EINVAL,1);
    if(!s->uncertain)s->diag.stage="update-pre-bound";
    ret=i14_c_bound(io,s); if(ret)return ret;
    s->dirty|=1U<<i14_c_slot(address);
    s->diag.stage="update-return"; s->diag.update_attempted=1;
    ret=io->update(io->context,address,mask,value);
    s->diag.update_rc=ret; s->diag.update_rc_known=1;
    if(ret)return i14_c_error(s,ret,1);
    s->diag.stage="update-post-bound";
    ret=i14_c_bound(io,s); if(ret)return ret;
    /* WR/FLAG_WR are strobes, not stable readback values. OUT is checked separately. */
    if((address==0xf62 && mask==256) || (address==0xf66 && mask==2)) {
        s->diag.stage="strobe-complete"; return 0;
    }
    ret=i14_c_read_existing(io,s,address,&observed,phase); if(ret)return ret;
    s->diag.stage="compare"; s->diag.compare_known=1; s->diag.compare_match=(observed&mask)==value;
    return (observed&mask)==value ? 0 : i14_c_error(s,-EIO,1);
}
static inline int i14_charge_stop(struct i14_charge_io *io, struct i14_charge_state *s)
{
    static const unsigned a[]={0xf48,0xf48,0xf7a,0xf62,0xf66,0xf66};
    static const unsigned m[]={16,8,0x40,16,1,2};
    unsigned i;
    int ret=i14_c_begin(io,s,I14_CHARGE_IO_NS);
    if(ret)return ret;
    s->proof_valid=0; s->stop_verified=0;
    for(i=0;i<6;i++) {
        ret=i14_c_update(io,s,a[i],m[i],0,"stop");
        if(ret)return ret;
    }
    s->running=0; s->stop_verified=1; s->phase="stopped"; s->error=0;
    if(!s->initial_stop_completed) {
        s->initial_stop_completed=1; s->initial_stop_ns=s->clock_ns;
    }
    return 0;
}
static inline int i14_c_stop_reason(struct i14_charge_io *io,struct i14_charge_state *s,
                                    int error,const char *reason)
{
    const char *phase=s->phase;
    unsigned address=s->address;
    int ret;
    s->reason=reason;
    if(s->uncertain)return s->error;
    ret=i14_charge_stop(io,s);
    if(ret)return ret;
    s->phase=phase; s->address=address; s->error=error;
    return error;
}
static inline int i14_c_watchdog(struct i14_charge_io *io,struct i14_charge_state *s,int arm)
{
    unsigned value,poll;
    unsigned long long feed_at;
    int ret;
    if(arm) {
        ret=i14_c_update(io,s,0xf62,15,0,"wdt-code0"); if(ret)return ret;
        ret=i14_c_update(io,s,0xf66,1,1,"wdt-int-enable"); if(ret)return ret;
        ret=i14_c_update(io,s,0xf62,16,16,"wdt-enable"); if(ret)return ret;
    }
    ret=i14_c_now(io,s,&feed_at); if(ret)return ret;
    ret=i14_c_update(io,s,0xf62,256,256,"wdt-feed"); if(ret)return ret;
    ret=i14_c_update(io,s,0xf66,2,2,"wdt-flag"); if(ret)return ret;
    for(poll=0;poll<1000;poll++) {
        ret=i14_c_read(io,s,0xf66,&value,"wdt-clear"); if(ret)return ret;
        if(!(value&4)){s->last_feed_ns=feed_at;return 0;}
        if(!arm)return i14_c_error(s,-EIO,0);
        io->pause(io->context);
    }
    return i14_c_error(s,-ETIMEDOUT,0);
}
static inline int i14_charge_selftest(struct i14_charge_io *io,struct i14_charge_state *s)
{
    unsigned value,gate,poll;
    int ret;
    if(s->uncertain)return -EUCLEAN;
    if(s->running)return -EBUSY;
    s->proof_valid=0; s->proof_gate_clear=0; s->reason="selftest";
    ret=i14_c_begin(io,s,I14_CHARGE_IO_NS); if(ret)return ret;
    ret=i14_c_read(io,s,0xf48,&gate,"selftest-off"); if(ret)return ret;
    if(gate&16)return i14_c_error(s,-EBUSY,0);
    ret=i14_charge_stop(io,s); if(ret)return ret;
    ret=i14_c_begin(io,s,I14_CHARGE_IO_NS); if(ret)return ret;
    s->stop_verified=0;
    /* Hypothesis candidate: own-stock MODE init while the stopped gates stay0. */
    ret=i14_c_update(io,s,0xf7a,4,4,"selftest-mode-init"); if(ret)goto failed;
    ret=i14_c_watchdog(io,s,1); if(ret)goto failed;
    ret=i14_c_update(io,s,0xf48,8,8,"selftest-gate-on-chr-off"); if(ret)goto failed;
    ret=i14_c_read(io,s,0xf48,&gate,"selftest-baseline"); if(ret)goto failed;
    if((gate&24)!=8){ret=-EPROTO;goto failed;}
    /* OUT0 was observed above. Success requires the actual CSDAC gate to clear
     * at OUT1 while CHR_EN stays0; this is not a physical current-cutoff test. */
    ret=i14_c_begin(io,s,6000000000ULL); if(ret)goto failed;
    for(poll=0;poll<12000;poll++) {
        ret=i14_c_read(io,s,0xf48,&gate,"selftest-chr-off"); if(ret)goto failed;
        if(gate&16){ret=-EPROTO;goto failed;}
        ret=i14_c_read(io,s,0xf66,&value,"selftest-expiry"); if(ret)goto failed;
        if(value&4) {
            ret=i14_c_read(io,s,0xf48,&gate,"selftest-expired-gate"); if(ret)goto failed;
            if(gate&24){ret=-EPROTO;goto failed;}
            ret=i14_charge_stop(io,s); if(ret)return ret;
            ret=i14_c_now(io,s,&s->proof_ns); if(ret)return ret;
            s->proof_valid=1; s->proof_gate_clear=1; s->proof_context=io->context;
            s->phase="selftest-gate-proved"; s->reason="";
            return 0;
        }
        if(!(gate&8)){ret=-EPROTO;goto failed;} /* Gate loss before OUT is not WDT proof. */
        io->pause(io->context);
    }
    ret=-ETIMEDOUT;
failed:
    return i14_c_stop_reason(io,s,ret,"watchdog-selftest-failed");
}
static inline int i14_c_sample(struct i14_charge_state *s,const struct i14_charge_sample *v,
                                unsigned long long now,int starting)
{
    int temperature;
    if(!v || v->fresh_mask!=7 || !v->sequence || v->sequence<=s->last_sequence ||
       v->sampled_ns>now || now-v->sampled_ns>I14_CHARGE_FRESH_NS ||
       (starting && v->sampled_ns<s->proof_ns))return -ESTALE;
    if(v->vbus_mv<4500 || v->vbus_mv>5500 || v->vbat_mv<3000 || v->vbat_mv>=3800 ||
       i14_battery_temperature_mc(v->ntc_mv,&temperature) ||
       i14_battery_temperature_probe_guard(0,temperature)!=1)return -ERANGE;
    if(!v->external_power_confirmed ||
       (s->profile==I14_CHARGE_WALL && v->vbat_mv<3300))return -EPERM;
    return 0;
}
static inline int i14_c_hardware(struct i14_charge_io *io,struct i14_charge_state *s,int active)
{
    unsigned chr,presence,undetected,ov,mode,value;
    int ret;
    ret=i14_c_read(io,s,0xf48,&chr,"charger-gates"); if(ret)return ret;
    if(!(chr&32) || !(chr&1) || (chr&128) ||
       (active==1 ? (chr&24)!=24 : (chr&16)!=0) ||
       (active==2 && !(chr&8)))return -EPERM;
    ret=i14_c_read(io,s,0xf4a,&value,"input-hv-threshold"); if(ret)return ret;
    if(active && (value&0xf0)!=0xa0)return -EPERM; /* Own stock code10 = 6500 mV. */
    ret=i14_c_read(io,s,0xf56,&presence,"presence-detectors"); if(ret)return ret;
    if((presence&5)!=5)return -EPERM;
    ret=i14_c_read(io,s,0xf9a,&undetected,"battery-present"); if(ret)return ret;
    if(undetected&1)return -EPERM;
    ret=i14_c_read(io,s,0xf54,&ov,"ov-protection"); if(ret)return ret;
    if(!(ov&1) || (ov&64) || ((ov>>1)&7)>3 ||
       (active && (ov&15)!=5))return -EPERM;
    ret=i14_c_read(io,s,0xf7a,&mode,"csdac-mode"); if(ret)return ret;
    if(active && (!(mode&4) || !(mode&64)))return -EPERM;
    if(!active)return 0;
    ret=i14_c_read(io,s,0xf4e,&value,"cv-code"); if(ret)return ret;
    if((value&31)!=13)return -EPERM;
    ret=i14_c_read(io,s,0xf50,&value,"cs-code"); if(ret)return ret;
    if((value&15)!=(s->profile==I14_CHARGE_PILOT?15U:12U))return -EPERM;
    ret=i14_c_read(io,s,0xf4c,&value,"cv-cs-enabled"); if(ret)return ret;
    if((value&10)!=10)return -EPERM;
    ret=i14_c_read(io,s,0xf62,&value,"wdt-live"); if(ret)return ret;
    if((value&31)!=16)return -EPERM;
    ret=i14_c_read(io,s,0xf66,&value,"wdt-no-expiry"); if(ret)return ret;
    return (value&5)==1 ? 0 : -EPERM;
}
static inline int i14_charge_start(struct i14_charge_io *io,struct i14_charge_state *s,
    enum i14_charge_profile profile,const struct i14_charge_sample *sample)
{
    /* The real handoff snapshot has reset HV/OV/mode values. Initialize
     * only own-stock fields while CHR_EN0, then recheck configured guards. */
    static const unsigned a[]={0xf4a,0xf7a,0xf4e,0xf54,0xf76,0xf76,0xf74,0xf74,0xf4c,0xf7a,0xf4c,0xf48};
    static const unsigned m[]={0xf0,4,31,15,7,0x70,7,0x70,8,0x40,2,8};
    static const unsigned v[]={0xa0,4,13,5,4,0x10,1,0x20,8,0x40,2,8};
    unsigned i;
    unsigned long long now;
    int ret;
    if(s->uncertain)return -EUCLEAN;
    if(s->running)return -EBUSY;
    ret=i14_c_now(io,s,&now); if(ret)return ret;
    if(!s->proof_valid || !s->proof_gate_clear || s->proof_context!=io->context ||
       now<s->proof_ns || now-s->proof_ns>I14_CHARGE_PROOF_NS) {
        s->proof_valid=0; return i14_c_error(s,-ENODATA,0);
    }
    s->proof_valid=0; /* Every attempt consumes its proof, even a guard refusal. */
    if(profile!=I14_CHARGE_PILOT && profile!=I14_CHARGE_WALL)return i14_c_error(s,-EINVAL,0);
    s->profile=profile; s->reason="start-guard";
    ret=i14_c_sample(s,sample,now,1); if(ret)return i14_c_error(s,ret,0);
    ret=i14_c_begin(io,s,I14_CHARGE_IO_NS); if(ret)return ret;
    ret=i14_c_hardware(io,s,0); if(ret)goto failed;
    s->stop_verified=0;
    ret=i14_c_update(io,s,0xf50,15,profile==I14_CHARGE_PILOT?15:12,"set-current"); if(ret)goto failed;
    for(i=0;i<12;i++) {
        ret=i14_c_update(io,s,a[i],m[i],v[i],"configure"); if(ret)goto failed;
    }
    ret=i14_c_watchdog(io,s,1); if(ret)goto failed;
    ret=i14_c_hardware(io,s,2); if(ret)goto failed;
    ret=i14_c_now(io,s,&now); if(ret)goto failed;
    ret=i14_c_sample(s,sample,now,1); if(ret)goto failed;
    ret=i14_c_update(io,s,0xf48,16,16,"enable-charge"); if(ret)goto failed;
    s->running=1; s->started_ns=now; s->last_sequence=sample->sequence;
    s->phase="charging"; s->reason="";
    return 0;
failed:
    return i14_c_stop_reason(io,s,ret,"start-failed");
}
static inline int i14_charge_kick(struct i14_charge_io *io,struct i14_charge_state *s,
    const struct i14_charge_sample *sample)
{
    unsigned long long now,limit;
    int ret;
    if(s->uncertain)return -EUCLEAN;
    if(!s->running)return -ENODATA;
    ret=i14_c_now(io,s,&now); if(ret)return ret;
    limit=s->profile==I14_CHARGE_PILOT?5000000000ULL:600000000000ULL;
    s->phase="kick-guard"; s->address=0;
    if(now-s->started_ns>=limit)return i14_c_stop_reason(io,s,-ETIME,"time-limit");
    if(now-s->last_feed_ns>=1000000000ULL)return i14_c_stop_reason(io,s,-ETIME,"feed-late");
    ret=i14_c_sample(s,sample,now,0); if(ret)return i14_c_stop_reason(io,s,ret,"sample-guard");
    ret=i14_c_begin(io,s,I14_CHARGE_IO_NS); if(ret)return ret;
    ret=i14_c_hardware(io,s,1); if(ret)goto failed;
    ret=i14_c_now(io,s,&now); if(ret)goto failed;
    if(now-s->started_ns>=limit || now-s->last_feed_ns>=1000000000ULL) {
        ret=-ETIME; goto failed;
    }
    ret=i14_c_sample(s,sample,now,0); if(ret)goto failed;
    ret=i14_c_watchdog(io,s,0); if(ret)goto failed;
    s->last_sequence=sample->sequence; s->phase="charging";
    return 0;
failed:
    return i14_c_stop_reason(io,s,ret,"kick-failed");
}
#endif
