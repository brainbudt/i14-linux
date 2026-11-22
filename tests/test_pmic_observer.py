"""Offline observer contract: real C core/frontend with fake kernel boundaries."""
from pathlib import Path
import hashlib
import os
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'kernel/i14-pmic-observer-core.h'
FRONT = ROOT / 'kernel/i14-pmic-observer.c'
ARTIFACT = ROOT / 'artifacts/pmic-ro-observer-prep-20261102'

HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "CORE"
static const unsigned expected[19]={0xf48,0xf4a,0xf4c,0xf4e,0xf50,0xf52,0xf54,0xf56,0xf62,0xf66,0xf68,0xf70,0xf72,0xf74,0xf76,0xf78,0xf7a,0xf9a,0xf9e};
struct fake { atomic_int claimed; atomic_int claims; int prepares,finishes,reads,at,rc,range,active,prepare_rc,clock_fault; unsigned clock_calls; };
static int claim(void *v) { struct fake *f=v;int zero=0;atomic_fetch_add(&f->claims,1);return atomic_compare_exchange_strong(&f->claimed,&zero,1)?0:-EALREADY; }
static int prepare(void *v) { struct fake *f=v;f->prepares++;return f->prepare_rc; }
static void finish(void *v) { ((struct fake*)v)->finishes++; }
static unsigned long long now(void *v) { struct fake *f=v;f->clock_calls++;if((f->clock_fault==1 && f->clock_calls==3)||(f->clock_fault==3 && f->clock_calls==2))return 0;if((f->clock_fault==2 && f->clock_calls>=3)||(f->clock_fault==4 && f->clock_calls>=2)||(f->clock_fault==6 && f->clock_calls>=4))return 1000000001ULL;if(f->clock_fault==5 && f->clock_calls==4)return 2;return f->clock_calls; }
static int rd(void *v,unsigned address,unsigned *value) {
 struct fake *f=v;int index=f->reads++;assert(index<19 && address==expected[index]);
 if(index==f->at && f->rc)return f->rc;
 *value=0x1000u+(unsigned)index;if(index==0)*value|=(unsigned)f->active;
 if(index==f->at && f->range)*value=0x10000u;return 0;
}
static struct i14_observer_io ops(struct fake *f) { return (struct i14_observer_io){f,claim,prepare,rd,finish,now}; }
static int observe(struct fake *f,struct i14_observer_state *s) { struct i14_observer_io o=ops(f);return i14_observer_observe(s,&o,"observe\n",8,1,1); }
struct thread_arg { struct fake *f;struct i14_observer_state *s;atomic_int *start;int rc; };
static void *thread_run(void *v) { struct thread_arg *a=v;while(!atomic_load(a->start)){} a->rc=observe(a->f,a->s);return NULL; }
int main(int argc,char **argv) {
 assert(argc>=2);struct fake f={0};struct i14_observer_state s={0};int rc=0;
 if(!strcmp(argv[1],"success")) {
  assert(!observe(&f,&s));assert(f.reads==19 && f.prepares==1 && f.finishes==1);
  assert(s.valid_mask==0x7ffffu && s.phase==I14_OBSERVER_COMPLETE && s.rc==0 && s.read_calls==19);
  for(unsigned i=0;i<19;i++){assert(s.values[i]==0x1000u+i);assert(s.read_start_ns[i]<=s.read_end_ns[i]);}
  int reads=f.reads;assert(observe(&f,&s)==-EALREADY && f.reads==reads && f.prepares==1);return 0;
 }
 if(!strcmp(argv[1],"error")) {
  f.at=atoi(argv[2]);f.rc=atoi(argv[3]);rc=observe(&f,&s);
  assert(rc==(f.rc<0?f.rc:-EIO) && f.reads==f.at+1 && s.read_calls==(unsigned)f.reads);
  assert(s.valid_mask==((1u<<(unsigned)f.at)-1) && s.failed_index==(unsigned)f.at && s.phase==I14_OBSERVER_READ_ERROR);
  assert(f.finishes==1 && observe(&f,&s)==-EALREADY && f.reads==f.at+1);return 0;
 }
 if(!strcmp(argv[1],"range")) {
  f.at=atoi(argv[2]);f.range=1;assert(observe(&f,&s)==-ERANGE);
  assert(f.reads==f.at+1 && s.valid_mask==((1u<<(unsigned)f.at)-1));
  assert(s.phase==I14_OBSERVER_RANGE_ERROR && s.failed_raw_valid==1 && s.failed_raw==0x10000u);
  assert(observe(&f,&s)==-EALREADY && f.reads==f.at+1);return 0;
 }
 if(!strcmp(argv[1],"active")) {
  f.active=atoi(argv[2]);assert(observe(&f,&s)==-EBUSY);
  assert(f.reads==1 && s.valid_mask==1 && s.failed_index==0 && s.phase==I14_OBSERVER_ACTIVE_CONFIG);
  assert(s.values[0]==(0x1000u|(unsigned)f.active));assert(observe(&f,&s)==-EALREADY && f.reads==1);return 0;
 }
 if(!strcmp(argv[1],"chr_det_only")) { f.active=0x20;assert(!observe(&f,&s) && f.reads==19);return 0; }
 if(!strcmp(argv[1],"invalid")) {
  static const char *const commands[]={"observe","observe\r\n","observe\nx","","Observe\n","observe\0\n"};
  static const size_t lengths[]={7,9,9,0,8,9};struct i14_observer_io o=ops(&f);
  for(unsigned i=0;i<6;i++)assert(i14_observer_observe(&s,&o,commands[i],lengths[i],1,1)==-EINVAL);
  assert(atomic_load(&f.claims)==0 && f.prepares==0 && f.reads==0);
  assert(!observe(&f,&s) && f.reads==19);return 0;
 }
 if(!strcmp(argv[1],"auth")) {
  struct i14_observer_io o=ops(&f);int root=atoi(argv[2]),cap=atoi(argv[3]);
  assert(i14_observer_observe(&s,&o,"observe\n",8,root,cap)==-EPERM);
  assert(atomic_load(&f.claims)==0 && !f.prepares && !f.reads && !s.attempted);return 0;
 }
 if(!strcmp(argv[1],"metadata")) {
  f.prepare_rc=-EOPNOTSUPP;assert(observe(&f,&s)==-EOPNOTSUPP);
  assert(atomic_load(&f.claimed)==1 && !f.reads && f.prepares==1 && f.finishes==1);
  assert(s.phase==I14_OBSERVER_METADATA_ERROR && s.valid_mask==0 && s.failed_index==19);
  assert(observe(&f,&s)==-EALREADY && !f.reads);return 0;
 }
 if(!strcmp(argv[1],"clock")) {
  f.clock_fault=atoi(argv[2]);assert(observe(&f,&s)==(f.clock_fault%2?-EIO:-ETIMEDOUT));
  assert(f.reads==(f.clock_fault<3?1:f.clock_fault<5?0:1) && s.valid_mask==(f.clock_fault>=5?1u:0u) && f.finishes==1);
  assert(s.phase==(f.clock_fault%2?I14_OBSERVER_CLOCK_ERROR:I14_OBSERVER_TIME_BUDGET));return 0;
 }
 if(!strcmp(argv[1],"concurrent")) {
  pthread_t threads[8];struct thread_arg args[8];atomic_int start=0;
  for(unsigned i=0;i<8;i++){args[i]=(struct thread_arg){&f,&s,&start,99};assert(!pthread_create(&threads[i],NULL,thread_run,&args[i]));}
  atomic_store(&start,1);unsigned won=0;
  for(unsigned i=0;i<8;i++){assert(!pthread_join(threads[i],NULL));won+=args[i].rc==0;assert(args[i].rc==0 || args[i].rc==-EALREADY);}
  assert(won==1 && f.reads==19 && f.prepares==1 && f.finishes==1);return 0;
 }
 if(!strcmp(argv[1],"format")) {
  char text[4096],small[16];int mode=atoi(argv[2]);if(mode){f.at=3;f.rc=-EIO;}else assert(!observe(&f,&s));
  if(mode)assert(observe(&f,&s)==-EIO);int reads=f.reads;
  int n=i14_observer_format(&s,text,sizeof(text));assert(n>0 && n<4096 && f.reads==reads);
  assert(strstr(text,"charging_status=UNKNOWN\n") && strstr(text,"handoff_qualified=0\n") && strstr(text,"read_side_effects=UNKNOWN\n"));
  if(mode){assert(strstr(text,"known_valid=0x00007\n") && strstr(text,"raw[0f4e]=UNKNOWN"));}
  else assert(strstr(text,"known_valid=0x7ffff\n") && strstr(text,"raw[0f9e]=0x1012"));
  assert(i14_observer_format(&s,small,sizeof(small))==-ENOSPC && f.reads==reads);fputs(text,stdout);return 0;
 }
 if(!strcmp(argv[1],"maxformat")) {
  char text[4096];assert(!observe(&f,&s));s.started_ns=s.ended_ns=~0ULL;
  for(unsigned i=0;i<19;i++){s.values[i]=0xffff;s.read_start_ns[i]=s.read_end_ns[i]=~0ULL;}
  int n=i14_observer_format(&s,text,sizeof(text));assert(n>0 && n<4096);printf("%d\n",n);return 0;
 }
 return 9;
}
'''

KERNEL_SHIM = r'''
#include <assert.h>
#include <errno.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#define __init
#define __exit
#define PAGE_SIZE 4096
#define CAP_SYS_RAWIO 17
#define GLOBAL_ROOT_UID 0
#define THIS_MODULE ((void*)1)
#define DEFINE_MUTEX(n) struct mutex n={0}
#define ATOMIC_INIT(v) ATOMIC_VAR_INIT(v)
typedef atomic_int atomic_t;
static int atomic_cmpxchg(atomic_t *v,int old,int new_value){atomic_compare_exchange_strong(v,&old,new_value);return old;}
struct mutex { int held; };
static void mutex_lock(struct mutex *m){assert(!m->held);m->held=1;}
static void mutex_unlock(struct mutex *m){assert(m->held);m->held=0;}
struct kobject { int dummy; };
struct attribute { const char *name;unsigned mode; };
struct kobj_attribute {struct attribute attr;ssize_t(*show)(struct kobject*,struct kobj_attribute*,char*);ssize_t(*store)(struct kobject*,struct kobj_attribute*,const char*,size_t);};
struct attribute_group {struct attribute **attrs;};
#define __ATTR(_n,_m,_s,_t) {{#_n,_m},_s,_t}
#define module_init(n) int wrapped_init(void){return n();}
#define module_exit(n) void wrapped_exit(void){n();}
#define MODULE_LICENSE(x)
#define MODULE_DESCRIPTION(x)
static struct kobject fake_object,*kernel_kobj=&fake_object;
static int root_uid=0,root_euid=0,has_cap=1,refs=0,ref_puts=0,reads=0,lookups=0,groups=0,module_available=1;
static int current_uid(void){return root_uid;}static int current_euid(void){return root_euid;}
static int uid_eq(int a,int b){return a==b;}static int capable(int capability){assert(capability==CAP_SYS_RAWIO);return has_cap;}
static int try_module_get(void *m){assert(m==THIS_MODULE);if(!module_available)return 0;refs++;return 1;}static void module_put(void *m){assert(m==THIS_MODULE);refs--;ref_puts++;}
static unsigned long long ktime_get_ns(void){static unsigned long long n;return ++n;}
static struct kobject *kobject_create_and_add(const char *n,struct kobject *parent){assert(!strcmp(n,"i14_pmic_observer") && parent==kernel_kobj);return &fake_object;}
static void kobject_put(struct kobject *k){assert(k==&fake_object);}
static int sysfs_create_group(struct kobject *k,const struct attribute_group *g){assert(k==&fake_object);assert(g->attrs[0] && g->attrs[1] && !g->attrs[2]);assert(!strcmp(g->attrs[0]->name,"state") && g->attrs[0]->mode==0444);assert(!strcmp(g->attrs[1]->name,"observe") && g->attrs[1]->mode==0200);groups++;return 0;}
static void sysfs_remove_group(struct kobject *k,const struct attribute_group *g){(void)g;assert(k==&fake_object);groups--;}
typedef unsigned short u16;
struct notifier_block{int(*notifier_call)(struct notifier_block*,unsigned long,void*);struct notifier_block *next;int priority;};
struct device_node{const char *compatible;int refs;};struct device_driver{const char *name;};struct regmap{int dummy;};
struct device{struct device *parent;struct device_driver *driver;struct device_node *of_node;void *data;};
struct platform_device{struct device dev;};
#include <linux/mfd/mt6397/core.h>
static struct device_node pmic_node={"mediatek,mt6328",0},duplicate_node={"mediatek,mt6328",0},pwrap_node={"mediatek,mt6735-pwrap",0},bad_node={"wrong",0};
static struct device_driver child_driver={"mt6397"},parent_driver={"mt-pmic-pwrap"},bad_driver={"wrong"};
static struct device parent_device;static struct platform_device pmic_device;
static struct mt6397_chip cached_chip;static struct regmap real_map,wrong_map;
static int metadata_variant,pmic_refs,parent_refs,lock_step;
static void metadata_setup(int variant){
 metadata_variant=variant;parent_device=(struct device){NULL,&parent_driver,&pwrap_node,NULL};pmic_device=(struct platform_device){{&parent_device,&child_driver,&pmic_node,&cached_chip}};
 cached_chip=(struct mt6397_chip){.dev=&pmic_device.dev,.regmap=&real_map,.chip_id=MT6328_CHIP_ID};
 if(variant==2)parent_device.driver=&bad_driver;if(variant==3)pmic_device.dev.driver=&bad_driver;
 if(variant==4)pmic_device.dev.of_node=&bad_node;if(variant==5)parent_device.of_node=&bad_node;
 if(variant==6)cached_chip.chip_id=0x32;if(variant==8)pmic_device.dev.data=NULL;
 if(variant==9)cached_chip.dev=&parent_device;if(variant==11)pmic_device.dev.parent=NULL;
 if(variant==12)parent_device.driver=NULL;if(variant==13)pmic_device.dev.driver=NULL;
 if(variant==14)cached_chip.regmap=NULL;
}
static int of_machine_is_compatible(const char *s){assert(!strcmp(s,"magcomm,magc6735-65u-1-m0"));return metadata_variant!=1;}
static void of_node_put(struct device_node *n){assert(n && n->refs>0);n->refs--;}
static struct device_node *of_node_get(struct device_node *n){assert(n);n->refs++;return n;}
static struct device_node *of_find_compatible_node(struct device_node *a,const char *b,const char *c){
 assert(!b && !strcmp(c,"mediatek,mt6328"));lookups++;
 if(!a){pmic_node.refs++;return &pmic_node;}
 assert(a==&pmic_node);of_node_put(a);
 if(metadata_variant==10){duplicate_node.refs++;return &duplicate_node;}return NULL;
}
static struct platform_device *of_find_device_by_node(struct device_node *n){assert(n==&pmic_node);lookups++;pmic_refs++;return &pmic_device;}
static int of_device_is_compatible(const struct device_node *n,const char *s){return n && !strcmp(n->compatible,s);}
static struct device *get_device(struct device *d){if(d){assert(d==&parent_device);parent_refs++;}return d;}
static void put_device(struct device *d){if(d==&parent_device){assert(parent_refs>0);parent_refs--;}else{assert(d==&pmic_device.dev && pmic_refs>0);pmic_refs--;}}
static void device_lock(struct device *d){if(d==&parent_device){assert(lock_step==0);lock_step=1;}else{assert(d==&pmic_device.dev && lock_step==1);lock_step=2;}}
static void device_unlock(struct device *d){if(d==&parent_device){assert(lock_step==1);lock_step=0;}else{assert(d==&pmic_device.dev && lock_step==2);lock_step=1;}}
static void *dev_get_drvdata(const struct device *d){return d->data;}
static struct regmap *dev_get_regmap(struct device *d,const char *n){assert(d==&parent_device && !n);lookups++;return metadata_variant==7?&wrong_map:&real_map;}
static int regmap_read(struct regmap *m,unsigned a,unsigned *v){(void)m;(void)a;(void)v;reads++;return -EIO;}
#include "FRONT"
int main(int argc,char **argv){
 assert(argc>=2);metadata_setup(!strcmp(argv[1],"metadata")?atoi(argv[2]):0);char text[4096];assert(!wrapped_init());assert(groups==1 && !reads && !lookups && !refs);
 assert(state_show(&fake_object,&state_attribute,text)>0 && strstr(text,"kind=historical-read-only-observation"));assert(!reads && !lookups);
 if(!strcmp(argv[1],"idle")){wrapped_exit();assert(!groups && !reads && !lookups && !refs);return 0;}
 if(!strcmp(argv[1],"going")){module_available=0;assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-ENODEV);assert(!refs && !reads && !lookups);assert(state_show(&fake_object,&state_attribute,text)>0 && strstr(text,"attempted=0"));return 0;}
 if(!strcmp(argv[1],"maxreport")){observation.attempted=1;observation.valid_mask=0x7ffff;observation.phase=I14_OBSERVER_COMPLETE;observation.failed_index=19;observation.started_ns=observation.ended_ns=~0ULL;for(unsigned i=0;i<19;i++){observation.values[i]=0xffff;observation.read_start_ns[i]=observation.read_end_ns[i]=~0ULL;}ssize_t n=state_show(&fake_object,&state_attribute,text);assert(n>0 && n<4096 && !reads && !lookups);return 0;}
 if(!strcmp(argv[1],"auth")){
  root_uid=501;assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-EPERM);root_uid=0;root_euid=501;assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-EPERM);root_euid=0;has_cap=0;assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-EPERM);has_cap=1;
  assert(!refs && !reads && !lookups);assert(observe_store(&fake_object,&observe_attribute,"observe",7)==-EINVAL);assert(!refs && !reads && !lookups);return 0;
 }
 if(!strcmp(argv[1],"metadata")){
  assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==(metadata_variant?-ENODEV:-EOPNOTSUPP));
  assert(refs==1 && !reads && !pmic_refs && !parent_refs && !lock_step && !pmic_node.refs && !duplicate_node.refs);
  assert(state_show(&fake_object,&state_attribute,text)>0 && strstr(text,"read_qualified=0"));
  assert(strstr(text,metadata_variant?"last_provider_metadata_match=0":"last_provider_metadata_match=1"));
  assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-EALREADY && !reads);return 0;
 }
 assert(!strcmp(argv[1],"blocked"));assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-EOPNOTSUPP);
 assert(refs==1 && !reads && lookups>0 && !pmic_refs && !parent_refs && !lock_step);assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-EALREADY);assert(refs==1 && ref_puts==1 && !reads);
 assert(state_show(&fake_object,&state_attribute,text)>0 && strstr(text,"latched-metadata") && strstr(text,"known_valid=0x00000") && strstr(text,"last_provider_metadata_match=1"));assert(!reads);return 0;
}
'''


class PmicObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ARTIFACT.mkdir(exist_ok=True)
        cls.scratch = tempfile.TemporaryDirectory(prefix='tests-', dir=ARTIFACT)
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.folder = Path(cls.scratch.name)
        cls.binaries = {}

    def binary(self, kind='core'):
        self.assertTrue(CORE.exists(), 'missing fixed read-only observer core')
        self.assertTrue(FRONT.exists(), 'missing zero-I/O observer frontend')
        if kind in self.binaries:
            return self.binaries[kind]
        text = HARNESS.replace('"CORE"', '"'+str(CORE)+'"') if kind == 'core' else KERNEL_SHIM.replace('"FRONT"', '"'+str(FRONT)+'"')
        source = self.folder/(kind+'.c');source.write_text(text)
        includes = self.folder/'includes'
        for name in re.findall(r'#include <([^>]+)>', FRONT.read_text()):
            if name.startswith('linux/'):
                path=includes/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()
        canonical=ROOT/'artifacts/exact-kernel-metadata-20261102/upstream/include/linux/mfd/mt6397/core.h'
        body=canonical.read_bytes()
        self.assertEqual(hashlib.sha256(body).hexdigest(),'e9391c0799c3c046cefee99187070531cf97baa6fc1113c1d3adfc1c20f24840')
        (includes/'linux/mfd/mt6397/core.h').write_bytes(body)
        (includes/'linux/notifier.h').touch()
        binary=self.folder/kind
        cmd=['/usr/bin/clang','-std=c11','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-fsanitize=address,undefined','-I',str(includes),str(source),'-o',str(binary)]
        result=subprocess.run(cmd,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.binaries[kind]=binary
        return binary

    def run_case(self, *args, kind='core'):
        result=subprocess.run([str(self.binary(kind)),*map(str,args)],capture_output=True,text=True,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return result.stdout

    def test_success_reads_exact_nineteen_words_once_and_keeps_receipt(self):
        self.run_case('success')

    def test_each_typed_error_stops_at_first_word_and_latches(self):
        for i in range(19):
            with self.subTest(i=i):self.run_case('error',i,-5)
        self.run_case('error',3,7)

    def test_each_out_of_range_word_is_invalid_and_stops_reads(self):
        for i in range(19):
            with self.subTest(i=i):self.run_case('range',i)

    def test_active_chr_csdac_or_automode_blocks_after_first_word(self):
        for bit in (4,8,16,28):
            with self.subTest(bit=bit):self.run_case('active',bit)

    def test_external_detection_bit_alone_is_not_an_enable_gate(self):
        self.run_case('chr_det_only')

    def test_nonexact_commands_never_reserve_or_prepare(self):
        self.run_case('invalid')

    def test_root_and_rawio_capability_are_both_required(self):
        for root,cap in ((0,0),(0,1),(1,0)):
            with self.subTest(root=root,cap=cap):self.run_case('auth',root,cap)

    def test_metadata_failure_consumes_attempt_without_register_io(self):
        self.run_case('metadata')

    def test_clock_regression_or_budget_failure_stops_further_reads(self):
        for variant in (1,2):
            with self.subTest(variant=variant):self.run_case('clock',variant)

    def test_expired_or_regressed_clock_before_call_does_not_issue_that_read(self):
        for variant in (3,4,5,6):
            with self.subTest(variant=variant):self.run_case('clock',variant)

    def test_concurrent_commands_have_only_one_reading_owner(self):
        self.run_case('concurrent')

    def test_complete_and_partial_reports_are_cached_and_honest(self):
        self.run_case('format',0);self.run_case('format',1)

    def test_report_with_maximum_values_fits_sysfs_page(self):
        self.assertLess(int(self.run_case('maxformat')),4096)

    def test_module_init_and_idle_status_have_zero_provider_or_pmic_io(self):
        self.run_case('idle',kind='front')

    def test_frontend_bad_auth_or_command_has_no_reference_or_io_side_effects(self):
        self.run_case('auth',kind='front')

    def test_frontend_unqualified_provider_is_terminal_and_reference_retained(self):
        self.run_case('blocked',kind='front')

    def test_frontend_cached_provider_binding_and_all_mismatches_never_read_pmic(self):
        for variant in range(15):
            with self.subTest(variant=variant):self.run_case('metadata',variant,kind='front')

    def test_frontend_module_going_refusal_never_reserves_or_reads(self):
        self.run_case('going',kind='front')

    def test_frontend_maximum_cached_report_fits_page_without_io(self):
        self.run_case('maxreport',kind='front')

    def test_compiled_frontend_has_no_pmic_write_or_raw_mmio_api(self):
        result=subprocess.run(['/usr/bin/nm','-a',str(self.binary('front'))],capture_output=True,text=True,check=True)
        names={line.split()[-1].lstrip('_') for line in result.stdout.splitlines() if line.split()}
        self.assertFalse(names & {'regmap_write','regmap_write_bits','regmap_update_bits','writel','readl','ioremap','iowrite16','ioread16'})


if __name__=='__main__':
    unittest.main()
