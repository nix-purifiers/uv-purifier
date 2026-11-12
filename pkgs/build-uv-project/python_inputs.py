# SPDX-License-Identifier: MIT
"""Apply Nix-authoritative wheel selections to an ephemeral native uv project."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import tomli_w


def selections(views, python=None):
    from python_repository import interpreter_tags, selections as choose
    return choose(views, interpreter_tags(python) if python is not None else None)


def supplied(path, roots):
    return any(Path(path).is_relative_to(root) for root in roots)


def prepare(project, views, old_roots, index=None, wheelhouse=None, python=None):
    selected = selections(views, python)
    pyproject = project / 'pyproject.toml'
    model = tomllib.loads(pyproject.read_text())
    uv = model.setdefault('tool', {}).setdefault('uv', {})
    overrides = [value for value in uv.get('override-dependencies', [])
                 if canonicalize_name(Requirement(value).name) not in selected]
    overrides.extend(name + ' @ ' + module['wheel'].as_uri() for name, module in sorted(selected.items()))
    uv['override-dependencies'] = overrides
    pyproject.write_text(tomli_w.dumps(model))
    lockfile = project / 'uv.lock'
    if lockfile.exists():
        lock = tomllib.loads(lockfile.read_text())
        for package in lock.get('package', []):
            name = canonicalize_name(package['name'])
            source = package.get('source', {})
            if name in selected and 'path' in source and supplied(source['path'], old_roots):
                module = selected[name]
                package['version'] = module['version']
                package['source'] = {'path': str(module['wheel'])}
                package['wheels'] = [{'filename': module['wheel'].name,
                    'hash': 'sha256:' + hashlib.sha256(module['wheel'].read_bytes()).hexdigest()}]
        def references(value):
            if isinstance(value, list):
                for item in value:
                    references(item)
            elif isinstance(value, dict):
                name = canonicalize_name(value.get('name', ''))
                source = value.get('source', {})
                if name in selected and isinstance(source, dict) and 'path' in source and supplied(source['path'], old_roots):
                    value['source'] = {'path': str(selected[name]['wheel'])}
                    if 'version' in value:
                        value['version'] = selected[name]['version']
                for child in value.values():
                    references(child)
        references(lock)
        manifest = lock.setdefault('manifest', {})
        overrides = [item for item in manifest.get('overrides', [])
                     if canonicalize_name(item['name']) not in selected]
        overrides.extend({'name': name, 'path': str(module['wheel'])} for name, module in sorted(selected.items()))
        manifest['overrides'] = overrides
        lockfile.write_text(tomli_w.dumps(lock))
    if index is not None:
        result = json.loads(index.read_text())
        for name, module in selected.items():
            directory = wheelhouse / name
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / module['wheel'].name
            if not target.exists():
                target.symlink_to(module['wheel'])
            result['paths'][str(module['wheel'])] = str(directory)
        index.write_text(json.dumps(result))
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--views', required=True, type=Path)
    parser.add_argument('--old-repositories', required=True, type=Path)
    parser.add_argument('--index', type=Path)
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--python', type=Path, help='project interpreter whose wheel tags must be selected')
    args = parser.parse_args()
    prepare(args.project, json.loads(args.views.read_text()), json.loads(args.old_repositories.read_text()),
            args.index, args.wheelhouse, python=args.python)


if __name__ == '__main__':
    main()
