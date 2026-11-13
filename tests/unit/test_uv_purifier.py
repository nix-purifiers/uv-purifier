# SPDX-License-Identifier: MIT
"""Unit tests of the lock tool (src/uv_purifier.py) and of the requirements
split (pkgs/build-uv-project/split_requirements.py). They need no network.

Run from the repository root: python3 -m unittest discover -s tests/unit
"""

import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


uvp = load("uv_purifier", "src/uv_purifier.py")
split = load("split_requirements", "pkgs/build-uv-project/split_requirements.py")


class DynamicBuildHooks(unittest.TestCase):
    def test_uv_backend_pins_keep_different_builds_separate(self):
        captured = {
            'builds': {'first==1': 0, 'second==1': 1},
            'buildRequirements': [
                {'packages': [{'name': 'uv-build', 'version': '0.12.24'}]},
                {'packages': [{'name': 'uv-build', 'version': '0.13.5'}]},
            ],
        }
        self.assertEqual(uvp.uv_build_pin(captured, 'first==1'), 'uv-build==0.12.24\n')
        self.assertEqual(uvp.uv_build_pin(captured, 'second==1'), 'uv-build==0.13.5\n')
        self.assertEqual(uvp.uv_build_pin(captured), '')
        self.assertEqual(uvp.uv_build_pin(captured, 'other==1'), '')
        captured['buildRequirements'].pop()
        self.assertEqual(uvp.uv_build_pin(captured), 'uv-build==0.12.24\n')

    def test_editable_classification_includes_directory_workspace_members(self):
        for kind, name, expected in [
            ('directory', 'member', True),
            ('directory', 'dependency', False),
            ('editable', 'dependency', True),
            ('git', 'member', False),
            ('virtual', 'member', False),
        ]:
            with self.subTest(kind=kind, name=name):
                self.assertEqual(uvp.editable_source({
                    'name': name, 'source': {kind: '.'},
                }, {'member'}), expected)

    def test_wheel_dependency_does_not_probe_editable_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = pathlib.Path(directory)
            (tree / 'backend.py').write_text('''
def get_requires_for_build_wheel(config):
    return ['fixture_dependency']
def get_requires_for_build_editable(config):
    import fixture_dependency
    return ['editable_dependency']
''')

            def install(args, **kwargs):
                if args[0] == 'venv':
                    binary = pathlib.Path(args[-1]) / 'bin'
                    binary.mkdir()
                    (binary / 'python').symlink_to(sys.executable)
                return subprocess.CompletedProcess(args, 0)

            with mock.patch.object(uvp, 'run_uv', side_effect=install), \
                    mock.patch.object(uvp, 'log') as log:
                requirements = uvp.dynamic_requires(str(tree), {'build-system': {
                    'build-backend': 'backend', 'backend-path': ['.'],
                }}, ['static_dependency'], directory)
            self.assertEqual(requirements, ['fixture_dependency'])
            log.assert_not_called()

    def test_editable_source_captures_editable_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = pathlib.Path(directory)
            (tree / 'backend.py').write_text('''
def get_requires_for_build_wheel(config):
    return ['wheel_dependency']
def get_requires_for_build_editable(config):
    return ['editable_dependency']
''')

            def install(args, **kwargs):
                if args[0] == 'venv':
                    binary = pathlib.Path(args[-1]) / 'bin'
                    binary.mkdir()
                    (binary / 'python').symlink_to(sys.executable)
                return subprocess.CompletedProcess(args, 0)

            with mock.patch.object(uvp, 'run_uv', side_effect=install):
                requirements = uvp.dynamic_requires(str(tree), {'build-system': {
                    'build-backend': 'backend', 'backend-path': ['.'],
                }}, ['static_dependency'], directory, editable=True)
            self.assertEqual(requirements, ['wheel_dependency', 'editable_dependency'])

    def test_wheel_hook_failure_without_repair_requirements_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = pathlib.Path(directory)
            (tree / 'backend.py').write_text('''
def get_requires_for_build_wheel(config):
    raise RuntimeError('broken wheel hook')
''')

            def install(args, **kwargs):
                if args[0] == 'venv':
                    binary = pathlib.Path(args[-1]) / 'bin'
                    binary.mkdir()
                    (binary / 'python').symlink_to(sys.executable)
                return subprocess.CompletedProcess(args, 0)

            with mock.patch.object(uvp, 'run_uv', side_effect=install):
                with self.assertRaisesRegex(uvp.Error, 'broken wheel hook'):
                    uvp.dynamic_requires(str(tree), {'build-system': {
                        'build-backend': 'backend', 'backend-path': ['.'],
                    }}, ['static_dependency'], directory)


