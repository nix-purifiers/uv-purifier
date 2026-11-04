#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Native locking, editable development and ordinary downstreams with Nix wheels."""
import argparse
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--state-root', type=Path, help='contain test scratch and caches in this directory')
args = parser.parse_args()
if args.state_root:
    state = args.state_root.resolve()
    for name in ('tmp', 'cache', 'state'):
        (state / name).mkdir(parents=True, exist_ok=True)
    os.environ.update(TMPDIR=str(state / 'tmp'), XDG_CACHE_HOME=str(state / 'cache'),
                      XDG_STATE_HOME=str(state / 'state'), PYTHONDONTWRITEBYTECODE='1')

root = Path(__file__).resolve().parent.parent
system = subprocess.check_output(['nix', 'eval', '--impure', '--raw', '--expr',
                                  'builtins.currentSystem'], text=True).strip()
snapshot = json.loads(subprocess.check_output(['nix', 'flake', 'metadata', '--json',
    '--no-write-lock-file', 'path:' + str(root)], text=True))['path']
with tempfile.TemporaryDirectory(prefix='uv-native-composition-') as temporary:
    temporary = Path(temporary)
    subprocess.run(['nix-store', '--add-root', str(temporary / 'source'), '--realise', snapshot], check=True)
    cli = Path(subprocess.check_output(['nix', 'build', str(root) + '#default', '--print-out-paths',
        '--out-link', str(temporary / 'cli')], text=True).strip()) / 'bin/uv-purifier'
    expression = """let
      f = builtins.getFlake ("path:" + builtins.getEnv "UV_COMPOSITION_FLAKE");
      p = f.inputs.nixpkgs.legacyPackages.${builtins.currentSystem};
    in p.runCommand "uv-composition-native-tools" {} ''
      mkdir -p "$out/bin"
      ln -s ${p.uv}/bin/uv "$out/bin/uv"
      ln -s ${p.python3.interpreter} "$out/bin/python"
    '' """
    tools = Path(subprocess.check_output(['nix', 'build', '--impure', '--print-out-paths',
        '--out-link', str(temporary / 'tools'), '--expr', expression], text=True,
        env=dict(os.environ, UV_COMPOSITION_FLAKE=snapshot)).strip())
    os.environ['UV_PYTHON'] = str(tools / 'bin/python')
    project = temporary / 'project'
    source = project / 'src/composed_app/__init__.py'
    source.parent.mkdir(parents=True)
    source.write_text('import simple\ndef main():\n    simple.main()\n')
    definition = project / 'pyproject.toml'
    definition.write_text('''[project]
name = "composed-app"
version = "1.0"
requires-python = ">=3.12"
dependencies = ["simple>=99"]
[project.scripts]
composed-app = "composed_app:main"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
''')
    subprocess.run([str(tools / 'bin/uv'), 'venv', '--python', str(tools / 'bin/python'),
                    str(project / '.venv')], check=True)
    original = definition.read_bytes()
    reference = 'path:' + snapshot + '#checks.' + system + '.composition-producer'
    subprocess.run([str(cli), 'lock', '--project', str(project), '--input', reference], check=True)
    lockfile = project / 'uv-purifier.lock.json'
    lock = json.loads(lockfile.read_text())
    assert len(lock['projectInputs']) == 1
    assert not any(entry['path'].startswith(('simple-', 'attrs-', 'docopt-', 'markupsafe-'))
                   for entry in lock['artifacts']), 'Nix-provided wheels leaked into the download lock'
    before = lockfile.read_bytes(), (project / 'uv.lock').read_bytes()
    subprocess.run([str(cli), 'lock', '--project', str(project)], check=True)
    assert before == (lockfile.read_bytes(), (project / 'uv.lock').read_bytes())
    # A lock made on another machine can name a repository absent here.
    # The source pin supplies the real files; stored paths are only bindings.
    module = importlib.util.spec_from_file_location('uv_purifier', root / 'src/uv_purifier.py')
    uvp = importlib.util.module_from_spec(module)
    module.loader.exec_module(uvp)
    old = lock['projectInputs'][0]['nativeRepository']
    missing = '/nix/store/' + '0' * 32 + '-unavailable-python-projection'
    assert not Path(missing).exists()
    native_lock = project / 'uv.lock'
    selected_version = next(package['version'] for package in
                            tomllib.loads(native_lock.read_text())['package'] if package['name'] == 'simple')
    foreign_version = '0.0.0+nix.foreign.catalog'
    native_lock.write_text(native_lock.read_text().replace(old, missing).replace(selected_version, foreign_version))
    lock = json.loads(lockfile.read_text())
    lock['projectInputs'][0]['nativeRepository'] = missing
    lock['inputsHash'] = uvp.inputs_hash(str(project), ['.'])
    lock['contentHash'] = uvp.content_hash(lock)
    lockfile.write_text(json.dumps(lock, indent=2) + '\n')
    bound = lockfile.read_bytes(), native_lock.read_bytes()
    subprocess.run([str(cli), 'prefetch', '--project', str(project)], check=True)
    assert bound == (lockfile.read_bytes(), native_lock.read_bytes()), 'prefetch changed committed locks'
    subprocess.run([str(project / '.venv/bin/python'), '-c',
        'from importlib.metadata import version; assert version("simple") == ' + repr(selected_version)], check=True)

    output = subprocess.check_output([str(tools / 'bin/uv'), 'run', '--project', str(project),
        '--no-sync', 'composed-app'], text=True)
    assert 'simple attrs=25.3.0 docopt=0.6.2 markupsafe=3.0.3 &lt;nix&gt;' in output, output
    assert definition.read_bytes() == original
    expression = """let
      f = builtins.getFlake ("path:" + builtins.getEnv "UV_COMPOSITION_FLAKE");
      p = import f.inputs.nixpkgs {};
      build = p.callPackage (f.outPath + "/nix/lib/build-uv-project.nix") {
        purifier = import f.inputs.purifier { lib = p.lib; };
      };
      source = builtins.path {
        path = /. + builtins.getEnv "UV_COMPOSITION_PROJECT";
        name = "source";
        filter = path: type: !(builtins.elem (baseNameOf path)
          [ ".git" ".venv" ".uv-purifier" "__pycache__" "dist" "uv.lock" ]);
      };
      project = build {
        src = source;
        uvLock = /. + builtins.getEnv "UV_COMPOSITION_NATIVE_LOCK";
        testCommand = [ "composed-app" ];
      };
    in [ project.tests project.packages.composed-app.projections.python ]"""
    built = json.loads(subprocess.check_output(['nix', 'build', '--impure', '--json',
        '--out-link', str(temporary / 'pure'), '--expr', expression], text=True,
        env=dict(os.environ, UV_COMPOSITION_FLAKE=snapshot, UV_COMPOSITION_PROJECT=str(project),
                 UV_COMPOSITION_NATIVE_LOCK=str(native_lock))))
    repository = Path(next(result['outputs']['out'] for result in built
                           if result['outputs']['out'].endswith('-python-projection')))
    closure = subprocess.check_output(['nix-store', '--query', '--requisites', str(repository)], text=True).splitlines()
    assert any(path.endswith('-composed-app-1.0') for path in closure), 'project runtime was not retained'
    assert any(path.endswith('-simple-0.1.0') for path in closure), 'provided Nix producer was not retained'
    downstream = temporary / 'downstream'
    subprocess.run([str(tools / 'bin/uv'), 'venv', '--python', str(tools / 'bin/python'), str(downstream)], check=True)
    subprocess.run([str(tools / 'bin/uv'), 'pip', 'install', '--python', str(downstream / 'bin/python'),
        '--no-index', '--find-links', str(repository), 'composed-app'], check=True)
    output = subprocess.check_output([str(downstream / 'bin/composed-app')], text=True)
    assert 'simple attrs=25.3.0 docopt=0.6.2 markupsafe=3.0.3 &lt;nix&gt;' in output, output
    subprocess.run([str(downstream / 'bin/python'), '-c',
        'from importlib.metadata import version; assert "+nix." in version("composed-app"); '
        'assert "+nix." in version("simple")'], check=True)
    subprocess.run([str(cli), 'publish', '--project', str(project), '--package', 'composed-app',
                    '--', '--dry-run'], check=True)
    assert definition.read_bytes() == original
    print('UV NATIVE, PURE AND ORDINARY DOWNSTREAM COMPOSITION PASSED', flush=True)
