#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Evolve a real uv project across registry/Nix origins and outgoing releases.

Needs Nix and networked capture, like composition-native.py. Every state is
built in the Nix sandbox and its projection consumed with ordinary native uv.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib


def run(*args, env=None):
    return subprocess.check_output([str(arg) for arg in args], text=True, env=env).strip()


root = Path(__file__).resolve().parents[1]
system = run('nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem')
snapshot = json.loads(run('nix', 'flake', 'metadata', '--json', '--no-write-lock-file',
                          'path:' + str(root)))['path']
with tempfile.TemporaryDirectory(prefix='uv-lifecycle-') as directory:
    temporary = Path(directory)
    run('nix-store', '--add-root', temporary / 'source', '--realise', snapshot)
    cli = Path(run('nix', 'build', str(root) + '#default', '--print-out-paths',
                   '--out-link', temporary / 'cli')) / 'bin/uv-purifier'
    tools = Path(run('nix', 'build', '--impure', '--print-out-paths',
        '--out-link', temporary / 'tools', '--expr', '''let
          f = builtins.getFlake ("path:" + builtins.getEnv "UV_LIFECYCLE_FLAKE");
          p = f.inputs.nixpkgs.legacyPackages.${builtins.currentSystem};
        in p.runCommand "uv-lifecycle-tools" {} ''
          mkdir -p $out/bin
          ln -s ${p.uv}/bin/uv $out/bin/uv
          ln -s ${p.python3.interpreter} $out/bin/python
        '' ''', env=dict(os.environ, UV_LIFECYCLE_FLAKE=snapshot)))
    # Capture, development and the producer must share the fixture's ABI,
    # independently of the Python preinstalled by the CI runner.
    os.environ['UV_PYTHON'] = str(tools / 'bin/python')
    project = temporary / 'project'
    (project / 'src/lifecycle_app').mkdir(parents=True)
    source = project / 'src/lifecycle_app/__init__.py'
    definition = project / 'pyproject.toml'
    version = '25.2.0'
    extra_dependency = False
    extra_buildable = False

    def manifest():
        dependencies = ['attrs==' + version]
        if extra_dependency:
            dependencies.append('docopt==0.6.2')
        definition.write_text('''[project]
name = "lifecycle-app"
version = "1.0"
requires-python = ">=3.12"
dependencies = ''' + json.dumps(dependencies) + '''
[project.scripts]
lifecycle-app = "lifecycle_app:main"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
''' + ('\n[tool.uv.workspace]\nmembers = ["packages/*"]\n' if extra_buildable else ''))
        source.write_text('from importlib.metadata import version\n'
                          'def main():\n    print("attrs=" + version("attrs"))\n')

    def lock(*args):
        original = definition.read_bytes()
        run(cli, 'lock', '--project', project, *args)
        assert definition.read_bytes() == original, 'locking modified user manifest'

    def names():
        return {package['name'] for package in tomllib.loads((project / 'uv.lock').read_text())['package']}

    def prove(label, nix_origin=False):
        expression = '''let
          f = builtins.getFlake ("path:" + builtins.getEnv "UV_LIFECYCLE_FLAKE");
          p = import f.inputs.nixpkgs {};
          build = p.callPackage (f.outPath + "/nix/lib/build-uv-project.nix") {
            purifier = import f.inputs.purifier { lib = p.lib; };
          };
          project = build {
            src = builtins.path {
              path = /. + builtins.getEnv "UV_LIFECYCLE_PROJECT";
              filter = path: type: !(builtins.elem (baseNameOf path)
                [ ".git" ".venv" ".uv-purifier" "__pycache__" "dist" ]);
            };
            testCommand = [ "lifecycle-app" ];
          };
        in { packages = builtins.attrNames project.packages;
             outputs = [ project.tests ] ++ map (package: package.projections.python)
               (builtins.attrValues project.packages); }
        '''
        environment = dict(os.environ, UV_LIFECYCLE_FLAKE=snapshot,
                           UV_LIFECYCLE_PROJECT=str(project))
        packages = json.loads(run('nix', 'eval', '--impure', '--json', '--expr',
                                   '(' + expression + ').packages', env=environment))
        assert ('extra-app' in packages) == extra_buildable, packages
        builds = json.loads(run('nix', 'build', '--impure', '--json',
            '--out-link', temporary / ('pure-' + label), '--expr',
            '(' + expression + ').outputs', env=environment))
        repositories = [Path(result['outputs']['out']) for result in builds
                        if result['outputs']['out'].endswith('-python-projection')]
        repository = next(path for path in repositories if list(path.glob('lifecycle_app-*.whl')))
        native = tomllib.loads((project / 'uv.lock').read_text())
        attrs = next(package for package in native['package'] if package['name'] == 'attrs')
        assert ('path' in attrs['source']) == nix_origin, attrs
        if nix_origin:
            assert attrs['source']['path'].startswith('/nix/store/'), attrs
        # The editable developer environment must change along with the locks.
        run(cli, 'prefetch', '--project', project,
            env=dict(os.environ, XDG_CACHE_HOME=str(temporary / 'development-cache')))
        assert run(project / '.venv/bin/lifecycle-app') == 'attrs=' + version
        installed = run(project / '.venv/bin/python', '-c',
            'import importlib.util; print(importlib.util.find_spec("extra_app") is not None)')
        assert (installed == 'True') == extra_buildable, installed
        # Native downstream uses only the published projection and a new cache.
        downstream = temporary / ('downstream-' + label)
        native_env = dict(os.environ, UV_CACHE_DIR=str(temporary / ('cache-' + label)),
                          UV_OFFLINE='1', UV_NO_INDEX='1', UV_PYTHON_DOWNLOADS='never')
        run(tools / 'bin/uv', 'venv', '--python', tools / 'bin/python', downstream, env=native_env)
        links = [argument for path in repositories for argument in ('--find-links', str(path))]
        targets = ['lifecycle-app'] + (['extra-app'] if extra_buildable else [])
        run(tools / 'bin/uv', 'pip', 'install', '--python', downstream / 'bin/python',
            '--no-index', *links, *targets, env=native_env)
        extra = run(downstream / 'bin/python', '-c',
                    'import importlib.util; print(importlib.util.find_spec("extra_app") is not None)')
        assert (extra == 'True') == extra_buildable, extra
        if extra_buildable:
            assert run(downstream / 'bin/python', '-c', 'import extra_app; print(extra_app.VALUE)') == 'extra'
        selected = run(downstream / 'bin/python', '-c',
                       'from importlib.metadata import version; print(version("attrs"))')
        # Unmodified third-party wheels retain their original native version;
        # the native lock's source identity proves the selected Nix origin.
        assert selected == version, selected
        assert run(downstream / 'bin/lifecycle-app') == 'attrs=' + selected
        present = run(downstream / 'bin/python', '-c',
                      'import importlib.util; print(importlib.util.find_spec("docopt") is not None)')
        assert (present == 'True') == extra_dependency, 'outgoing dependency graph differs from manifest'
        print('PASS uv.lifecycle.' + label, flush=True)

    manifest()
    lock()
    prove('registry-initial')
    version = '25.3.0'
    manifest()
    run(cli, 'update', '--project', project, 'attrs')
    prove('dependency-bump')
    reference = 'path:' + snapshot + '#checks.' + system + '.composition-producer'
    lock('--input', reference)
    assert json.loads((project / 'uv-purifier.lock.json').read_text())['projectInputs']
    prove('registry-to-nix', nix_origin=True)
    # Relocking preserves the supplied origin and semantic selections.
    before = (project / 'uv.lock').read_bytes()
    lock()
    assert (project / 'uv.lock').read_bytes() == before
    lock('--no-inputs')
    result = json.loads((project / 'uv-purifier.lock.json').read_text())
    assert not result.get('projectInputs'), 'removed Nix origin retained in purifier lock'
    native = tomllib.loads((project / 'uv.lock').read_text())
    assert not native.get('manifest', {}).get('overrides'), 'Nix overrides survived reversal'
    assert reference not in (project / 'uv-purifier.lock.json').read_text()
    assert not any((project / '.uv-purifier/project-inputs').iterdir()), 'removed producer roots retained'
    prove('nix-to-registry')
    extra_dependency = True
    manifest()
    lock()
    assert 'docopt' in names()
    prove('add-dependency')
    extra_dependency = False
    manifest()
    lock()
    assert 'docopt' not in names()
    prove('drop-dependency')
    member = project / 'packages/extra-app'
    (member / 'src/extra_app').mkdir(parents=True)
    (member / 'src/extra_app/__init__.py').write_text('VALUE = "extra"\n')
    (member / 'pyproject.toml').write_text('''[project]
name = "extra-app"
version = "1.0"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
''')
    extra_buildable = True
    manifest()
    lock()
    prove('add-buildable')
    extra_buildable = False
    manifest()
    lock()
    assert 'extra-app' not in names(), 'dropped workspace member retained in native lock'
    prove('drop-buildable')
