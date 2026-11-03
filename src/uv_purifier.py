# SPDX-License-Identifier: MIT
"""uv-purifier: locks what a uv project needs beyond uv.lock, so that Nix can
build the project offline in its sandbox.

uv.lock pins every runtime and development dependency with its URL and hash,
and the Nix library reads it directly. It leaves out the build requirements:
the packages that a workspace member's build backend needs, and the packages
that building a dependency from its sdist needs. `uv-purifier lock` resolves
those with uv itself and writes them to uv-purifier.lock.json beside uv.lock.
"""

import argparse
import base64
import configparser
import contextlib
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
import zipfile

LOCK_FILE = "uv-purifier.lock.json"
FORMAT_VERSION = 1

# The build requirements PEP 517 prescribes, and uv uses, for a project
# without a [build-system] table or without its `requires` key.
DEFAULT_BUILD_REQUIRES = ["setuptools>=40.8.0"]

# The keys of [tool.uv] that choose where packages come from. The throwaway
# projects that resolve build requirements copy them from the root
# pyproject.toml, so build requirements resolve from the same indexes as the
# project's own dependencies.
INDEX_KEYS = (
    "index",
    "index-url",
    "extra-index-url",
    "find-links",
    "index-strategy",
    "no-index",
)


class Error(Exception):
    pass


def log(message):
    print(f"uv-purifier: {message}", file=sys.stderr)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sri(uv_hash):
    """Converts a uv.lock hash ("sha256:<hex>") to an SRI hash."""
    algorithm, _, hexdigest = uv_hash.partition(":")
    if algorithm != "sha256":
        raise Error(f"unsupported hash algorithm in {uv_hash}")
    return "sha256-" + base64.b64encode(bytes.fromhex(hexdigest)).decode()


def file_name(url):
    """The file name a distribution has in the flat directory the Nix build
    reads: the last segment of its URL, percent-decoded."""
    return urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])


def package_key(package):
    return f"{package['name']}=={package.get('version', '')}"


def is_pure_wheel(name):
    """Whether a wheel installs on every platform: its ABI tag is `none` and
    its platform tag is `any`."""
    stem = name[: -len(".whl")]
    parts = stem.split("-")
    return len(parts) >= 5 and parts[-2] == "none" and parts[-1] == "any"


def entry_name(entry):
    """The file name of a wheel or sdist entry of uv.lock. An entry of a
    registry package has a URL; an entry of a local file has a path or a
    file name."""
    for key in ("url", "path", "filename"):
        if key in entry:
            return file_name(entry[key])
    raise Error(f"uv.lock has a file entry without a URL, path or file name: {entry}")


def may_need_build(package):
    """Whether some platform may have to build this registry package from
    its sdist: it has an sdist and no wheel that installs everywhere. The Nix
    library decides per system and Python version which of these it
    actually builds, and fetches only their build requirements."""
    if "sdist" not in package:
        return False
    return not any(is_pure_wheel(entry_name(w)) for w in package.get("wheels", []))


# The kinds of `source` in uv.lock. `editable`, `directory` and `virtual`
# are source trees in the project; a workspace member is one of them.
SOURCE_KINDS = ("registry", "git", "url", "path", "directory", "editable", "virtual")


def source_kind(package):
    source = package.get("source", {})
    for kind in SOURCE_KINDS:
        if kind in source:
            return kind, source[kind]
    raise Error(f"{package['name']} has a source uv-purifier does not know: {source}")


def workspace_members(uv_lock):
    """The names of the workspace members. A workspace lists them in
    [manifest]; a single project is its own member, the source tree at the
    project root."""
    manifest = uv_lock.get("manifest", {})
    if "members" in manifest:
        return set(manifest["members"])
    return {
        p["name"]
        for p in uv_lock.get("package", [])
        if source_kind(p)[0] in ("editable", "directory", "virtual") and source_kind(p)[1] == "."
    }


def parse_git_source(value):
    """Splits a git source of uv.lock, such as
    `https://host/repo?tag=v1&subdirectory=pkg#<commit>`, into the
    repository URL, the commit and the subdirectory."""
    parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    if not parsed.fragment:
        raise Error(f"the git source {value} names no commit")
    repository = urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
    return repository, parsed.fragment, query.get("subdirectory", [None])[0]


