"""Create a Windows folder bundle and sharing ZIP from the verified single-file exe.

The app bytecode and runtime are preserved exactly. Native libraries and assets
live in _internal, so the bootloader uses its standard onedir mode and does not
extract a temporary runtime on each launch.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PureWindowsPath
import shutil
import struct
import tempfile
import zipfile


def runtime_path(root, name):
    """Confine Windows archive paths to the new runtime directory."""
    relative = PureWindowsPath(name)
    if (not relative.parts or relative.anchor or relative.drive
            or any(part in ('.', '..') or ':' in part or part.endswith((' ', '.')) for part in relative.parts)):
        raise ValueError(f'Unsafe runtime path: {name!r}')
    # The wheel loads "shapely.libs"; its original Windows archive used "Shapely.libs".
    # Match the import path so this also works from a case-sensitive WSL share.
    parts = relative.parts
    if parts[0].casefold() == 'shapely.libs':
        parts = ('shapely.libs', *parts[1:])
    return root.joinpath(*parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new folder; existing folders are never overwritten')
    parser.add_argument('--name', choices=('mprice_decoder.exe', 'MaplePrice.exe'), default='mprice_decoder.exe')
    args = parser.parse_args()
    from PyInstaller.archive.readers import CArchiveReader
    import pefile
    from rebuild_decoder import resource_contents

    source, output = args.source.resolve(), args.output.resolve()
    zipped = output.with_name(output.name + '.zip')
    if output.exists() or zipped.exists():
        raise SystemExit('Output folder or ZIP already exists; choose a new output path.')
    output.parent.mkdir(parents=True, exist_ok=True)
    original = source.read_bytes()
    archive = CArchiveReader(source)
    base_pe = pefile.PE(data=original)
    if base_pe.OPTIONAL_HEADER.DATA_DIRECTORY[4].VirtualAddress:
        raise SystemExit('Use the unsigned build; repackaging would invalidate an Authenticode signature.')
    cookie_format = '!8sIIII64s'
    cookie_size = struct.calcsize(cookie_format)
    magic, _, _, _, version, library = struct.unpack(
        cookie_format, original[archive._end_offset-cookie_size:archive._end_offset])
    stage = Path(tempfile.mkdtemp(prefix='.fast-start-', dir=output.parent))
    zip_stage = stage / 'sharing.zip'
    bundle = stage / output.name
    runtime = bundle / '_internal'
    runtime.mkdir(parents=True)
    body, toc = io.BytesIO(), []
    extracted = {}

    def append(name, blob, raw_size, compressed, kind):
        start = body.tell()
        body.write(blob)
        toc.append((start, len(blob), raw_size, compressed, kind, name))

    try:
        for name, (offset, size, raw_size, compressed, kind) in archive.toc.items():
            if kind in ('b', 'x'):
                dest = runtime_path(runtime, name)
                key = dest.relative_to(runtime).as_posix().casefold()
                if key in extracted:
                    raise ValueError(f'Duplicate Windows runtime path: {name!r}')
                raw = archive.extract(name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                extracted[key] = (dest, hashlib.sha256(raw).digest())
            elif kind in ('m', 's', 'z', 'l'):
                blob = original[archive._start_offset+offset:archive._start_offset+offset+size]
                append(name, blob, raw_size, compressed, kind)
            else:
                raise ValueError(f'Unsupported archive entry {name!r}: {kind!r}')
        options = [option for option in archive.options if not option.startswith('pyi-contents-directory ')]
        options.append('pyi-contents-directory _internal')
        for option in options:
            append(option, b'', 0, 0, 'o')
        toc_start = body.tell()
        for offset, length, raw_size, compressed, kind, name in toc:
            encoded = name.encode()
            entry_size = (18 + len(encoded) + 1 + 15) // 16 * 16
            body.write(struct.pack(f'!IIIIBc{entry_size-18}s', entry_size, offset, length, raw_size,
                                   compressed, kind.encode(), encoded))
        toc_size = body.tell() - toc_start
        body.write(struct.pack(cookie_format, magic, body.tell()+cookie_size, toc_start, toc_size, version, library))
        result = bytearray(original[:archive._start_offset] + body.getvalue())
        pe = pefile.PE(data=result)
        checksum = pe.OPTIONAL_HEADER.get_field_absolute_offset('CheckSum')
        struct.pack_into('<I', result, checksum, 0)
        struct.pack_into('<I', result, checksum, pefile.PE(data=result).generate_checksum())
        exe = bundle / args.name
        exe.write_bytes(result)
        (bundle / 'READ-ME.txt').write_text(
            f'Extract this entire ZIP once, then open {args.name}.\n'
            'Keep the _internal folder next to the exe.\n'
            'A Cozy loading screen appears while MaplePrice starts.\n'
            'Press F10 for options, including Exit MaplePrice.\n'
            'To share with friends, send the complete ZIP.\n'
            'Your existing settings and captured shop visits are kept.\n', encoding='utf-8')

        # An archive with no extractable entries is the bootloader's onedir mode.
        built = CArchiveReader(exe)
        assert all(entry[-1] not in ('b', 'x') for entry in built.toc.values())
        assert 'pyi-contents-directory _internal' in built.options
        for name in built.toc:
            assert built.extract(name) == archive.extract(name), name
        for dest, digest in extracted.values():
            assert hashlib.sha256(dest.read_bytes()).digest() == digest, dest.name
        assert (runtime / library.split(b'\0', 1)[0].decode()).is_file(), 'Python runtime missing'
        built_pe = pefile.PE(data=result)
        assert built_pe.verify_checksum()
        assert resource_contents(built_pe) == resource_contents(base_pe), 'UAC/icon resources changed'
        private = runtime / 'mprice_decoder/assets/private-upload.json'
        if private.is_file():
            assert not (runtime / 'mprice_decoder/assets/client.key').exists()

        with zipfile.ZipFile(zip_stage, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as sharing:
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    sharing.write(path, path.relative_to(stage).as_posix())
        with zipfile.ZipFile(zip_stage) as sharing:
            assert sharing.testzip() is None, 'Sharing ZIP failed its integrity check'
            file_count = len(sharing.infolist())
        runtime_bytes = sum(path.stat().st_size for path, _ in extracted.values())
        bundle.replace(output)
        zip_stage.replace(zipped)
        print(json.dumps({'exe': str(output / exe.name), 'zip': str(zipped),
                          'exe_bytes': len(result), 'files': file_count,
                          'runtime_bytes': runtime_bytes,
                          'zip_bytes': zipped.stat().st_size, 'per_launch_extraction': False,
                          'verification': 'app bytecode, runtime files, UAC/icon resources, checksum and ZIP integrity passed'},
                         indent=2))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == '__main__':
    main()
