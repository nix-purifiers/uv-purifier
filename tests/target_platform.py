# SPDX-License-Identifier: MIT
"""Controlled wheel admission against a real selected historical interpreter."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import zipfile


def check(run, state, snapshot, target_expression, python, helper_uv, system, shared, env):
    directory = state / 'platform-admission'
    directory.mkdir()
    facts = json.loads(run(python, '-I', '-c',
        'import json,os,platform,sys,sysconfig;print(json.dumps(dict('
        'libc=os.confstr("CS_GNU_LIBC_VERSION").split()[1],'
        'freeThreaded=bool(sysconfig.get_config_var("Py_GIL_DISABLED")),'
        'arch=platform.machine(),minor="%d%d" % sys.version_info[:2])))'))
    libc_minor = int(facts['libc'].split('.')[1])
    api = runpy.run_path(str(Path(snapshot) / 'src/uv_purifier.py'))
    fixture = directory / 'project'
    fixture.mkdir()
    (fixture / 'pyproject.toml').write_text('[project]\nname="probe-root"\nversion="1.0"\ndependencies=["target-probe==1.0"]\n')
    run(helper_uv, 'venv', '--python', python, directory / 'venv')
    observations = {}
    abi = 'cp' + facts['minor'] + ('t' if facts['freeThreaded'] else '')
    other_abi = 'cp' + facts['minor'] + ('' if facts['freeThreaded'] else 't')
    for compatible, minimum, candidate_abi in (
        (False, libc_minor + 1, abi), (True, libc_minor, abi),
        (False, libc_minor, other_abi), (not facts['freeThreaded'], libc_minor, 'abi3'),
    ):
        tag = f'cp{facts["minor"]}-{candidate_abi}-manylinux_2_{minimum}_{facts["arch"]}'
        wheelhouse = directory / tag
        wheelhouse.mkdir()
        wheel = wheelhouse / f'target_probe-1.0-{tag}.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('target_probe.py', 'answer=42\n')
            archive.writestr('target_probe-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: target-probe\nVersion: 1.0\n')
            archive.writestr('target_probe-1.0.dist-info/WHEEL', f'Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {tag}\n')
            archive.writestr('target_probe-1.0.dist-info/RECORD', '')
        native = subprocess.run([str(helper_uv), '--no-config', 'pip', 'install', '--reinstall', '--offline', '--no-index',
                                 '--python', str(directory / 'venv/bin/python'), '--find-links', str(wheelhouse), 'target-probe==1.0'],
                                env=env, text=True, capture_output=True)
        assert (native.returncode == 0) == compatible, native.stderr
        if not compatible:
            assert 'No solution found' in native.stderr, native.stderr
        else:
            assert run(directory / 'venv/bin/python', '-I', '-c', 'import target_probe;print(target_probe.answer)') == '42'
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        (fixture / 'uv.lock').write_text('version=1\nrequires-python=">=3.8"\n'
            '[[package]]\nname="probe-root"\nversion="1.0"\nsource={virtual="."}\n'
            'dependencies=[{name="target-probe"}]\n[[package]]\nname="target-probe"\nversion="1.0"\n'
            'source={registry="https://example.invalid/simple"}\nwheels=[{url="https://example.invalid/' +
            wheel.name + '",hash="sha256:' + digest + '"}]\n')
        lock = dict(version=1, inputsHash=api['inputs_hash'](str(fixture), ['.']), artifacts=[],
                    buildRequirements=[], builds={}, sources={}, outputs={})
        lock['contentHash'] = api['content_hash'](lock)
        (fixture / 'uv-purifier.lock.json').write_text(json.dumps(lock))
        prefix = ('let f=builtins.getFlake ' + json.dumps('path:' + snapshot) +
                  '; p=f.inputs.nixpkgs.legacyPackages.' + system + '; t=(' + target_expression + '); ')
        for kind, expression in {
            'package': prefix + 'b=p.callPackage (f.outPath+"/pkgs/build-uv-project/default.nix") '
                '{purifier=import ' + shared + ' {inherit (p) lib;};}; in '
                '(b {src=' + str(fixture) + ';python=t;}).interactive.drvPath',
            'cli': prefix + 'in (import (f.outPath+"/pkgs/cli/cli.nix") {'
                'nixpkgs=f.inputs.nixpkgs.outPath;purifier=' + shared + ';src=' + json.dumps(str(fixture)) + ';'
                'pythonVersion=t.pythonVersion;pythonInterpreter=t.interpreter;pythonGlibcVersion=' +
                json.dumps(facts['libc']) + ';pythonFreeThreaded=' + json.dumps(facts['freeThreaded']) + ';}).drvPath',
        }.items():
            result = subprocess.run(['nix', 'eval', '--impure', '--raw', '--expr', expression], env=env,
                                    text=True, capture_output=True)
            assert (result.returncode == 0) == compatible, result.stderr or result.stdout
            if not compatible:
                assert 'no wheel of target-probe==1.0 installs' in result.stderr, result.stderr
            observations[f'{tag}:{kind}'] = {'admitted': compatible, 'nativeAdmitted': native.returncode == 0}
    return {'target': facts, 'observations': observations,
            'scope': 'wheel platform and ABI tag admission; payload is pure Python, no compiled ABI credit'}