def prefetch_git(repository, commit):
    """Fetches a commit with its submodules, as uv checks it out, and returns
    the store path of the checkout and its NAR hash."""
    result = subprocess.run(
        [
            "nix-prefetch-git",
            "--url", repository,
            "--rev", commit,
            "--fetch-submodules",
            "--quiet",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise Error(f"nix-prefetch-git could not fetch {repository} at {commit}:\n{result.stderr.strip()}")
    info = json.loads(result.stdout)
    return info["path"], info["hash"]


def check_hash(path, expected_uv_hash, what):
    algorithm, _, expected = expected_uv_hash.partition(":")
    actual = sha256_file(path)
    if algorithm != "sha256" or actual != expected:
        raise Error(f"{what} has sha256 {actual}, but uv.lock says {expected_uv_hash}")


def build_requires(pyproject):
    build_system = pyproject.get("build-system")
    if build_system is None or "requires" not in build_system:
        return list(DEFAULT_BUILD_REQUIRES)
    return list(build_system["requires"])


# The backend PEP 517 prescribes, and uv uses, for a project without a
# `build-backend`.
DEFAULT_BUILD_BACKEND = "setuptools.build_meta:__legacy__"

# Asks a build backend for the requirements it adds at build time, through
# the PEP 517 hooks, as uv's build isolation does. It runs in the source tree
# with the static build requirements installed, and prints a JSON list.
HOOKS_SCRIPT = """
import importlib, json, sys
sys.path[:0] = json.loads(sys.argv[2])
module, _, attribute = sys.argv[1].partition(":")
backend = importlib.import_module(module)
for part in filter(None, attribute.split(".")):
    backend = getattr(backend, part)
requires, failed = [], {}
for hook in (
    "get_requires_for_build_sdist",
    "get_requires_for_build_wheel",
    "get_requires_for_build_editable",
):
    if hasattr(backend, hook):
        try:
            requires += getattr(backend, hook)({})
        except Exception as e:
            failed[hook] = f"{type(e).__name__}: {e}"
print(json.dumps({"requires": requires, "failed": failed}))
"""


def dynamic_requires(tree, pyproject, static, workdir):
    """The requirements the build backend of a source tree reports through
    its PEP 517 hooks for an sdist, a wheel and an editable install. A
    backend such as hatchling asks for `editables` only this way."""
    build_system = pyproject.get("build-system", {})
    backend = build_system.get("build-backend", DEFAULT_BUILD_BACKEND)
    backend_path = [os.path.join(tree, p) for p in build_system.get("backend-path", [])]
    venv = tempfile.mkdtemp(dir=workdir)
    for args in (["venv", "--quiet", venv], ["pip", "install", "--quiet", "--python", venv, *static]):
        result = run_uv(args, cwd=tree, env=resolution_env())
        if result.returncode != 0:
            raise Error(f"installing the build requirements {', '.join(static)} failed:\n{result.stderr.strip()}")
    result = subprocess.run(
        [os.path.join(venv, "bin", "python"), "-c", HOOKS_SCRIPT, backend, json.dumps(backend_path)],
        cwd=tree,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise Error(f"the build backend {backend} failed to report its requirements:\n{result.stderr.strip()}")
    report = json.loads(result.stdout.strip().splitlines()[-1])
    failed = report["failed"]
    if "get_requires_for_build_wheel" in failed:
        raise Error(
            f"the build backend {backend} failed to report the requirements of a wheel: "
            + failed["get_requires_for_build_wheel"]
        )
    for hook, error in failed.items():
        log(f"the build backend {backend} failed in {hook}, so its requirements are not locked: {error}")
    return report["requires"]


def unpack(archive, workdir):
    """Unpacks an sdist or source archive and returns its source tree: the
    single top directory, as in an sdist or a GitHub archive, or else the
    unpacked root."""
    target = tempfile.mkdtemp(dir=workdir)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            z.extractall(target)
    else:
        with tarfile.open(archive) as t:
            t.extractall(target, filter="data")
    entries = os.listdir(target)
    if len(entries) == 1 and os.path.isdir(os.path.join(target, entries[0])):
        return os.path.join(target, entries[0])
    return target


def scratch_copy(tree, workdir):
    """A writable copy of a source tree. A backend may write into the tree
    while it reports its requirements, as setuptools writes its egg-info, so
    the hooks run on a copy rather than on the project or a store path."""
    copy = os.path.join(tempfile.mkdtemp(dir=workdir), "tree")
    shutil.copytree(tree, copy, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "dist", ".uv-purifier"))
    subprocess.run(["chmod", "-R", "u+w", copy], check=True)
    return copy


def tree_requirements(tree, workdir):
    """Every build requirement of a source tree: the static ones of its
    [build-system] table and the ones its backend adds."""
    pyproject = tree_pyproject(tree)
    static = build_requires(pyproject)
    requires = list(static)
    for requirement in dynamic_requires(tree, pyproject, static, workdir):
        if requirement not in requires:
            requires.append(requirement)
    return requires


DOWNLOAD_ATTEMPTS = 3


def download(url, expected_uv_hash, directory):
    """Downloads a locked file and checks its hash. A download whose hash
    does not match is tried again, because a network path now and then
    hands back other bytes for a URL; one that keeps not matching fails."""
    target = tempfile.mkdtemp(dir=directory)
    target = os.path.join(target, file_name(url) or "download")
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        with urllib.request.urlopen(url) as response, open(target, "wb") as out:
            shutil.copyfileobj(response, out)
        try:
            check_hash(target, expected_uv_hash, url)
            return target
        except Error as e:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            log(f"{e} ({os.path.getsize(target)} bytes); downloading it again")
    return target


def tree_pyproject(directory):
    """The pyproject.toml of a source tree, or {} when it has none."""
    path = os.path.join(directory, "pyproject.toml")
    if not os.path.exists(path):
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def sdist_pyproject(path):
    """The pyproject.toml at the top of an sdist or source archive, or {}
    when it has none. The archive's top directory may have any name, as in a
    GitHub archive."""
    candidates = {}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                parts = name.split("/")
                if len(parts) == 2 and parts[1] == "pyproject.toml":
                    candidates[name] = archive.read(name)
    else:
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                parts = member.name.split("/")
                if len(parts) == 2 and parts[1] == "pyproject.toml" and member.isfile():
                    extracted = archive.extractfile(member)
                    if extracted is not None:
                        candidates[member.name] = extracted.read()
    if not candidates:
        return {}
    return tomllib.load(io.BytesIO(candidates[min(candidates)]))


# The time the build requirements are resolved as of. The lock run sets it
# from the lockfile it replaces, so a relock picks the same versions; a
# relock with --upgrade sets it to now.
EXCLUDE_NEWER = None


def resolution_env():
    """The environment of uv when it resolves build requirements: no
    distribution uploaded after EXCLUDE_NEWER counts."""
    return dict(os.environ, UV_EXCLUDE_NEWER=EXCLUDE_NEWER) if EXCLUDE_NEWER else None


def run_uv(args, cwd, env=None):
    result = subprocess.run(
        ["uv", *args], cwd=cwd, env=env, text=True, capture_output=True, check=False
    )
    return result


def resolve_build_requirements(requires, requires_python, index_config, constraints, workdir):
    """Resolves one set of build requirements for every platform, by locking
    a throwaway project that depends on them. A package that uv.lock pins
    gets uv.lock's version through `constraints`, so a build and the project
    share one version of it; when the build requirements cannot have that
    version, the set resolves without the constraints. Returns the packages
    of the throwaway project's uv.lock."""
    project = tempfile.mkdtemp(dir=workdir)

    def attempt(with_constraints):
        lines = [
            "[project]",
            'name = "uv-purifier-build-requirements"',
            'version = "0"',
            f"requires-python = {json.dumps(requires_python)}",
            f"dependencies = {json.dumps(requires)}",
            "",
            "[tool.uv]",
            "package = false",
        ]
        if with_constraints:
            lines.append(f"constraint-dependencies = {json.dumps(constraints)}")
        with open(os.path.join(project, "pyproject.toml"), "w") as f:
            f.write("\n".join(lines) + "\n")
            f.writelines(toml_assignment(key, value) for key, value in index_config.items())
        return run_uv(["lock", "--quiet"], cwd=project, env=resolution_env())

    result = attempt(True)
    if result.returncode != 0 and constraints:
        log(f"the build requirements {', '.join(requires)} cannot have uv.lock's versions; resolving them on their own")
        result = attempt(False)
    if result.returncode != 0:
        raise Error(result.stderr.strip())
    with open(os.path.join(project, "uv.lock"), "rb") as f:
        lock = tomllib.load(f)
    return [
        p
        for p in lock.get("package", [])
        if p["name"] != "uv-purifier-build-requirements"
    ]


def toml_assignment(key, value):
    """A [tool.uv] entry for the throwaway project, in TOML. Index entries
    are a list of tables; the other keys are strings, lists of strings or
    booleans, which JSON writes the way TOML reads them."""
    if key == "index":
        out = []
        for table in value:
            out.append("\n[[tool.uv.index]]\n")
            for k, v in table.items():
                out.append(f"{k} = {json.dumps(v)}\n")
        return "".join(out)
    return f"{key} = {json.dumps(value)}\n"


class EntryPoints(configparser.ConfigParser):
    """A parser of a wheel's entry_points.txt, which keeps the case of the
    script names."""

    def optionxform(self, optionstr):
        return optionstr


def scratch_project(root, uv_lock, workdir):
    """A writable copy of the project, for builds that may write into the
    source tree, as setuptools writes its egg-info. A relative path that
    uv.lock names outside the project gets a symlink at the same place
    beside the copy, so the copy resolves it as the project does."""
    outside = [
        source_kind(p)[1]
        for p in uv_lock.get("package", [])
        if source_kind(p)[0] in ("path", "directory", "editable")
        and not inside_project(source_kind(p)[1])
        and not os.path.isabs(source_kind(p)[1])
    ]
    depth = max((len(os.path.normpath(p).split(os.sep)) for p in outside), default=0)
    base = tempfile.mkdtemp(dir=workdir)
    copy = os.path.join(base, *(["nested"] * depth), "project")
    shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "dist", ".uv-purifier"))
    for path in outside:
        link = os.path.normpath(os.path.join(copy, path))
        if not os.path.exists(link):
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(os.path.normpath(os.path.join(root, path)), link)
    return copy