class ScratchProjectVCS(unittest.TestCase):
    def test_scratch_build_retains_tagged_version_without_copying_build_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / 'source'
            root.mkdir()
            (root / 'pyproject.toml').write_text('[project]\nname="dynamic"\ndynamic=["version"]\n')
            subprocess.run(['git', 'init', '-q', root], check=True)
            subprocess.run(['git', '-C', root, 'add', 'pyproject.toml'], check=True)
            subprocess.run(['git', '-C', root, '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.invalid',
                            'commit', '-qm', 'fixture'], check=True)
            subprocess.run(['git', '-C', root, 'tag', 'v2.3.4'], check=True)
            (root / '.venv').mkdir()
            scratch = pathlib.Path(uvp.scratch_project(str(root), {'package': []}, directory))
            version = subprocess.check_output(['git', '-C', scratch, 'describe', '--tags'], text=True).strip()
            self.assertEqual(version, 'v2.3.4')
            self.assertFalse((scratch / '.venv').exists())
            (scratch / 'pyproject.toml').write_text('build writes stay in scratch')
            self.assertIn('dynamic', (root / 'pyproject.toml').read_text())

    def test_archive_without_repository_remains_buildable_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / 'source'
            root.mkdir()
            (root / 'pyproject.toml').write_text('[project]\nname="static"\nversion="1"\n')
            with mock.patch.object(uvp.subprocess, 'run', return_value=mock.Mock(returncode=128)):
                scratch = pathlib.Path(uvp.scratch_project(str(root), {'package': []}, directory))
            self.assertFalse((scratch / '.git').exists())
            self.assertEqual((scratch / 'pyproject.toml').read_bytes(), (root / 'pyproject.toml').read_bytes())


