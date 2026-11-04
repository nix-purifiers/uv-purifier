# uv-purifier

uv-purifier keeps uv's development workflow on locked,
Nix-resolved dependencies. Use the normal build and test commands in your
working tree; building the project itself in the Nix sandbox is an
optional next step.

uv.lock already pins every dependency of the project with its URL and hash,
and the Nix library reads it directly. What uv.lock leaves out are the build
requirements: the build backend of each workspace member, and the packages
needed to build a dependency from its source. `uv-purifier lock` resolves
those with uv and writes them to `uv-purifier.lock.json` beside uv.lock.

## Quickstart

From the project root:

```zsh
nix shell 'github:nix-purifiers/uv-purifier'
uv-purifier lock
uv-purifier prefetch
. .venv/bin/activate
pytest
```

`prefetch` creates and installs the project’s `.venv` from the locked
dependencies. The activation command makes it available to your shell.

Commit the lockfiles that the lock command writes, including the build
tool's own lockfile when it has one. Keep using the project's usual build,
test and editor commands with this setup. Nix provides the locked
dependencies; the development commands above run outside the Nix sandbox.
No project Nix files are needed for this workflow.

Sandboxed builds of the whole project are an optional
[next step](#building-the-project-with-nix).

For persistent use, add these packages through your usual Nix configuration.


## Development loop

uv has no plugin mechanism, so uv-purifier is a separate command. It runs
uv for every step that resolves or installs packages.

```zsh
nix shell 'github:nix-purifiers/uv-purifier'
uv-purifier prefetch
uv run --no-sync pytest
```

`prefetch` realizes in the Nix store everything development needs on this
system, keeps it alive with a garbage-collector root at
`.uv-purifier/prefetch` beside the purifier lockfile (add `.uv-purifier/`
to `.gitignore`), and
installs the project's `.venv` from it: the dependencies, including the
default dependency groups, with their hashes; the packages from git, URLs
and local paths from the wheels Nix built; and the workspace members and
editable path dependencies as editable installs. Run the project and its
tests with `uv run --no-sync`, or with `.venv/bin/` directly. An edit to a
member shows at once.

A uv command that needs a package outside the lock, such as `uv add` of a
new dependency, downloads it from the index when the network is up. With
the network cut off, uv stops with its own network error, which does not
name `uv-purifier lock`. In both cases the next steps are the ones below:
`uv-purifier lock`, then `uv-purifier prefetch`.

To add a dependency, add it with uv, which needs the network, then relock
and prefetch again. `uv-purifier lock` keeps uv.lock's resolution: it runs
`uv lock` only where uv.lock is missing or no longer matches
pyproject.toml, which changes no version that uv can keep.

```zsh
nix shell 'github:nix-purifiers/uv-purifier'
uv add requests
uv-purifier lock
uv-purifier prefetch
```

To upgrade dependencies, `uv-purifier update` updates both lockfiles in
one run. It runs `uv lock --upgrade`, or `uv lock --upgrade-package` for
each package it names, and then relocks the purifier lockfile with the
build requirements resolved anew:

```zsh
nix shell 'github:nix-purifiers/uv-purifier'
# every package
uv-purifier update
# only requests
uv-purifier update requests
```

A relock with `lock` resolves the build requirements as of the time the
lockfile records, so it keeps their versions and reproduces the lockfile
byte for byte when nothing else changed.

Every command finds both lockfiles in the project by default. For an
external build, `--lockfile <path>` or the environment variable
`UV_PURIFIER_LOCKFILE` names the purifier lockfile. `--tool-lockfile <path>` or `UV_PURIFIER_TOOL_LOCKFILE` names a uv.lock outside the
project. With a uv.lock outside the project, `lock` and `update` write
uv.lock to that path and leave the uv.lock of the project as it is. When
that path has no file yet, `lock` starts from the uv.lock of the project.
A dev shell of a packaging flake can set both variables to the paths it
passes as `lockFile` and `uvLock`.

`uv-purifier publish -- --token "$TOKEN"`
uploads the `release` that Nix built, with `uv publish`; arguments after
`--` go to `uv publish`, and `--package <member>` limits it to members.

## Exporting Python dependencies

Each built package exposes `projections.python`: a wheelhouse containing the
package, its workspace dependencies and its native runtime dependencies.
Project wheels use Nix-derived versions, with dependency metadata rewritten
for ordinary Python installers. Unchanged third-party wheels retain their
published versions. Dependencies available only as source archives are built
into wheels with their locked build requirements.

## Dependencies from another purified project

```zsh
nix shell 'github:nix-purifiers/uv-purifier'
uv-purifier lock \
  --input 'github:owner/library/<rev>#library'
uv-purifier prefetch
uv run --no-sync pytest
```

The producer must expose `projections.python`. Its wheels override native
version requests automatically; project dependency declarations stay unchanged.
The purifier lock records the pinned Nix source, and omits the supplied wheels
from its downloads. Relocking retains the pin; `update` refreshes it. Repeat
`--input` for additional producers. Conflicting providers are rejected.

To return all dependencies to their native origins, run
`uv-purifier lock --no-inputs`, then `uv-purifier prefetch`. This clears retained
Nix inputs and their helper GC roots after successful resolution.
`uv-purifier update --no-inputs` also updates native selections; package names
limit that update as usual. `--no-inputs` and `--input` are mutually exclusive.

Use `uv-purifier lock` and `uv-purifier update` to retain these bindings.
`prefetch` resolves recorded wheel paths for the current Python and machine
without changing the committed lockfiles. Development still uses `.venv`
and `uv run --no-sync`.

For composed projects, `publish` uploads the projected wheels and their
runtime closure, with dependency metadata for ordinary installers. These
wheels use local versions; the destination index must accept them. The
exported wheelhouse also works directly with `--find-links`.

## Building the project with Nix

Once the native development workflow is working, you can also build and
test the whole project in the Nix sandbox. For this step, keep a flake or
another Nix build definition in the project's repository, or maintain an
external packaging repository that points at its sources and lockfiles.
The builder consumes the same lockfiles as the development workflow.

`overlays.default` adds one attribute to the package set: the scope
`uv-purifier`. The scope holds the builder `buildUvProject` and the
`uv-purifier` command.

A project whose authors want a flake that packages it can add one. The
flake can live in the project's repository or in another one, and it calls
the builder directly:

```nix
{
  inputs.uv-purifier.url =
    "github:nix-purifiers/uv-purifier";
  inputs.nixpkgs.follows = "uv-purifier/nixpkgs";

  outputs =
    { nixpkgs, uv-purifier, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ uv-purifier.overlays.default ];
      };
      project = pkgs.uv-purifier.buildUvProject {
        src = ./.;
      };
    in
    {
      # A virtual environment with the member `app`, its
      # dependencies and
      # its scripts in bin/.
      packages.${system}.default = project.packages.app;
      checks.${system}.tests = project.tests;
    };
}
```

`uv-purifier.buildUvProject.override` replaces a package the builder takes
from the package set, such as `uv` or `python3`. `uv-purifier.overrideScope`
replaces what everything in the scope shares.

`buildUvProject { src, ... }` returns:

- `packages.<member>`: for each workspace member, a virtual environment
  with the member and its runtime dependencies, and its scripts in `bin/`.
  `packages.<member>.scripts.<script>` names each script. A virtual
  workspace root has a package too.
- `tests`: the project's test command (`testCommand`, by default
  `[ "pytest" ]`), run in the sandbox from a copy of `src`, in an
  environment with every member and the default dependency groups.