def derived_outputs(package, pyproject):
    """The outputs of a workspace member that the Nix library derives from
    uv.lock and the member's pyproject.toml (nix/lib/build-uv-project.nix,
    derivedOutputs): the sdist and a pure wheel named after the normalized
    name and the version uv.lock records, and the scripts of
    [project.scripts] and [project.gui-scripts]. A field the build makes
    otherwise, such as a dynamic version, is recorded in the lockfile."""
    stem = re.sub(r"[-_.]+", "_", package["name"]).lower()
    version = package.get("version")
    project = pyproject.get("project", {})
    dynamic = project.get("dynamic", [])
    scripts = (
        None
        if "scripts" in dynamic or "gui-scripts" in dynamic
        else sorted(set(project.get("scripts", {})) | set(project.get("gui-scripts", {})))
    )
    return {
        "sdist": f"{stem}-{version}.tar.gz" if version else None,
        "wheel": f"{stem}-{version}-py3-none-any.whl" if version else None,
        "scripts": scripts,
    }


def member_outputs(root, name, workdir):
    """What a workspace member's build produces, from one `uv build` of it:
    the file names of its sdist and wheel, and the scripts the wheel
    installs (its console_scripts and gui_scripts entry points, which
    include scripts that a setup.py defines)."""
    out = tempfile.mkdtemp(dir=workdir)
    log(f"building {name} to record its outputs")
    result = run_uv(["build", "--package", name, "--out-dir", out], cwd=root)
    if result.returncode != 0:
        raise Error(f"`uv build --package {name}` failed:\n{result.stderr.strip()}")
    files = sorted(n for n in os.listdir(out) if n != ".gitignore")
    sdists = [n for n in files if n.endswith((".tar.gz", ".zip"))]
    wheels = [n for n in files if n.endswith(".whl")]
    if len(sdists) != 1 or len(wheels) != 1:
        raise Error(f"`uv build --package {name}` made {files}, not one sdist and one wheel")
    scripts = []
    with zipfile.ZipFile(os.path.join(out, wheels[0])) as wheel:
        for entry in wheel.namelist():
            parts = entry.split("/")
            if len(parts) == 2 and parts[0].endswith(".dist-info") and parts[1] == "entry_points.txt":
                parser = EntryPoints(delimiters=("=",), interpolation=None)
                parser.read_string(wheel.read(entry).decode())
                for section in ("console_scripts", "gui_scripts"):
                    if parser.has_section(section):
                        scripts += parser.options(section)
    return {"sdist": sdists[0], "wheel": wheels[0], "scripts": sorted(set(scripts))}