class ProjectPython(unittest.TestCase):
    def test_direct_sources_and_editables_respect_markers_without_losing_hashes(self):
        lines = ['# frozen export', 'attrs==25.3.0 \\', '    --hash=sha256:abc',
                 'windows-only @ https://example.org/package.whl ; sys_platform == "win32"',
                 '-e ../windows-source ; sys_platform == "win32"',
                 'file:///local/source ; python_version < "3.13"',
                 'linux-only @ git+https://example.org/repo ; sys_platform == "linux"']
        active = '\n'.join(line for block in uvp.active_requirement_blocks(lines,
                           {'sys_platform': 'linux', 'python_version': '3.12'}) for line in block)
        self.assertIn('--hash=sha256:abc', active)
        self.assertNotIn('windows-only', active)
        self.assertNotIn('windows-source', active)
        self.assertIn('file:///local/source', active)
        self.assertIn('linux-only @ git+', active)

    def test_frozen_registry_selection_uses_target_markers_and_requested_groups(self):
        text = ('requests==2.32.5 \\\n    --hash=sha256:abc\n'
                'pywin32==311 ; sys_platform == "win32"\n'
                'typing-extensions==4.15.0 ; python_version < "3.13"\n')
        def exported(arguments, cwd, env):
            pathlib.Path(arguments[arguments.index('--output-file') + 1]).write_text(text)
            return mock.Mock(returncode=0)
        for system, version, expected in (
            ('linux', '3.12', ['requests==2.32.5', 'typing-extensions==4.15.0']),
            ('win32', '3.12', ['pywin32==311', 'requests==2.32.5', 'typing-extensions==4.15.0']),
            ('linux', '3.14', ['requests==2.32.5']),
        ):
            with self.subTest(system=system, version=version), \
                    mock.patch.object(uvp, 'run_helper_uv', side_effect=exported) as export:
                actual = uvp.active_registry_packages('/project',
                    {'executable': '/target/python', 'markers': {'sys_platform': system, 'python_version': version}}, ['--group', 'test'])
                self.assertEqual(actual, expected)
                self.assertIn('--frozen', export.call_args.args[0])
                self.assertIn('--group', export.call_args.args[0])

    def test_resolved_project_runtime_is_pinned_and_caller_environment_restored(self):
        for previous in (None, '3.8'):
            environment = {} if previous is None else {'UV_PYTHON': previous}
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(uvp, 'project_python', return_value='/opt/user-sdk/bin/python'):
                with self.assertRaisesRegex(RuntimeError, 'backend failed'):
                    with uvp.project_runtime('/project'):
                        self.assertEqual(os.environ['UV_PYTHON'], '/opt/user-sdk/bin/python')
                        raise RuntimeError('backend failed')
                self.assertEqual(os.environ.get('UV_PYTHON'), previous)

    def test_native_resolver_and_helper_frontend_are_selected_independently(self):
        with mock.patch.dict(os.environ, {'UV_PURIFIER_HELPER_UV': '/helper/uv'}), mock.patch.object(
            uvp.subprocess, 'run', return_value=mock.Mock(returncode=0)
        ) as command:
            uvp.run_uv(['lock'], cwd='/project')
            self.assertEqual(command.call_args.args[0], ['uv', 'lock'])
            uvp.run_helper_uv(['build'], cwd='/project')
            self.assertEqual(command.call_args.args[0], ['/helper/uv', 'build'])

    def test_nix_build_retains_selected_historical_store_interpreter(self):
        for executable, free_threaded in (
            ('/nix/store/target-python/bin/python3.8', False),
            ('/nix/store/target-python/bin/python3.14t', True),
        ):
            with self.subTest(executable=executable), mock.patch.dict(os.environ, {
                'UV_PURIFIER_NIX_LIB': '/library', 'UV_PURIFIER_NIXPKGS': '/nixpkgs',
                'UV_PURIFIER_LIBRARY': '/purifier',
            }), mock.patch.object(uvp, 'project_python', return_value='/project/.venv/bin/python'), mock.patch.object(
                uvp.subprocess, 'check_output', return_value=json.dumps({'version': '3.14' if free_threaded else '3.8', 'executable': executable, 'glibc': '2.38', 'freeThreaded': free_threaded})
            ), mock.patch.object(uvp.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='/nix/store/result\n')) as build:
                self.assertEqual(uvp.nix_build('/project', '/project/uv-purifier.lock.json'), '/nix/store/result')
                command = build.call_args.args[0]
                self.assertEqual('pythonInterpreter' in command, executable.startswith('/nix/store/'))
                self.assertEqual('pythonGlibcVersion' in command, executable.startswith('/nix/store/'))
                if 'pythonInterpreter' in command:
                    self.assertEqual(command[command.index('pythonInterpreter') + 1], executable)
                    self.assertEqual(command[command.index('pythonGlibcVersion') + 1], '2.38')
                    self.assertEqual(command[command.index('pythonFreeThreaded') + 1], 'true' if free_threaded else 'false')

    def test_local_inputs_preserve_a_host_sdk_and_actual_tags(self):
        identity = {'version': '3.8', 'executable': '/opt/user-sdk/bin/python',
                    'freeThreaded': False, 'glibc': '2.28'}
        with mock.patch.dict(os.environ, {'UV_PURIFIER_NIX_LIB': '/library',
                'UV_PURIFIER_NIXPKGS': '/nixpkgs', 'UV_PURIFIER_LIBRARY': '/purifier'}), \
                mock.patch.object(uvp, 'target_wheel_tags', return_value=['cp38-cp38-manylinux_2_28_x86_64']), \
                mock.patch.object(uvp.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='/nix/store/inputs\n')) as build:
            self.assertEqual(uvp.nix_build('/project', '/project/uv-purifier.lock.json',
                             local=True, selected_python=identity, active_packages=[]), '/nix/store/inputs')
            command = build.call_args.args[0]
            self.assertEqual(command[command.index('pythonInterpreter') + 1], identity['executable'])
            self.assertEqual(command[command.index('localInputs') + 1], 'true')
            self.assertIn('pythonActivePackages', command)
            self.assertIn('cp38-cp38-manylinux_2_28_x86_64', command[command.index('pythonWheelTags') + 1])
            with self.assertRaisesRegex(uvp.Error, 'sandboxed release'):
                uvp.nix_build('/project', '/project/uv-purifier.lock.json', package='project', selected_python=identity)

    def test_explicit_target_is_passed_to_python_find(self):
        with mock.patch.dict(os.environ, {"UV_PYTHON": "/fixture/python"}), mock.patch.object(
            uvp, "run_uv", return_value=mock.Mock(returncode=0, stdout="/fixture/python\n")
        ) as run:
            self.assertEqual(uvp.project_python("/project"), "/fixture/python")
            run.assert_called_once_with(
                ["python", "find", "--no-python-downloads", "/fixture/python"], cwd="/project"
            )


