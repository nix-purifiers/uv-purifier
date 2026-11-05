#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise an independently packaged historical Python with current uv/helpers."""
import argparse
import hashlib
from email.parser import Parser
import json
import os
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-nixpkgs', type=Path, required=True)
    parser.add_argument('--python-attribute', default='python39')
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--shared-library', type=Path, help='explicit shared-library candidate; recorded separately')
    parser.add_argument('--native-uv-epoch', help='explicit package fixture from tests/sdk, independent of helper and Python')
    parser.add_argument('--compiled-dependency-version', default='2.1.5', help='explicit MarkupSafe fixture version; recorded in the receipt')
    parser.add_argument('--sandwiches', action='store_true', help='exercise an ordinary republisher between purifier users and its own conflicting constraint')
    parser.add_argument('--multiple-inputs', action='store_true', help='compose two Nix inputs with a shared transitive leaf, then reject a competing provider')
    args = parser.parse_args()
    if not re.fullmatch(r'python[A-Za-z0-9_]+', args.python_attribute):
        parser.error('expected an explicit Python package attribute')
    sdk = Path(__file__).resolve().parents[1]
    if args.native_uv_epoch and (Path(args.native_uv_epoch).name != args.native_uv_epoch or
                                not (sdk / 'tests/sdk' / (args.native_uv_epoch + '.nix')).is_file()):
        parser.error('native uv epoch must name an existing tests/sdk package fixture')
    state = args.state_root.resolve()
    state.mkdir(parents=True, exist_ok=False)
    for directory in ('tmp', 'cache', 'config', 'data', 'state'):
        (state / directory).mkdir()
    env = dict(os.environ, TMPDIR=str(state / 'tmp'), XDG_CACHE_HOME=str(state / 'cache'),
               XDG_CONFIG_HOME=str(state / 'config'), XDG_DATA_HOME=str(state / 'data'),
               XDG_STATE_HOME=str(state / 'state'), UV_PYTHON_DOWNLOADS='never',
               UV_CACHE_DIR=str(state / 'cache/uv'))
    env.pop('UV_PROJECT_ENVIRONMENT', None)
    report = {'version': 1, 'scope': 'exact Python target/helper tuple; no inferred intervals',
              'fixtureParameters': {'markupsafe': args.compiled_dependency_version, 'sandwiches': args.sandwiches,
                                    'multipleInputs': args.multiple_inputs},
              'cacheProfile': 'cold-native-cache',
              'driverDigest': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'targetNixpkgs': str(args.target_nixpkgs.resolve()),
              'targetAttribute': args.python_attribute,
              'results': {name: {'status': 'not-run'} for name in
                          ('target-runtime', 'target-platform-admission', 'native-lock', 'producer-pure-build',
                           'incoming-lock', 'interactive-prefetch', 'incoming-pure-build',
                           'publication-dry-run', 'ordinary-consumer',
                           'dependency-add-with-nix', 'dependency-drop-with-nix')}}
    active = 'target-runtime'
    extra_dependency = False
    if args.sandwiches:
        report['results'].update({name: {'status': 'not-run'} for name in
            ('ordinary-republisher', 'ordinary-own-eviction-conflict', 'nix-ordinary-nix-sandwich',
             'ordinary-nix-ordinary-sandwich')})
    if args.multiple_inputs:
        report['results'].update({name: {'status': 'not-run'} for name in
            ('multiple-inputs-shared-leaf', 'multiple-inputs-reversed', 'competing-input-preserves-state')})

    def save():
        report['fixtureLockDigests'] = {
            str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
            for name in ('epoch_provider', 'epoch_consumer', 'ordinary_middle',
                         'ordinary-eviction-control', 'epoch_final', 'epoch_other',
                         'epoch_multi', 'epoch_multi_reverse', 'competing-other')
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

    ordinary_caches = {}

    def ordinary_cache(label):
        if label not in ordinary_caches:
            cache = state / 'ordinary-caches' / label
            assert not cache.exists(), cache
            ordinary_caches[label] = cache
            report.setdefault('ordinaryCacheProfiles', {})[label] = {
                'initialState': 'absent', 'path': str(cache),
            }
        return ordinary_caches[label]

    def ordinary_uv(label, executable, *arguments, cwd=sdk):
        cache = ordinary_cache(label)
        return run(executable, '--cache-dir', cache, *arguments, cwd=cwd,
                   environment=dict(env, UV_CACHE_DIR=str(cache)))

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
            report['sharedLibrary'] = {'path': shared_snapshot, 'narHash': shared['locked']['narHash'],
                                       'selection': 'explicit-candidate'}
        else:
            default_shared = run('nix', 'eval', '--impure', '--raw', '--expr',
                'let f=builtins.getFlake ' + json.dumps('path:' + snapshot) + '; in f.inputs.purifier.outPath')
            report['sharedLibrary'] = {'path': default_shared, 'narHash': run('nix', 'hash', 'path', default_shared),
                                       'selection': 'default-input'}
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
        target_free_threaded = run(python, '-I', '-c', 'import sysconfig;print(bool(sysconfig.get_config_var("Py_GIL_DISABLED")))') == 'True'
        report['targetPython']['freeThreaded'] = target_free_threaded
        minor = '.'.join(version.split('.')[:2])
        next_minor = str(int(version.split('.')[1]) + 1)
        override = ['--override-input', 'purifier', 'path:' + shared_snapshot] if shared_snapshot else []
        package_prefix = 'let f=builtins.getFlake ' + json.dumps('path:' + snapshot) + '; p=f.inputs.nixpkgs.legacyPackages.' + system + '; in '
        if args.native_uv_epoch:
            native_expression = 'p.callPackage (f.outPath + ' + json.dumps('/tests/sdk/' + args.native_uv_epoch + '.nix') + ') {}'
            library_expression = 'builtins.storePath ' + json.dumps(shared_snapshot) if shared_snapshot else 'f.inputs.purifier.outPath'
            cli = Path(run('nix', 'build', '--impure', '--expr', package_prefix +
                           'p.callPackage (f.outPath + "/pkgs/cli") {nativeUv=' + native_expression + '; purifierSource=' + library_expression + ';}',
                           '--out-link', state / 'cli', '--print-out-paths')) / 'bin/uv-purifier'
        else:
            native_expression = 'p.uv'
            cli = Path(run('nix', 'build', str(sdk) + '#default', '--out-link', state / 'cli', '--print-out-paths', *override)) / 'bin/uv-purifier'
        uv = Path(run('nix', 'build', '--impure', '--expr', package_prefix + native_expression,
                      '--out-link', state / 'uv', '--print-out-paths')) / 'bin/uv'
        env['UV_PYTHON'] = str(python)
        report.update(nativeManager=run(uv, '--version'), nativeManagerPackage=str(uv.parent.parent),
                      helperPackage=str(cli.parent.parent))
        helper_uv = Path(run('nix', 'build', '--impure', '--expr', package_prefix + 'p.uv',
                             '--out-link', state / 'helper-uv', '--print-out-paths')) / 'bin/uv'
        report.update(helperManager=run(helper_uv, '--version'), helperManagerPackage=str(helper_uv.parent.parent))
        passed(active)

        active = 'target-platform-admission'
        import runpy
        check_target_platform = runpy.run_path(str(Path(snapshot) / 'tests/target_platform.py'))['check']
        library_path = shared_snapshot or run('nix', 'eval', '--impure', '--raw', '--expr',
            'let f=builtins.getFlake ' + json.dumps('path:' + snapshot) + '; in f.inputs.purifier.outPath')
        report['targetPlatformAdmission'] = check_target_platform(
            run, state, snapshot, target_expression, python, helper_uv, system, library_path, env)
        report['targetPlatformDriverDigest'] = hashlib.sha256(
            (Path(snapshot) / 'tests/target_platform.py').read_bytes()).hexdigest()
        passed(active)

        def native_environment(directory, consumer=False, published=False, extra_packages=(), creator_version=None):
            assert (run(directory / 'bin/python', '-I', '-c',
                'import sysconfig;print(bool(sysconfig.get_config_var("Py_GIL_DISABLED")))') == 'True') == target_free_threaded
            if target_free_threaded:
                assert run(directory / 'bin/python', '-I', '-c',
                    'import sys,markupsafe._speedups;print(sys._is_gil_enabled())') == 'False'
            configuration = dict(line.split(' = ', 1) for line in (directory / 'pyvenv.cfg').read_text().splitlines() if ' = ' in line)
            assert configuration['uv'] == (creator_version or report['nativeManager'].split()[1]), configuration
            site = Path(run(directory / 'bin/python', '-E', '-s', '-c', 'import sysconfig;print(sysconfig.get_path("purelib"))'))
            records = [Parser().parsestr(path.read_text()) for path in site.glob('*.dist-info/METADATA')]
            graph = {record['Name'].lower().replace('_', '-'): record['Version'] for record in records}
            expected = {'epoch-provider', 'markupsafe'} | ({'epoch-consumer'} if consumer else set())
            expected.update(extra_packages)
            if consumer and extra_dependency:
                expected.add('attrs')
            assert set(graph) == expected and len(records) == len(graph), graph
            assert graph['markupsafe'] == args.compiled_dependency_version, graph
            if consumer and extra_dependency:
                assert graph['attrs'] == '23.2.0', graph
            assert ('+nix.' in graph['epoch-provider']) == consumer, graph
            if consumer:
                assert ('+nix.' in graph['epoch-consumer']) == published, graph
            requirements = {record['Name'].lower().replace('_', '-'): record.get_all('Requires-Dist', []) for record in records}
            if consumer:
                # This controlled fixture declares no extras or conditional attrs
                # requirement, so its metadata edge must follow the manifest edit.
                edges = {re.match(r'[A-Za-z0-9][A-Za-z0-9._-]*', value).group().lower().replace('_', '-')
                         for value in requirements['epoch-consumer']}
                assert ('attrs' in edges) == extra_dependency, requirements
            report.setdefault('selectedRuntimeGraphs', {})[active + ':' + str(directory)] = {
                'packages': graph, 'requirements': requirements,
                'nativeManager': configuration['uv'],
            }
            return graph

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
                  ''' + ('nativeUv=p.callPackage (sdk.outPath + ' + json.dumps('/tests/sdk/' + args.native_uv_epoch + '.nix') + ') {};' if args.native_uv_epoch else '') + '''
                };
                project=build {src=builtins.path {path=self;filter=path: type:
                  !(builtins.elem (baseNameOf path) [".venv" ".uv-purifier" "__pycache__"]);};
                  python=old.''' + args.python_attribute + ''';
                  testCommand=["''' + name + '''"];};
              in {packages.''' + system + '''={default=project.packages."''' + name.replace('_', '-') + '''";
                tests=project.tests;};};
            }''')
            run('nix', 'flake', 'lock', 'path:' + str(directory))
            return directory

        active = 'native-lock'
        producer = project('epoch_provider', ['markupsafe==' + args.compiled_dependency_version])
        run(uv, 'lock', '--no-python-downloads', cwd=producer)
        native_bytes = (producer / 'uv.lock').read_bytes()
        run(cli, 'lock', '--project', producer)
        assert native_bytes == (producer / 'uv.lock').read_bytes(), 'helper changed the selected native lock'
        report['nativeLockHeader'] = (producer / 'uv.lock').read_text().split('[[package]]', 1)[0]
        passed(active)
        active = 'producer-pure-build'
        package = Path(run('nix', 'build', 'path:' + str(producer) + '#default',
                           '--out-link', state / 'producer', '--print-out-paths'))
        assert run(package / 'bin/epoch_provider') == 'target=' + minor
        native_environment(package)
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
        native_environment(consumer / '.venv', consumer=True)
        run(consumer / '.venv/bin/python', '-I', '-c', 'import epoch_provider,markupsafe._speedups')
        assert locks == [(consumer / name).read_bytes() for name in ('uv.lock', 'uv-purifier.lock.json')]
        assert original == (consumer / 'pyproject.toml').read_bytes()
        passed(active)
        active = 'incoming-pure-build'
        composed = Path(run('nix', 'build', 'path:' + str(consumer) + '#default',
                            '--out-link', state / 'consumer', '--print-out-paths'))
        assert run(composed / 'bin/epoch_consumer') == 'target=' + minor
        native_environment(composed, consumer=True)
        passed(active)
        active = 'publication-dry-run'
        run(cli, 'publish', '--project', consumer, '--package', 'epoch-consumer', '--', '--dry-run')
        passed(active)
        active = 'ordinary-consumer'
        repository = Path(run('nix', 'build', 'path:' + str(consumer) + '#default.projections.python',
                              '--out-link', state / 'projection', '--print-out-paths'))
        ordinary = state / 'ordinary'
        ordinary_uv('baseline', uv, 'venv', '--python', python, ordinary)
        ordinary_uv('baseline', uv, 'pip', 'install', '--offline', '--python', ordinary / 'bin/python', '--no-index',
            '--find-links', repository, 'epoch-consumer')
        assert run(ordinary / 'bin/epoch_consumer') == 'target=' + minor
        run(ordinary / 'bin/python', '-I', '-c', 'import epoch_provider,markupsafe._speedups')
        original_ordinary_graph = native_environment(ordinary, consumer=True, published=True)
        passed(active)
        if args.sandwiches:
            active = 'ordinary-republisher'
            report['ordinaryRepublisherManager'] = {'version': report['helperManager'],
                'package': report['helperManagerPackage'], 'role': 'ordinary project lock/build/install'}
            ordinary_creator = report['helperManager'].split()[1]
            middle = state / 'ordinary_middle'
            (middle / 'src/ordinary_middle').mkdir(parents=True)
            (middle / 'src/ordinary_middle/__init__.py').write_text('import epoch_consumer\n')
            published_version = run(ordinary / 'bin/python', '-I', '-c',
                'from importlib.metadata import version;print(version("epoch-consumer"))')
            middle_definition = ('[project]\nname="ordinary-middle"\nversion="1.0"\nrequires-python=' +
                json.dumps('>=' + minor + ',<3.' + next_minor) + '\ndependencies=' +
                json.dumps(['epoch-consumer==' + published_version]) +
                '\n[build-system]\nrequires=["hatchling==1.18.0"]\nbuild-backend="hatchling.build"\n')
            (middle / 'pyproject.toml').write_text(middle_definition)
            ordinary_uv('republisher', helper_uv, 'lock', '--offline', '--no-index', '--find-links', repository, '--python', python, cwd=middle)
            middle_lock = (middle / 'uv.lock').read_bytes()
            ordinary_uv('republisher', helper_uv, 'venv', '--python', python, middle / '.venv')
            ordinary_uv('republisher', helper_uv, 'pip', 'install', '--offline', '--no-index', '--python', middle / '.venv/bin/python',
                '--find-links', repository, 'epoch-consumer==' + published_version)
            native_environment(middle / '.venv', consumer=True, published=True, creator_version=ordinary_creator)
            ordinary_uv('republisher', helper_uv, 'build', '--wheel', '--python', python, '--out-dir', middle / 'dist', cwd=middle)
            middle_wheel, = (middle / 'dist').glob('*.whl')
            report['ordinaryPublication'] = {'wheel': middle_wheel.name,
                'sha256': hashlib.sha256(middle_wheel.read_bytes()).hexdigest(),
                'upstreamVersion': published_version}
            assert (middle / 'pyproject.toml').read_text() == middle_definition
            assert (middle / 'uv.lock').read_bytes() == middle_lock
            assert not (middle / 'uv-purifier.lock.json').exists()
            assert not (middle / 'flake.nix').exists()
            passed(active)

            active = 'ordinary-own-eviction-conflict'
            # Make the requested alternative genuinely resolvable on its own;
            # otherwise an empty candidate set would mask missing transitive pins.
            alternatives = state / 'ordinary-alternative-wheels'
            alternatives.mkdir()
            import zipfile
            with zipfile.ZipFile(alternatives / 'markupsafe-99.0-py3-none-any.whl', 'w') as wheel:
                wheel.writestr('markupsafe/__init__.py', 'fixture_origin="ordinary-alternative"\n')
                wheel.writestr('markupsafe-99.0.dist-info/METADATA',
                    'Metadata-Version: 2.1\nName: MarkupSafe\nVersion: 99.0\n')
                wheel.writestr('markupsafe-99.0.dist-info/WHEEL',
                    'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                wheel.writestr('markupsafe-99.0.dist-info/RECORD', '')
            control = state / 'ordinary-eviction-control'
            control.mkdir()
            (control / 'pyproject.toml').write_text(
                '[project]\nname="alternative-control"\nversion="1.0"\ndependencies=["markupsafe==99.0"]\n')
            ordinary_uv('eviction-control', helper_uv, 'lock', '--offline', '--no-index', '--find-links', alternatives,
                '--python', python, cwd=control)
            assert 'version = "99.0"' in (control / 'uv.lock').read_text()
            report['ordinaryAlternative'] = {'version': '99.0',
                'scope': 'synthetic pure wheel, positively resolved only as a constraint-conflict control'}
            conflicting_definition = middle_definition.replace(
                'dependencies=' + json.dumps(['epoch-consumer==' + published_version]),
                'dependencies=' + json.dumps(['epoch-consumer==' + published_version,
                                             'markupsafe==99.0']))
            (middle / 'pyproject.toml').write_text(conflicting_definition)
            conflict = subprocess.run([str(helper_uv), '--cache-dir', str(ordinary_cache('republisher')),
                'lock', '--offline', '--no-index', '--find-links',
                str(repository), '--find-links', str(alternatives), '--python', str(python)],
                cwd=middle, env=dict(env, UV_CACHE_DIR=str(ordinary_cache('republisher'))), text=True, capture_output=True)
            assert conflict.returncode and 'No solution found' in conflict.stderr, conflict.stderr
            assert args.compiled_dependency_version in conflict.stderr and '99.0' in conflict.stderr, conflict.stderr
            assert (middle / 'uv.lock').read_bytes() == middle_lock, 'failed eviction rewrote ordinary lock'
            native_environment(middle / '.venv', consumer=True, published=True, creator_version=ordinary_creator)
            (middle / 'pyproject.toml').write_text(middle_definition)
            report['ordinaryEvictionDiagnostic'] = conflict.stderr
            passed(active)

            active = 'nix-ordinary-nix-sandwich'
            final = project('epoch_final', ['ordinary-middle==1.0'])
            (final / 'vendor').mkdir()
            import shutil
            shutil.copyfile(middle_wheel, final / 'vendor' / middle_wheel.name)
            with (final / 'pyproject.toml').open('a') as definition:
                definition.write('\n[tool.uv.sources]\nordinary-middle={path=' +
                                 json.dumps('vendor/' + middle_wheel.name) + '}\n')
            final_manifest = (final / 'pyproject.toml').read_bytes()
            run(cli, 'lock', '--project', final, '--input', 'path:' + str(consumer) + '#default')
            final_locks = [(final / name).read_bytes() for name in ('uv.lock', 'uv-purifier.lock.json')]
            run(cli, 'prefetch', '--project', final)
            assert (final / 'pyproject.toml').read_bytes() == final_manifest
            assert final_locks == [(final / name).read_bytes() for name in ('uv.lock', 'uv-purifier.lock.json')]
            native_environment(final / '.venv', consumer=True, published=True,
                               extra_packages=('ordinary-middle', 'epoch-final'))
            final_package = Path(run('nix', 'build', 'path:' + str(final) + '#default',
                '--out-link', state / 'sandwich-final', '--print-out-paths'))
            final_graph = native_environment(final_package, consumer=True, published=True,
                                             extra_packages=('ordinary-middle', 'epoch-final'))
            assert final_graph['ordinary-middle'] == final_graph['epoch-final'] == '1.0', final_graph
            assert run(final_package / 'bin/epoch_final') == 'target=' + minor
            run(final_package / 'bin/python', '-I', '-c', 'import ordinary_middle,epoch_consumer,epoch_provider')
            passed(active)
            active = 'ordinary-nix-ordinary-sandwich'
            middle_wheel_digest = hashlib.sha256(middle_wheel.read_bytes()).hexdigest()
            final_repository = Path(run('nix', 'build', 'path:' + str(final) + '#default.projections.python',
                '--out-link', state / 'sandwich-final-projection', '--print-out-paths'))
            ordinary_final = state / 'ordinary-final'
            ordinary_uv('sandwich-final', uv, 'venv', '--python', python, ordinary_final)
            ordinary_uv('sandwich-final', uv, 'pip', 'install', '--offline', '--no-index', '--python', ordinary_final / 'bin/python',
                '--find-links', final_repository, 'epoch-final')
            final_ordinary_graph = native_environment(ordinary_final, consumer=True, published=True,
                extra_packages=('ordinary-middle', 'epoch-final'))
            for name in ('ordinary-middle', 'epoch-final'):
                assert '+nix.' in final_ordinary_graph[name], final_ordinary_graph
            for name in original_ordinary_graph:
                assert final_ordinary_graph[name] == original_ordinary_graph[name], (name, final_ordinary_graph)
            run(ordinary_final / 'bin/python', '-I', '-c', 'import ordinary_middle,epoch_final,epoch_consumer,epoch_provider')
            assert middle_wheel_digest == hashlib.sha256(middle_wheel.read_bytes()).hexdigest()
            assert (middle / 'uv.lock').read_bytes() == middle_lock
            assert (middle / 'pyproject.toml').read_text() == middle_definition
            native_environment(ordinary, consumer=True, published=True)
            passed(active)
        if args.multiple_inputs:
            active = 'multiple-inputs-shared-leaf'
            other = project('epoch_other', ['markupsafe==' + args.compiled_dependency_version])
            run(cli, 'lock', '--project', other)
            input_uris = ['path:' + str(consumer) + '#default', 'path:' + str(other) + '#default']
            graphs = []
            for name, uris, label in (
                ('epoch_multi', input_uris, 'multiple-inputs-shared-leaf'),
                ('epoch_multi_reverse', list(reversed(input_uris)), 'multiple-inputs-reversed'),
            ):
                active = label
                multi = project(name, ['epoch-consumer>=99', 'epoch-other>=99'])
                definition = (multi / 'pyproject.toml').read_bytes()
                run(cli, 'lock', '--project', multi, '--input', uris[0], '--input', uris[1])
                locked = [(multi / file).read_bytes() for file in ('uv.lock', 'uv-purifier.lock.json')]
                input_records = json.loads(locked[1])['projectInputs']
                assert len(input_records) == 2, input_records
                report.setdefault('multipleInputRecords', {})[label] = input_records
                run(cli, 'prefetch', '--project', multi)
                assert definition == (multi / 'pyproject.toml').read_bytes()
                assert locked == [(multi / file).read_bytes() for file in ('uv.lock', 'uv-purifier.lock.json')]
                graph = native_environment(multi / '.venv', consumer=True, published=True,
                    extra_packages=('epoch-other', name.replace('_', '-')))
                assert '+nix.' in graph['epoch-other'], graph
                provided = {key: graph[key] for key in ('epoch-provider', 'epoch-consumer', 'epoch-other', 'markupsafe')}
                graphs.append(provided)
                pure = Path(run('nix', 'build', 'path:' + str(multi) + '#default',
                    '--out-link', state / label, '--print-out-paths'))
                pure_graph = native_environment(pure, consumer=True, published=True,
                    extra_packages=('epoch-other', name.replace('_', '-')))
                assert graph == pure_graph
                run(pure / 'bin/python', '-I', '-c', 'import epoch_consumer,epoch_provider,epoch_other')
                passed(active)
            assert graphs[0] == graphs[1], graphs

            active = 'competing-input-preserves-state'
            import shutil
            competing = state / 'competing-other'
            shutil.copytree(other, competing, ignore=shutil.ignore_patterns('.venv', '.uv-purifier', '__pycache__'))
            with (competing / 'src/epoch_other/__init__.py').open('a') as changed:
                changed.write('\nfixture_variant="competing-nix-provider"\n')
            before = [(multi / file).read_bytes() for file in ('pyproject.toml', 'uv.lock', 'uv-purifier.lock.json')]
            # Explicit --input arguments describe the complete requested set;
            # keep both existing providers while adding the competing one.
            conflict = subprocess.run([str(cli), 'lock', '--project', str(multi),
                '--input', input_uris[0], '--input', input_uris[1], '--input',
                'path:' + str(competing) + '#default'], env=env, text=True, capture_output=True)
            assert conflict.returncode and 'conflicting Nix providers' in conflict.stderr, conflict.stderr
            assert before == [(multi / file).read_bytes() for file in ('pyproject.toml', 'uv.lock', 'uv-purifier.lock.json')]
            after = native_environment(multi / '.venv', consumer=True, published=True,
                extra_packages=('epoch-other', 'epoch-multi-reverse'))
            assert {key: after[key] for key in graphs[1]} == graphs[1]
            report['competingInputDiagnostic'] = conflict.stderr
            passed(active)
        supplied = json.loads((consumer / 'uv-purifier.lock.json').read_text())['projectInputs']
        for label, extra_dependency in [('dependency-add-with-nix', True), ('dependency-drop-with-nix', False)]:
            active = label
            definition = original.decode().replace(
                'dependencies=["epoch-provider>=99"]',
                'dependencies=["epoch-provider>=99", "attrs==23.2.0"]') if extra_dependency else original.decode()
            (consumer / 'pyproject.toml').write_text(definition)
            run(cli, 'lock', '--project', consumer)
            assert json.loads((consumer / 'uv-purifier.lock.json').read_text())['projectInputs'] == supplied
            phase_locks = [(consumer / name).read_bytes() for name in ('uv.lock', 'uv-purifier.lock.json')]
            run(cli, 'prefetch', '--project', consumer)
            assert definition == (consumer / 'pyproject.toml').read_text()
            assert phase_locks == [(consumer / name).read_bytes() for name in ('uv.lock', 'uv-purifier.lock.json')]
            native_environment(consumer / '.venv', consumer=True)
            phase_package = Path(run('nix', 'build', 'path:' + str(consumer) + '#default',
                                     '--out-link', state / (label + '-pure'), '--print-out-paths'))
            assert run(phase_package / 'bin/epoch_consumer') == 'target=' + minor
            native_environment(phase_package, consumer=True)
            phase_projection = Path(run('nix', 'build', 'path:' + str(consumer) + '#default.projections.python',
                                        '--out-link', state / (label + '-projection'), '--print-out-paths'))
            phase_downstream = state / (label + '-ordinary')
            ordinary_uv(label, uv, 'venv', '--python', python, phase_downstream)
            ordinary_uv(label, uv, 'pip', 'install', '--offline', '--no-index', '--python', phase_downstream / 'bin/python',
                '--find-links', phase_projection, 'epoch-consumer')
            native_environment(phase_downstream, consumer=True, published=True)
            assert run(phase_downstream / 'bin/epoch_consumer') == 'target=' + minor
            passed(active)
    except Exception as error:
        report['results'][active] = {'status': 'failed', 'error': str(error)}
        save()
        raise


if __name__ == '__main__':
    main()
