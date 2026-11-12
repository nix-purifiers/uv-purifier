# SPDX-License-Identifier: MIT
"""Export the installed native runtime as wheels, excluding project-owned wheels."""
import argparse
from importlib.metadata import PathDistribution
from pathlib import Path
import shutil

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--environment', required=True, type=Path)
parser.add_argument('--own', action='append', default=[], type=Path)
parser.add_argument('--candidate', action='append', default=[], type=Path)
parser.add_argument('--destination', required=True, type=Path)
args = parser.parse_args()
owned = {parse_wheel_filename(path.name)[0] for directory in args.own for path in directory.glob('*.whl')}
candidates = {}
for directory in args.candidate:
    for path in sorted(directory.glob('*.whl')):
        name, version, _, _ = parse_wheel_filename(path.name)
        candidates.setdefault((name, version), []).append(path)
args.destination.mkdir(parents=True, exist_ok=True)
for directory in sorted(args.environment.glob('lib/python*/site-packages/*.dist-info')):
    distribution = PathDistribution(directory)
    name = canonicalize_name(distribution.metadata['Name'])
    if name in owned:
        continue
    key = name, Version(distribution.version)
    if key not in candidates:
        raise ValueError(f'no locked runtime wheel for installed distribution {key}')
    for source in candidates[key]:
        target = args.destination / source.name
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError(f'conflicting native runtime wheel: {source.name}')
        shutil.copyfile(source, target)
