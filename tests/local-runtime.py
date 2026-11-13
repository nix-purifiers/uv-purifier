#!/usr/bin/env python3
"""Retain a real installed runtime through local capture and offline replay.

Run under an external process-group deadline, with a private disk --state.
The supplied SDK is used as-is; this test never builds a language runtime.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--other-python', type=Path, help='distinct installed SDK for a real environment identity rejection')
    parser.add_argument('--fixture', choices=['simple', 'sources', 'conditional-sources'], default='simple')
    args = parser.parse_args()
    args.state = args.state.resolve()
    args.state.mkdir(parents=True, exist_ok=True)
    previous = args.state / (args.fixture + '-receipt.json')
    if previous.exists():
        history = args.state / 'history' / (args.fixture + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        history.mkdir(parents=True)
        for artifact in args.state.glob(args.fixture + '-*'):
            if artifact.is_file():
                shutil.copy2(artifact, history / artifact.name)
        first_environment = args.state / (args.fixture + '-first-environment')
        if first_environment.exists():
            first_environment.rename(history / 'first-environment')
    root = Path(__file__).resolve().parents[1]
    project = args.state / args.fixture
    if not project.exists():
        shutil.copytree(root / 'tests/fixtures' / args.fixture, project)
    temporary = args.state / 'tmp'
    temporary.mkdir(exist_ok=True)
    environment = dict(os.environ, UV_PYTHON=str(args.python.resolve()),
                       UV_PYTHON_DOWNLOADS='never', UV_CACHE_DIR=str(args.state / 'uv-cache'),
                       XDG_CACHE_HOME=str(args.state / 'cache'), XDG_DATA_HOME=str(args.state / 'data'),
                       XDG_STATE_HOME=str(args.state / 'state'), TMPDIR=str(temporary),
                       TMPPREFIX=str(temporary / 'zsh'))
    environment['NIX_CONFIG'] = environment.get('NIX_CONFIG', '') + '\ncores = 2\nmax-jobs = 1\n'
    receipt = {'fixture': args.fixture, 'project': str(project), 'phases': [], 'status': 'running'}
    receipt['source_hashes'] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                               for name in ('src/uv_purifier.py', 'pkgs/build-uv-project/default.nix',
                                            'pkgs/cli/cli.nix', 'tests/local-runtime.py')}

    def save():
        (args.state / (args.fixture + '-receipt.json')).write_text(json.dumps(receipt, indent=2) + '\n')

    def run(name, command, extra=None, cwd=None, expect_failure=False):
        command = [str(item) for item in command]
        phase = {'phase': name, 'argv': command, 'status': 'running', 'environment_overrides': extra or {},
                 'cwd': str(cwd or project), 'expected_failure': expect_failure}
        receipt['phases'].append(phase)
        save()
        log = args.state / (args.fixture + '-' + name + '.log')
        with log.open('w') as output:
            result = subprocess.run(command, cwd=cwd or project, env=environment | (extra or {}),
                                    stdout=output, stderr=subprocess.STDOUT)
        passed = (result.returncode != 0) if expect_failure else (result.returncode == 0)
        phase.update(status='passed' if passed else 'failed', exit_code=result.returncode)
        save()
        if not passed:
            raise RuntimeError(name + ' failed: ' + str(log))
        return log.read_text()

    def digests(paths):
        return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

    identity_code = ('import json,os,sys,sysconfig;print(json.dumps({'
                     '"executable":os.path.realpath(getattr(sys,"_base_executable",sys.executable)),'
                     '"prefix":sys.base_prefix,"version":list(sys.version_info[:3]),'
                     '"soabi":sysconfig.get_config_var("SOABI")}))')
    try:
        identity = json.loads(run('sdk', [args.python, '-I', '-c', identity_code]))
        if identity['executable'].startswith('/nix/store/'):
            raise RuntimeError('This acceptance case needs a real non-store interpreter executable')
        receipt['runtime'] = identity
        sdk_hash = hashlib.sha256(Path(identity['executable']).read_bytes()).hexdigest()
        receipt['runtime_executable_sha256'] = sdk_hash
        inputs = [project / path.relative_to(root / 'tests/fixtures' / args.fixture)
                  for path in (root / 'tests/fixtures' / args.fixture).rglob('*') if path.is_file()
                  and path.name not in ('uv.lock', 'uv-purifier.lock.json')]
        before = digests(inputs)
        built = run('build-cli', ['nix', 'build', 'path:' + str(root) + '#default',
                                 '--out-link', args.state / 'cli', '--print-out-paths'])
        receipt['cli'] = built.strip().splitlines()[-1]
        cli = Path(receipt['cli']) / 'bin/uv-purifier'
        run('lock', [cli, 'lock'])
        locks = [project / name for name in ('uv.lock', 'uv-purifier.lock.json')]
        locked = digests(locks)
        run('identical-relock', [cli, 'lock'])
        assert digests(locks) == locked, 'Relocking changed lock bytes'
        for attempt in ('first', 'replay'):
            if attempt == 'replay':
                (project / '.venv').rename(args.state / (args.fixture + '-first-environment'))
            extra = {'UV_OFFLINE': '1'}
            if attempt == 'replay':
                extra.update(XDG_CACHE_HOME=str(args.state / (args.fixture + '-cold-cache')),
                             UV_CACHE_DIR=str(args.state / (args.fixture + '-cold-uv-cache')))
            run(attempt + '-prefetch', [cli, 'prefetch'], extra)
            installed = json.loads(run(attempt + '-runtime', [project / '.venv/bin/python', '-I', '-c', identity_code]))
            assert installed == identity, (installed, identity)
            if args.fixture == 'conditional-sources':
                text = run(attempt + '-command', [project / '.venv/bin/python', '-I', '-c',
                    'import active_direct,active_editable,importlib.metadata as m; '
                    'assert active_direct.VALUE=="active conditional source executed"; '
                    'assert active_editable.VALUE=="active conditional editable executed"; '
                    'names={d.metadata["Name"].lower() for d in m.distributions()}; '
                    'assert "windows-direct" not in names and "windows-editable" not in names; '
                    'print(active_direct.VALUE)'], {'UV_OFFLINE': '1'})
            else:
                text = run(attempt + '-command', [project / '.venv/bin' / args.fixture], {'UV_OFFLINE': '1'})
            if args.fixture == 'simple':
                assert 'attrs=25.3.0 docopt=0.6.2 markupsafe=3.0.3 &lt;nix&gt;' in text, text
            elif args.fixture == 'sources':
                assert 'attrs=25.3.0 six=1.17.0 idna=3.10 localdir 0.3.0 localsdist 0.4.0 localwheel 0.5.0' in text, text
            assert digests(locks) == locked, 'Prefetch changed a lockfile'
            assert digests(inputs) == before, 'A user input changed'
        if args.other_python is not None:
            other = json.loads(run('other-sdk', [args.other_python, '-I', '-c', identity_code]))
            assert other['executable'] != identity['executable'], 'Comparison SDK must be distinct'
            text = run('reject-runtime-substitution', [cli, 'prefetch'],
                       {'UV_PYTHON': str(args.other_python.resolve()), 'UV_OFFLINE': '1'}, expect_failure=True)
            assert 'different runtime' in text, text
            assert json.loads(run('preserved-runtime', [project / '.venv/bin/python', '-I', '-c', identity_code])) == identity
        constraint = args.state / (args.fixture + '-constraint')
        constraint.mkdir(exist_ok=True)
        manifest = constraint / 'pyproject.toml'
        manifest.write_text('[project]\nname="runtime-constraint"\nversion="0"\nrequires-python="<%d.%d"\n'
                            % tuple(identity['version'][:2]))
        manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
        text = run('requires-python-error', [cli, 'lock'], cwd=constraint, expect_failure=True)
        assert 'requires-python' in text or 'requires Python' in text, text
        assert hashlib.sha256(manifest.read_bytes()).hexdigest() == manifest_hash
        assert hashlib.sha256(Path(identity['executable']).read_bytes()).hexdigest() == sdk_hash
        assert digests(locks) == locked and digests(inputs) == before
        receipt.update(status='passed', lock_digests=locked, input_digests=before)
    except Exception as error:
        receipt.update(status='failed', error=str(error))
        raise
    finally:
        save()


if __name__ == '__main__':
    main()
