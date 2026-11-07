#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A Python 3.14 purifier SDK must select wheels for its Python 3.12 project."""
import json
import os
from pathlib import Path
import subprocess
from source_snapshot import archive_source
import tempfile
root=Path(__file__).resolve().parent.parent
system=subprocess.check_output(['nix','eval','--impure','--raw','--expr','builtins.currentSystem'],text=True).strip()
_, snapshot = archive_source(lambda *command: subprocess.check_output(command, text=True).strip(), root)
with tempfile.TemporaryDirectory(prefix='uv-target-python-') as temporary:
    temporary=Path(temporary)
    subprocess.run(['nix-store','--add-root',str(temporary/'source'),'--realise',snapshot],check=True)
    cli=Path(subprocess.check_output(['nix','build',str(root)+'#default','--out-link',str(temporary/'cli'),'--print-out-paths'],text=True).strip())/'bin/uv-purifier'
    env=dict(os.environ,UV_COMPOSITION_FLAKE=snapshot,UV_PYTHON_DOWNLOADS='never')
    expression='''let f=builtins.getFlake ("path:"+builtins.getEnv "UV_COMPOSITION_FLAKE");
    p=f.inputs.nixpkgs.legacyPackages.${builtins.currentSystem};
    in p.runCommand "uv-python312-composition-tools" {} ''
      mkdir -p "$out/bin"
      ln -s ${p.uv}/bin/uv "$out/bin/uv"
      ln -s ${p.python312.interpreter} "$out/bin/python"
    '' '''
    tools=Path(subprocess.check_output(['nix','build','--impure','--expr',expression,'--out-link',str(temporary/'tools'),'--print-out-paths'],env=env,text=True).strip())
    env['PATH']=str(tools/'bin')+':'+env['PATH']
    project=temporary/'project'
    project.mkdir()
    definition='''[project]
name = "target-python"
version = "1.0"
requires-python = ">=3.12"
dependencies = ["simple>=99"]
'''
    (project/'pyproject.toml').write_text(definition)
    def run(*args,**kwargs):
        return subprocess.run([str(arg) for arg in args],cwd=project,env=env,check=True,**kwargs)
    run(tools/'bin/uv','venv','--python',tools/'bin/python','.venv')
    run(cli,'lock','--input','path:'+snapshot+'#checks.'+system+'.composition-producer-python312')
    lockfile=project/'uv-purifier.lock.json'
    before=lockfile.read_bytes(),(project/'uv.lock').read_bytes()
    run(cli,'prefetch')
    result=run(project/'.venv/bin/python','-I','-c','import sys,markupsafe._speedups;assert sys.version_info[:2]==(3,12);print(markupsafe._speedups.__file__)',capture_output=True,text=True)
    assert result.stdout.strip().endswith('.so'),result.stdout
    assert before==(lockfile.read_bytes(),(project/'uv.lock').read_bytes())
    assert (project/'pyproject.toml').read_text()==definition
    print('Python 3.12 compiled Nix input selected and installed by the Python 3.14 SDK')
