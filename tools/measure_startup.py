"""Time a frozen Windows app through --help without interrupting the running overlay."""
import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time


def prepare_probe(source):
    """Clone only the elevation manifest; --help exits before any privileged work."""
    import pefile

    raw = bytearray(source.read_bytes())
    pe = pefile.PE(data=raw)
    changed = 0
    for resource in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if resource.id != 24:  # RT_MANIFEST
            continue
        for name in resource.directory.entries:
            for language in name.directory.entries:
                entry = language.data.struct
                start = pe.get_offset_from_rva(entry.OffsetToData)
                content = bytes(raw[start:start + entry.Size])
                before = b'level="requireAdministrator"'
                after = b'level="asInvoker"'
                if before in content:
                    content = content.replace(before, after + b' ' * (len(before) - len(after)))
                    raw[start:start + entry.Size] = content
                    changed += 1
    if changed != 1:
        raise RuntimeError('Expected exactly one administrator manifest for the temporary startup probe.')
    checksum = pe.OPTIONAL_HEADER.get_field_absolute_offset('CheckSum')
    struct.pack_into('<I', raw, checksum, 0)
    struct.pack_into('<I', raw, checksum, pefile.PE(data=raw).generate_checksum())
    probe = source.with_name(f'.startup-probe-{os.getpid()}.exe')
    with probe.open('xb') as stream:
        stream.write(raw)
    return probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('exe', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--probe', action='store_true', help='measure through a temporary non-elevated --help copy')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Run with Windows Python to measure the Windows executable.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'exe': str(args.exe), 'started': datetime.now(timezone.utc).isoformat(),
              'admin': bool(ctypes.windll.shell32.IsUserAnAdmin()), 'complete': False, 'runs': []}

    def save():
        staging = args.output.with_suffix('.json.part')
        staging.write_text(json.dumps(report, indent=2), encoding='utf-8')
        staging.replace(args.output)

    save()
    measured_exe = args.exe
    try:
        if args.probe:
            measured_exe = prepare_probe(args.exe)
            report['non_elevated_help_probe'] = True
            save()
        for index in range(args.runs):
            measured = {'startup_seconds': None}
            started = time.perf_counter()
            proc = subprocess.Popen([str(measured_exe), '--help'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
                                    creationflags=subprocess.CREATE_NO_WINDOW)

            def read_output():
                with args.output.with_suffix(f'.run{index + 1}.txt').open('w', encoding='utf-8') as stream:
                    for line in proc.stdout:
                        if measured['startup_seconds'] is None and line.lstrip().startswith('usage: '):
                            measured['startup_seconds'] = round(time.perf_counter() - started, 3)
                        stream.write(line)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            try:
                measured['exit_code'] = proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                raise RuntimeError('Startup probe exceeded 180 seconds.')
            reader.join(timeout=5)
            measured['total_seconds'] = round(time.perf_counter() - started, 3)
            report['runs'].append(measured)
            save()
            if measured['exit_code'] != 0 or measured['startup_seconds'] is None:
                raise RuntimeError('Startup probe failed; see the associated output file.')
        report['complete'] = True
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        if measured_exe != args.exe:
            measured_exe.unlink(missing_ok=True)
    save()


if __name__ == '__main__':
    main()