def inputs_hash(root, tree_dirs):
    """The hash the Nix library compares to tell whether the lockfile still
    matches uv.lock and the pyproject.toml files of the source trees in the
    project. nix/lib/lock.nix computes the same text. A source tree outside
    the project contributes no file, because the Nix build may take it from
    elsewhere."""
    files = ["uv.lock"] + sorted(
        "pyproject.toml" if d == "." else f"{d}/pyproject.toml"
        for d in tree_dirs
        if inside_project(d) and os.path.exists(os.path.join(root, d, "pyproject.toml"))
    )
    text = "".join(f"{f} {sha256_file(os.path.join(root, f))}\n" for f in files)
    return hashlib.sha256(text.encode()).hexdigest()


def content_hash(lockfile):
    """The hash of every field of a lockfile but `contentHash`, over the
    JSON that Nix's builtins.toJSON writes for the same value: keys sorted,
    no spaces. The Nix library refuses a lockfile whose fields no longer
    match it, which is a lockfile edited by hand."""
    fields = {k: v for k, v in lockfile.items() if k != "contentHash"}
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def inside_project(path):
    return not (path == ".." or path.startswith(("/", "../")))


def previous_lock(lockfile):
    try:
        with open(lockfile) as source:
            value = json.load(source)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def previous_resolution(lockfile):
    """The time the build requirements of the existing lockfile were
    resolved as of, or None when there is no lockfile to keep."""
    try:
        with open(lockfile) as f:
            return json.load(f).get("resolvedAt")
    except (OSError, ValueError):
        return None


def update(root, lockfile, packages, inputs=None):
    """Updates both lockfiles: `uv lock --upgrade`, or with package names
    `uv lock --upgrade-package` for each, and then the purifier lockfile
    against the result, with its build requirements resolved anew."""
    previous = previous_lock(lockfile)
    if inputs is not None or previous.get('projectInputs'):
        return lock(root, lockfile, upgrade=True, inputs=inputs, upgrade_packages=packages)
    args = ["lock"]
    if packages:
        for package in packages:
            args += ["--upgrade-package", package]
    else:
        args.append("--upgrade")
    log(f"running `uv {' '.join(args)}`")
    result = subprocess.run(["uv", *args], cwd=root, check=False)
    if result.returncode != 0:
        raise Error(f"`uv {' '.join(args)}` failed")
    lock(root, lockfile, upgrade=True)


def input_helpers():
    tools = os.environ.get('UV_PURIFIER_PROJECT_INPUTS_TOOLS')
    if not tools:
        raise Error('Nix project inputs require the packaged uv-purifier command')
    sys.path.insert(0, tools)
    sys.path.insert(0, os.environ['UV_PURIFIER_NIX_LIB'])
    import project_inputs
    import python_inputs
    return project_inputs, python_inputs


def lock(root, lockfile, upgrade=False, inputs=None, upgrade_packages=()):
    previous = previous_lock(lockfile)
    old_inputs = previous.get('projectInputs', [])
    explicit_inputs = inputs is not None
    if inputs is None:
        inputs = [entry['reference'] for entry in old_inputs]
    if not inputs:
        # Resolve against the user's original manifest, which has no generated
        # Nix overrides. Remove helper-owned roots only after capture succeeds.
        if upgrade and explicit_inputs:
            arguments = ['lock']
            if upgrade_packages:
                for name in upgrade_packages:
                    arguments += ['--upgrade-package', name]
            else:
                arguments += ['--upgrade']
            resolved = subprocess.run(['uv', *arguments], cwd=root, check=False)
            if resolved.returncode != 0:
                raise Error('`uv lock` failed while clearing Nix inputs')
        result = native_lock(root, lockfile, upgrade)
        if old_inputs:
            roots = Path(root) / '.uv-purifier' / 'project-inputs'
            if roots.is_dir():
                for link in roots.iterdir():
                    if link.is_symlink():
                        link.unlink()
        return result
    sources, bindings = input_helpers()
    try:
        pinned = sources.pin(inputs, old_inputs, refresh=upgrade)
        views = sources.python_views(pinned, os.path.join(root, '.uv-purifier', 'project-inputs'))
        old_roots = [entry['nativeRepository'] for entry in old_inputs if 'nativeRepository' in entry]
        old_roots += [str(view['repository']) for view in views]
        with tempfile.TemporaryDirectory(prefix='uv-purifier-inputs-') as temporary:
            old_lock = os.path.join(root, 'uv.lock')
            model = tomllib.load(open(old_lock, 'rb')) if os.path.exists(old_lock) else {}
            effective = scratch_project(root, model, temporary)
            selected = bindings.prepare(Path(effective), views, old_roots, python=project_python(root))
            if upgrade:
                arguments = ['lock']
                if upgrade_packages:
                    for package in upgrade_packages:
                        arguments += ['--upgrade-package', package]
                else:
                    arguments += ['--upgrade']
                subprocess.run(['uv', *arguments], cwd=effective, check=True)
            else:
                # prepare can rebind a previous Nix path, but a registry pin
                # needs the native resolver to adopt its new Nix origin. The
                # generated manifest alone cannot validate that transition.
                arguments = ['lock']
                for name in sorted(selected):
                    arguments += ['--upgrade-package', name]
                subprocess.run(['uv', *arguments], cwd=effective, check=True)
            generated = os.path.join(temporary, LOCK_FILE)
            if os.path.exists(lockfile):
                shutil.copyfile(lockfile, generated)
            native_lock(effective, generated, upgrade)
            import tomli_w
            native = tomllib.load(open(os.path.join(effective, 'uv.lock'), 'rb'))
            with open(old_lock, 'w') as output:
                output.write(tomli_w.dumps(native))
            result = json.load(open(generated))
            for entry, view in zip(pinned, views):
                entry['nativeRepository'] = str(view['repository'])
            result['projectInputs'] = pinned
            trees = [source_kind(package)[1] for package in native.get('package', [])
                     if source_kind(package)[0] in ('editable', 'directory', 'virtual')]
            result['inputsHash'] = inputs_hash(root, trees)
            result['contentHash'] = content_hash(result)
            with open(lockfile, 'w') as output:
                json.dump(result, output, indent=2)
                output.write('\n')
    except (ValueError, subprocess.CalledProcessError) as error:
        raise Error(str(error)) from error


