#!/usr/bin/env python3
"""Streaming export primitive only; no transport, device main or cleanup permission.

The phase caller must first restore cron, close actors and validate its exact
owned destination/identity. A successful export alone never authorizes cleanup.
"""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tarfile

MAX_FILE = 16 << 20
MAX_BYTES = 1281 << 20
MAX_FILES = 1024
MAX_META = 1 << 20
CHUNK = 1 << 20
FIRST = 'EXPORT-MANIFEST.json'
LAST = 'EXPORT-COMPLETE.json'


def safe_name(name):
    if (not isinstance(name, str) or len(name.encode()) > 100
            or not 1 <= len(name.split('/')) <= 3
            or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ['.', '..']
                   for p in name.split('/')) or name in [FIRST, LAST]):
        raise ValueError('unsafe export name')
    return name


@contextmanager
def directory(path):
    inherited = type(path) is int
    if inherited:
        fd = os.dup(path)
    else:
        path = Path(path).absolute()
        if path.anchor != '/' or '..' in path.parts:
            raise ValueError('unexpected export directory')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        if not inherited:
            for part in path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
        yield fd
    except OSError as error:
        raise ValueError('export directory unavailable or symlinked') from error
    finally:
        os.close(fd)


@contextmanager
def parent_descriptor(root, name, create=False):
    parts = safe_name(name).split('/')
    with directory(root) as initial:
        fd = os.dup(initial)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError('export parent directory owner/mode differs')
            yield fd, parts[-1]
        finally:
            os.close(fd)


def metadata(info):
    return {'dev': info.st_dev, 'inode': info.st_ino, 'bytes': info.st_size,
            'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns, 'nlink': info.st_nlink}


def regular(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) not in [0o600, 0o700]
            or info.st_nlink != 1 or not 0 <= info.st_size <= MAX_FILE):
        raise ValueError('unexpected export file type/owner/mode/links/size')


