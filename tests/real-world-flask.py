#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Capture and execute pinned upstream Flask through native, pure and ordinary flows."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from source_snapshot import archive_source


SAMPLE_REV = '49943b5a94413a530761350f04e896f76e8d620e'
SAMPLE_HASH = '07ax2qyr3d7x7sayws07dv8dgbi9s8mrxj3lnlqwl6p0h417pvh7'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--python-attribute', default='python3', choices=['python3', 'python311', 'python312', 'python313', 'python314', 'python315'])
    parser.add_argument('--native-uv-epoch', choices=['uv-0.13.7'])
    args = parser.parse_args()
    sdk = Path(__file__).resolve().parents[1]
    state = args.state_root.resolve()
    state.mkdir(parents=True, exist_ok=False)
    for name in ('tmp', 'cache', 'config', 'data', 'state'):
        (state / name).mkdir()
    env = dict(os.environ, TMPDIR=str(state / 'tmp'), XDG_CACHE_HOME=str(state / 'cache'),
               XDG_CONFIG_HOME=str(state / 'config'), XDG_DATA_HOME=str(state / 'data'),
               XDG_STATE_HOME=str(state / 'state'), UV_CACHE_DIR=str(state / 'cache/uv'),
               UV_PYTHON_DOWNLOADS='never', PYTHONDONTWRITEBYTECODE='1')
    env.pop('UV_PROJECT_ENVIRONMENT', None)
    env['NIX_CONFIG'] = env.get('NIX_CONFIG', '') + '\nmax-jobs = 2\ncores = 2\neval-cache = false\nsandbox = true\n'
    stages = ('capture-lock', 'relock', 'prefetch', 'offline-upstream-tests',
              'pure-upstream-tests', 'runtime-derivation', 'ordinary-consumer', 'ordinary-missing-dependency-control')
    report = {'schema': 1, 'scope': 'exact pinned Flask and shipped Python/uv; no version intervals',
              'driverSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'sample': {'url': 'https://github.com/pallets/flask', 'revision': SAMPLE_REV},
              'results': {name: 'not-run' for name in stages},
              'executionOptions': {'max-jobs': 2, 'cores': 2, 'eval-cache': False, 'sandbox': True}}

    def save():
        report['allPassed'] = all(value == 'passed' for value in report['results'].values())
        (state / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    def run(*command, cwd=sdk, environment=env, fail=False):
        command = [str(item) for item in command]
        if command[0] == 'nix':
            command[1:1] = ['--option', 'max-jobs', '2', '--option', 'cores', '2',
                            '--option', 'eval-cache', 'false', '--option', 'sandbox', 'true']
        result = subprocess.run(command, cwd=cwd, env=environment, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with (state / 'commands.log').open('a') as log:
            log.write(json.dumps({'argv': command, 'cwd': str(cwd), 'exit': result.returncode}) + '\n')
            log.write(result.stdout + result.stderr + '\n')
        if fail:
            assert result.returncode != 0, 'negative control unexpectedly succeeded'
        elif result.returncode:
            raise RuntimeError((result.stdout + result.stderr)[-8000:])
        return (result.stdout + result.stderr).strip() if fail else result.stdout.strip()

    def passed(name):
        report['results'][name] = 'passed'
        save()
        print('PASS flask.' + name, flush=True)

    def digests(project):
        return {name: hashlib.sha256((project / name).read_bytes()).hexdigest()
                for name in ('pyproject.toml', 'uv.lock', 'uv-purifier.lock.json')}

    active = 'capture-lock'
    save()
    try:
        report['sandbox'] = {'effective': run('nix', 'config', 'show', 'sandbox'),
                             'hostCaptureOutsideSandbox': True}
        assert report['sandbox']['effective'] == 'true', 'pure/runtime checks require sandbox=true'
        save()
        metadata, snapshot = archive_source(run, sdk)
        report['source'] = {'path': snapshot, 'narHash': metadata['locked']['narHash'],
                            'revision': run('git', 'rev-parse', 'HEAD'),
                            'dirty': bool(run('git', 'status', '--porcelain', '--untracked-files=no')),
                            'flakeLockSha256': hashlib.sha256((sdk / 'flake.lock').read_bytes()).hexdigest()}
        system = run('nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem')
        report['system'] = system
        prefix = 'let f=builtins.getFlake ' + json.dumps('path:' + snapshot) + '; p=f.inputs.nixpkgs.legacyPackages.' + system + '; in '
        def build(expression):
            rows = json.loads(run('nix', 'build', '--impure', '--no-link', '--json', '--expr', prefix + expression))
            return [row['outputs']['out'] for row in rows]
        uv_expression = '(p.callPackage (f.outPath + \"/tests/sdk/uv-0.13.7.nix\") {})' if args.native_uv_epoch else 'p.uv'
        cli = Path(build('(f.packages.' + system + '.cli.override { nativeUv = ' + uv_expression + '; })')[0]) / 'bin/uv-purifier'
        python = Path(build('p.' + args.python_attribute)[0]) / 'bin/python3'
        uv = Path(build(uv_expression)[0]) / 'bin/uv'
        helper_uv = Path(build('p.uv')[0]) / 'bin/uv'
        helper_python = Path(build('p.python3')[0]) / 'bin/python3'
        env['UV_PYTHON'] = str(python)
        report['runtime'] = {'python': str(python), 'pythonVersion': run(python, '--version'),
                             'uv': str(uv), 'uvVersion': run(uv, '--version'), 'pythonAttribute': args.python_attribute,
                             'uvSelection': args.native_uv_epoch or 'shipped-default',
                             'helperUv': str(helper_uv), 'helperUvVersion': run(helper_uv, '--version'),
                             'helperPython': str(helper_python), 'helperPythonVersion': run(helper_python, '--version')}
        source = Path(build('p.fetchFromGitHub { owner="pallets"; repo="flask"; rev=' +
                            json.dumps(SAMPLE_REV) + '; sha256=' + json.dumps(SAMPLE_HASH) + '; }')[0])
        report['sample'].update(path=str(source), narHash=run('nix', 'hash', 'path', source))
        project = state / 'flask'
        shutil.copytree(source, project)
        for path in [project, *project.rglob('*')]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o200)
        manifest = (project / 'pyproject.toml').read_bytes()
        native_lock = (project / 'uv.lock').read_bytes()
        run(cli, 'lock', '--project', project)
        assert (project / 'pyproject.toml').read_bytes() == manifest
        assert (project / 'uv.lock').read_bytes() == native_lock, 'capture changed upstream native lock'
        report['lockDigests'] = digests(project)
        passed(active)
        active = 'relock'
        run(cli, 'lock', '--project', project)
        assert digests(project) == report['lockDigests'], 'relock changed lock bytes'
        passed(active)
        # Freeze a clean packaging source before the editable environment exists.
        frozen = state / 'pure-source'
        shutil.copytree(project, frozen, ignore=shutil.ignore_patterns('.venv', '.uv-purifier', '__pycache__', '.git'))
        packaging = state / 'packaging'
        packaging.mkdir()
        (packaging / 'flake.nix').write_text('''{
  inputs.sdk.url = %s;
  inputs.nixpkgs.follows = "sdk/nixpkgs";
  outputs = {sdk, nixpkgs, ...}: let
    system = %s;
    p = import nixpkgs { inherit system; overlays = [ sdk.overlays.default ]; };
    nativeUv = %s;
    cli = p.uv-purifier.cli.override { inherit nativeUv; };
    project = p.uv-purifier.buildUvProject {
      src = ./pure-source;
      python = p.%s;
      inherit nativeUv;
      testCommand = [ (p.writeShellScript "flask-upstream-and-extension" ''
        set -eu
        pytest -q -p no:cacheprovider
        %s
      '') ];
    };
  in {
    checks.${system}.runtime = import (sdk.outPath + "/tests/flask-runtime.nix") {
      pkgs = p;
      inherit project cli;
      src = ./pure-source;
      uv = nativeUv;
      python = p.%s;
      requireCSource = %s;
      receipt = builtins.fromJSON (builtins.readFile ./capture-receipt.json);
    };
    packages.${system} = {
      tests = project.tests;
      projection = project.packages.flask.projections.python;
    };
  };
}
''' % (json.dumps('path:' + snapshot), json.dumps(system), '(p.callPackage (sdk.outPath + \"/tests/sdk/uv-0.13.7.nix\") {})' if args.native_uv_epoch else 'p.uv', args.python_attribute, 'python -I ${sdk.outPath}/tests/native-extension.py' if args.python_attribute == 'python315' else ':', args.python_attribute, 'true' if args.python_attribute == 'python315' else 'false'))
        (packaging / 'capture-receipt.json').write_text(json.dumps(report, indent=2) + '\n')
        shutil.move(str(frozen), str(packaging / 'pure-source'))
        active = 'prefetch'
        run(cli, 'prefetch', '--project', project,
            environment=dict(env, CC='/foreign-host-compiler', CXX='/foreign-host-cxx',
                             SDKROOT='/foreign-host-sdk', NIX_CFLAGS_COMPILE='-I/foreign-host-headers'))
        assert digests(project) == report['lockDigests'], 'prefetch changed manifests or locks'
        passed(active)
        active = 'offline-upstream-tests'
        run(uv, '--offline', 'run', '--project', project, '--no-sync', 'pytest', '-q', '-p', 'no:cacheprovider', cwd=project)
        passed(active)
        active = 'pure-upstream-tests'
        report['derivations'] = {}
        def evidence_build(name, attribute):
            rows = json.loads(run('nix', 'build', '--no-link', '-L', '--json', 'path:' + str(packaging) + attribute))
            report['derivations'][name] = rows
            for row in rows:
                row['outputNarHashes'] = {key: run('nix', 'hash', 'path', path) for key, path in row['outputs'].items()}
            save()
            return rows[0]['outputs']['out']
        evidence_build('pure-upstream-tests', '#tests')
        projection = evidence_build('projection', '#projection')
        report['projection'] = projection
        passed(active)
        active = 'runtime-derivation'
        evidence_build('native-and-ordinary-runtime', '#checks.' + system + '.runtime')
        passed(active)
        active = 'ordinary-consumer'
        cache = state / 'ordinary-cache'
        assert not cache.exists()
        ordinary_env = dict(env, UV_CACHE_DIR=str(cache))
        downstream = state / 'ordinary'
        run(uv, '--cache-dir', cache, 'venv', '--python', python, downstream, environment=ordinary_env)
        run(uv, '--cache-dir', cache, 'pip', 'install', '--offline', '--no-index', '--find-links', projection,
            '--python', downstream / 'bin/python', 'flask', environment=ordinary_env)
        smoke = '''from flask import Flask
from importlib.metadata import version
app=Flask(__name__)
@app.get('/')
def index(): return 'purified flask'
assert app.test_client().get('/').data == b'purified flask'
assert '+nix.' in version('flask')
'''
        run(downstream / 'bin/python', '-I', '-c', smoke, environment=ordinary_env)
        report['ordinaryCache'] = {'initialState': 'absent', 'path': str(cache), 'install': 'offline/no-index'}
        passed(active)
        active = 'ordinary-missing-dependency-control'
        incomplete = state / 'incomplete-publication'
        incomplete.mkdir()
        for wheel in Path(projection).glob('*.whl'):
            if not wheel.name.lower().startswith('werkzeug-'):
                (incomplete / wheel.name).symlink_to(wheel)
        assert not list(incomplete.glob('werkzeug-*.whl'))
        missing = state / 'missing'
        missing_cache = state / 'missing-cache'
        negative_env = dict(env, UV_CACHE_DIR=str(missing_cache))
        run(uv, '--cache-dir', missing_cache, 'venv', '--python', python, missing, environment=negative_env)
        error = run(uv, '--cache-dir', missing_cache, 'pip', 'install', '--offline', '--no-index',
                    '--find-links', incomplete, '--python', missing / 'bin/python', 'flask',
                    environment=negative_env, fail=True)
        assert 'werkzeug' in error.lower(), error
        assert digests(project) == report['lockDigests'], 'runtime changed manifests or locks'
        passed(active)
    except BaseException as error:
        report['results'][active] = 'failed'
        report['error'] = repr(error)
        save()
        raise


if __name__ == '__main__':
    main()
