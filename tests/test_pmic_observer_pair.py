"""Offline fixed-pair behavior; real shared C engine and fake kernel boundaries."""
from pathlib import Path
import hashlib
import importlib.util
import os
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'kernel/i14-pmic-observer-core.h'
FRONT = ROOT / 'kernel/i14-pmic-observer.c'
PAIR = ROOT / 'kernel/i14-pmic-observer-pair.c'
spec = importlib.util.spec_from_file_location('observer_test_boundaries', ROOT / 'tests/test_pmic_observer.py')
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

# Catches widened/reordered profiles, retries, invalid partial publication,
# missing first-word guards, clock checks, cleanup, authorization or once claim.
HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define I14_OBSERVER_FIXED_PAIR
#include "CORE"
static const unsigned expected[2] = {0x0f48, 0x0f7a};
struct fake {
 atomic_int consumed,claims;
 unsigned prepares,finishes,reads,ticks,active;
 int error_index,read_rc,range,prepare_rc,clock_fault;
};
static int claim(void *v) {
 struct fake *f=v;int zero=0;atomic_fetch_add(&f->claims,1);
 return atomic_compare_exchange_strong(&f->consumed,&zero,1)?0:-EALREADY;
}
static int prepare(void *v) {struct fake *f=v;f->prepares++;return f->prepare_rc;}
static void finish(void *v) {((struct fake*)v)->finishes++;}
static unsigned long long now(void *v) {
 struct fake *f=v;unsigned n=++f->ticks;
 /* started=1, first begin/end=2/3, second begin/end=4/5. */
 if(f->clock_fault>0 && n==(unsigned)f->clock_fault)return 0;
 if(f->clock_fault<0 && n==(unsigned)-f->clock_fault)return 1000000001ULL;
 return n;
}
static int read_word(void *v,unsigned address,unsigned *value) {
 struct fake *f=v;unsigned index=f->reads++;
 assert(index<2 && address==expected[index]); /* Also excludes WDT/ADC/third read. */
 if(index==(unsigned)f->error_index && f->read_rc)return f->read_rc;
 *value=index?0x4321u:(0x1000u|f->active);
 if(index==(unsigned)f->error_index && f->range)*value=0x10000u;
 return 0;
}
static struct i14_observer_io ops(struct fake *f) {
 return (struct i14_observer_io){f,claim,prepare,read_word,finish,now};
}
static int observe(struct fake *f,struct i14_observer_state *s) {
 struct i14_observer_io io=ops(f);
 return i14_observer_observe(s,&io,"observe\n",8,1,1);
}
static void report(struct fake *f,struct i14_observer_state *s) {
 char text[4096],tiny[8];unsigned before=f->reads;
 assert(i14_observer_format(s,text,sizeof(text))>0);
 assert(strstr(text,"profile=fixed-pair-f48-f7a-v1\n") && strstr(text,"word_count=2\n"));
 assert(strstr(text,"charging_status=UNKNOWN\n") && strstr(text,"handoff_qualified=0\n"));
 assert(strstr(text,"read_side_effects=UNKNOWN\n") && strstr(text,"hardware_go=0\n"));
 assert(!strstr(text,"raw[0f62]") && !strstr(text,"raw[0f66]") && !strstr(text,"raw[0f4a]"));
 assert(strstr(text,s->valid_mask&1?"raw[0f48]=0x":"raw[0f48]=UNKNOWN known=0"));
 assert(strstr(text,s->valid_mask&2?"raw[0f7a]=0x":"raw[0f7a]=UNKNOWN known=0"));
 assert(i14_observer_format(s,tiny,sizeof(tiny))==-ENOSPC && before==f->reads);
 fputs(text,stdout);
}
struct thread_arg {struct fake *f;struct i14_observer_state *s;atomic_int *start;int rc;};
static void *run_thread(void *v) {
 struct thread_arg *a=v;while(!atomic_load(a->start)){}
 a->rc=observe(a->f,a->s);return NULL;
}
int main(int argc,char **argv) {
 assert(argc>=2);struct fake f={0};struct i14_observer_state s={0};
 if(!strcmp(argv[1],"success")) {
  assert(!observe(&f,&s));assert(f.reads==2 && s.read_calls==2);
  assert(s.phase==I14_OBSERVER_COMPLETE && s.failed_index==2 && s.valid_mask==3);
  assert(s.values[0]==0x1000 && s.values[1]==0x4321);
  assert(s.read_start_ns[0]==2 && s.read_end_ns[0]==3 && s.read_start_ns[1]==4 && s.read_end_ns[1]==5);
 } else if(!strcmp(argv[1],"active")) {
  f.active=(unsigned)strtoul(argv[2],NULL,0);assert(observe(&f,&s)==-EBUSY);
  assert(f.reads==1 && s.valid_mask==1 && s.failed_index==0 && s.phase==I14_OBSERVER_ACTIVE_CONFIG);
 } else if(!strcmp(argv[1],"uninterpreted")) {
  f.active=(unsigned)strtoul(argv[2],NULL,0);assert(!observe(&f,&s) && f.reads==2);
  assert(s.values[0]==(0x1000|f.active) && s.valid_mask==3);
 } else if(!strcmp(argv[1],"error")) {
  f.error_index=atoi(argv[2]);f.read_rc=atoi(argv[3]);int want=f.read_rc<0?f.read_rc:-EIO;
  assert(observe(&f,&s)==want && s.rc==want && s.phase==I14_OBSERVER_READ_ERROR);
  assert(f.reads==(unsigned)f.error_index+1 && s.failed_index==(unsigned)f.error_index);
  assert(s.valid_mask==(f.error_index?1u:0u));
 } else if(!strcmp(argv[1],"range")) {
  f.error_index=atoi(argv[2]);f.range=1;assert(observe(&f,&s)==-ERANGE);
  assert(s.phase==I14_OBSERVER_RANGE_ERROR && s.failed_raw_valid && s.failed_raw==0x10000);
  assert(f.reads==(unsigned)f.error_index+1 && s.valid_mask==(f.error_index?1u:0u));
 } else if(!strcmp(argv[1],"clock")) {
  f.clock_fault=atoi(argv[2]);unsigned at=(unsigned)abs(f.clock_fault);
  assert(observe(&f,&s)==(f.clock_fault>0?-EIO:-ETIMEDOUT));
  assert(s.phase==(f.clock_fault>0?I14_OBSERVER_CLOCK_ERROR:I14_OBSERVER_TIME_BUDGET));
  assert(f.reads==(at==2?0u:at==5?2u:1u) && s.valid_mask==(at>=4?1u:0u));
  assert(s.failed_index==(at>=4?1u:0u));
 } else if(!strcmp(argv[1],"metadata")) {
  f.prepare_rc=-EOPNOTSUPP;assert(observe(&f,&s)==-EOPNOTSUPP);
  assert(!f.reads && !s.valid_mask && s.phase==I14_OBSERVER_METADATA_ERROR);
 } else if(!strcmp(argv[1],"auth")) {
  struct i14_observer_io io=ops(&f);
  for(int root=0;root<=1;root++)for(int cap=0;cap<=1;cap++)if(!root || !cap)
   assert(i14_observer_observe(&s,&io,"observe\n",8,root,cap)==-EPERM);
  const char *bad[]={"observe","observe\r\n","observe\nx","","Observe\n","observe\0\n"};
  const size_t sizes[]={7,9,9,0,8,9};
  for(unsigned i=0;i<6;i++)assert(i14_observer_observe(&s,&io,bad[i],sizes[i],1,1)==-EINVAL);
  assert(!f.claims && !f.prepares && !f.reads && !s.attempted);return 0;
 } else if(!strcmp(argv[1],"concurrent")) {
  pthread_t threads[8];struct thread_arg args[8];atomic_int start=0;unsigned winners=0;
  for(unsigned i=0;i<8;i++){args[i]=(struct thread_arg){&f,&s,&start,99};assert(!pthread_create(&threads[i],NULL,run_thread,&args[i]));}
  atomic_store(&start,1);
  for(unsigned i=0;i<8;i++){assert(!pthread_join(threads[i],NULL));winners+=args[i].rc==0;assert(!args[i].rc || args[i].rc==-EALREADY);}
  assert(winners==1 && f.reads==2);
 } else return 9;
 assert(f.prepares==1 && f.finishes==1 && s.attempted && s.read_calls==f.reads);
 unsigned calls=f.reads;struct i14_observer_state receipt=s;
 assert(observe(&f,&s)==-EALREADY && f.reads==calls && f.prepares==1 && f.finishes==1);
 assert(!memcmp(&receipt,&s,sizeof(s)));report(&f,&s);return 0;
}
'''

# Reuse only established fake Linux boundaries; compile the actual pair entry.
FRONT_HARNESS = legacy.KERNEL_SHIM.split('int main(int argc,char **argv)')[0].replace(
    '"i14_pmic_observer"', '"i14_pmic_observer_pair"') + r'''