def native_lock(root, lockfile, upgrade=False):
    """Writes the purifier lockfile. uv.lock keeps its resolution: `uv lock`
    runs only to write it where it is missing or does not match
    pyproject.toml, which changes no version it can keep. With `upgrade`,
    as `update` runs it, the build requirements resolve anew; without it,
    as of the time the lockfile it replaces records."""
    global EXCLUDE_NEWER
    uv_lock_path = os.path.join(root, "uv.lock")
    check = run_uv(["lock", "--check", "--quiet"], cwd=root)
    if not os.path.exists(uv_lock_path) or check.returncode != 0:
        log("uv.lock is missing or does not match pyproject.toml; running `uv lock`")
        result = subprocess.run(["uv", "lock"], cwd=root, check=False)
        if result.returncode != 0:
            raise Error("`uv lock` failed")
    resolved_at = None if upgrade else previous_resolution(lockfile)
    if resolved_at is None:
        resolved_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        log(f"resolving build requirements as of {resolved_at}, as the lockfile did (`uv-purifier update` resolves them anew)")
    EXCLUDE_NEWER = resolved_at
    with open(uv_lock_path, "rb") as f:
        uv_lock = tomllib.load(f)
    with open(os.path.join(root, "pyproject.toml"), "rb") as f:
        root_pyproject = tomllib.load(f)
    tool_uv = root_pyproject.get("tool", {}).get("uv", {})
    index_config = {k: tool_uv[k] for k in INDEX_KEYS if k in tool_uv}
    requires_python = uv_lock.get("requires-python", ">=3")

    artifacts = {}
    requirement_sets = []
    set_index = {}
    builds = {}
    sources = {}
    tree_dirs = []

    # What uv.lock already records, which the purifier lockfile does not
    # repeat: the files of the registry packages, by file name with their
    # URL, and the version of each registry package, as a constraint for
    # the build requirements.
    uv_files = {}
    constraints = []
    for package in uv_lock.get("package", []):
        if source_kind(package)[0] == "registry":
            constraints.append(f"{package['name']}=={package['version']}")
            for entry in package.get("wheels", []) + ([package["sdist"]] if "sdist" in package else []):
                uv_files[file_name(entry["url"])] = entry["url"]

    def add_artifact(entry):
        name = file_name(entry["url"])
        if uv_files.get(name) == entry["url"]:
            return name
        known = artifacts.get(name)
        record = {"path": name, "url": entry["url"], "mirrors": [], "hash": sri(entry["hash"])}
        if known is not None and known != record:
            raise Error(f"two locked files are both named {name}: {known['url']} and {entry['url']}")
        artifacts[name] = record
        return name

    pending = []

    def request(key, requires):
        canonical = tuple(sorted(requires))
        if canonical not in set_index:
            set_index[canonical] = len(requirement_sets)
            requirement_sets.append({"requires": list(canonical)})
            pending.append(canonical)
        builds[key] = set_index[canonical]

    def outside_or_local(path):
        return path if os.path.isabs(path) else os.path.normpath(os.path.join(root, path))

    def request_tree(key, tree, workdir):
        """Locks the build requirements of a source tree. When its backend
        cannot report the requirements it adds, the static ones stand, and
        a build that needs more fails in the sandbox with uv's error."""
        log(f"reading the build requirements of {key}")
        try:
            requires = tree_requirements(tree, workdir)
        except Error as e:
            log(f"using only the static build requirements of {key}: {e}")
            requires = build_requires(tree_pyproject(tree))
        request(key, requires)

    with tempfile.TemporaryDirectory(prefix="uv-purifier-") as workdir:
        for package in uv_lock.get("package", []):
            key = package_key(package)
            kind, value = source_kind(package)
            if kind in ("editable", "directory", "virtual"):
                tree_dirs.append(value)
                if kind == "virtual":
                    continue
                request_tree(key, scratch_copy(outside_or_local(value), workdir), workdir)
            elif kind == "registry":
                if may_need_build(package):
                    sdist = package["sdist"]
                    downloaded = download(sdist["url"], sdist["hash"], workdir)
                    request_tree(key, unpack(downloaded, workdir), workdir)
            elif kind == "git":
                repository, commit, subdirectory = parse_git_source(value)
                log(f"fetching {package['name']} from {repository} at {commit}")
                checkout, nar_hash = prefetch_git(repository, commit)
                path = f"git/{package['name']}-{commit}"
                artifacts[path] = {
                    "path": path,
                    # The build needs no .git: it gives setuptools-scm and
                    # hatch-vcs the version from uv.lock.
                    "git": {
                        "url": repository,
                        "rev": commit,
                        "submodules": True,
                        "leaveDotGit": False,
                        "commitObject": None,
                    },
                    "hash": nar_hash,
                }
                sources[key] = path
                copy = scratch_copy(checkout, workdir)
                tree = os.path.join(copy, subdirectory) if subdirectory else copy
                request_tree(key, tree, workdir)
            elif kind == "url":
                if "sdist" in package:
                    downloaded = download(value, package["sdist"]["hash"], workdir)
                    request_tree(key, unpack(downloaded, workdir), workdir)
            elif kind == "path":
                if "sdist" in package:
                    archive = outside_or_local(value)
                    check_hash(archive, package["sdist"]["hash"], value)
                    request_tree(key, unpack(archive, workdir), workdir)

        seen = set()
        while pending:
            canonical = pending.pop(0)
            entry = requirement_sets[set_index[canonical]]
            log(f"resolving the build requirements {', '.join(canonical)}")
            try:
                packages = resolve_build_requirements(
                    list(canonical), requires_python, index_config, constraints, workdir
                )
            except Error as e:
                log(f"could not resolve {', '.join(canonical)}: {e}")
                entry["error"] = str(e)
                entry["packages"] = []
                continue
            records = []
            for package in packages:
                if source_kind(package)[0] != "registry":
                    raise Error(
                        f"the build requirement {package['name']} resolved to a {source_kind(package)[0]} source; "
                        "build requirements come from an index"
                    )
                record = {"name": package["name"], "version": package["version"]}
                record["wheels"] = [add_artifact(w) for w in package.get("wheels", [])]
                if "sdist" in package:
                    record["sdist"] = add_artifact(package["sdist"])
                records.append(record)
                key = package_key(package)
                if key not in seen and key not in builds and may_need_build(package):
                    seen.add(key)
                    downloaded = download(package["sdist"]["url"], package["sdist"]["hash"], workdir)
                    request_tree(key, unpack(downloaded, workdir), workdir)
            entry["packages"] = records

        members = workspace_members(uv_lock)
        project = scratch_project(root, uv_lock, workdir)
        # Each member's outputs as its build makes them, recorded where they
        # differ from what Nix derives.
        outputs = {}
        for package in uv_lock.get("package", []):
            if package["name"] not in members or source_kind(package)[0] == "virtual":
                continue
            made = member_outputs(project, package["name"], workdir)
            derived = derived_outputs(
                package, tree_pyproject(os.path.join(root, source_kind(package)[1]))
            )
            differs = {k: v for k, v in made.items() if derived[k] != v}
            if differs:
                outputs[package["name"]] = differs

    result = {
        "version": FORMAT_VERSION,
        "inputsHash": inputs_hash(root, tree_dirs),
        "resolvedAt": resolved_at,
        "artifacts": [artifacts[k] for k in sorted(artifacts)],
        "buildRequirements": requirement_sets,
        "builds": dict(sorted(builds.items())),
        "sources": dict(sorted(sources.items())),
        "outputs": dict(sorted(outputs.items())),
    }
    result["contentHash"] = content_hash(result)
    with open(lockfile, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    log(
        f"wrote {lockfile}: {len(builds)} builds, {len(requirement_sets)} sets of build requirements"
    )


def built_members(root):
    """The workspace members that have a build, which `publish` uploads."""
    with open(os.path.join(root, "uv.lock"), "rb") as f:
        uv_lock = tomllib.load(f)
    members = workspace_members(uv_lock)
    return [
        p["name"]
        for p in uv_lock.get("package", [])
        if p["name"] in members and source_kind(p)[0] != "virtual"
    ]


def check_lockfile(root, path):
    """Checks, before a Nix build, the things the Nix library checks, so
    that a stale or edited lockfile fails with the project's own paths."""
    if not os.path.exists(path):
        raise Error(f"{path} does not exist; run `uv-purifier lock`")
    try:
        with open(path) as f:
            lockfile = json.load(f)
    except ValueError as e:
        raise Error(f"{path} is not valid JSON ({e}); rewrite it with `uv-purifier lock`") from e
    if lockfile.get("version") != FORMAT_VERSION:
        raise Error(
            f"{path} has lock format version {lockfile.get('version')}; this uv-purifier writes "
            f"version {FORMAT_VERSION}. Run `uv-purifier lock`"
        )
    if lockfile.get("contentHash") != content_hash(lockfile):
        raise Error(f"{path} was changed after `uv-purifier lock` wrote it; rewrite it with `uv-purifier lock`")
    with open(os.path.join(root, "uv.lock"), "rb") as f:
        uv_lock = tomllib.load(f)
    tree_dirs = [
        source_kind(p)[1]
        for p in uv_lock.get("package", [])
        if source_kind(p)[0] in ("editable", "directory", "virtual")
    ]
    if lockfile.get("inputsHash") != inputs_hash(root, tree_dirs):
        raise Error(
            f"{path} was made for another uv.lock or pyproject.toml. "
            "Run `uv-purifier lock`, which also runs `uv lock` when pyproject.toml changed"
        )


def project_python(root):
    found = run_uv(['python','find','--no-python-downloads'], cwd=root)
    if found.returncode != 0 or not found.stdout.strip():
        raise Error('uv could not select the project Python: ' + found.stderr.strip())
    return found.stdout.strip()


def nix_build(root, lockfile, package=None, out_link=None):
    """Builds, with the Nix library this program was installed with, a
    member's release (its sdist and wheel), or without `package` the inputs
    of a development environment. Returns the store path. With `out_link`,
    the result stays alive through a garbage-collector root there."""
    library = os.environ.get("UV_PURIFIER_NIX_LIB")
    nixpkgs = os.environ.get("UV_PURIFIER_NIXPKGS")
    purifier = os.environ.get("UV_PURIFIER_LIBRARY")
    if not library or not nixpkgs or not purifier:
        raise Error(
            "this command needs the uv-purifier package, which sets UV_PURIFIER_NIX_LIB, "
            "UV_PURIFIER_NIXPKGS and UV_PURIFIER_LIBRARY"
        )
    args = [
        "nix-build",
        *(["--out-link", out_link] if out_link else ["--no-out-link"]),
        os.path.join(library, "cli.nix"),
        "--arg", "nixpkgs", nixpkgs,
        "--arg", "purifier", purifier,
        "--argstr", "src", root,
    ]
    python_version = subprocess.check_output([project_python(root), '-I', '-c',
        'import sys;print("%d.%d" % sys.version_info[:2])'],text=True).strip()
    args += ["--argstr", "pythonVersion", python_version]
    if os.path.abspath(lockfile) != os.path.join(root, LOCK_FILE):
        args += ["--argstr", "lockFile", os.path.abspath(lockfile)]
    if package is not None:
        args += ["--argstr", "package", package]
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise Error(f"the Nix build of {package or 'the development environment'} failed")
    return result.stdout.strip().splitlines()[-1]


def editable_trees(root):
    """The source trees `prefetch` installs as editable: every workspace
    member with a build, and every editable path dependency."""
    with open(os.path.join(root, "uv.lock"), "rb") as f:
        uv_lock = tomllib.load(f)
    members = workspace_members(uv_lock)
    trees = []
    for package in uv_lock.get("package", []):
        kind, value = source_kind(package)
        if kind == "editable" or (kind == "directory" and package["name"] in members):
            trees.append(value)
    return trees


# Where `prefetch` keeps its garbage-collector root, beside the purifier
# lockfile.
GC_ROOT = os.path.join(".uv-purifier", "prefetch")

# The environment variable that names the purifier lockfile of an external
# build, as `--lockfile` does.
LOCKFILE_VARIABLE = "UV_PURIFIER_LOCKFILE"

# The environment variable that names a uv.lock kept outside the project,
# as `--tool-lockfile` does.
TOOL_LOCKFILE_VARIABLE = "UV_PURIFIER_TOOL_LOCKFILE"


@contextlib.contextmanager
def tool_lockfile(root, outside, writes):
    """Runs a command with the uv.lock at `outside` in the project, since uv
    reads and writes uv.lock only there. The project's own uv.lock waits in
    .uv-purifier/ and is put back afterwards, even when the command fails.
    With `writes`, as for `lock` and `update`, the uv.lock the command left
    is copied to `outside` first; when `outside` does not exist yet, the
    command starts from the project's uv.lock."""
    if outside is None:
        yield
        return
    in_tree = os.path.join(root, "uv.lock")
    if not os.path.exists(outside) and not writes:
        raise Error(f"{outside} does not exist; run `uv-purifier lock --tool-lockfile {outside}`")
    saved = os.path.join(root, ".uv-purifier", "uv.lock.project")
    os.makedirs(os.path.dirname(saved), exist_ok=True)
    had_tree_copy = os.path.exists(in_tree)
    if had_tree_copy:
        shutil.copyfile(in_tree, saved)
    try:
        if os.path.exists(outside):
            shutil.copyfile(outside, in_tree)
        yield
        if writes and os.path.exists(in_tree):
            os.makedirs(os.path.dirname(outside), exist_ok=True)
            shutil.copyfile(in_tree, outside)
            log(f"wrote {outside}")
    finally:
        if had_tree_copy:
            shutil.copyfile(saved, in_tree)
            os.remove(saved)
        elif os.path.exists(in_tree):
            os.remove(in_tree)


def prefetch(root, lockfile, inputs=None):
    """Realizes in the Nix store everything the locked development needs on
    this system, kept alive by a garbage-collector root beside the purifier
    lockfile, and
    installs the project's .venv from it offline: the dependencies with
    their hashes, the packages from git, URLs and local paths from their
    wheels, and the workspace members and editable path dependencies as
    editable installs. Afterwards `uv run --no-sync`, the tests and the
    editor work with no network."""
    if inputs is None:
        check_lockfile(root, lockfile)
        log("building the locked files with Nix")
        inputs = nix_build(root, lockfile, out_link=os.path.join(os.path.dirname(lockfile), GC_ROOT))
    # uv gets a cache of its own. With the user's cache, which holds index
    # data from earlier online runs, uv picks distributions from that index
    # data even with UV_NO_INDEX, and then fails offline to download them.
    cache = os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache")),
        "uv-purifier",
        "uv",
    )
    environment = dict(
        os.environ,
        UV_OFFLINE="1",
        UV_NO_INDEX="1",
        UV_FIND_LINKS=os.path.join(inputs, "wheelhouse"),
        UV_PYTHON_DOWNLOADS="never",
        UV_CACHE_DIR=cache,
    )
    venv = os.environ.get("UV_PROJECT_ENVIRONMENT", os.path.join(root, ".venv"))
    python = os.path.join(venv, "bin", "python")

    def uv(*args):
        result = subprocess.run(["uv", *args], cwd=root, env=environment, check=False)
        if result.returncode != 0:
            raise Error(f"`uv {' '.join(args)}` failed")

    with tempfile.TemporaryDirectory(prefix="uv-purifier-") as workdir:
        exported = os.path.join(workdir, "exported.txt")
        requirements = os.path.join(workdir, "requirements.txt")
        wheels = os.path.join(workdir, "wheels.txt")
        effective = os.path.join(inputs, "project")
        project_args = ["--project", effective] if os.path.isdir(effective) else []
        uv("export", *project_args, "--frozen", "--all-packages", "--no-emit-workspace",
           "--format", "requirements.txt", "--output-file", exported)
        result = subprocess.run(
            [sys.executable, "-I", os.path.join(inputs, "split_requirements.py"),
             exported, os.path.join(inputs, "direct.json"), requirements, wheels],
            check=False,
        )
        if result.returncode != 0:
            raise Error("splitting the exported requirements failed")
        if not os.path.exists(python):
            uv("venv", "--python", os.path.join(inputs, "python"), venv)
        uv("pip", "sync", "--python", python, "--require-hashes", requirements)
        with open(wheels) as f:
            for directory in f.read().split():
                files = [
                    os.path.join(directory, n)
                    for n in sorted(os.listdir(directory))
                    if n.endswith(".whl")
                ]
                uv("pip", "install", "--python", python, "--no-deps", *files)
        trees = editable_trees(root)
        if trees:
            editables = [a for tree in trees for a in ("--editable", tree)]
            uv("pip", "install", "--python", python, "--no-deps", *editables)
    log(f"installed {venv}; run commands in it with `uv run --no-sync`")


