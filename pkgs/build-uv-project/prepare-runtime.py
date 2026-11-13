#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Give installed ELF files access to their declared native runtime libraries.

Wheel archives remain unchanged and hash checked by uv. Never follow symlinks
out of the installed environment, including the venv's interpreter link.
"""
import argparse
import base64
import csv
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile


def detach(path):
    if path.stat().st_nlink > 1:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            detached = Path(temporary.name)
        try:
            shutil.copy2(path, detached)
            detached.replace(path)
        finally:
            detached.unlink(missing_ok=True)


def has_runpath(path):
    """Identify RUNPATH without adding a binutils runtime dependency."""
    with path.open('rb') as source:
        header = source.read(64)
        endian = '<' if header[5] == 1 else '>'
        wide = header[4] == 2
        offset = struct.unpack_from(endian + ('Q' if wide else 'I'), header, 32 if wide else 28)[0]
        size, count = struct.unpack_from(endian + 'HH', header, 54 if wide else 42)
        for index in range(count):
            source.seek(offset + index * size)
            segment = struct.unpack(endian + ('IIQQQQQQ' if wide else 'IIIIIIII'),
                                    source.read(56 if wide else 32))
            if segment[0] != 2:  # PT_DYNAMIC
                continue
            source.seek(segment[2] if wide else segment[1])
            remaining = segment[5] if wide else segment[4]
            entry_size = 16 if wide else 8
            for _ in range(remaining // entry_size):
                tag, _ = struct.unpack(endian + ('qQ' if wide else 'iI'), source.read(entry_size))
                if tag == 29:  # DT_RUNPATH
                    return True
                if tag == 0:
                    break
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('environment', type=Path)
    parser.add_argument('--patchelf', required=True)
    parser.add_argument('--library-dir', action='append', default=[])
    parser.add_argument('--interpreter', help='dynamic loader of the selected immutable Python')
    args = parser.parse_args()
    modified = set()
    records = []
    for directory, _, names in os.walk(args.environment, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            if name == 'RECORD' and path.parent.name.endswith('.dist-info'):
                records.append(path)
            with path.open('rb') as source:
                if source.read(4) != b'\x7fELF':
                    continue
            needed = subprocess.run([args.patchelf, '--print-needed', str(path)],
                                    capture_output=True, text=True)
            # Static ELF executables have no dynamic dependencies or RPATH.
            if needed.returncode:
                continue
            if args.interpreter:
                interpreter = subprocess.run([args.patchelf, '--print-interpreter', str(path)],
                                             capture_output=True, text=True)
                if interpreter.returncode == 0 and interpreter.stdout.strip() != args.interpreter:
                    detach(path)
                    subprocess.run([args.patchelf, '--set-interpreter', args.interpreter, str(path)], check=True)
                    modified.add(path.resolve())
            dependencies = needed.stdout.splitlines()
            additions = [directory for directory in args.library_dir
                         if any((Path(directory) / dependency).exists() for dependency in dependencies)]
            if not additions:
                continue
            old = subprocess.check_output([args.patchelf, '--print-rpath', str(path)], text=True).strip()
            paths = old.split(':') if old else []
            updated = list(dict.fromkeys(paths + additions))
            if updated != paths:
                # A new RUNPATH also blocks an ancestor's inherited RPATH.
                # Keep existing RUNPATH semantics, otherwise use RPATH.
                preserve_rpath = [] if has_runpath(path) else ['--force-rpath']
                # Older environments may still share unpacked cache inodes.
                detach(path)
                subprocess.run([args.patchelf, *preserve_rpath, '--set-rpath', ':'.join(updated), str(path)], check=True)
            modified.add(path.resolve())
    for record in records:
        with record.open(newline='') as source:
            rows = list(csv.reader(source))
        changed = False
        for row in rows:
            if not row:
                continue
            path = (record.parent.parent / row[0]).resolve()
            if path in modified:
                with path.open('rb') as source:
                    digest = hashlib.file_digest(source, 'sha256').digest()
                fields = ['sha256=' + base64.urlsafe_b64encode(digest).rstrip(b'=').decode(), str(path.stat().st_size)]
                if row[1:] != fields:
                    row[1:] = fields
                    changed = True
        if changed:
            detach(record)
            with record.open('w', newline='') as output:
                csv.writer(output).writerows(rows)


if __name__ == '__main__':
    main()