def tree(root):
    result = {}
    directories = 0

    def visit(fd, prefix):
        nonlocal directories
        directories += 1
        info = os.fstat(fd)
        if (directories > 256 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('unexpected export directory owner/mode/count')
        with os.scandir(fd) as entries:
            for entry in entries:
                name = safe_name(prefix + entry.name)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        visit(child, name + '/')
                    finally:
                        os.close(child)
                else:
                    regular(info)
                    result[name] = metadata(info)
                    if len(result) > MAX_FILES or sum(v['bytes'] for v in result.values()) > MAX_BYTES:
                        raise ValueError('export file count/total quota exceeded')

    with directory(root) as fd:
        visit(fd, '')
    if not result:
        raise ValueError('empty export')
    return dict(sorted(result.items()))


@contextmanager
def opened(root, name, expected):
    with parent_descriptor(root, name) as (parent, basename):
        fd = os.open(basename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            regular(info)
            if metadata(info) != expected:
                raise ValueError('export file metadata changed')
            yield stream
            after = os.fstat(stream.fileno())
            current = os.stat(basename, dir_fd=parent, follow_symlinks=False)
            if metadata(after) != expected or metadata(current) != expected or not stat.S_ISREG(current.st_mode):
                raise ValueError('export file changed during read')


def inventory(root):
    files = tree(root)
    for name, info in files.items():
        digest = hashlib.sha256()
        with opened(root, name, info) as stream:
            remaining = info['bytes']
            while remaining:
                raw = stream.read(min(CHUNK, remaining))
                if not raw:
                    raise ValueError('short export file')
                digest.update(raw)
                remaining -= len(raw)
            if stream.read(1):
                raise ValueError('export file grew')
        info['sha256'] = digest.hexdigest()
    return {'files': files, 'total_bytes': sum(v['bytes'] for v in files.values())}


def validate_manifest(value):
    if not isinstance(value, dict) or set(value) != {'files', 'total_bytes'}:
        raise ValueError('unexpected export manifest')
    files = value['files']
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError('export manifest count outside bound')
    for name, info in files.items():
        safe_name(name)
        if (not isinstance(info, dict) or type(info.get('bytes')) is not int
                or not 0 <= info['bytes'] <= MAX_FILE
                or not re.fullmatch('[0-9a-f]{64}', str(info.get('sha256', '')))):
            raise ValueError('invalid export size/hash')
    total = sum(v['bytes'] for v in files.values())
    if type(value['total_bytes']) is not int or value['total_bytes'] != total or total > MAX_BYTES:
        raise ValueError('invalid export total quota')


def binding_bytes(binding):
    if (not isinstance(binding, dict)
            or set(binding) != {'phase_id', 'grant_id', 'boot_id', 'config_sha256'}
            or any(not isinstance(binding[k], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', binding[k])
                   for k in ['phase_id', 'grant_id'])
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', str(binding['boot_id']))
            or not re.fullmatch('[0-9a-f]{64}', str(binding['config_sha256']))):
        raise ValueError('invalid export binding')
    return binding


def encoded(value):
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    if len(raw) > MAX_META:
        raise ValueError('export metadata exceeds bound')
    return raw


def add_bytes(archive, name, raw):
    item = tarfile.TarInfo(name)
    item.size = len(raw)
    item.mode = 0o600
    archive.addfile(item, io.BytesIO(raw))


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size):
        if not 0 <= size <= CHUNK:
            raise ValueError('unbounded archive read request')
        raw = self.stream.read(size)
        self.digest.update(raw)
        return raw


def export_tar(root, manifest, binding, stream):
    validate_manifest(manifest)
    binding_bytes(binding)
    wanted = {n: {k: v for k, v in info.items() if k != 'sha256'} for n, info in manifest['files'].items()}
    if tree(root) != wanted:
        raise ValueError('export namespace/metadata changed')
    first = encoded({'schema': 1, 'binding': binding, 'manifest': manifest})
    with tarfile.open(fileobj=stream, mode='w|', format=tarfile.USTAR_FORMAT) as archive:
        add_bytes(archive, FIRST, first)
        for name, info in manifest['files'].items():
            item = tarfile.TarInfo(name)
            item.size = info['bytes']
            item.mode = 0o600
            with opened(root, name, wanted[name]) as source:
                reader = HashReader(source)
                archive.addfile(item, reader)
                if reader.digest.hexdigest() != info['sha256'] or source.read(1):
                    raise ValueError('export content changed')
        if tree(root) != wanted:
            raise ValueError('export namespace changed during stream')
        add_bytes(archive, LAST, encoded({'manifest_sha256': hashlib.sha256(first).hexdigest(),
                                        'files': len(wanted), 'total_bytes': manifest['total_bytes']}))


def read_metadata(archive, member):
    if not member or not member.isreg() or not 0 < member.size <= MAX_META:
        raise ValueError('invalid export metadata member')
    return archive.extractfile(member).read(MAX_META + 1)


class PlainTarInfo(tarfile.TarInfo):
    @classmethod
    def fromtarfile(cls, archive):
        raw = archive.fileobj.read(tarfile.BLOCKSIZE)
        member = cls.frombuf(raw, archive.encoding, archive.errors)
        # Reject PAX/GNU extended headers before tarfile reads their declared body.
        if member.type not in [tarfile.REGTYPE, tarfile.AREGTYPE] or not 0 <= member.size <= MAX_FILE:
            raise ValueError('only bounded regular tar members are permitted')
        member.offset = archive.fileobj.tell() - tarfile.BLOCKSIZE
        member.offset_data = archive.fileobj.tell()
        archive.offset = member.offset_data + ((member.size + 511) // 512) * 512
        return member


def verify_end(archive):
    padding = archive.offset - archive.fileobj.tell()
    if not 0 <= padding < tarfile.BLOCKSIZE:
        raise ValueError('invalid final member alignment')
    # Our USTAR writer emits member padding, two zero blocks and one full record.
    size = padding + 2 * tarfile.BLOCKSIZE
    raw = archive.fileobj.read(size)
    if len(raw) != size or any(raw):
        raise ValueError('missing or damaged tar zero terminators')
    size = (-archive.fileobj.tell()) % tarfile.RECORDSIZE
    raw = archive.fileobj.read(size)
    if len(raw) != size or any(raw) or archive.fileobj.read(1):
        raise ValueError('truncated padding or trailing tar bytes')


def unpack_files(archive, root_fd, manifest, manifest_raw):
    seen = set()
    while True:
        member = archive.next()
        if member is None:
            raise ValueError('missing export completion receipt')
        if member.name == LAST:
            complete = json.loads(read_metadata(archive, member))
            wanted = {'manifest_sha256': hashlib.sha256(manifest_raw).hexdigest(),
                      'files': len(manifest['files']), 'total_bytes': manifest['total_bytes']}
            if complete != wanted or seen != set(manifest['files']):
                raise ValueError('incomplete export')
            verify_end(archive)
            persisted = inventory(root_fd)
            if any(persisted['files'].get(name, {}).get('sha256') != info['sha256']
                   or persisted['files'][name]['bytes'] != info['bytes']
                   for name, info in manifest['files'].items()) or set(persisted['files']) != seen:
                raise ValueError('persisted export readback differs')
            return manifest
        name = safe_name(member.name)
        if (not member.isreg() or name in seen or name not in manifest['files']
                or member.size != manifest['files'][name]['bytes']):
            raise ValueError('unexpected/duplicate export member')
        seen.add(name)
        digest = hashlib.sha256()
        with parent_descriptor(root_fd, name, create=True) as (parent, basename):
            fd = os.open(basename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as output:
                source = archive.extractfile(member)
                remaining = member.size
                while remaining:
                    block = source.read(min(CHUNK, remaining))
                    if not block:
                        raise ValueError('short archive member')
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
                output.flush()
                os.fsync(output.fileno())
        if digest.hexdigest() != manifest['files'][name]['sha256']:
            raise ValueError('archive content hash differs')


def unpack_tar(stream, destination, expected_binding):
    binding_bytes(expected_binding)
    target = Path(destination).absolute()
    try:
        with tarfile.open(fileobj=stream, mode='r|', tarinfo=PlainTarInfo) as archive:
            first = archive.next()
            if not first or first.name != FIRST:
                raise ValueError('missing export manifest')
            raw = read_metadata(archive, first)
            value = json.loads(raw)
            if (set(value) != {'schema', 'binding', 'manifest'} or value['schema'] != 1
                    or value['binding'] != expected_binding):
                raise ValueError('foreign export binding/schema')
            manifest = value['manifest']
            validate_manifest(manifest)
            with directory(target.parent) as parent:
                os.mkdir(target.name, 0o700, dir_fd=parent)
                root_fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=parent)
                try:
                    result = unpack_files(archive, root_fd, manifest, raw)
                    # A substituted name must not turn descriptor-local success into acceptance.
                    with directory(target) as current:
                        a, b = os.fstat(root_fd), os.fstat(current)
                        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                            raise ValueError('destination namespace changed')
                    return result
                finally:
                    os.close(root_fd)
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ValueError('invalid or truncated export archive') from error
