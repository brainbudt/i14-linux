#!/usr/bin/env python3
"""Reviewed owner operations for H1, sent by strict managed SSH on stdin.

Usage: python3 -B - select|idle|startup|renew SERVICE IMPL [FRESH_RESULT_PATH].
No deployment, input injection, reboot, retry, permit creation or final K here.
The host owner supplies the current grant, entry K and boot-bound permit/freeze.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import traceback

NAMES={'ui':'i14-ui','power':'i14-battery-guard','cpufreq':'i14-cpufreq','netwatch':'i14-netwatch'}
FREEZE=Path('/run/h1-freeze.json')

def bind(rows,service,impl):
    path=('/usr/libexec/i14-rs/' if impl=='rust' else '/usr/sbin/')+NAMES[service]
    kids=[p for p in rows if p['argv'] and Path(p['argv'][0]).name!='supervise-daemon' and path in p['argv']]
    parents=[p for p in rows if p['argv'][:2]==['supervise-daemon','i14-'+service]]
    if len(kids)!=1 or len(parents)!=1:
        raise ValueError('expected one selected child and supervisor')
    child,parent=kids[0],parents[0]
    exe=path if impl=='rust' else os.path.realpath('/bin/sh' if service=='netwatch' else '/usr/bin/python3')
    if (child['ppid']!=parent['pid'] or child['exe']!=exe or parent['ppid']!=1
            or parent['exe']!='/sbin/supervise-daemon' or path not in parent['argv']):
        raise ValueError('child/supervisor/executable binding differs')
    return dict(child=child,supervisor=parent,entrypoint=path)

def bcct_matches(text,paused):
    limit=re.search(r'^curr_limit (\d+)$',text,re.M)
    slot=re.search(r'^mtk-cl-bcct00 (-?\d+) mA, state ([01])$',text,re.M)
    return bool(limit and slot and int(slot[2])==int(paused)
                and ((int(limit[1])==1 and int(slot[1])==1) if paused else int(limit[1])==65535))


def ui_ready(identify,await_on):
    before=identify()
    await_on()
    after=identify()
    if after!=before:
        raise ValueError('UI identity changed while waiting for first backlight on')
    return after

def captured(argv,timeout):
    def decode(value):
        return value.decode(errors='replace') if isinstance(value,bytes) else (value or '')
    try:
        r=subprocess.run(argv,capture_output=True,timeout=timeout)
        return dict(command=argv,rc=r.returncode,timeout=False,stdout=decode(r.stdout),stderr=decode(r.stderr)),None
    except subprocess.TimeoutExpired as error:
        return dict(command=argv,rc=None,timeout=True,stdout=decode(error.stdout),stderr=decode(error.stderr)),error

def command(argv,timeout=20):
    trace,error=captured(argv,timeout)
    print(json.dumps(trace),flush=True)
    if error is not None:raise error
    if trace['rc']:
        raise ValueError('owner command failed; dependent operations stopped')
    return trace

def payload(argv,out,trace):
    record,error=captured(argv,85 if 'idle' in argv else 30)
    trace.update(record)
    if out is not None and out.is_file():
        raw=out.read_bytes();trace.update(result_sha256=hashlib.sha256(raw).hexdigest(),result_path=str(out))
        try:trace['result']=json.loads(raw)
        except ValueError:trace['result_parse_error']='ValueError'
    print(json.dumps(trace),flush=True)
    if error is not None:raise error
    if trace['rc']:
        raise ValueError('measurement refused; no retry')

def shutdown_record(raw,boot):
    g=json.loads(raw)
    if not (g['boot_id']==boot and g.get('stopped') is True and g['valid'] is False
            and g['level'] is None and g['enabled'] is False and g['error'] is None
            and g.get('drain') is not True):
        raise ValueError('previous guard did not publish a clean shutdown')
    return g

def await_publication(read_raw,previous,started,boot,clock=time.monotonic,sleep=time.sleep):
    """The known retired record lacks drain; strict preflight starts after replacement."""
    shutdown_record(previous,boot)
    deadline=clock()+15
    while True:
        raw=read_raw()
        if clock()>=deadline:raise ValueError('new guard did not publish within 15s')
        if raw!=previous:
            g=json.loads(raw)
            if not (g['boot_id']==boot and g['error'] is None and g.get('drain') is False
                    and g.get('stopped') is not True and g['updated_monotonic']>=started):
                raise ValueError('new guard publication identity/safety differs')
            return g
        sleep(.05)

def await_guard(read_guard,started,clock=time.monotonic,sleep=time.sleep):
    """Only the known empty first tick may warm up; read_guard checks safety."""
    deadline=clock()+90
    while True:
        g=read_guard()
        if clock()>=deadline:raise ValueError('new guard did not become valid within 90s')
        if g['updated_monotonic']>=started:
            if g['valid'] is True:return g
            if not (g['valid'] is False and g['level'] is None and g['raw_level'] is None
                    and g['enabled'] is True and g['limit']==80):
                raise ValueError('new guard invalid beyond normal empty warmup')
        sleep(1)

class Owner:
    def __init__(self):
        def load(name,path):
            spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
        info=FREEZE.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError('freeze is not root0600 regular file')
        self.freeze=json.loads(FREEZE.read_text())
        allowed={'/run/i14-h1.py','/run/i14-h1-input.py','/run/i14-touch','/etc/conf.d/i14-ui','/usr/sbin/i14-ui','/usr/libexec/i14-rs/i14-ui'}
        for path,digest in self.freeze['sha256'].items():
            if path not in allowed and not re.fullmatch(r'/usr/lib/i14ui/[a-z0-9_]+\.py',path):
                raise ValueError('unexpected frozen source path')
            if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest:
                raise ValueError('frozen source mismatch')
        if not {'/run/i14-h1.py','/run/i14-h1-input.py'}<=self.freeze['sha256'].keys():
            raise ValueError('instrument pins missing')
        self.h1=load('h1','/run/i14-h1.py');self.ctl=load('ctl','/run/i14-h1-input.py')
        self.ready()
    def checked_guard(self):
        if self.h1.preflight()!=self.freeze['boot_id']:
            raise ValueError('wrong frozen boot')
        g=json.loads(Path('/run/i14-charge-status.json').read_text())
        if not (g['boot_id']==self.freeze['boot_id'] and g['error'] is None
                and g['drain'] is False and 0<=self.h1.uptime()-g['updated_monotonic']<150):
            raise ValueError('current charge guard has wrong boot, error, drain or stale publication')
        if (int(Path('/sys/class/power_supply/usb/online').read_text())!=1
                or not 0<=int(Path('/sys/class/power_supply/battery/batt_temp').read_text())<=350
                or hashlib.sha256(Path('/mnt/data/media/0/i14-linux/ui.json').read_bytes()).hexdigest()!='302ba00081c89f06d43e1fbbb12d51b5b9819b3bce6670ef0ae545c1ba8e7bea'):
            raise ValueError('USB/temperature/settings readiness changed')
        return g
    def ready(self):
        g=self.checked_guard()
        if not (g['valid'] is True and 50<=g['level']<=80 and g['raw_level']<=80
                and g['enabled'] is True and g['limit']==80):
            raise ValueError('current charge guard/band is not ready')
        if not bcct_matches(Path('/proc/driver/thermal/clbcct').read_text(),g['paused']):
            raise ValueError('BCCT differs from guard state')
        return g
    def renew(self):
        self.ready()  # The host separately rechecks its current scoped lease/actors.
        permit=self.h1.PERMIT
        value=json.loads(permit.read_text());value['expires_uptime_s']=self.h1.uptime()+900
        with permit.open('w') as f:json.dump(value,f)
        print(json.dumps(dict(permit_renewed=value,sha256=hashlib.sha256(permit.read_bytes()).hexdigest())),flush=True)
    def identify(self,service,impl):
        return bind(self.ctl.processes(),service,impl)
    def select(self,service,impl,restart=False):
        self.ready();path=Path('/etc/conf.d/i14-'+service);raw=path.read_bytes()
        selected=re.findall(rb'^i14_impl="(rust|python)"$',raw,re.M)
        if len(selected)!=1:raise ValueError('selector missing or ambiguous')
        before=self.identify(service,selected[0].decode());changed=selected[0].decode()!=impl
        if changed or restart:
            command(['rc-service','i14-'+service,'stop'])
            if any(p['pid'] in [before['child']['pid'],before['supervisor']['pid']] for p in self.ctl.processes()):
                raise ValueError('old child/supervisor survived stop')
            new=re.sub(rb'^i14_impl="(?:rust|python)"$',b'i14_impl="'+impl.encode()+b'"',raw,flags=re.M)
            if changed:
                with path.open('wb') as f:f.write(new);f.flush();os.fsync(f.fileno())
            if service=='power':
                shutdown_raw=Path('/run/i14-charge-status.json').read_text()
                print(json.dumps(dict(guard_shutdown=shutdown_record(shutdown_raw,self.freeze['boot_id']))),flush=True)
            command(['rc-service','i14-'+service,'start']);deadline=time.monotonic()+15
            while True:
                try:
                    result=self.identify(service,impl)
                    break
                except (ValueError,FileNotFoundError):
                    if time.monotonic()>=deadline:raise
                    time.sleep(.05)
            if service=='power':
                def publication():
                    if self.identify(service,impl)!=result:raise ValueError('power identity changed before publication')
                    return Path('/run/i14-charge-status.json').read_text()
                first=await_publication(publication,shutdown_raw,result['child']['start_ticks']/os.sysconf('SC_CLK_TCK'),self.freeze['boot_id'])
                print(json.dumps(dict(guard_first_publication=first)),flush=True)
                g=await_guard(self.checked_guard,result['child']['start_ticks']/os.sysconf('SC_CLK_TCK'))
                if self.identify(service,impl)!=result:raise ValueError('power identity changed during warmup')
                print(json.dumps(dict(guard_warmup_complete=g,monotonic=time.monotonic())),flush=True)
            if service=='ui':result=ui_ready(lambda:self.identify(service,impl),lambda:self.ctl.await_backlight_on(self.h1.light))
        else:result=before
        if service=='ui':
            self.freeze['sha256'][str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
            with FREEZE.open('w') as f:json.dump(self.freeze,f)
        result['entrypoint_sha256']=hashlib.sha256(Path(result['entrypoint']).read_bytes()).hexdigest()
        print(json.dumps(dict(selected=service,implementation=impl,identity=result,changed=changed,restarted=changed or restart,selector_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),monotonic=time.monotonic())),flush=True)
        self.ready();return result
    def idle(self,service,impl,out):
        binding=self.select(service,impl,restart=True);deadline=time.monotonic()+75
        while self.h1.light()!=0:
            self.ready()
            if time.monotonic()>=deadline:raise ValueError('natural screen timeout did not reach zero')
            time.sleep(1)
        self.ready()
        child = binding['child']
        argv = ['python3', '-B', '/run/i14-h1.py', 'idle', '--impl', impl,
                '--service', service, '--pid', str(child['pid']),
                '--start-ticks', str(child['start_ticks']), '--out', str(out)]
        payload(argv, out, dict(identity=binding, light_before=self.h1.light(), clk_tck=os.sysconf('SC_CLK_TCK')))
    def startup(self,impl,out):
        self.identify('ui','rust');command(['rc-service','i14-ui','stop'])
        try:
            if any(any(v in p['argv'] for v in [*self.h1.UI.values(),'/usr/libexec/i14-rs/i14-splash','/usr/sbin/i14-splash']) for p in self.ctl.processes()):
                raise ValueError('UI/splash remains after stop')
            self.h1.LIGHT.write_text('0')
            payload(['python3','-B','/run/i14-h1.py','startup','--impl',impl,'--out',str(out)],out,{})
        finally:
            if any(any(v in p['argv'] for v in self.h1.UI.values()) for p in self.ctl.processes()):
                raise ValueError('startup child remains; restoration HOLD')
            command(['rc-service','i14-ui','start'])
            deadline=time.monotonic()+8
            while True:
                try:
                    self.identify('ui','rust');break
                except ValueError:
                    if time.monotonic()>=deadline:raise
                    time.sleep(.05)
            result=ui_ready(lambda:self.identify('ui','rust'),lambda:self.ctl.await_backlight_on(self.h1.light))
            print(json.dumps(dict(restored_ui=result,monotonic=time.monotonic())),flush=True)

def main():
    action,service,impl=sys.argv[1:4]
    if action not in ('select','idle','startup','renew') or service not in NAMES or impl not in ('rust','python') or (action=='startup' and service!='ui'):
        raise ValueError('invalid owner operation')
    out=Path(sys.argv[4]) if len(sys.argv)==5 else None
    if action in ('idle','startup') and (out is None or out.parent!=Path('/run') or not re.fullmatch(r'h1-[a-z0-9-]+\.json',out.name) or os.path.lexists(out)):
        raise ValueError('fresh /run/h1-result path required')
    owner=Owner()
    if action=='renew':owner.renew()
    elif action=='select':owner.select(service,impl)
    elif action=='idle':owner.idle(service,impl,out)
    else:owner.startup(impl,out)

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,subprocess.SubprocessError) as e:
        print(json.dumps(dict(owner_failure=type(e).__name__,frames=[dict(function=f.f_code.co_name,line=line) for f,line in traceback.walk_tb(e.__traceback__) if f.f_code.co_filename==__file__])),file=sys.stderr)
        raise SystemExit(1)
