#!/usr/bin/env python3
"""Deterministic offline stock-charge archive, no boot/flash operations."""
import argparse, gzip, hashlib, json, pathlib, re, stat, struct
SOURCE = pathlib.Path(__file__).resolve().parent

def inputs(build):
    binary = (build / 'busybox').read_bytes()
    if binary[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<HH', binary, 16) != (2,183):
        raise ValueError('expected executable little-endian AArch64 ELF64')
    off = struct.unpack_from('<Q',binary,32)[0]
    stride, count = struct.unpack_from('<HH',binary,54)
    if stride < 56 or off + stride*count > len(binary): raise ValueError('bad ELF program headers')
    if {2,3} & {struct.unpack_from('<I',binary,off+i*stride)[0] for i in range(count)}:
        raise ValueError('dynamic/interpreted BusyBox forbidden')
    if hashlib.sha256(binary).hexdigest() != '995784e2780121e1113453ec5794806b3a25d04457009083c73e52a5634d38aa':
        raise ValueError('unqualified BusyBox bytes')
    config=set((build/'busybox.config').read_text().splitlines())
    if not {'CONFIG_STATIC=y','CONFIG_BLOCKDEV=y','CONFIG_ASH=y','CONFIG_MDEV=y'} <= config:
        raise ValueError('required BusyBox config')
    applets=sorted(pathlib.PurePosixPath(n).name for n in (build/'busybox.links').read_text().splitlines())
    if len(applets)!=47 or ('\0'.join(applets)+'\0').encode() not in binary:
        raise ValueError('compiled applet table not present in exact binary')
    return binary, applets

def entries(build):
    binary, applets = inputs(build)
    result = {n:(stat.S_IFDIR|0o755,b'',0,0) for n in ('bin','dev','proc','sys','run')}
    result['bin/busybox']=(stat.S_IFREG|0o755,binary,0,0)
    result['init']=(stat.S_IFREG|0o755,(SOURCE/'stock-charge-init').read_bytes(),0,0)
    result['storage-readonly']=(stat.S_IFREG|0o755,(SOURCE/'storage-readonly').read_bytes(),0,0)
    for name in ('sh','mount','dmesg','sleep','setsid','reboot','blockdev','sha256sum','mdev'):
        if name in applets: result['bin/'+name]=(stat.S_IFLNK|0o777,b'/bin/busybox',0,0)
    for name, major, minor in (('console',5,1),('null',1,3),('kmsg',1,11)):
        result['dev/'+name]=(stat.S_IFCHR|0o600,b'',major,minor)
    return result

def archive(entries):
    data=bytearray()
    for ino,(name,(mode,body,major,minor)) in enumerate([*sorted(entries.items()),('TRAILER!!!',(0,b'',0,0))],1):
        encoded=name.encode()+b'\0'
        fields=(ino,mode,0,0,2 if stat.S_ISDIR(mode) else 1,0,len(body),0,0,major,minor,len(encoded),0)
        data.extend(b'070701'+''.join(f'{f:08x}' for f in fields).encode()+encoded)
        data.extend(b'\0'*(-len(data)%4));data.extend(body);data.extend(b'\0'*(-len(data)%4))
    data.extend(b'\0'*(-len(data)%512))
    return gzip.compress(data,mtime=0)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('build',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);a=p.parse_args()
    binary,applets=inputs(a.build); items=entries(a.build);data=archive(items)
    a.output.mkdir(parents=True,mode=0o700,exist_ok=False)
    (a.output/'stock-charge.cpio.gz').write_bytes(data)
    manifest={'hardware_go':False,'status':'OFFLINE_COMPONENTS_ONLY','missing_applets':sorted({'blockdev','mdev'}-set(applets)), 'busybox_sha256':hashlib.sha256(binary).hexdigest(),'applets_compiled':applets,'syscall_compatibility_3_18':'UNTESTED','ttyGS0_assignment':'UNTESTED','normal_reboot':'UNTESTED','archive_sha256':hashlib.sha256(data).hexdigest(),'entries':{n:{'mode':oct(v[0]),'sha256':hashlib.sha256(v[1]).hexdigest()} for n,v in sorted(items.items())}}
    (a.output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({'status':manifest['status'],'missing_applets':manifest['missing_applets'],'archive_sha256':manifest['archive_sha256']}))
if __name__=='__main__': main()
