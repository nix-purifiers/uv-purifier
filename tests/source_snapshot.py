# SPDX-License-Identifier: MIT
"""Materialize a Git-aware, lock-preserving source before using its store path."""
import json
from pathlib import Path


def archive_source(run, source):
    reference = str(Path(source).resolve())
    archived = json.loads(run('nix', 'flake', 'archive', '--json', '--no-update-lock-file', reference))
    metadata = json.loads(run('nix', 'flake', 'metadata', '--json', '--no-update-lock-file', reference))
    snapshot = Path(archived['path'])
    assert str(snapshot) == metadata['path'], 'source changed while being archived'
    assert snapshot.is_dir() and snapshot.resolve() == snapshot, 'source is not physically materialized'
    assert not (snapshot / '.git').exists(), 'snapshot contains Git administrative state'
    assert run('nix', 'hash', 'path', snapshot) == metadata['locked']['narHash'], 'source NAR hash mismatch'
    return metadata, str(snapshot)