- `release`: the sdist and wheel of every member with a build, which
  `uv-purifier publish` uploads. `release.artifacts."<file name>"` names
  each file, such as `release.artifacts."app-2.0.0-py3-none-any.whl"`.
- `fetches`: the download entries of the
  [purifier](https://github.com/nix-purifiers/purifier) library that the
  build fetches for the system: a file entry per locked file, and a git
  entry per git checkout. SBOM, license and vulnerability tools read it.

Every name in the result is known when Nix evaluates, from the lockfiles,
without building anything. `uv-purifier lock` records the file names and
scripts of each member by building it once, so scripts that a setup.py
defines are included.

`buildUvProject` also takes:

- `fetchers`, which goes to the purifier library's `fetchEntry` and `tree`,
  such as `fetchers.fetchurl` for a company mirror or credentials.
- `python` (default `python3` of the package set), which must satisfy the project's
  `requires-python`.
- `nativeUv` (default the package set's `uv`), the native resolver and installer.
- `lockFile` (default `src + "/uv-purifier.lock.json"`), the purifier
  lockfile. A file given outside `src` replaces any copy in it.
- `uvLock` (default `src + "/uv.lock"`). A file given outside `src`
  replaces the copy in it.
- `pathSources`, the local packages that uv.lock names by a path outside
  `src`, such as `pathSources."../shared" = ../shared;`. A flake's source
  tree cannot reach outside itself. A path inside `src` needs no entry.
- `testCommand`, the command of `tests`.

Metadata conversion runs on the builder's helper `python3`, independently of
this project interpreter. A historical interpreter can come from a separately
pinned package set. Native `prefetch` and `publish` retain an already-selected
Nix-store interpreter instead of looking up its minor version in the helper's
current package set. A non-Nix interpreter continues to use the package-set
lookup for the selected minor version.

Linux wheel selection uses the selected Python package's libc version. For an
immutable interpreter selected by the CLI, this version comes from the running
target interpreter. The helper package set's newer libc cannot admit a wheel
that requires a newer runtime than the target provides.

`nativeUv` selects the project's resolver and installer independently of the
host `uv` build/export/publication frontend. It defaults to the same tool.
Pass `nativeUv` to `buildUvProject`, override it on that builder, or override it
on the `cli` package; the
CLI retains both package identities when it invokes Nix. Project lock and
installation commands use the selected native manager. Helper export is frozen
and its lock bytes are checked before installation. Offline and index settings
are explicit native flags, including for older managers that ignore newer uv
environment variables. Locked wheel installation does not reapply project
resolver configuration.

`lockFile` and `uvLock` serve an external build, in which a packaging
flake builds a project that it does not change. The packaging flake keeps
the purifier lockfile beside itself. When the committed uv.lock of the
project is missing or stale, the packaging flake can keep its own uv.lock
too. The commands take the same paths through `--lockfile` and
`--tool-lockfile`, as the Development loop section describes.

`lib.uv-purifier.readLock { src, lockFile ? ..., uvLock ? ... }` reads uv.lock and the
purifier lockfile into the lock in memory of the purifier contract,
`{ purifier, entries, outputs, problems }`, which tools that read every
purifier, such as an SBOM generator, read in one shape. A compiled wheel's
entry carries the `systems` its platform tags name. `buildUvProject` checks
it with the purifier library's `checkLock` and takes the downloads of its
system from `entriesFor`.

### One-off builds without a project Nix file

For an ad hoc build, the builder can also be evaluated directly from the
project root. Replace the example package name with one from your project:

```zsh
nix build --impure --expr 'let
  purifier = builtins.getFlake "github:nix-purifiers/uv-purifier";
  pkgs = import purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ purifier.overlays.default ];
  };
in
(pkgs.uv-purifier.buildUvProject { src = ./.; }).packages.app'
```

This uses the purifier's pinned nixpkgs, matching the lock command above.
To build with ambient `<nixpkgs>` from your Nix search path instead,
replace `import purifier.inputs.nixpkgs` with `import <nixpkgs>`.
Both forms need no Nix files in your project. The build tool and language
versions must match the lockfile; with ambient nixpkgs, lock with the
purifier from that package set or pass a compatible toolchain to the builder.

## Working offline

After `prefetch`, `uv run --no-sync`, the tests and the editor work with
the network cut off. `uv run` without `--no-sync` and `uv sync` do not:
with no network, uv installs a registry package only from the URL in
uv.lock or from its own cache, whose layout is internal to uv. Setting
`UV_NO_SYNC=1` makes `--no-sync` the default.

`prefetch` runs uv with a cache of its own, under
`$XDG_CACHE_HOME/uv-purifier`. With the usual uv cache, which holds index
data from online runs, uv picks distributions from that index data even
with no index configured, and then cannot download them offline.

## Editor

An editor or language server that uses the project's `.venv`, such as
pyright, Pylance or basedpyright, resolves every import from it after
`prefetch`, with no network.

## Troubleshooting

| What happened | The error says | Fix |
| --- | --- | --- |
| uv.lock changed, or a dependency was added to pyproject.toml without `uv lock` | `uv-purifier.lock.json was made for another uv.lock or another pyproject.toml` | `uv-purifier lock`, which also runs `uv lock` when pyproject.toml changed |
| uv-purifier.lock.json was edited by hand or by a merge | `was changed after uv-purifier lock wrote it` | `uv-purifier lock` |
| uv-purifier.lock.json is not valid JSON | `is not valid JSON` | `uv-purifier lock` |
| uv-purifier.lock.json is missing | `does not exist` | `uv-purifier lock`, and commit the file |
| The lockfile has another format version | `has lock format version` | `uv-purifier lock` |
| A package must be built from its sdist on this system, but the lockfile has no build requirements for it | `has no build requirements for` | `uv-purifier lock` |
| A downloaded file did not match the hash in uv.lock | `has sha256 …, but uv.lock says …` | The lock run tries again twice; if it keeps failing, check the network path |
| uv.lock names a local path outside `src` | `which is outside src. Pass it in pathSources` | Pass the path in `pathSources` |

`prefetch` and `publish` check the lockfile before they start Nix, so their
errors name the project's own files.

## Supported language repositories

uv.lock records where each package comes from, and uv-purifier builds
packages from each of those sources. The list below says how each kind of
package builds.

- Packages from an index install from the wheel for the system and Python
  of the build, or else build from their sdist.
- Packages from git build from a checkout of the locked commit, with its
  submodules and from the `subdirectory` uv.lock names. A project that
  reads its version from git with setuptools-scm or hatch-vcs gets the
  version uv.lock records, because the checkout has no repository.
- Packages from a URL install the wheel, or build the archive.
- Packages from a local directory, sdist or wheel build or install from
  `src` or `pathSources`.

The build runs on Linux, for x86_64 and aarch64, and on Apple Silicon Macs;
the wheel selection (the purifier library's `wheels`) reads a wheel's Linux
platform tags against the glibc of the package set, and its macOS tags
against the package set's oldest macOS. On a Mac, a workspace member whose
own wheel has compiled code doesn't build: the library's `wheelName` names
Linux wheels only. uv.lock and `uv-purifier.lock.json` cover every
platform, so a lock made anywhere builds on each. A build backend reports
the requirements it adds at build time for the platform of the lock run; a
backend that asks for other packages on another platform makes that
platform's build fail with uv's error naming the package.

## Examples

For day-to-day work on these projects, use the native-tool setup and
commands in [Quickstart](#quickstart) and [Development loop](#development-loop).
The Nix expressions below show optional sandboxed packaging of those
projects.

### Flask

[Flask](https://github.com/pallets/flask) is a uv project with a
flit-core build, dependencies with compiled wheels, and dependency groups
for tests, typing and docs. In a checkout of Flask, lock it, then install
the development environment and run its tests:

```zsh
nix shell 'github:nix-purifiers/uv-purifier'
git clone https://github.com/pallets/flask
cd flask
uv-purifier lock
uv-purifier prefetch
uv run --no-sync pytest
```

Then build Flask from the same directory, with the command of the
Quickstart and `packages.flask` in place of `packages.app`:

```zsh
nix build --impure --expr 'let
  purifier = builtins.getFlake "github:nix-purifiers/uv-purifier";
  pkgs = import purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ purifier.overlays.default ];
  };
in
(pkgs.uv-purifier.buildUvProject { src = ./.; }).packages.flask'
```

A packaging flake outside the Flask repository can also build Flask,
without changing that repository. It keeps `uv-purifier.lock.json` beside
the flake and passes it as `lockFile`:

```nix
{
  inputs.uv-purifier.url =
    "github:nix-purifiers/uv-purifier";
  inputs.nixpkgs.follows = "uv-purifier/nixpkgs";
  inputs.flask = {
    url = "github:pallets/flask";
    flake = false;
  };

  outputs =
    {
      nixpkgs,
      uv-purifier,
      flask,
      ...
    }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ uv-purifier.overlays.default ];
      };
      project = pkgs.uv-purifier.buildUvProject {
        src = flask;
        lockFile = ./uv-purifier.lock.json;
      };
    in
    {
      # Flask and its dependencies, with the `flask`
      # command in bin/.
      packages.${system}.default =
        project.packages.flask;
      checks.${system}.tests = project.tests;
    };
}
```

To make that lockfile, run `lock` in a checkout of the same Flask commit
with the path of the packaging flake:

```zsh
nix shell 'github:nix-purifiers/uv-purifier'
uv-purifier lock --lockfile \
  ../flask-packaging/uv-purifier.lock.json
```

## Lifecycle regression

`python3 tests/composition-lifecycle.py` evolves a real project through a
registry dependency bump, registry-to-Nix-to-registry origin changes, dependency
addition/removal and workspace buildable addition/removal. Each state prepares
the editable developer environment, builds in the Nix sandbox and installs the
outgoing projection with ordinary uv using a fresh offline cache. Capture uses
the network; this is not a full host network-namespace isolation test. CI runs
it on both supported Linux architectures.

The lifecycle regression also adds and drops an ordinary registry dependency
while retaining a Nix input, and drops the final dependency without any Nix
inputs. It checks runtime and installed metadata cleanup, unchanged locks and
provided source identity, and preservation of an existing ordinary downstream.
Use `--state-root <directory>` to contain its scratch files and caches.

`tests/composition-epochs.py` separates the helper, native uv and target Python.
It locks a project with a compiled dependency, builds a producer, consumes it as
an explicit Nix input, prepares the interactive environment, builds a consumer
and installs the outgoing projection with ordinary uv in a fresh environment.
Manifests and locks must remain unchanged during prefetch. The report retains
source and target package-set hashes, exact interpreters, manager version,
driver digest, fixture lock digests and individual operation results.

The target-platform observation compares native wheel admission with both the
pure builder and CLI build evaluation: a wheel requiring a newer glibc must be
rejected, and one naming the target's glibc must be admitted. Its payload is
pure Python, so this observation establishes platform admission, without adding
compiled extension ABI or compiler compatibility credit.

```zsh
python3 -B tests/composition-epochs.py \
  --target-nixpkgs /nix/store/PINNED-HISTORICAL-NIXPKGS-source \
  --python-attribute python38 --state-root .python-epochs
```

`--shared-library /path/to/purifier` selects and records an explicit shared
library candidate for dependent review. Passing a candidate does not establish
support with the default shared input. Python 3.8.19 and 3.9.20 are individual
historical target observations; they do not establish all earlier releases,
other Python implementations, extension build toolchains or uv manager epochs.

`--native-uv-epoch uv-0.3.0` additionally selects the pinned package fixture in
`tests/sdk/`. uv 0.3.0 has project lock/install APIs but no build, export or
publish commands; a separate helper supplies those frontends without replacing
the native resolver. The regression checks the environment creator's uv version,
the installed graph and metadata, publication dry-run, and dependency addition
and removal while retaining the Nix input. Initial native lock bytes and supplied
source identity must survive helper operations. Package fixtures are executed
test points, not a version whitelist or an inferred compatibility range.

## Credits

The purifier idea was inspired by nixcloud's
[cargo (libnix)](https://github.com/nixcloud/cargo).