class FileNames(unittest.TestCase):
    def test_last_segment_of_the_path(self):
        self.assertEqual(
            uvp.file_name("https://files.pythonhosted.org/packages/77/06/ab/attrs-25.3.0-py3-none-any.whl"),
            "attrs-25.3.0-py3-none-any.whl",
        )

    def test_percent_escapes_are_decoded(self):
        self.assertEqual(
            uvp.file_name("https://download.pytorch.org/whl/torch-2.5.0%2Bcpu-cp312-cp312-linux_x86_64.whl"),
            "torch-2.5.0+cpu-cp312-cp312-linux_x86_64.whl",
        )

    def test_query_and_fragment_are_not_part_of_the_name(self):
        self.assertEqual(uvp.file_name("https://example.org/pkg-1.0.tar.gz?x=1#sha256=ab"), "pkg-1.0.tar.gz")

    def test_entry_name_of_a_local_file(self):
        self.assertEqual(uvp.entry_name({"path": "vendor/pkg-1.0-py3-none-any.whl"}), "pkg-1.0-py3-none-any.whl")
        self.assertEqual(uvp.entry_name({"filename": "pkg-1.0.tar.gz"}), "pkg-1.0.tar.gz")
        with self.assertRaises(uvp.Error):
            uvp.entry_name({"hash": "sha256:00"})




class Hashes(unittest.TestCase):
    def test_sri_of_a_uv_hash(self):
        empty = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.assertEqual(uvp.sri(empty), "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=")

    def test_other_algorithms_are_refused(self):
        with self.assertRaises(uvp.Error):
            uvp.sri("md5:d41d8cd98f00b204e9800998ecf8427e")

    def test_content_hash_ignores_key_order_and_itself(self):
        a = {"version": 1, "builds": {"b==1": 0, "a==1": 1}}
        b = {"builds": {"a==1": 1, "b==1": 0}, "version": 1, "contentHash": "x"}
        self.assertEqual(uvp.content_hash(a), uvp.content_hash(b))

    def test_content_hash_is_over_compact_sorted_json(self):
        # builtins.toJSON writes {"a":[1,null,true],"b":"é"} for the same value.
        value = {"b": "é", "a": [1, None, True]}
        import hashlib

        expected = hashlib.sha256('{"a":[1,null,true],"b":"é"}'.encode()).hexdigest()
        self.assertEqual(uvp.content_hash(value), expected)

    def test_inputs_hash_covers_uv_lock_and_tree_pyprojects(self):
        with tempfile.TemporaryDirectory() as root:
            pathlib.Path(root, "uv.lock").write_text("lock")
            pathlib.Path(root, "pyproject.toml").write_text("root")
            pathlib.Path(root, "pkg").mkdir()
            pathlib.Path(root, "pkg", "pyproject.toml").write_text("pkg")
            before = uvp.inputs_hash(root, [".", "pkg", "../outside"])
            pathlib.Path(root, "pkg", "pyproject.toml").write_text("changed")
            self.assertNotEqual(before, uvp.inputs_hash(root, [".", "pkg", "../outside"]))


