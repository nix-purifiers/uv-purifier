# SPDX-License-Identifier: MIT
#
# The flake's checks. Every one builds in the sandbox; the only network
# access is the fixed-output fetches of locked files. The checks named
# `conformance-*` are those of the purifier contract
# (github:nix-purifiers/purifier, CONTRACT.md). conformance-relock and
# conformance-rebuild need the network or a Nix of their own, so they are
# jobs of .github/workflows/check.yml.
{
  pkgs,
  cli,
  # The flake's library: `uv-purifier`, with readLock, and `purifier`.
  lib',
  # The flake's `overlays.default`, which adds the scope `uv-purifier`.
  overlay,
}:
let
  inherit (pkgs) lib runCommand;
  inherit (lib') purifier;

  # The package set with the overlay a consumer applies, and its scope.
  withScope = pkgs.extend overlay;
  scope = withScope.uv-purifier;

  # Every fixture builds the way a consumer's package does: through the
  # builder in the scope.
  build = scope.buildUvProject;

  # A consumer's package, written for callPackage, that takes the scope by
  # name.
  consumer = withScope.callPackage (
    { uv-purifier }: (uv-purifier.buildUvProject { src = ./fixtures/simple; }).packages.simple
  ) { };

  # The builder with `uv` replaced through `override`: the replacement
  # reaches the builds.
  overridden =
    (scope.buildUvProject.override { uv = pkgs.hello; } { src = ./fixtures/simple; }).packages.simple;

  # The scope with `cli` replaced through `overrideScope`.
  rescoped = scope.overrideScope (_final: _prev: { cli = pkgs.hello; });

  # A single project built with hatchling. attrs installs from its wheel,
  # docopt, which has only an sdist, is built with the locked setuptools,
  # and markupsafe installs its compiled wheel for the system.
  simple = build { src = ./fixtures/simple; };

  nativeTestTool = pkgs.writeShellScriptBin "purifier-native-test-tool" ''
    python -I -c 'import attrs; assert attrs.__version__ == "25.3.0"'
  '';
  withNativeTestTool = build {
    src = ./fixtures/simple;
    nativeCheckInputs = [ nativeTestTool ];
    testCommand = [ "purifier-native-test-tool" ];
  };

  # A virtual workspace root with two members: greeting (setuptools) and
  # app (flit-core), which depends on greeting and on six. Its test command
  # runs app's script.
  workspace = build {
    src = ./fixtures/workspace;
    testCommand = [ "app" ];
  };

  # A project with a dependency of every kind uv.lock records besides an
  # index: attrs from git (built with hatch-vcs, which reads its version
  # from git), six from a GitHub archive URL, idna from a wheel URL, and
  # packages from a local directory, a local sdist and a local wheel.
  sources = build { src = ./fixtures/sources; };

  # A project that depends on an editable path outside its directory,
  # which the build gets through pathSources.
  outside = build {
    src = ./fixtures/outside/app;
    pathSources."../shared" = ./fixtures/outside/shared;
  };

  # The simple fixture without its purifier lockfile, built from the copy
  # kept outside the source, as a packaging flake builds a project its
  # author does not change.
  external = build {
    src = lib.fileset.toSource {
      root = ./fixtures/simple;
      fileset = lib.fileset.difference ./fixtures/simple ./fixtures/simple/uv-purifier.lock.json;
    };
    lockFile = ./fixtures/simple/uv-purifier.lock.json;
  };

  # Runs `uv-purifier prefetch` on a copy of a fixture, with the inputs Nix
  # built for it, in the sandbox, which has no network, then runs a script
  # of the .venv it installed. `edit` changes a member's source afterwards,
  # and the script runs again: a member installed as editable prints
  # `edited`. pyright, the type checker behind Pylance and the pyright
  # language server, then checks `sources` against the .venv, as an editor
  # would: every import must resolve.
  offline =
    name: fixture: project: script: expected: edit: edited: sources:
    runCommand "uv-purifier-test-offline-${name}"
      {
        nativeBuildInputs = [
          cli
          pkgs.pyright
        ];
      }
      ''
        cp -r ${fixture} project
        chmod -R u+w project
        cd project
        # Python checks its cached bytecode by the modification time in
        # seconds and the size of the source, so the edit right after the
        # first run would go unseen through a cache.
        export HOME=$TMPDIR UV_CACHE_DIR=$TMPDIR/uv-cache PYTHONDONTWRITEBYTECODE=1
        export UV_PYTHON=${project.interactive}/python
        uv-purifier prefetch --inputs ${project.interactive}
        check() {
          output=$(.venv/bin/${script})
          echo "$output"
          if [ "$output" != "$1" ]; then
            echo "expected: $1" >&2
            exit 1
          fi
        }
        check ${lib.escapeShellArg expected}
        ${edit}
        check ${lib.escapeShellArg edited}
        pyright --pythonpath .venv/bin/python ${lib.escapeShellArgs sources}
        touch $out
      '';

  # Runs a script and compares what it prints.
  runs =
    name: script: expected:
    runCommand "uv-purifier-test-${name}" { } ''
      output=$(${script})
      echo "$output"
      if [ "$output" != ${lib.escapeShellArg expected} ]; then
        echo "expected: "${lib.escapeShellArg expected} >&2
        exit 1
      fi
      touch $out
    '';

  negative = import ./negative.nix { inherit pkgs; };
in
{
  composition-producer = simple.packages.simple;
  native-check-inputs =
    assert withNativeTestTool.packages.simple.drvPath == simple.packages.simple.drvPath;
    assert withNativeTestTool.release.drvPath == simple.release.drvPath;
    assert withNativeTestTool.interactive.drvPath == simple.interactive.drvPath;
    withNativeTestTool.tests;
  composition-producer-python312 =
    (build {
      src = ./fixtures/simple;
      python = pkgs.python312;
    }).packages.simple;

  simple =
    runs "simple" simple.packages.simple.scripts.simple
      "simple attrs=25.3.0 docopt=0.6.2 markupsafe=3.0.3 &lt;nix&gt;";

  runtime-projections =
    runCommand "uv-runtime-projection-downstreams"
      {
        nativeBuildInputs = [
          pkgs.uv
          pkgs.python3
        ];
      }
      ''
        export HOME=$TMPDIR UV_OFFLINE=1 UV_NO_INDEX=1 UV_PYTHON_DOWNLOADS=never
        uv venv --python ${pkgs.python3.interpreter} simple-env
        uv pip install --python simple-env/bin/python \
          --find-links ${simple.packages.simple.projections.python} simple
        output=$(simple-env/bin/simple)
        test "$output" = 'simple attrs=25.3.0 docopt=0.6.2 markupsafe=3.0.3 &lt;nix&gt;'
        uv venv --python ${pkgs.python3.interpreter} workspace-env
        uv pip install --python workspace-env/bin/python \
          --find-links ${workspace.packages.app.projections.python} app
        test "$(workspace-env/bin/app)" = 'hello, nix six=1.17.0'
        workspace-env/bin/python - <<'PYTHON'
        from importlib.metadata import version
        assert '+nix.' in version('app')
        assert '+nix.' in version('greeting')
        assert version('six') == '1.17.0'
        PYTHON
        touch $out
      '';

  # The simple fixture built with nixpkgs' Python and its attrs among the
  # build inputs, whose setup hook puts that attrs on PYTHONPATH. The build
  # still installs the locked attrs, which the script imports with no
  # PYTHONPATH.
  pythonpath =
    runs "pythonpath"
      (simple.packages.simple.overrideAttrs (old: {
        nativeBuildInputs = old.nativeBuildInputs ++ [
          pkgs.python3
          pkgs.python3Packages.attrs
        ];
        # The test means something only while the setup hook sets it.
        preBuild = ''
          echo "PYTHONPATH=$PYTHONPATH"
          case "''${PYTHONPATH:-}" in
            *${pkgs.python3Packages.attrs}*) ;;
            *) echo "the setup hook did not put nixpkgs' attrs on PYTHONPATH" >&2; exit 1 ;;
          esac
        '';
      })).scripts.simple
      "simple attrs=25.3.0 docopt=0.6.2 markupsafe=3.0.3 &lt;nix&gt;";

  # The builder lives in the scope `uv-purifier` of the package set. A
  # package in the package set takes the scope by name,
  # `buildUvProject.override` replaces a package the builder takes, and
  # `overrideScope` replaces what the scope shares.
  overlay =
    let
      inScope = scope ? buildUvProject && !(withScope ? buildUvProject);
      overrideReaches = lib.elem pkgs.hello overridden.nativeBuildInputs;
      scopeOverridden = rescoped.cli == pkgs.hello && rescoped ? buildUvProject;
    in
    if !inScope then
      throw "buildUvProject is not in the scope uv-purifier alone"
    else if !overrideReaches then
      throw "buildUvProject.override { uv = ...; } did not reach the build of the package"
    else if !scopeOverridden then
      throw "uv-purifier.overrideScope did not replace what the scope shares"
    else
      runs "overlay" consumer.scripts.simple
        "simple attrs=25.3.0 docopt=0.6.2 markupsafe=3.0.3 &lt;nix&gt;";

  # The package of the virtual root has app, its scripts and every member
  # app depends on.
  workspace = runs "workspace" workspace.packages.workspace.scripts.app "hello, nix six=1.17.0";

  sources =
    runs "sources" sources.packages.sources.scripts.sources
      "attrs=25.3.0 six=1.17.0 idna=3.10 localdir 0.3.0 localsdist 0.4.0 localwheel 0.5.0";

  outside =
    runs "outside" outside.packages.outside-app.scripts.outside-app
      "outside-app with shared 0.2.0";

  # The release is what `uv build` makes for each member: its sdist and
  # its wheel.
  release = runCommand "uv-purifier-test-release" { } ''
    ls ${workspace.release}
    ${lib.concatMapStrings (file: ''
      test -f ${workspace.release.artifacts.${file}}
      test -e ${workspace.release}/${file}
    '') (builtins.attrNames workspace.release.artifacts)}
    touch $out
  '';

  # The project's test command, in its development environment.
  tests = workspace.tests;

  # Builds of the simple fixture that the conformance-rebuild job of CI
  # builds twice with `nix build --rebuild`: the sdist and wheel of its
  # member, and its environment.
  rebuild-release = simple.release.members.simple;
  rebuild-package = simple.packages.simple;

  # The download entries the build fetches, for SBOM, license and
  # vulnerability tools: a file entry per locked file, and a git entry for
  # the checkout.
  fetches =
    let
      kinds = map (
        e:
        if e ? git then
          "git"
        else if e ? url then
          "file"
        else
          "other"
      ) sources.fetches;
    in
    assert lib.elem "git" kinds && lib.elem "file" kinds && !lib.elem "other" kinds;
    runCommand "uv-purifier-test-fetches" { } "touch $out";

  conformance-offline =
    offline "sources" ./fixtures/sources sources "sources"
      "attrs=25.3.0 six=1.17.0 idna=3.10 localdir 0.3.0 localsdist 0.4.0 localwheel 0.5.0"
      "sed -i 's/attrs=/attrs: /' src/sources/__init__.py"
      "attrs: 25.3.0 six=1.17.0 idna=3.10 localdir 0.3.0 localsdist 0.4.0 localwheel 0.5.0"
      [ "src" ];

  offline-workspace =
    offline "workspace" ./fixtures/workspace workspace "app" "hello, nix six=1.17.0"
      "sed -i 's/hello/greetings/' packages/greeting/src/greeting/__init__.py"
      "greetings, nix six=1.17.0"
      [ "packages" ];

  # The external build, and the same build from a source whose uv.lock is
  # not valid, with the fixture's uv.lock passed from outside the source.
  conformance-external =
    let
      expected = "simple attrs=25.3.0 docopt=0.6.2 markupsafe=3.0.3 &lt;nix&gt;";
      replaced = build {
        src = runCommand "simple-with-a-broken-uv-lock" { } ''
          cp -r ${./fixtures/simple} $out
          chmod -R u+w $out
          rm $out/uv-purifier.lock.json
          echo 'not a uv.lock' > $out/uv.lock
        '';
        lockFile = ./fixtures/simple/uv-purifier.lock.json;
        uvLock = ./fixtures/simple/uv.lock;
      };
    in
    runCommand "uv-purifier-test-conformance-external" { } ''
      test -e ${runs "external" external.packages.simple.scripts.simple expected}
      test -e ${runs "external-uv-lock" replaced.packages.simple.scripts.simple expected}
      touch $out
    '';

  inherit (negative)
    conformance-stale
    conformance-tampered
    conformance-missing
    conformance-malformed
    ;

  nix-library = import ./nix-library.nix {
    inherit pkgs purifier;
    inherit (lib'.uv-purifier) readLock;
  };

  flask = import ./real-world/flask {
    inherit pkgs cli;
    inherit (scope) buildUvProject;
  };

  # The unit tests of the lock tool and of split_requirements.py.
  unit =
    runCommand "uv-purifier-test-unit"
      {
        src = lib.fileset.toSource {
          root = ../.;
          fileset = lib.fileset.unions [
            ../src
            ../pkgs/build-uv-project
            ./unit
            ./fixtures/simple
          ];
        };
        nativeBuildInputs = [
          (pkgs.python3.withPackages (ps: [ ps.packaging ]))
          pkgs.git
        ];
      }
      ''
        cd $src
        python3 -m unittest discover -s tests/unit -v
        touch $out
      '';

  # The outputs of a project are known by name when Nix evaluates, from
  # uv-purifier.lock.json: reading their names builds nothing.
  outputs =
    let
      found = {
        release = builtins.attrNames workspace.release.artifacts;
        workspaceScripts = builtins.attrNames workspace.packages.workspace.scripts;
        simpleScripts = builtins.attrNames simple.packages.simple.scripts;
        packages = builtins.attrNames workspace.packages;
      };
      expected = {
        release = [
          "app-2.0.0-py3-none-any.whl"
          "app-2.0.0.tar.gz"
          "greeting-1.0.0-py3-none-any.whl"
          "greeting-1.0.0.tar.gz"
        ];
        workspaceScripts = [ "app" ];
        simpleScripts = [ "simple" ];
        packages = [
          "app"
          "greeting"
          "workspace"
        ];
      };
      failures = lib.filter (name: found.${name} != expected.${name}) (lib.attrNames expected);
    in
    runCommand "uv-purifier-test-outputs" { } (
      if failures == [ ] then
        "touch $out"
      else
        ''
          echo "unexpected outputs: "${lib.escapeShellArg (builtins.toJSON (lib.getAttrs failures found))} >&2
          exit 1
        ''
    );

  cli = runCommand "uv-purifier-test-cli" { } ''
    ${lib.getExe cli} --help
    ${lib.getExe cli} lock --help
    ${lib.getExe cli} prefetch --help
    ${lib.getExe cli} publish --help
    touch $out
  '';
}
// lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
  native-runtime = import ./native-runtime.nix { inherit pkgs; };
}