def publish(root, lockfile, packages, uv_publish_args):
    """Uploads with `uv publish` the release that Nix built: the sdist and
    wheel of each member with a build, or of the members named."""
    check_lockfile(root, lockfile)
    members = built_members(root)
    for package in packages:
        if package not in members:
            raise Error(f"{package} is not a workspace member with a build; the members are {', '.join(members)}")
    files = []
    for package in packages or members:
        log(f"building the release of {package} with Nix")
        out = nix_build(root, lockfile, package)
        files += [
            os.path.join(out, name)
            for name in sorted(os.listdir(out))
            if name.endswith((".whl", ".tar.gz", ".zip"))
        ]
    unique = {}
    for file in files:
        name = os.path.basename(file)
        if name in unique and Path(unique[name]).read_bytes() != Path(file).read_bytes():
            raise Error(f"conflicting projected wheel publication: {name}")
        unique[name] = file
    result = subprocess.run(["uv", "publish", *uv_publish_args, *unique.values()], cwd=root, check=False)
    if result.returncode != 0:
        raise Error("uv publish failed")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="uv-purifier", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    def common(command_parser):
        command_parser.add_argument("--project", default=".", help="the directory that holds uv.lock")
        command_parser.add_argument(
            "--lockfile",
            help=f"the purifier lockfile, when it is kept outside the project (default: ${LOCKFILE_VARIABLE}, "
            f"or else {LOCK_FILE} in the project)",
        )
        command_parser.add_argument(
            "--tool-lockfile",
            help=f"a uv.lock kept outside the project, used in place of the project's own (default: ${TOOL_LOCKFILE_VARIABLE}); "
            "lock and update write it there and leave the project's uv.lock as it is",
        )

    lock_parser = sub.add_parser(
        "lock",
        help=f"write {LOCK_FILE}, keeping uv.lock's resolution (`uv lock` runs only where uv.lock is missing or stale)",
    )
    common(lock_parser)
    lock_inputs = lock_parser.add_mutually_exclusive_group()
    lock_inputs.add_argument("--input", action="append", help="a pinned Nix producer with a Python projection")
    lock_inputs.add_argument("--no-inputs", action="store_true", help="remove retained Nix inputs and resolve native dependency origins")
    update_parser = sub.add_parser(
        "update",
        help="update both lockfiles: `uv lock --upgrade` (or --upgrade-package for each package named), then the purifier lockfile",
    )
    common(update_parser)
    update_inputs = update_parser.add_mutually_exclusive_group()
    update_inputs.add_argument("--input", action="append", help="a pinned Nix producer with a Python projection")
    update_inputs.add_argument("--no-inputs", action="store_true", help="remove retained Nix inputs and resolve native dependency origins")
    update_parser.add_argument("packages", nargs="*", help="the packages to update (every package when none)")
    prefetch_parser = sub.add_parser(
        "prefetch",
        help="realize the locked files with a GC root and install the project's .venv from them, for offline work",
    )
    common(prefetch_parser)
    prefetch_parser.add_argument(
        "--inputs",
        help="the inputs Nix built for the project (the `interactive` output of buildUvProject); "
        "built with Nix when absent",
    )
    publish_parser = sub.add_parser(
        "publish",
        help="upload with `uv publish` the release that Nix built",
    )
    common(publish_parser)
    publish_parser.add_argument(
        "--package",
        action="append",
        default=[],
        help="a workspace member to publish (repeatable; every member with a build when absent)",
    )
    publish_parser.add_argument(
        "uv_publish_args",
        nargs=argparse.REMAINDER,
        help="arguments for `uv publish`, after `--`",
    )
    args = parser.parse_args(argv)
    root = os.path.abspath(args.project)
    named = args.lockfile or os.environ.get(LOCKFILE_VARIABLE)
    lockfile = os.path.abspath(named) if named else os.path.join(root, LOCK_FILE)
    named_tool = args.tool_lockfile or os.environ.get(TOOL_LOCKFILE_VARIABLE)
    outside = os.path.abspath(named_tool) if named_tool else None
    try:
        with tool_lockfile(root, outside, writes=args.command in ("lock", "update")):
            if args.command == "lock":
                lock(root, lockfile, inputs=[] if args.no_inputs else args.input)
            elif args.command == "update":
                update(root, lockfile, args.packages, [] if args.no_inputs else args.input)
            elif args.command == "prefetch":
                prefetch(root, lockfile, args.inputs)
            elif args.command == "publish":
                extra = args.uv_publish_args
                if extra and extra[0] == "--":
                    extra = extra[1:]
                publish(root, lockfile, args.package, extra)
    except Error as e:
        log(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
