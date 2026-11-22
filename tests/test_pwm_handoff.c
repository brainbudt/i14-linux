/* Compile an unmodified function slice of the actual patched public driver.
 * Removing held clock refs or period+1 must break the inherited-state tests.
 */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#define __iomem
typedef uint32_t u32;
typedef uint64_t u64;
struct clk { unsigned refs; unsigned enables,disables; int fail; u64 rate; };
struct pwm_chip { void *data; };
struct pwm_device { int unused; };
struct pwm_state { bool enabled; unsigned polarity; u64 period,duty_cycle; };
#define PWM_POLARITY_NORMAL 0
#define NSEC_PER_SEC UINT64_C(1000000000)
#define BIT(n) (1U<<(n))
#define FIELD_GET(mask,value) (((value)&(mask))>>__builtin_ctz(mask))
#define DIV64_U64_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define pwmchip_get_drvdata(c) ((c)->data)
#define pwmchip_parent(c) (c)
#define ERR_PTR(e) ((void *)(intptr_t)(e))
#define dev_err(...) ((void)0)
static unsigned writes;
static uint32_t readl(const void *p) { return *(const uint32_t *)p; }
static void writel(uint32_t v,void *p) { writes++;*(uint32_t *)p=v; }
static u64 clk_get_rate(struct clk *c) { return c->rate; }
static int clk_prepare_enable(struct clk *c)
{
    if(c->fail) return -EIO;
    c->refs++;c->enables++;return 0;
}
static void clk_disable_unprepare(struct clk *c)
{
    assert(c->refs);c->refs--;c->disables++;
}
static u64 mul_u64_u64_div_u64(u64 a,u64 b,u64 d) { return (__uint128_t)a*b/d; }
#include I14_PWM_FUNCTIONS
static const struct mtk_pwm_data own={.enable_mask=1,.con0=0x10,.con1=0x14,
    .has_commit=true,.commit=8,.commit_mask=1,.preserve_boot_state=true};
int main(void)
{
    uint32_t regs[16]={1,0,0,0,0,0x03ff03ff};
    struct clk main={.rate=26000000},mm={.rate=26000000};
    struct mtk_disp_pwm m={.data=&own,.clk_main=&main,.clk_mm=&mm,.base=regs};
    struct pwm_chip chip={.data=&m};struct pwm_device pwm={0};struct pwm_state s={0};
    assert(!mtk_disp_pwm_get_state(&chip,&pwm,&s));
    assert(s.enabled && s.period==39385 && s.duty_cycle==39347);
    assert(m.enabled && main.refs==1 && mm.refs==1);
    assert(main.disables==0 && mm.disables==0 && writes==0);
    assert(!mtk_disp_pwm_get_state(&chip,&pwm,&s));
    assert(main.enables==1 && mm.enables==1 && main.refs==1 && mm.refs==1);
    main.rate=0;
    assert(mtk_disp_pwm_get_state(&chip,&pwm,&s)==-EINVAL);
    assert(mtk_disp_pwm_apply(&chip,&pwm,&s)==-EINVAL);
    assert(main.refs==1 && mm.refs==1 && writes==0);
    main.rate=26000000;
    assert(!mtk_disp_pwm_apply(&chip,&pwm,&s));
    assert(regs[0]==1 && regs[4]==0 && regs[5]==0x03ff03ff && regs[2]==0);
    assert(main.enables==1 && mm.enables==1);
    unsigned before=writes;
    s.duty_cycle=s.period+1;
    assert(mtk_disp_pwm_apply(&chip,&pwm,&s)==-EINVAL);
    assert(main.refs==1 && mm.refs==1 && writes==before);
    s.duty_cycle=39347;s.polarity=1;
    assert(mtk_disp_pwm_apply(&chip,&pwm,&s)==-EINVAL);
    assert(main.refs==1 && mm.refs==1 && writes==before);
    s=(struct pwm_state){.period=39385,.duty_cycle=19693,.enabled=true};
    assert(!mtk_disp_pwm_apply(&chip,&pwm,&s));
    assert(regs[5]==0x020003ff && main.refs==1 && mm.refs==1);
    s.enabled=false;
    assert(!mtk_disp_pwm_apply(&chip,&pwm,&s));
    assert(regs[0]==0 && !m.enabled && main.refs==0 && mm.refs==0);
    assert(main.disables==1 && mm.disables==1);
    before=writes;
    assert(!mtk_disp_pwm_apply(&chip,&pwm,&s));
    assert(main.refs==0 && mm.refs==0 && writes==before);
    s.enabled=true;main.rate=0;
    assert(mtk_disp_pwm_get_state(&chip,&pwm,&s)==-EINVAL);
    assert(mtk_disp_pwm_apply(&chip,&pwm,&s)==-EINVAL);
    assert(main.enables==1 && mm.enables==1 && writes==before);
    main.rate=26000000;s.period=0;
    assert(mtk_disp_pwm_apply(&chip,&pwm,&s)==-EINVAL);
    s.period=1;s.duty_cycle=0;
    assert(mtk_disp_pwm_apply(&chip,&pwm,&s)==-EINVAL);
    s.period=UINT64_C(1000000000000);
    assert(mtk_disp_pwm_apply(&chip,&pwm,&s)==-EINVAL);
    assert(main.enables==1 && mm.enables==1 && writes==before);
    s.period=39385;s.duty_cycle=19693;mm.fail=1;
    assert(mtk_disp_pwm_get_state(&chip,&pwm,&s)==-EIO);
    assert(main.refs==0 && mm.refs==0 && !m.enabled);
    assert(mtk_disp_pwm_apply(&chip,&pwm,&s)==-EIO);
    assert(main.refs==0 && mm.refs==0 && !m.enabled);
    mm.fail=0;
    assert(!mtk_disp_pwm_apply(&chip,&pwm,&s));
    assert(main.refs==1 && mm.refs==1 && m.enabled);
    regs[0]=0;
    assert(!mtk_disp_pwm_get_state(&chip,&pwm,&s));
    assert(main.refs==0 && mm.refs==0 && !m.enabled);
    /* Full-active inherited PWM must also return a valid period/duty pair. */
    regs[0]=1;regs[5]=0x040003ff;
    assert(!mtk_disp_pwm_get_state(&chip,&pwm,&s));
    assert(s.period==39385 && s.duty_cycle==39385 && s.enabled);
    assert(!mtk_disp_pwm_apply(&chip,&pwm,&s));
    assert(regs[5]==0x040003ff && main.refs==1 && mm.refs==1);
    s.enabled=false;
    assert(!mtk_disp_pwm_apply(&chip,&pwm,&s));
    assert(main.refs==0 && mm.refs==0);
    /* Existing MT8173 behavior stays outside the I14 opt-in. */
    struct mtk_pwm_data legacy=own;legacy.preserve_boot_state=false;
    m.data=&legacy;regs[0]=1;regs[5]=0x03ff03ff;
    assert(!mtk_disp_pwm_get_state(&chip,&pwm,&s));
    assert(s.period==39347 && main.refs==0 && mm.refs==0 && !m.enabled);
    puts("PASS: real PWM functions preserve firmware refs, roundtrip counts, balance errors and disable");
}
