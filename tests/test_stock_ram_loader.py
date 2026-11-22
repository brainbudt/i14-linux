"""Offline stock-loader contract: real pure C, fake native syscalls only."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tools/i14-stock-ram-kexec.c'
ASSETS = [
    ROOT/'artifacts/stock-entry-shim-20261102/stock-entry.page',
    ROOT/'artifacts/stock-charge-components-20261101/components/Image',
    ROOT/'artifacts/stock-charge-readiness-fix-20261102/package/stock-charge.cpio.gz',
    ROOT/'artifacts/stock-dtb-readiness-candidate-20261102/output/candidate.dtb',
]

HARNESS = r'''
#define I14_STOCK_TEST 1
#include "SOURCE"
#include <assert.h>
static unsigned char *bytes(const char *path,size_t *size) {
    FILE *f=fopen(path,"rb"); assert(f); assert(!fseek(f,0,SEEK_END));
    long n=ftell(f); assert(n>=0); rewind(f); *size=(size_t)n;
    unsigned char *p=malloc(*size+1); assert(p); assert(fread(p,1,*size,f)==*size); assert(!fclose(f)); return p;
}
static struct segment segs[4];
static void segments(void) {
    struct asset a[4]={{(unsigned char*)1,4096},{(unsigned char*)2,16446424},
                       {(unsigned char*)3,208378},{(unsigned char*)4,75744}};
    assert(make_segments(a,segs));
}
static const char *boot="01234567-89ab-cdef-0123-456789abcdef";
struct fake { int variant,guards,loads,announces,execs; uint64_t now; char token[33]; };
static int fg(void *v,int loaded,const char *before,char out[37]) {
    struct fake *f=v; f->guards++;
    assert(loaded==(f->guards==1?0:1));
    if(f->variant==1 || (f->variant==6 && f->guards==2)) return 0;
    if(before && strcmp(before,boot)) return 0;
    strcpy(out,(f->variant==7 && f->guards==2)?"11234567-89ab-cdef-0123-456789abcdef":boot);
    return 1;
}
static int fnonce(void *v,char out[33]) { struct fake *f=v; memset(out,'a',32);out[32]=0; return f->variant!=9; }
static int fl(void *v,const struct segment *s) {
    struct fake *f=v; f->loads++; assert(s[0].mem==0x40000000 && s[1].memsz==0x1286000);
    return f->variant==2?-1:0;
}
static int fa(void *v,const char *id,const char *token) {
    struct fake *f=v; f->announces++; assert(!strcmp(id,boot));strcpy(f->token,token);
    if(f->variant==11)f->now=6000;
    return f->variant!=10;
}
static int fc(void *v,char *out,size_t cap,uint64_t deadline) {
    struct fake *f=v; (void)deadline; assert(cap>=42);
    if(f->variant==3)return 0;
    if(f->variant==4){f->now=5000;return -1;}
    snprintf(out,cap,"EXECUTE %s\n",f->token);
    if(f->variant==5)out[8]='b';
    if(f->variant==8)f->now=5001;
    if(f->variant==12){out[40]='x';out[41]='\n';return 42;}
    return 41;
}
static int fe(void *v) { struct fake *f=v;f->execs++;return -1; }
static uint64_t fm(void *v) { return ((struct fake*)v)->now; }
int main(int argc,char **argv) {
    assert(argc>=2);
    if(!strcmp(argv[1],"hash")) { char out[65];digest_hex((unsigned char*)"abc",3,out);puts(out);return 0; }
    if(!strcmp(argv[1],"command")) {
        char line[64]={0};int n=native_command(NULL,line,sizeof(line),native_millis(NULL)+100);
        printf("length=%d\n",n);if(n>0)fwrite(line,1,(size_t)n,stdout);return 0;
    }
    if(!strcmp(argv[1],"asset")) {
        assert(argc==5);size_t n;unsigned char *p=bytes(argv[3],&n);int index=atoi(argv[2]);
        if(!strcmp(argv[4],"flip"))p[n/2]^=1;
        if(!strcmp(argv[4],"short"))n--;
        int okay=asset_blob_ok((unsigned)index,p,n);free(p);return okay?0:7;
    }
    if(!strcmp(argv[1],"header")) {
        size_t n;unsigned char *p=bytes(argv[2],&n);int mutation=atoi(argv[3]);
        if(mutation)p[(unsigned[]){0,8,16,24,56}[mutation]]^=1;
        int okay=image_header_ok(p,n);free(p);return okay?0:7;
    }
    if(!strcmp(argv[1],"layout")) {
        segments();int mutation=atoi(argv[2]);
        if(mutation==1)segs[1].memsz=16449536;
        if(mutation==2)segs[2].mem=0x43000000;
        if(mutation==3)segs[3].mem++;
        if(mutation==4)segs[0].mem=UINT64_MAX-2047;
        if(mutation==5)segs[2].memsz=208378;
        if(mutation==6)segs[1].mem=segs[0].mem;
        if(!layout_ok(segs))return 7;
        printf("%llx/%zx %llx/%zx %llx/%zx %llx/%zx\n",
          (unsigned long long)segs[0].mem,segs[0].memsz,(unsigned long long)segs[1].mem,segs[1].memsz,
          (unsigned long long)segs[2].mem,segs[2].memsz,(unsigned long long)segs[3].mem,segs[3].memsz);return 0;
    }
    if(!strcmp(argv[1],"iomem")) {
        segments();const char *maps[]={"40000000-bf00ffff : System RAM\n",
          "00000000-00000000 : System RAM\n","40000000-4dffffff : System RAM\n",
          "40000000-4dffffff : reserved\n","40000000-44000000 : System RAM\n"};
        return iomem_ok(maps[atoi(argv[2])],segs)?0:7;
    }
    if(!strcmp(argv[1],"identity") || !strcmp(argv[1],"postidentity")) {
        struct snapshot s={0,0,4096,"7.0.10-i14-bringup","aarch64","0\n","0\n","0\n",boot,
          "9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a",
          "867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a"};
        int post=!strcmp(argv[1],"postidentity");if(post)s.loaded="1\n";
        switch(atoi(argv[2])) {case 1:s.uid=501;break;case 2:s.euid=501;break;case 3:s.page=16384;break;
          case 4:s.release="3.18.19";break;case 5:s.machine="x86_64";break;case 6:s.online="0-3\n";break;
          case 7:s.disabled="1\n";break;case 8:s.loaded=post?"0\n":"1\n";break;case 9:s.model="bad";break;
          case 10:s.compatible="bad";break;case 11:s.boot="bad";break;
          case 12:s.boot="11234567-89ab-cdef-0123-456789abcdef";break;}
        return snapshot_ok(&s,post,post?boot:NULL)?0:7;
    }
    if(!strcmp(argv[1],"storage")) {
        char values[27][32];const char *names[27];int ro[27];
        strcpy(values[0],"mmcblk0");strcpy(values[1],"mmcblk0boot0");strcpy(values[2],"mmcblk0boot1");
        for(unsigned i=3;i<26;i++)snprintf(values[i],sizeof(values[i]),"mmcblk0p%u",i-2);
        for(unsigned i=0;i<27;i++){names[i]=values[i];ro[i]=1;}strcpy(values[26],"mmcblk0boot2");
        size_t n=26;uint64_t capacity=30535680;int mutation=atoi(argv[2]);
        if(mutation==1)n=25;if(mutation==2)n=27;if(mutation==3)ro[8]=0;
        if(mutation==4)capacity--;if(mutation==5)names[25]=names[24];if(mutation==6)names[25]="mmcblk0p24";
        return storage_ok(names,ro,n,capacity)?0:7;
    }
    if(!strcmp(argv[1],"metadata")) {
        struct stat st={0};st.st_mode=S_IFREG|0600;st.st_uid=0;st.st_nlink=1;st.st_size=4096;
        switch(atoi(argv[2])) {case 1:st.st_uid=501;break;case 2:st.st_mode|=0040;break;
          case 3:st.st_nlink=2;break;case 4:st.st_size--;break;case 5:st.st_mode=S_IFLNK|0600;break;}
        return file_metadata_ok(&st,4096)?0:7;
    }
    if(!strcmp(argv[1],"symlink")) {
        int d=open(argv[2],O_RDONLY|O_DIRECTORY);assert(d>=0);int f=open_asset_at(d,"asset");
        if(f>=0)close(f);close(d);return f<0 && errno==ELOOP?0:7;
    }
    if(!strcmp(argv[1],"session")) {
        struct fake f={0};f.variant=atoi(argv[2]);struct session state={0};segments();
        struct operations ops={fg,fnonce,fl,fa,fc,fe,fm};
        (void)run_session(&state,&ops,&f,segs);
        int loads=f.loads,execs=f.execs;(void)run_session(&state,&ops,&f,segs);
        assert(f.loads==loads && f.execs==execs);
        printf("loads=%d announces=%d execs=%d phase=%d\n",f.loads,f.announces,f.execs,state.phase);return 0;
    }
    return 9;
}
'''


class StockRamLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.addClassCleanup(cls.tmp.cleanup)
        cls.exe=None
        if SOURCE.exists():
            p=Path(cls.tmp.name);h=p/'test.c';h.write_text(HARNESS.replace('SOURCE',str(SOURCE)))
            cls.exe=p/'test'
            result=subprocess.run(['/usr/bin/clang','-std=c11','-D_DEFAULT_SOURCE','-Wall','-Wextra','-Werror',
              '-Wno-unused-function','-fsanitize=address,undefined',str(h),'-o',str(cls.exe)],capture_output=True,text=True)
            if result.returncode:raise AssertionError('host C compile failed:\n'+result.stderr)

    def run_core(self,*args,okay=True):
        self.assertIsNotNone(self.exe,'missing fixed stock loader implementation')
        r=subprocess.run([str(self.exe),*map(str,args)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0 if okay else 7,r.stdout+r.stderr)
        return r.stdout

    def test_sha256_known_vector(self):
        self.assertEqual(self.run_core('hash').strip(),'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')

    def test_exact_four_assets_reject_corruption_and_old_lengths(self):
        for i,p in enumerate(ASSETS):
            with self.subTest(asset=i):
                self.run_core('asset',i,p,'valid')
                self.run_core('asset',i,p,'flip',okay=False)
                self.run_core('asset',i,p,'short',okay=False)

    def test_stock_header_checks_offset_runtime_flags_and_magic(self):
        self.run_core('header',ASSETS[1],0)
        for i in range(1,5):self.run_core('header',ASSETS[1],i,okay=False)

    def test_four_rounded_segments_and_bad_ranges(self):
        self.assertEqual(self.run_core('layout',0).strip(),'40000000/1000 40080000/1286000 44000000/33000 4e000000/13000')
        for i in range(1,7):self.run_core('layout',i,okay=False)

    def test_iomem_requires_unmasked_system_ram_covering_every_segment(self):
        self.run_core('iomem',0)
        for i in range(1,5):self.run_core('iomem',i,okay=False)

    def test_source_identity_cpu_boot_and_loaded_slot_guards(self):
        self.run_core('identity',0)
        for i in range(1,12):self.run_core('identity',i,okay=False)

    def test_exact_26_readonly_storage_nodes_and_capacity(self):
        self.run_core('storage',0)
        for i in range(1,7):self.run_core('storage',i,okay=False)

    def test_postload_identity_requires_own_slot_and_same_boot(self):
        self.run_core('postidentity',0)
        self.run_core('postidentity',8,okay=False)
        self.run_core('postidentity',12,okay=False)

    def test_asset_metadata_requires_private_root_regular_single_link_exact_size(self):
        self.run_core('metadata',0)
        for i in range(1,6):self.run_core('metadata',i,okay=False)

    def test_asset_open_never_follows_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'real').write_bytes(b'x');(p/'asset').symlink_to(p/'real')
            self.run_core('symlink',p)

    def test_session_executes_only_after_own_successful_load_and_exact_token(self):
        out=self.run_core('session',0)
        self.assertIn('loads=1 announces=1 execs=1',out)

    def test_guard_and_nonce_failure_never_load(self):
        for i in (1,9):self.assertIn('loads=0 announces=0 execs=0',self.run_core('session',i))

    def test_failed_load_never_announces_or_executes(self):
        self.assertIn('loads=1 announces=0 execs=0',self.run_core('session',2))

    def test_eof_timeout_bad_token_changed_identity_boot_or_late_command_never_executes(self):
        for i in (3,4,5,6,7,8,10):
            with self.subTest(case=i):self.assertIn('loads=1',self.run_core('session',i));self.assertIn('execs=0',self.run_core('session',i))

    def test_slow_receipt_publication_does_not_restart_execute_deadline(self):
        self.assertIn('execs=0',self.run_core('session',11))

    def test_extra_bytes_inside_command_line_never_execute(self):
        self.assertIn('execs=0',self.run_core('session',12))

    def test_real_bounded_stdin_parser_exact_oversized_and_eof(self):
        self.assertIsNotNone(self.exe,'missing fixed stock loader implementation')
        token='EXECUTE '+'a'*32+'\n'
        for value,wanted in [(token,'length=41\n'+token),(token[:-1]+'x\n','length=42\n'+token[:-1]+'x\n'),
                             ('x'*100,'length=-1\n')]:
            r=subprocess.run([str(self.exe),'command'],input=value,capture_output=True,text=True,timeout=3)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(r.stdout,wanted)
        r=subprocess.run([str(self.exe),'command'],input='',capture_output=True,text=True,timeout=3)
        self.assertEqual(r.returncode,0,r.stderr)
        # poll may report readable EOF or HUP, depending on the host platform.
        self.assertIn(r.stdout,('length=0\n','length=-1\n'))

    def test_real_stdin_timeout_is_bounded_without_data_or_eof(self):
        self.assertIsNotNone(self.exe,'missing fixed stock loader implementation')
        p=subprocess.Popen([str(self.exe),'command'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            p.wait(timeout=3)
            out=p.stdout.read();err=p.stderr.read()
            self.assertEqual(p.returncode,0,err)
            self.assertEqual(out,'length=-1\n')
        finally:
            if p.poll() is None:p.kill();p.wait()
            p.stdin.close();p.stdout.close();p.stderr.close()


if __name__=='__main__':unittest.main()
