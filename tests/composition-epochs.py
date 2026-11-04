#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise an independently packaged historical Python with current uv/helpers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-nixpkgs', type=Path, required=True)
    parser.add_argument('--python-attribute', default='python39')
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--shared-library', type=Path, help='explicit shared-library candidate; recorded separately')
    args = parser.parse_args()
    if not args.python_attribute.startswith('python') or not args.python_attribute[6:].isdigit():
        parser.error('expected an explicit Python package attribute')
    sdk = Path(__file__).resolve().parents[1]
    state = args.state_root.resolve()
    state.mkdir(parents=True, exist_ok=False)
    for directory in ('tmp', 'cache', 'config', 'data', 'state'):
        (state / directory).mkdir()
    env = dict(os.environ, TMPDIR=str(state / 'tmp'), XDG_CACHE_HOME=str(state / 'cache'),
               XDG_CONFIG_HOME=str(state / 'config'), XDG_DATA_HOME=str(state / 'data'),
               XDG_STATE_HOME=str(state / 'state'), UV_PYTHON_DOWNLOADS='never',
               UV_CACHE_DIR=str(state / 'cache/uv'))
    report = {'version': 1, 'scope': 'exact Python target/helper tuple; no inferred intervals',
              'cacheProfile': 'cold-native-cache',
              'driverDigest': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'targetNixpkgs': str(args.target_nixpkgs.resolve()),
              'targetAttribute': args.python_attribute,
              'results': {name: {'status': 'not-run'} for name in
                          ('target-runtime', 'native-lock', 'producer-pure-build',
                           'incoming-lock', 'interactive-prefetch', 'incoming-pure-build', 'ordinary-consumer')}}
    active = 'target-runtime'

    def save():
        report['fixtureLockDigests'] = {
            str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
            for name in ('epoch_provider', 'epoch_consumer')
            for filename in ('uv.lock', 'uv-purifier.lock.json', 'flake.lock')
            if (path := state / name / filename).is_file()
        }
        report['selectedCasesPassed'] = all(row['status'] == 'passed' for row in report['results'].values())
        (state / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    def run(*command, cwd=sdk, environment=env):
        result = subprocess.run([str(item) for item in command], cwd=cwd, env=environment,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with (state / 'commands.log').open('a') as log:
            log.write(json.dumps([str(item) for item in command]) + '\n' + result.stdout + result.stderr + '\n')
        if result.returncode:
            raise RuntimeError(result.stderr[-6000:])
        return result.stdout.strip()

    def passed(name):
        report['results'][name] = {'status': 'passed'}
        save()
        print('PASS python.epoch.' + name, flush=True)

    save()
    try:
        metadata = json.loads(run('nix', 'flake', 'metadata', '--json', '--no-write-lock-file', str(sdk)))
        snapshot = metadata['path']
        archived = json.loads(run('nix', 'flake', 'archive', '--json', '--no-write-lock-file', str(sdk)))
        assert archived['path'] == snapshot, 'SDK source changed during fixture setup'
        shared_snapshot = None
        if args.shared_library:
            shared = json.loads(run('nix', 'flake', 'metadata', '--json', '--no-write-lock-file', str(args.shared_library.resolve())))
            shared_snapshot = shared['path']
            archived_shared = json.loads(run('nix', 'flake', 'archive', '--json', '--no-write-lock-file', str(args.shared_library.resolve())))
            assert archived_shared['path'] == shared_snapshot, 'shared source changed during fixture setup'
            report['sharedLibrary'] = {'path': shared_snapshot, 'narHash': shared['locked']['narHash']}
        system = run('nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem')
        report.update(system=system, source={'path': snapshot, 'narHash': metadata['locked']['narHash'],
                                             'revision': run('git', 'rev-parse', 'HEAD'),
                                             'dirty': bool(run('git', 'status', '--porcelain', '--untracked-files=no'))})
        report['targetNixpkgsNarHash'] = run('nix', 'hash', 'path', args.target_nixpkgs.resolve())
        helper_expression = 'let f=builtins.getFlake ' + json.dumps('path:' + snapshot) + '; in f.inputs.nixpkgs.legacyPackages.' + system + '.python3'
        report['helperPython'] = {'version': json.loads(run('nix', 'eval', '--impure', '--json', '--expr', '(' + helper_expression + ').version')),
                                  'package': run('nix', 'eval', '--impure', '--raw', '--expr', '(' + helper_expression + ').outPath')}
        target_expression = 'let p=import ' + str(args.target_nixpkgs.resolve()) + ' {config.allowInsecure=true;}; in p.' + args.python_attribute
        target = Path(run('nix', 'build', '--impure', '--expr', target_expression,
                          '--out-link', state / 'target', '--print-out-paths'))
        python = target / 'bin/python3'
        version = run(python, '-I', '-c', 'import platform;print(platform.python_version())')
        report['targetPython'] = {'package': str(target), 'version': version}
        minor = '.'.join(version.split('.')[:2])
        next_minor = str(int(version.split('.')[1]) + 1)
        override = ['--override-input', 'purifier', 'path:' + shared_snapshot] if shared_snapshot else []
        cli = Path(run('nix', 'build', str(sdk) + '#default', '--out-link', state / 'cli', '--print-out-paths', *override)) / 'bin/uv-purifier'
        uv = Path(run('nix', 'build', '--impure', '--expr',
                      'let f=builtins.getFlake ' + json.dumps('path:' + snapshot) +
                      '; in f.inputs.nixpkgs.legacyPackages.' + system + '.uv',
                      '--out-link', state / 'uv', '--print-out-paths')) / 'bin/uv'
        env['UV_PYTHON'] = str(python)
        report.update(nativeManager=run(uv, '--version'), helperPackage=str(cli.parent.parent))
        passed(active)

        def project(name, dependencies):
            directory = state / name
            (directory / 'src' / name).mkdir(parents=True)
            (directory / 'src' / name / '__init__.py').write_text(
                'import sys\nimport markupsafe._speedups\n'
                'def main():\n    print("target=" + str(sys.version_info[0]) + "." + str(sys.version_info[1]))\n')
            (directory / 'pyproject.toml').write_text(
                '[project]\nname=' + json.dumps(name) + '\nversion="1.0"\nrequires-python=' +
                json.dumps('>=' + minor + ',<3.' + next_minor) + '\ndependencies=' + json.dumps(dependencies) +
                '\n[project.scripts]\n' + name + '="' + name + ':main"\n'
                '[build-system]\nrequires=["hatchling==1.18.0"]\nbuild-backend="hatchling.build"\n')
            (directory / 'flake.nix').write_text('''{
              inputs.sdk.url = ''' + json.dumps('path:' + snapshot) + ''';
              inputs.target.url = ''' + json.dumps('path:' + str(args.target_nixpkgs.resolve())) + ''';
              ''' + ('inputs.sdk.inputs.purifier.url = ' + json.dumps('path:' + shared_snapshot) + ';' if shared_snapshot else '') + '''
              outputs = {self,sdk,target}: let
                p=sdk.inputs.nixpkgs.legacyPackages.''' + system + ''';
                old=import target {system="''' + system + '''";config.allowInsecure=true;};
                build=p.callPackage (sdk.outPath+"/nix/lib/build-uv-project.nix") {
                  purifier=import sdk.inputs.purifier {lib=p.lib;};
                };
                project=build {src=builtins.path {path=self;filter=path: type:
                  !(builtins.elem (baseNameOf path) [".venv" ".uv-purifier" "__pycache__"]);};
                  python=old.''' + args.python_attribute + ''';testCommand=["''' + name + '''"];};
              in {packages.''' + system + '''={default=project.packages."''' + name.replace('_', '-') + '''";
                tests=project.tests;};};
            }''')
            run('nix', 'flake', 'lock', 'path:' + str(directory))
            return directory

        active = 'native-lock'
        producer = project('epoch_provider', ['markupsafe==2.1.5'])
        run(cli, 'lock', '--project', producer)
        passed(active)
        active = 'producer-pure-build'
        package = Path(run('nix', 'build', 'path:' + str(producer) + '#default',
                           '--out-link', state / 'producer', '--print-out-paths'))
        assert run(package / 'bin/epoch_provider') == 'target=' + minor
        passed(active)
        consumer = project('epoch_consumer', ['epoch-provider>=99'])
        active = 'incoming-lock'
        original = (consumer / 'pyproject.toml').read_bytes()
        run(cli, 'lock', '--project', consumer, '--input', 'path:' + str(producer) + '#default')
        assert original == (consumer / 'pyproject.toml').read_bytes()
        passed(active)
        active = 'interactive-prefetch'
        locks = [(consumer / name).read_bytes() for name in ('uv.lock', 'uv-purifier.lock.json')]
        run(cli, 'prefetch', '--project', consumer)
        assert run(consumer / '.venv/bin/epoch_consumer') == 'target=' + minor
        run(consumer / '.venv/bin/python', '-I', '-c', 'import epoch_provider,markupsafe._speedups')
        assert locks == [(consumer / name).read_bytes() for name in ('uv.lock', 'uv-purifier.lock.json')]
        assert original == (consumer / 'pyproject.toml').read_bytes()
        passed(active)
        active = 'incoming-pure-build'
        composed = Path(run('nix', 'build', 'path:' + str(consumer) + '#default',
                            '--out-link', state / 'consumer', '--print-out-paths'))
        assert run(composed / 'bin/epoch_consumer') == 'target=' + minor
        passed(active)
        active = 'ordinary-consumer'
        repository = Path(run('nix', 'build', 'path:' + str(consumer) + '#default.projections.python',
                              '--out-link', state / 'projection', '--print-out-paths'))
        ordinary = state / 'ordinary'
        run(uv, 'venv', '--python', python, ordinary)
        run(uv, 'pip', 'install', '--python', ordinary / 'bin/python', '--no-index',
            '--find-links', repository, 'epoch-consumer')
        assert run(ordinary / 'bin/epoch_consumer') == 'target=' + minor
        run(ordinary / 'bin/python', '-I', '-c', 'import epoch_provider,markupsafe._speedups')
        passed(active)
    except Exception as error:
        report['results'][active] = {'status': 'failed', 'error': str(error)}
        save()
        raise


if __name__ == '__main__':
    main()
