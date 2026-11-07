# SPDX-License-Identifier: MIT
"""Assert Flask's runtime closure excludes its custom default development groups."""
import argparse
from importlib.metadata import distributions
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--projection', type=Path)
args = parser.parse_args()
expected = {'flask', 'blinker', 'click', 'itsdangerous', 'jinja2', 'markupsafe', 'werkzeug'}
actual = {distribution.metadata['Name'].lower().replace('_', '-') for distribution in distributions()}
assert actual == expected, (actual, expected)
if args.projection:
    published = {wheel.name.split('-')[0].lower().replace('_', '-')
                 for wheel in args.projection.glob('*.whl')}
    assert published == expected, (published, expected)
print(sorted(actual))