class Wheels(unittest.TestCase):
    def test_pure_wheels(self):
        self.assertTrue(uvp.is_pure_wheel("six-1.17.0-py2.py3-none-any.whl"))
        self.assertTrue(uvp.is_pure_wheel("pkg-1.0-1-py3-none-any.whl"))
        self.assertFalse(uvp.is_pure_wheel("markupsafe-3.0.3-cp314-cp314-manylinux_2_17_x86_64.whl"))
        self.assertFalse(uvp.is_pure_wheel("cryptography-46.0.3-cp311-abi3-manylinux_2_34_x86_64.whl"))

    def test_may_need_build(self):
        sdist = {"url": "https://x/pkg-1.0.tar.gz", "hash": "sha256:00"}
        pure = {"url": "https://x/pkg-1.0-py3-none-any.whl", "hash": "sha256:00"}
        binary = {"url": "https://x/pkg-1.0-cp314-cp314-manylinux_2_17_x86_64.whl", "hash": "sha256:00"}
        self.assertFalse(uvp.may_need_build({"sdist": sdist, "wheels": [pure]}))
        self.assertTrue(uvp.may_need_build({"sdist": sdist, "wheels": [binary]}))
        self.assertTrue(uvp.may_need_build({"sdist": sdist}))
        self.assertFalse(uvp.may_need_build({"wheels": [binary]}))


class Sources(unittest.TestCase):
    def test_git_source_with_tag_and_subdirectory(self):
        repository, commit, subdirectory = uvp.parse_git_source(
            "https://github.com/org/repo?tag=v1&subdirectory=python%2Fpkg#0123456789abcdef"
        )
        self.assertEqual(repository, "https://github.com/org/repo")
        self.assertEqual(commit, "0123456789abcdef")
        self.assertEqual(subdirectory, "python/pkg")

    def test_git_source_without_a_commit_is_refused(self):
        with self.assertRaises(uvp.Error):
            uvp.parse_git_source("https://github.com/org/repo?branch=main")

    def test_source_kinds(self):
        for kind in uvp.SOURCE_KINDS:
            self.assertEqual(uvp.source_kind({"name": "p", "source": {kind: "v"}}), (kind, "v"))
        with self.assertRaises(uvp.Error):
            uvp.source_kind({"name": "p", "source": {"svn": "v"}})

    def test_workspace_members_from_the_manifest(self):
        lock = {
            "manifest": {"members": ["app", "lib"]},
            "package": [
                {"name": "app", "source": {"editable": "packages/app"}},
                {"name": "dep", "source": {"editable": "../dep"}},
            ],
        }
        self.assertEqual(uvp.workspace_members(lock), {"app", "lib"})

    def test_a_single_project_is_its_own_member(self):
        lock = {
            "package": [
                {"name": "app", "source": {"editable": "."}},
                {"name": "dep", "source": {"directory": "vendor/dep"}},
            ]
        }
        self.assertEqual(uvp.workspace_members(lock), {"app"})

    def test_paths_inside_and_outside_the_project(self):
        self.assertTrue(uvp.inside_project("vendor/dep"))
        self.assertTrue(uvp.inside_project("."))
        self.assertFalse(uvp.inside_project(".."))
        self.assertFalse(uvp.inside_project("../shared"))
        self.assertFalse(uvp.inside_project("/abs/dep"))


class BuildRequirements(unittest.TestCase):
    def test_default_and_declared(self):
        self.assertEqual(uvp.build_requires({}), ["setuptools>=40.8.0"])
        self.assertEqual(uvp.build_requires({"build-system": {"build-backend": "x"}}), ["setuptools>=40.8.0"])
        self.assertEqual(uvp.build_requires({"build-system": {"requires": ["hatchling"]}}), ["hatchling"])

    def write_tar(self, path, top):
        data = b'[build-system]\nrequires = ["flit_core"]\n'
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo(f"{top}/pyproject.toml")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    def test_sdist_pyproject_under_any_top_directory(self):
        with tempfile.TemporaryDirectory() as d:
            archive = os.path.join(d, "1.17.0.tar.gz")
            self.write_tar(archive, "six-1.17.0")
            self.assertEqual(uvp.build_requires(uvp.sdist_pyproject(archive)), ["flit_core"])
            tree = uvp.unpack(archive, d)
            self.assertEqual(os.path.basename(tree), "six-1.17.0")

    def test_zip_sdist(self):
        with tempfile.TemporaryDirectory() as d:
            archive = os.path.join(d, "pkg-1.0.zip")
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("pkg-1.0/pyproject.toml", '[build-system]\nrequires = ["hatchling"]\n')
            self.assertEqual(uvp.build_requires(uvp.sdist_pyproject(archive)), ["hatchling"])

    def test_sdist_without_pyproject(self):
        with tempfile.TemporaryDirectory() as d:
            archive = os.path.join(d, "old-1.0.tar.gz")
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("old-1.0/setup.py")
                tar.addfile(info, io.BytesIO(b""))
            self.assertEqual(uvp.build_requires(uvp.sdist_pyproject(archive)), ["setuptools>=40.8.0"])

    def test_previous_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, uvp.LOCK_FILE)
            self.assertIsNone(uvp.previous_resolution(path))
            pathlib.Path(path).write_text('{"resolvedAt": "2026-11-01T00:00:00Z"}')
            self.assertEqual(uvp.previous_resolution(path), "2026-11-01T00:00:00Z")
            pathlib.Path(path).write_text("{not json")
            self.assertIsNone(uvp.previous_resolution(path))