int main(int argc,char **argv) {
 assert(argc>=2);metadata_setup(!strcmp(argv[1],"metadata")?atoi(argv[2]):0);
 char text[4096];assert(!wrapped_init() && groups==1 && !reads && !lookups && !refs);
 assert(state_show(&fake_object,&state_attribute,text)>0);
 assert(strstr(text,"profile=fixed-pair-f48-f7a-v1\n") && strstr(text,"word_count=2\n"));
 assert(strstr(text,"raw[0f48]=UNKNOWN") && strstr(text,"raw[0f7a]=UNKNOWN"));
 assert(!strstr(text,"raw[0f4a]") && !reads && !lookups);
 if(!strcmp(argv[1],"idle")){wrapped_exit();assert(!groups && !reads && !refs);return 0;}
 if(!strcmp(argv[1],"going")) {
  module_available=0;assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-ENODEV);
  assert(!refs && !reads && !lookups && !held_ref && !observation.attempted);return 0;
 }
 if(!strcmp(argv[1],"auth")) {
  root_uid=501;assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-EPERM);root_uid=0;
  root_euid=501;assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-EPERM);root_euid=0;
  has_cap=0;assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-EPERM);has_cap=1;
  assert(observe_store(&fake_object,&observe_attribute,"observe",7)==-EINVAL);
  assert(!refs && !reads && !lookups && !held_ref && !observation.attempted);return 0;
 }
 assert(!strcmp(argv[1],"metadata"));
 assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==(metadata_variant?-ENODEV:-EOPNOTSUPP));
 assert(refs==1 && held_ref==1 && !reads && !pmic_refs && !parent_refs && !lock_step);
 assert(!pmic_node.refs && !duplicate_node.refs && provider.map==NULL && provider.pmic==NULL && provider.parent==NULL);
 assert(state_show(&fake_object,&state_attribute,text)>0 && strstr(text,"read_qualified=0\n"));
 assert(strstr(text,metadata_variant?"last_provider_metadata_match=0":"last_provider_metadata_match=1"));
 assert(strstr(text,"charging_status=UNKNOWN\n") && strstr(text,"handoff_qualified=0\n"));
 assert(strstr(text,"raw[0f48]=UNKNOWN") && strstr(text,"raw[0f7a]=UNKNOWN"));
 int calls=lookups;assert(observe_store(&fake_object,&observe_attribute,"observe\n",8)==-EALREADY);
 assert(refs==1 && ref_puts==1 && !reads && lookups==calls);
 assert(state_show(&fake_object,&state_attribute,text)>0 && !reads && lookups==calls);
 return 0;
}
'''


class PmicObserverPairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix='i14-pmic-pair-')
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.folder = Path(cls.scratch.name)
        cls.binaries = {}

    def binary(self, kind):
        if kind in self.binaries:
            return self.binaries[kind]
        text = HARNESS.replace('"CORE"', '"'+str(CORE)+'"') if kind == 'core' else FRONT_HARNESS.replace('"FRONT"', '"'+str(PAIR)+'"')
        source = self.folder/(kind+'.c')
        source.write_text(text)
        includes = self.folder/'includes'
        for name in re.findall(r'#include <([^>]+)>', FRONT.read_text()):
            if name.startswith('linux/'):
                path = includes/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
        canonical = ROOT/'artifacts/exact-kernel-metadata-20261102/upstream/include/linux/mfd/mt6397/core.h'
        body = canonical.read_bytes()
        self.assertEqual(hashlib.sha256(body).hexdigest(), 'e9391c0799c3c046cefee99187070531cf97baa6fc1113c1d3adfc1c20f24840')
        (includes/'linux/mfd/mt6397/core.h').write_bytes(body)
        (includes/'linux/notifier.h').touch()
        binary = self.folder/kind
        result = subprocess.run(['/usr/bin/clang', '-std=c11', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread', '-fsanitize=address,undefined', '-I', str(includes), str(source), '-o', str(binary)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.binaries[kind] = binary
        return binary

    def run_case(self, *args, kind='core'):
        result = subprocess.run([str(self.binary(kind)), *map(str, args)], capture_output=True, text=True, env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        return result.stdout

    def test_success_reads_only_f48_then_f7a_once_with_independent_timestamps(self):
        self.run_case('success')

    def test_first_active_configuration_preserves_partial_unknown_second_word(self):
        for mask in (4, 8, 16, 28):
            with self.subTest(mask=mask):
                self.run_case('active', mask)

    def test_other_raw_bits_are_not_new_enable_or_handoff_inferences(self):
        for mask in (0x20, 0x40, 0x80, 0xffe3):
            with self.subTest(mask=mask):
                self.run_case('uninterpreted', mask)

    def test_each_first_or_second_callback_error_is_terminal(self):
        for index in (0, 1):
            for rc in (-5, -110, 7):
                with self.subTest(index=index, rc=rc):
                    self.run_case('error', index, rc)

    def test_each_out_of_range_result_is_invalid_and_terminal(self):
        for index in (0, 1):
            with self.subTest(index=index):
                self.run_case('range', index)

    def test_clock_regression_and_budget_stop_before_and_after_each_read(self):
        for tick in (2, 3, 4, 5):
            for sign in (-1, 1):
                with self.subTest(tick=tick, sign=sign):
                    self.run_case('clock', sign*tick)

    def test_metadata_failure_finishes_and_consumes_without_reads(self):
        self.run_case('metadata')

    def test_exact_command_root_and_rawio_precede_claim(self):
        self.run_case('auth')

    def test_concurrent_requests_have_one_owner_and_two_total_reads(self):
        self.run_case('concurrent')

    def test_frontend_pair_identity_init_and_cached_status_perform_no_io(self):
        self.run_case('idle', kind='front')

    def test_frontend_auth_command_and_module_refusal_do_not_reserve(self):
        self.run_case('auth', kind='front')
        self.run_case('going', kind='front')

    def test_frontend_gate_zero_blocks_even_matching_metadata_and_cleans_up(self):
        for variant in range(15):
            with self.subTest(variant=variant):
                self.run_case('metadata', variant, kind='front')

    def test_compiled_pair_has_no_write_raw_mmio_adc_or_watchdog_interface(self):
        result = subprocess.run(['/usr/bin/nm', '-a', str(self.binary('front'))], capture_output=True, text=True, check=True)
        names = {line.split()[-1].lstrip('_') for line in result.stdout.splitlines() if line.split()}
        self.assertFalse(names & {'regmap_write', 'regmap_write_bits', 'regmap_update_bits', 'writel', 'readl', 'ioremap', 'iowrite16', 'ioread16', 'iio_read_channel_raw', 'watchdog_ping'})


if __name__ == '__main__':
    unittest.main()
