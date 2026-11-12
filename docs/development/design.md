# Design

## Two lockfiles

uv.lock pins every package the project installs, including development
groups and optional dependencies, for every platform. A package from an
index has its wheels and its sdist, with URLs and SHA-256 hashes. A package
from git has its commit, one from a URL has the URL and the hash of the
file, and one from a local path has the path and, for a file, its hash. The
Nix library reads uv.lock with `builtins.fromTOML` (`lib-overlays/default/lock.nix`).

uv.lock does not pin build requirements, which uv's build isolation
resolves at build time. `uv-purifier lock` (`src/uv_purifier.py`) fills that
gap. It keeps the resolution of uv.lock: it runs `uv lock` only when
`uv lock --check` fails, which happens where uv.lock is missing or no
longer matches pyproject.toml, and `uv lock` changes no version it can
keep. `uv-purifier update` runs `uv lock --upgrade`, or
`uv lock --upgrade-package` for each package named, and then `lock` with
the build requirements resolved anew. `lock` then visits every package the
build may have to build:

- every workspace member with a build;
- every package from git, a URL or a local path that is not a wheel;
- every package from an index that has no wheel tagged `none-any`, which
  some system may have to build from its sdist (the Nix build decides
  which systems);
- the build requirements themselves, by the last rule, until nothing new
  turns up.

For each, it takes the source tree (a scratch copy of a local tree or of a
git checkout, or an unpacked archive) and:

1. reads the static requirements of `[build-system].requires`, or uv's
   default `setuptools>=40.8.0`;
2. installs them into a throwaway virtual environment and calls the
   backend's `get_requires_for_build_sdist`, `get_requires_for_build_wheel`
   and `get_requires_for_build_editable` hooks (PEP 517 and PEP 660) in the
   tree, which report the requirements the backend adds, such as
   hatchling's `editables`;
3. resolves the union by running `uv lock` on a throwaway virtual project
   that depends on it. The throwaway project copies the index settings of
   the root pyproject.toml, and the result covers every platform, like
   uv.lock does.

Git checkouts are fetched with `nix-prefetch-git --fetch-submodules`, which
reports the NAR hash that nixpkgs' `fetchgit` checks.

`uv-purifier.lock.json` records:

- `artifacts`: the download entries of the shared purifier library. A file
  entry `{ path, url, mirrors, hash }` is a file of a build requirement,
  with its file name as `path`, an empty `mirrors` list and an SRI `hash`.
  A git entry `{ path, git = { url, rev, submodules, leaveDotGit,
  commitObject }, hash }` is a checkout, with `submodules = true` because
  uv checks out submodules. `leaveDotGit` is false and `commitObject` is
  null, because the build passes the version from uv.lock to
  setuptools-scm and hatch-vcs and needs no `.git`.
- `buildRequirements`: each distinct set, with its resolved packages and
  the file names of their wheels and sdist. A set that did not resolve has
  `error` instead, and the Nix build reports it only if it has to build
  that package.
- `builds`: for each package that may be built, `"<name>==<version>"`, the
  index of its set.
- `sources`: for each package from git, the `path` of its git entry.
- `outputs`: for a workspace member whose build makes something other
  than what Nix derives, the fields that differ. Nix derives the sdist and
  a pure wheel from the normalized name and the version in uv.lock, and the
  scripts from [project.scripts] and [project.gui-scripts]. The lock run
  builds each member once with `uv build`, in a scratch copy of the
  project, reads the scripts from the wheel's `entry_points.txt`, and
  records a field only when the build makes it otherwise, such as with a
  dynamic version, a compiled wheel or scripts from a setup.py. The Nix
  library exposes the outputs as `release.artifacts` and
  `packages.<member>.scripts`, and each build fails when it makes
  something else.

The lockfile repeats nothing uv.lock records. A file of a build
requirement that uv.lock also lists is named in its set of build
requirements but has no entry in `artifacts`; Nix takes its URL and hash
from uv.lock. The build requirements resolve with every version uv.lock
pins as a constraint, so a package that a build and the project both use
has one version; a set that cannot have those versions resolves without
them, and the lock run says so.
- `inputsHash`: a hash over uv.lock and the pyproject.toml of every source
  tree in the project. The Nix library computes the same hash and refuses
  the lockfile when they differ.

## Choosing the files for a system

The `wheels` function of the shared purifier library
(github:nix-purifiers/purifier, which took it from this repository)
decides from a wheel's file name whether it installs
with the build's Python on the build's system (PEP 425 tags; on Linux
manylinux 1, 2010, 2014 and `2_N` against the glibc of the package set,
on macOS the arm64 and universal2 tags up to the package set's oldest
macOS; `abi3`). For
each package from an index, `pkgs/build-uv-project/default.nix` takes its wheels
that install, or else its sdist and the build of it. A build adds the files
of its set of build requirements, chosen the same way, and the builds those
need in turn. Every chosen file is a file entry, and the library's `tree`
fetches them and links them into one flat directory, the wheelhouse.

uv makes the final choice among the fetched wheels when it installs. When a
wheel that `wheels` accepts is one uv rejects, uv tries to build the
sdist, which is missing from the wheelhouse, and the build fails with uv's
error.

## Packages from git, URLs and local paths

Each of these gets a wheel of its own (`directWheels`). A wheel file is
used as it is. Any other source is built with `uv build --wheel` from the
git checkout (and its `subdirectory`), the unpacked archive, or the local
directory. `SETUPTOOLS_SCM_PRETEND_VERSION` holds the version uv.lock
records, because a checkout from `fetchgit` has no repository for
setuptools-scm or hatch-vcs to read.

A local path inside `src` is read from `src`. A path outside it, such as
`../shared`, comes from the `pathSources` argument, because a flake's
source tree cannot reach outside itself. The `uv-purifier` command maps
such paths to their absolute locations itself (`pkgs/cli/cli.nix`).

## Installing in the sandbox

uv runs with `UV_OFFLINE`, `UV_NO_INDEX` and `UV_FIND_LINKS` pointing at the
wheelhouse, and with the Python of the package set (`UV_PYTHON`,
`UV_PYTHON_DOWNLOADS=never`).

- A member's package is `uv build --package <member>`, which builds its
  sdist and then its wheel from the sdist, in an isolated environment that
  takes the build backend from the wheelhouse.
- A member's environment starts from `uv export --frozen --no-dev
  --package <member> --no-emit-workspace`. `pkgs/build-uv-project/split_requirements.py`
  takes out every requirement that does not come from an index (uv export
  writes those as `name @ <url>` or as a path) and replaces it with the
  wheel Nix built. The rest is installed with
  `uv pip install --require-hashes` into a new virtual environment,
  followed by the replaced wheels and the wheels of the member and of the
  members it depends on.

`uv sync` cannot do this step. With no network, uv installs a registry
package of uv.lock only from the URL in uv.lock or from its cache, and
ignores `--find-links`. The cache's layout is internal to uv.

## Developing offline

`uv-purifier prefetch` builds the `interactive` output of `buildUvProject`,
with a garbage-collector root at `.uv-purifier/prefetch` beside the purifier lockfile,
(the Python, the wheelhouse, and the wheels from git, URLs and local paths
with their index for `split_requirements.py`) and installs the project's
`.venv` from it the same way, with the default dependency groups, using
`uv pip sync` so packages that left the lock are removed. The workspace
members and editable path dependencies are installed editable last.

## The interpreter

The Python comes from nixpkgs (`python ? python3`). uv never downloads one
in the build. The lockfiles record no Python version, because uv.lock
covers every version that `requires-python` allows.
