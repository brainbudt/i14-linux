#!/usr/bin/env python3
"""Strict independently parsed allowlist validation; reads local files only."""
import argparse, gzip, hashlib, importlib.util, json, pathlib, re, stat

def parse_newc(data):
    off=0;seen={}
    while True:
        if off+110>len(data) or data[off:off+6] != b'070701': raise ValueError('newc magic/header bounds')
        f=[int(data[off+6+i*8:off+14+i*8],16) for i in range(13)]
        if f[11]<1 or off+110+f[11]>len(data): raise ValueError('name bounds')
        name_end=off+110+f[11];raw=data[off+110:name_end]
        if raw[-1:]!=b'\0' or b'\0' in raw[:-1]: raise ValueError('name terminator')
        name=raw[:-1].decode();start=(name_end+3)&~3;end=start+f[6];aligned_end=(end+3)&~3
        if aligned_end>len(data): raise ValueError('data bounds')
        if any(data[name_end:start]) or any(data[end:aligned_end]): raise ValueError('nonzero newc padding')
        if name=='TRAILER!!!':
            if f != [len(seen)+1,0,0,0,1,0,0,0,0,0,0,11,0] or any(data[end:]):
                raise ValueError('trailer metadata/payload')
            return seen
        if name in seen: raise ValueError('duplicate entry '+name)
        nlink=2 if stat.S_ISDIR(f[1]) else 1
        if f[0]!=len(seen)+1 or f[4]!=nlink or f[2:4]!=[0,0] or f[5]!=0 or f[7:9]!=[0,0] or f[12]!=0:
            raise ValueError('newc metadata')
        seen[name]=(f[1],data[start:end],f[9],f[10]);off=aligned_end

def check_virtual_mounts(script):
    chain=b'''mounts() {
    mount -t proc -o nosuid,nodev,noexec proc /proc || return 1
    mount -t sysfs -o nosuid,nodev,noexec sysfs /sys || return 1
    mount -t devtmpfs -o nosuid devtmpfs /dev ||
        mount -t tmpfs -o nosuid,mode=0755 tmpfs /dev || return 1
    mount -t tmpfs -o nosuid,nodev,noexec,mode=0755 tmpfs /run || return 1
}'''
    if script.count(chain)!=1 or re.search(rb'\bmount\b',script.replace(chain,b'',1)):
        raise ValueError('unexpected virtual mount chain or extra mount')

def verify(build, package):
    source=pathlib.Path(__file__).with_name('make-stock-charge-cpio.py')
    spec=importlib.util.spec_from_file_location('stock_make',source);maker=importlib.util.module_from_spec(spec);spec.loader.exec_module(maker)
    expected=maker.entries(build)
    script=expected['init'][1]
    if any(word in script for word in (b'mount_all',b'healthd',b'adbd',b'mass_storage',b'insmod',b'modprobe',b'/data',b'/cache',b'/system',b'devmem',b'/proc/sys/kernel/hotplug',b'/etc/mdev.conf')):
        raise ValueError('forbidden init component')
    check_virtual_mounts(script)
    compressed=(package/'stock-charge.cpio.gz').read_bytes()
    if compressed[4:8] != bytes(4): raise ValueError('gzip timestamp')
    seen=parse_newc(gzip.decompress(compressed))
    if list(seen)!=sorted(expected): raise ValueError('noncanonical entry order')
    if seen!=expected: raise ValueError('contents/modes differ from exact allowlist')
    manifest=json.loads((package/'manifest.json').read_text())
    if manifest['hardware_go'] is not False or manifest['archive_sha256']!=hashlib.sha256(compressed).hexdigest(): raise ValueError('manifest mismatch')
    return len(seen)
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('build',type=pathlib.Path);p.add_argument('package',type=pathlib.Path);a=p.parse_args()
    print('PASS entries='+str(verify(a.build,a.package))+' hardware_go=false')