class DerivedOutputs(unittest.TestCase):
    """derived_outputs, which the Nix library computes the same way; the
    lockfile records only what differs from it."""

    def test_names_and_scripts_from_uv_lock_and_pyproject(self):
        derived = uvp.derived_outputs(
            {"name": "Zope.Interface-x", "version": "1.0"},
            {"project": {"scripts": {"b": "m:b", "a": "m:a"}, "gui-scripts": {"c": "m:c"}}},
        )
        self.assertEqual(derived["sdist"], "zope_interface_x-1.0.tar.gz")
        self.assertEqual(derived["wheel"], "zope_interface_x-1.0-py3-none-any.whl")
        self.assertEqual(derived["scripts"], ["a", "b", "c"])

    def test_dynamic_fields_are_not_derived(self):
        derived = uvp.derived_outputs(
            {"name": "app"},
            {"project": {"dynamic": ["version", "scripts"]}},
        )
        self.assertEqual(derived, {"sdist": None, "wheel": None, "scripts": None})


class CheckLockfile(unittest.TestCase):
    """check_lockfile, which `sync` and `publish` run before a Nix build, on
    copies of tests/fixtures/simple."""

    def copy(self, d):
        import shutil

        project = os.path.join(d, "simple")
        # copyfile keeps no mode, so a copy of a read-only store path is
        # writable.
        shutil.copytree(ROOT / "tests" / "fixtures" / "simple", project, copy_function=shutil.copyfile)
        for directory, _, _ in os.walk(project):
            os.chmod(directory, 0o755)
        return project

    def test_a_current_lockfile_passes(self):
        with tempfile.TemporaryDirectory() as d:
            project = self.copy(d)
            uvp.check_lockfile(project, os.path.join(project, uvp.LOCK_FILE))

    def test_a_lockfile_outside_the_project(self):
        with tempfile.TemporaryDirectory() as d:
            project = self.copy(d)
            outside = os.path.join(d, "kept-elsewhere.json")
            os.rename(os.path.join(project, uvp.LOCK_FILE), outside)
            uvp.check_lockfile(project, outside)

    def assert_fails(self, project, text):
        with self.assertRaises(uvp.Error) as raised:
            uvp.check_lockfile(project, os.path.join(project, uvp.LOCK_FILE))
        self.assertIn(text, str(raised.exception))

    def test_a_changed_uv_lock(self):
        with tempfile.TemporaryDirectory() as d:
            project = self.copy(d)
            with open(os.path.join(project, "uv.lock"), "a") as f:
                f.write("# changed\n")
            self.assert_fails(project, "Run `uv-purifier lock`, which also runs `uv lock` when pyproject.toml changed")

    def test_an_edited_lockfile(self):
        with tempfile.TemporaryDirectory() as d:
            project = self.copy(d)
            path = os.path.join(project, uvp.LOCK_FILE)
            lock = json.loads(pathlib.Path(path).read_text())
            lock["builds"] = {}
            pathlib.Path(path).write_text(json.dumps(lock))
            self.assert_fails(project, "was changed after `uv-purifier lock` wrote it")

    def test_a_malformed_lockfile(self):
        with tempfile.TemporaryDirectory() as d:
            project = self.copy(d)
            pathlib.Path(project, uvp.LOCK_FILE).write_text("{")
            self.assert_fails(project, "is not valid JSON")

    def test_a_missing_lockfile(self):
        with tempfile.TemporaryDirectory() as d:
            project = self.copy(d)
            os.remove(os.path.join(project, uvp.LOCK_FILE))
            self.assert_fails(project, "does not exist; run `uv-purifier lock`")


EXPORTED = """\
# This file was autogenerated by uv via the following command:
#    uv export --frozen
./vendor/localdir
    # via sources
-e ./vendor/editable
    # via sources
./vendor/localsdist-0.4.0.tar.gz \\
    --hash=sha256:b8e0
    # via sources
attrs @ git+https://github.com/python-attrs/attrs@3dca08ce
    # via sources
six @ https://github.com/benjaminp/six/archive/refs/tags/1.17.0.tar.gz \\
    --hash=sha256:32f3
    # via sources
markupsafe==3.0.3 \\
    --hash=sha256:aaaa \\
    --hash=sha256:bbbb
    # via sources
typing-extensions==4.15.0 ; python_full_version < '3.13' \\
    --hash=sha256:cccc
"""


class ToolLockfile(unittest.TestCase):
    """A uv.lock kept outside the project, through --tool-lockfile."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.dir.name, "project")
        os.makedirs(self.root)
        self.in_tree = os.path.join(self.root, "uv.lock")
        self.outside = os.path.join(self.dir.name, "packaging", "uv.lock")
        pathlib.Path(self.in_tree).write_text("project\n")

    def tearDown(self):
        self.dir.cleanup()

    def test_a_write_starts_from_the_project_and_leaves_it(self):
        with uvp.tool_lockfile(self.root, self.outside, writes=True):
            self.assertEqual(pathlib.Path(self.in_tree).read_text(), "project\n")
            pathlib.Path(self.in_tree).write_text("locked\n")
        self.assertEqual(pathlib.Path(self.outside).read_text(), "locked\n")
        self.assertEqual(pathlib.Path(self.in_tree).read_text(), "project\n")

    def test_a_read_uses_the_outside_copy(self):
        os.makedirs(os.path.dirname(self.outside))
        pathlib.Path(self.outside).write_text("outside\n")
        with uvp.tool_lockfile(self.root, self.outside, writes=False):
            self.assertEqual(pathlib.Path(self.in_tree).read_text(), "outside\n")
        self.assertEqual(pathlib.Path(self.in_tree).read_text(), "project\n")

    def test_a_failed_command_restores_the_project(self):
        with self.assertRaises(uvp.Error), uvp.tool_lockfile(self.root, self.outside, writes=True):
            pathlib.Path(self.in_tree).write_text("half written\n")
            raise uvp.Error("lock failed")
        self.assertEqual(pathlib.Path(self.in_tree).read_text(), "project\n")
        self.assertFalse(os.path.exists(self.outside))

    def test_a_read_needs_the_outside_copy(self):
        with (
            self.assertRaises(uvp.Error) as raised,
            uvp.tool_lockfile(self.root, self.outside, writes=False),
        ):
            pass
        self.assertIn("uv-purifier lock --tool-lockfile", str(raised.exception))


class SplitRequirements(unittest.TestCase):
    def run_split(self, direct, exported_text=EXPORTED):
        with tempfile.TemporaryDirectory() as d:
            exported = os.path.join(d, "exported.txt")
            index = os.path.join(d, "direct.json")
            requirements = os.path.join(d, "requirements.txt")
            wheels = os.path.join(d, "wheels.txt")
            pathlib.Path(exported).write_text(exported_text)
            pathlib.Path(index).write_text(json.dumps(direct))
            split.main(exported, index, requirements, wheels)
            return pathlib.Path(requirements).read_text(), pathlib.Path(wheels).read_text().split()

    def test_index_requirements_stay_and_the_others_become_wheels(self):
        kept, wheels = self.run_split(
            {
                "names": {"Attrs": "/w/attrs", "six": "/w/six"},
                "paths": {
                    "./vendor/localdir": "/w/localdir",
                    "vendor/editable": "/w/editable",
                    "./vendor/localsdist-0.4.0.tar.gz": "/w/localsdist",
                },
            }
        )
        self.assertIn("markupsafe==3.0.3 \\", kept)
        self.assertIn("    --hash=sha256:bbbb", kept)
        self.assertIn("typing-extensions==4.15.0 ; python_full_version < '3.13' \\", kept)
        self.assertNotIn("attrs @", kept)
        self.assertNotIn("./vendor", kept)
        self.assertEqual(wheels, ["/w/localdir", "/w/editable", "/w/localsdist", "/w/attrs", "/w/six"])

    def test_file_urls_select_supplied_wheels(self):
        kept, wheels = self.run_split(
            {"paths": {"/nix/store/wheels/library-1+nix.test.whl": "/w/library"}},
            "file:///nix/store/wheels/library-1%2Bnix.test.whl\n",
        )
        self.assertEqual(kept.strip(), "")
        self.assertEqual(wheels, ["/w/library"])

    def test_explicit_editable_source_needs_no_release_wheel(self):
        kept, wheels = self.run_split(
            {"editablePaths": ["../sdk-runtime"]},
            "-e ../sdk-runtime\nrequests==2.32.5 --hash=sha256:abc\n",
        )
        self.assertNotIn('sdk-runtime', kept)
        self.assertIn('requests==2.32.5', kept)
        self.assertEqual(wheels, [])

    def test_a_requirement_without_a_wheel_is_an_error(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_split({"names": {"attrs": "/w/attrs"}, "paths": {}})
        self.assertIn("six @", str(raised.exception))


if __name__ == "__main__":
    unittest.main()


class InputRemoval(unittest.TestCase):
    def test_cli_explicitly_clears_inputs_for_lock_and_update(self):
        for operation in ('lock', 'update'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as root:
                with mock.patch.object(uvp, operation) as invoke, \
                        mock.patch.object(uvp, 'project_python', return_value='/fixture/python'):
                    self.assertEqual(uvp.main([operation, '--project', root, '--no-inputs']), 0)
                    self.assertEqual(invoke.call_args.kwargs.get('inputs',
                        invoke.call_args.args[-1] if operation == 'update' else None), [])

    def test_failed_capture_keeps_producer_roots(self):
        with tempfile.TemporaryDirectory() as root:
            roots = pathlib.Path(root, '.uv-purifier/project-inputs')
            roots.mkdir(parents=True)
            link = roots / '0'
            link.symlink_to('/nix/store/producer')
            with mock.patch.object(uvp, 'previous_lock', return_value={'projectInputs': [{'reference': 'producer'}]}), \
                 mock.patch.object(uvp, 'native_lock', side_effect=uvp.Error('failed capture')):
                with self.assertRaises(uvp.Error):
                    uvp.lock(root, str(pathlib.Path(root, 'lock.json')), inputs=[])
            self.assertTrue(link.is_symlink())

    def test_successful_capture_releases_only_helper_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            roots = pathlib.Path(root, '.uv-purifier/project-inputs')
            roots.mkdir(parents=True)
            (roots / '0').symlink_to('/nix/store/producer')
            (roots / 'note').write_text('leave other files alone')
            with mock.patch.object(uvp, 'previous_lock', return_value={'projectInputs': [{'reference': 'producer'}]}), \
                 mock.patch.object(uvp, 'native_lock'):
                uvp.lock(root, str(pathlib.Path(root, 'lock.json')), inputs=[])
            self.assertFalse((roots / '0').is_symlink())
            self.assertTrue((roots / 'note').exists())


    def test_named_update_while_clearing_inputs_keeps_requested_scope(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(uvp, 'previous_lock', return_value={'projectInputs': [{'reference': 'producer'}]}), \
                 mock.patch.object(uvp.subprocess, 'run', return_value=mock.Mock(returncode=0)) as resolve, \
                 mock.patch.object(uvp, 'native_lock'):
                uvp.lock(root, str(pathlib.Path(root, 'lock.json')), upgrade=True,
                         inputs=[], upgrade_packages=['attrs'])
            self.assertEqual(resolve.call_args.args[0], ['uv', 'lock', '--upgrade-package', 'attrs'])

    def test_regular_update_does_not_repeat_native_resolution(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(uvp, 'previous_lock', return_value={}), \
                 mock.patch.object(uvp.subprocess, 'run') as resolve, \
                 mock.patch.object(uvp, 'native_lock'):
                uvp.lock(root, str(pathlib.Path(root, 'lock.json')), upgrade=True)
            resolve.assert_not_called()
