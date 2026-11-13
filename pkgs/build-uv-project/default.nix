# SPDX-License-Identifier: MIT
#
# `buildUvProject`: builds a uv project in the Nix sandbox from its two
# lockfiles, as the purifier contract (github:nix-purifiers/purifier,
# CONTRACT.md) describes.
#
# The file is a package for callPackage. The first argument set holds what
# it takes from the package set, and the result is the builder. The
# uv-purifier overlay (`overlays.default`) adds it to the package set as
# `buildUvProject`, so a package written for callPackage takes it by name:
#
#   { buildUvProject }: (buildUvProject { src = ./.; }).packages.app
#
# `buildUvProject.override` replaces what it takes from the package set.
#
# uv.lock pins the project's dependencies, and uv-purifier.lock.json pins
# the build requirements of the workspace members and of the dependencies
# that may have to be built from their sdists. For the system and Python it
# builds with, this function picks the locked files that the build needs:
# for each dependency, its wheels that install, or else its sdist and the
# build requirements that building it needs. It links them into one
# directory, the wheelhouse, and runs uv with no network and no index, so
# that uv reads nothing but the wheelhouse.
#
# The result has:
# - `packages.<member>`: for each workspace member, a virtual environment
#   with the member and its runtime dependencies installed, and the
#   member's scripts in `bin/`, by name in `.scripts`.
# - `tests`: the project's test command, run in a development environment
#   (the default dependency groups and every member) on a copy of `src`.
# - `release`: the sdist and wheel of every member with a build, which
#   `uv-purifier publish` uploads, by file name in `.artifacts`.
# - `fetches`: the download entries of the shared library that the build
#   fetches for this system.
# - `interactive`: what `uv-purifier prefetch` installs the project's .venv
#   from.
{
  lib,
  stdenv,
  patchelf,
  zlib,
  linkFarm,
  runCommand,
  writeText,
  unzip,
  zstd,
  uv,
  nativeUv ? uv,
  glibc,
  python3,
  fetchurl,
  fetchgit,
  # The shared library of the nix-purifiers (github:nix-purifiers/purifier),
  # which checks the lock, fetches download entries and holds the wheel
  # helpers. The overlay passes the library that uv-purifier is pinned to.
  purifier,
}:
let
  defaultNativeUv = nativeUv;
in
{
  # The project root, which holds uv.lock, the root pyproject.toml and, unless
  # `lockFile` names another path, uv-purifier.lock.json.
  src,
  # The fetchers of the shared library's fetchEntry and tree, such as a
  # `fetchurl` that sends credentials or uses a company mirror.
  fetchers ? { },
  # The Python the project builds and runs with. It must satisfy the
  # project's requires-python.
  python ? python3,
  # Project manager used for installation; host `uv` handles build/export APIs.
  nativeUv ? defaultNativeUv,
  # The purifier lockfile. A file outside `src` replaces any copy in it,
  # so a packaging flake can build a project its author does not change.
  lockFile ? src + "/uv-purifier.lock.json",
  # uv.lock. A file outside `src` replaces the copy in it, so a packaging
  # flake can build a project whose committed uv.lock is missing or stale.
  uvLock ? src + "/uv.lock",
  # The local packages that uv.lock names by a path outside `src`, such as
  # "../shared", each mapped to a Nix path that holds it. A path inside
  # `src` is read from `src`.
  pathSources ? { },
  projectDependencies ? null,
  # The command `tests` runs in the development environment, from a copy of
  # `src`.
  testCommand ? [ "pytest" ],
  # Native commands needed by tests, independently of Python dependencies
  # selected by the project's native uv lock and dependency groups.
  nativeCheckInputs ? [ ],
  # Native tools required by PEP 517 source builds, such as Rust or C
  # compilers. Python build requirements remain selected by the lock.
  nativeBuildInputs ? [ ],
  # Additional locked dependency groups used by the development/test
  # environment, matching `uv-purifier prefetch --group`.
  dependencyGroups ? [ ],
  # Native uv group/extra selection for development and tests only.
  developmentSelection ? { },
  # Standard manylinux wheels may depend on compiler runtime libraries
  # supplied by the host distribution rather than bundled in the wheel.
  runtimeLibraries ? lib.optionals stdenv.hostPlatform.isLinux [
    stdenv.cc.cc.lib
    zlib
  ],
}:
let
  inherit (builtins)
    concatMap
    elemAt
    filter
    genericClosure
    listToAttrs
    map
    ;

  # The lock in memory of the purifier contract, checked: evaluation fails
  # here with the problems of a stale, edited or incomplete lockfile.
  locked = purifier.checkLock (
    import ../../lib-overlays/default/read-lock.nix { inherit lib; } { inherit src lockFile uvLock; }
  );

  # The details of the lockfiles that the builds need beyond the lock in
  # memory: which packages build, and with which build requirements.
  sourceLock = import ../../lib-overlays/default/lock.nix { inherit src lockFile uvLock; };
  platformGraph = import ./platform-packages.nix { inherit lib; } {
    uvLock = builtins.fromTOML (builtins.readFile uvLock);
    memberKeys = map (member: member.key) sourceLock.members;
    # OS facts follow the build platform. Other predicates remain unknown
    # here; the selected SDK evaluates full markers when uv installs.
    environment =
      lib.optionalAttrs stdenv.hostPlatform.isLinux {
        sys_platform = "linux";
        os_name = "posix";
        platform_system = "Linux";
      }
      // lib.optionalAttrs stdenv.hostPlatform.isDarwin {
        sys_platform = "darwin";
        os_name = "posix";
        platform_system = "Darwin";
      };
  };
  # Local adoption already has the selected interpreter's probed active
  # graph. Never substitute the Nix host's OS facts for that interpreter.
  lock =
    sourceLock
    // lib.optionalAttrs (!(python ? activePackages)) {
      distributions = filter (
        package: builtins.hasAttr package.key platformGraph.keys
      ) sourceLock.distributions;
      direct = filter (package: builtins.hasAttr package.key platformGraph.keys) sourceLock.direct;
    };

  inputRecords = lock.purifier.projectInputs or [ ];
  dependencies =
    if projectDependencies == null then
      purifier.lockedProjectInputs {
        inputs = inputRecords;
        system = stdenv.hostPlatform.system;
      }
    else
      projectDependencies;
  supplied = purifier.projectInputs {
    inherit dependencies;
    formats = [ "python" ];
  };
  hasInputs = supplied != [ ];
  oldRepositories = map (input: input.nativeRepository) inputRecords;
  isProvided =
    distribution:
    lib.any (repository: lib.hasPrefix (repository + "/") distribution.value) oldRepositories;
  inputViews = writeText "uv-python-inputs.json" (
    builtins.toJSON (
      map (input: {
        repository = toString input.repository;
        catalog = input.repository.catalog;
      }) supplied
    )
  );
  oldRepositoryFile = writeText "uv-python-original-repositories.json" (
    builtins.toJSON oldRepositories
  );
  # Metadata conversion runs on the helper interpreter, independently of the
  # project runtime. Wheel compatibility is still queried from `python`.
  inputPython = python3.withPackages (ps: [
    ps.packaging
    ps.tomli-w
  ]);

  # The downloads of this system, by file name.
  systemFiles = listToAttrs (
    map (entry: {
      name = entry.path;
      value = entry;
    }) (purifier.entriesFor stdenv.hostPlatform.system locked)
  );

  # The source the builds run in: `src` itself, or a copy of it with the
  # lockfiles given outside it.
  buildSrc =
    if hasInputs then
      runCommand "${baseNameOf (toString src)}-project-inputs" { nativeBuildInputs = [ inputPython ]; } ''
        cp -r ${src} "$out"
        chmod -R u+w "$out"
        cp ${lockFile} "$out/uv-purifier.lock.json"
        cp --no-preserve=mode ${uvLock} "$out/uv.lock"
        mkdir -p "$out/.uv-purifier-inputs"
        cp --no-preserve=mode ${directIndexBase} "$out/.uv-purifier-inputs/direct.json"
        PYTHONPATH=${purifier.projectInputsTool} python ${./python_inputs.py} --python ${python.interpreter} --project "$out" --views ${inputViews} \
          --old-repositories ${oldRepositoryFile} \
          --index "$out/.uv-purifier-inputs/direct.json" \
          --wheelhouse "$out/.uv-purifier-inputs/wheels"
      ''
    else if lockFile == src + "/uv-purifier.lock.json" && uvLock == src + "/uv.lock" then
      src
    else
      runCommand "${baseNameOf (toString src)}-locked" { } ''
        cp -r ${src} $out
        chmod -R u+w $out
        cp ${lockFile} $out/uv-purifier.lock.json
        cp ${uvLock} $out/uv.lock
      '';

  # The fetchers of the shared library and the packages that the steps
  # after a fetch build with.
  fetchArgs = {
    inherit
      fetchurl
      fetchgit
      runCommand
      writeText
      unzip
      zstd
      python3
      ;
  }
  // fetchers;
  fetch = purifier.fetchEntry fetchArgs;

  platform = stdenv.hostPlatform;
  target = "Python ${python.pythonVersion} on ${platform.system}";
  wheelTarget = {
    inherit (python) pythonVersion;
  }
  //
    lib.optionalAttrs
      (python.freeThreaded
        or (lib.any (tag: builtins.match "cp[0-9]+t" tag != null) (python.pythonABITags or [ ]))
      )
      {
        freeThreaded = true;
      }
  // (
    if platform.isDarwin then
      {
        # macOS wheel tags name the CPU arm64, and the oldest macOS that a
        # wheel runs on, which is at most nixpkgs' darwinMinVersion.
        arch = "arm64";
        macosVersion = platform.darwinMinVersion;
      }
    else
      {
        arch = platform.parsed.cpu.name;
        # A historical Python can use an older libc than the helper package set.
        # CLI-selected immutable interpreters carry their probed runtime version.
        glibcVersion = python.glibcVersion or (python.stdenv.cc.libc.version or glibc.version);
      }
  );
  # Local adoption probes the user's interpreter through the existing tag
  # worker. These are its actual ABI/platform tags, not a nixpkgs SDK choice.
  installs =
    if python ? wheelTags then
      let
        compatible = lib.genAttrs python.wheelTags (_: true);
      in
      name:
      let
        parts = lib.splitString "-" (lib.removeSuffix ".whl" name);
        count = builtins.length parts;
      in
      count >= 5
      && lib.any (
        implementation:
        lib.any (
          abi:
          lib.any (platform: compatible ? ${implementation + "-" + abi + "-" + platform}) (
            lib.splitString "." (elemAt parts (count - 1))
          )
        ) (lib.splitString "." (elemAt parts (count - 2)))
      ) (lib.splitString "." (elemAt parts (count - 3)))
    else
      purifier.wheels wheelTarget;

  # What installing a locked distribution needs on this system: its wheels
  # that install, or else its sdist and a build of it.
  needs =
    distribution:
    let
      wheels = filter installs distribution.wheels;
    in
    if wheels != [ ] then
      {
        files = wheels;
        builds = [ ];
      }
    else if distribution.sdist != null then
      {
        files = [ distribution.sdist ];
        builds = [ distribution.key ];
      }
    else
      throw "uv-purifier: no wheel of ${distribution.key} installs with ${target}, and it has no sdist";

  # A package of a set of build requirements, in the shape of `needs`.
  requirementDistribution = package: {
    key = "${package.name}==${package.version}";
    inherit (package) wheels;
    sdist = package.sdist or null;
  };

  requirementSet =
    key:
    let
      set = elemAt lock.buildRequirements (
        lock.builds.${key}
          or (throw "uv-purifier: uv-purifier.lock.json has no build requirements for ${key}, which ${target} has to build. Run `uv-purifier lock`.")
      );
    in
    if set ? error then
      throw "uv-purifier: building ${key} with ${target} needs the build requirements ${lib.concatStringsSep ", " set.requires}, which `uv-purifier lock` could not resolve:\n${set.error}"
    else
      set;

  requirementNeeds = key: map (p: needs (requirementDistribution p)) (requirementSet key).packages;

  # The builds that some builds need, with the builds that their build
  # requirements need in turn.
  buildClosure =
    keys:
    map (item: item.key) (genericClosure {
      startSet = map (key: { inherit key; }) keys;
      operator = item: map (key: { inherit key; }) (concatMap (n: n.builds) (requirementNeeds item.key));
    });

  dependencyNeeds = map needs lock.distributions;
  builtMembers = filter (m: m.kind != "virtual") lock.members;
  # A package from git, a URL or a local path is built from its source,
  # unless it is a wheel.
  builtDirect = filter (d: d.wheel == null) lock.direct;
  builds = buildClosure (
    map (m: m.key) builtMembers ++ map (d: d.key) builtDirect ++ concatMap (n: n.builds) dependencyNeeds
  );

  fileNames = lib.unique (
    concatMap (n: n.files) dependencyNeeds
    ++ concatMap (key: concatMap (n: n.files) (requirementNeeds key)) builds
  );

  # Every chosen file, as the download entry of this system at its file
  # name.
  wheelhouseEntries = map (
    name:
    systemFiles.${name}
      or (throw "uv-purifier: the lock has no download of ${name} for ${stdenv.hostPlatform.system}. Run `uv-purifier lock`.")
  ) fileNames;

  wheelhouse = purifier.tree (
    fetchArgs
    // {
      name = "uv-purifier-wheelhouse";
      entries = wheelhouseEntries;
    }
  );

  # A file entry of the shared library for a URL and hash of uv.lock.
  fileEntry = url: hash: {
    path = lock.fileName url;
    inherit url hash;
    mirrors = [ ];
  };

  # uv reads the wheelhouse and nothing else, with the Python of the build.
  uvEnv = {
    UV_OFFLINE = "1";
    UV_NO_INDEX = "1";
    UV_FIND_LINKS = "${wheelhouse}";
    UV_PYTHON = python.interpreter;
    UV_PYTHON_DOWNLOADS = "never";
    UV_LINK_MODE = "copy";
    UV_NO_PROGRESS = "1";
  };
  uvSetup = ''
    export HOME=$TMPDIR UV_CACHE_DIR=$TMPDIR/uv-cache
    export CARGO_BUILD_JOBS=$NIX_BUILD_CORES
  '';
  prepareRuntime = lib.optionalString platform.isLinux ''
    ${python3.interpreter} -I ${./prepare-runtime.py} "$out" \
      --patchelf ${patchelf}/bin/patchelf \
      --interpreter "$(${patchelf}/bin/patchelf --print-interpreter ${python.interpreter})" \
      ${lib.escapeShellArgs (
        lib.concatMap (library: [
          "--library-dir"
          "${lib.getLib library}/lib"
        ]) runtimeLibraries
      )}
  '';
  # `uv build --out-dir` also writes a .gitignore into the directory, which
  # is no distribution.
  uvBuild =
    key: args:
    let
      packages = (requirementSet key).packages;
      backend = filter (package: package.name == "uv-build") packages;
      constraints = writeText "uv-build-constraints.txt" (
        lib.concatMapStrings (package: "uv-build==${package.version}\n") backend
      );
    in
    ''
      ${lib.optionalString (backend != [ ]) ''
        backendEnv="$TMPDIR/uv-backend"
        ${uv}/bin/uv venv --no-python-downloads --python ${python.interpreter} "$backendEnv"
        ${uv}/bin/uv pip install --python "$backendEnv/bin/python" --no-deps \
          ${lib.escapeShellArgs (map (package: "${package.name}==${package.version}") packages)}
        (
          out="$backendEnv"
          ${prepareRuntime}
        )
      ''}
      ${uv}/bin/uv build --force-pep517 ${
        lib.optionalString (
          backend != [ ]
        ) ''--no-build-isolation --python "$backendEnv/bin/python" --build-constraints ${constraints}''
      } ${args} --out-dir $out
      rm -f $out/.gitignore
    '';

  membersByName = listToAttrs (
    map (m: {
      inherit (m) name;
      value = m;
    }) lock.members
  );

  memberVersion =
    m:
    let
      version = lib.removePrefix "${m.name}==" m.key;
    in
    if m.kind == "virtual" then
      (if version == "" then "0" else version)
    else
      builtins.elemAt (lib.splitString "-" (outputsOf m).wheel) 1;

  # A local path of uv.lock, as a Nix path.
  localPath =
    path:
    if lock.insideProject path then
      src + "/${path}"
    else
      pathSources.${path}
        or (throw "uv-purifier: uv.lock names the local path ${path}, which is outside `src`. Pass it in `pathSources`, as `pathSources.\"${path}\" = <a Nix path>;`.");

  # The source a direct package builds from: a source tree, or an archive
  # that the build unpacks.
  directSource =
    d:
    if d.kind == "git" then
      let
        checkout = fetch d.git;
      in
      if d.subdirectory == null then checkout else "${checkout}/${d.subdirectory}"
    else if d.kind == "url" then
      fetch (fileEntry d.value d.sdistHash)
    else
      localPath d.value;

  # A wheel of each direct package, in a directory of its own.
  directWheels = listToAttrs (
    map (d: {
      inherit (d) name;
      value =
        if isProvided d then
          "${buildSrc}/.uv-purifier-inputs/wheels/${d.name}"
        else if d.wheel != null then
          linkFarm "${d.name}-wheel" [
            {
              inherit (d.wheel) name;
              path =
                if d.wheel.url != null then fetch (fileEntry d.wheel.url d.wheel.hash) else localPath d.value;
            }
          ]
        else
          stdenv.mkDerivation {
            pname = "${d.name}-wheel";
            inherit (d) version;
            src = directSource d;
            nativeBuildInputs = [
              uv
              unzip
            ]
            ++ nativeBuildInputs;
            # A project that reads its version from git, through
            # setuptools-scm or hatch-vcs, finds no repository in the
            # checkout and takes the version uv.lock records.
            env = uvEnv // {
              SETUPTOOLS_SCM_PRETEND_VERSION = d.version;
            };
            dontConfigure = true;
            buildPhase = ''
              runHook preBuild
              ${uvSetup}
              ${uvBuild d.key "--wheel ."}
              runHook postBuild
            '';
            dontInstall = true;
            dontFixup = true;
          };
    }) lock.direct
  );

  # Retain wheels for native runtime packages available only as sdists.
  # Their original source archives and build requirements remain locked.
  indexWheels = listToAttrs (
    map (distribution: {
      name = distribution.key;
      value = stdenv.mkDerivation {
        pname = "${distribution.name}-runtime-wheel";
        version = lib.removePrefix "${distribution.name}==" distribution.key;
        src = fetch systemFiles.${distribution.sdist};
        nativeBuildInputs = [ uv ] ++ nativeBuildInputs;
        env = uvEnv;
        dontConfigure = true;
        buildPhase = ''
          runHook preBuild
          ${uvSetup}
          ${uvBuild distribution.key "--wheel ."}
          runHook postBuild
        '';
        dontInstall = true;
        dontFixup = true;
      };
    }) (filter (distribution: (needs distribution).builds != [ ]) lock.distributions)
  );

  # What split_requirements.py needs to swap the direct packages of an
  # exported requirements file for their wheels: each package from git or a
  # URL by its name, and each local package by its path.
  directIndex = if hasInputs then "${buildSrc}/.uv-purifier-inputs/direct.json" else directIndexBase;
  directIndexBase = makeDirectIndex false;
  interactiveDirectIndex = if hasInputs then directIndex else makeDirectIndex true;
  makeDirectIndex =
    editableSources:
    writeText "uv-purifier-direct.json" (
      builtins.toJSON {
        editablePaths = map (d: d.value) (
          filter (d: editableSources && !(isProvided d) && d.kind == "editable") lock.direct
        );
        names = listToAttrs (
          map (d: {
            inherit (d) name;
            value = "${directWheels.${d.name}}";
          }) (filter (d: !(isProvided d) && (d.kind == "git" || d.kind == "url")) lock.direct)
        );
        paths = listToAttrs (
          map
            (d: {
              name = if lib.hasPrefix "/" d.value || lib.hasPrefix "." d.value then d.value else "./${d.value}";
              value = "${directWheels.${d.name}}";
            })
            (
              filter (
                d:
                !(isProvided d) && d.kind != "git" && d.kind != "url" && (!editableSources || d.kind != "editable")
              ) lock.direct
            )
        );
      }
    );

  # What a member's build produces, from the lock in memory.
  outputsOf = m: locked.outputs.${m.name};

  # The file name of a member's wheel for this system and Python. The lock
  # run built the wheel on its own system, so a compiled wheel's tags are
  # rewritten for this one; a pure wheel keeps its name. The library's
  # wheelName knows only Linux platform tags, so on macOS only a pure wheel
  # has a name.
  wheelName =
    if platform.isDarwin then
      name:
      if lib.hasSuffix "-any.whl" name then
        name
      else
        throw "uv-purifier: the lock run built ${name}, a wheel with compiled code, and uv-purifier can name the wheel that such a member builds only on Linux, not with ${target}"
    else
      purifier.wheelName wheelTarget;

  distributionFiles =
    m:
    let
      outputs = outputsOf m;
    in
    [
      outputs.sdist
      (wheelName outputs.wheel)
    ];

  # What `uv build` makes for each member with a build: its sdist, and its
  # wheel built from the sdist.
  distributions = listToAttrs (
    map (m: {
      inherit (m) name;
      value = stdenv.mkDerivation {
        pname = "${m.name}-distributions";
        version = memberVersion m;
        src = buildSrc;
        nativeBuildInputs = [ uv ] ++ nativeBuildInputs;
        env = uvEnv // {
          # A source snapshot has no Git metadata. Use the native version
          # captured in this member's wheel, including dynamic uv projects.
          SETUPTOOLS_SCM_PRETEND_VERSION = memberVersion m;
        };
        dontConfigure = true;
        buildPhase = ''
          runHook preBuild
          ${uvSetup}
          ${lib.optionalString hasInputs ''
            cp ${src}/pyproject.toml pyproject.toml
            rm -rf .uv-purifier-inputs
          ''}
          ${uvBuild m.key "--package ${lib.escapeShellArg m.name}"}
          for file in ${lib.escapeShellArgs (distributionFiles m)}; do
            if [ ! -f "$out/$file" ]; then
              echo "uv-purifier: the build of ${m.name} made $(ls $out), not $file as uv-purifier.lock.json records. Run \`uv-purifier lock\`." >&2
              exit 1
            fi
          done
          runHook postBuild
        '';
        dontInstall = true;
        dontFixup = true;
      };
    }) builtMembers
  );

  # A member with the members it depends on, transitively.
  memberClosure =
    name:
    map (item: item.key) (genericClosure {
      startSet = [ { key = name; } ];
      operator =
        item:
        map (key: { inherit key; }) (
          filter (d: membersByName ? ${d}) membersByName.${item.key}.dependencies
        );
    });

  # Installs into the virtual environment $out the requirements that
  # `uv export` writes with `exportArgs`, the direct packages among them
  # from their wheels, and the wheels of `members`.
  installEnvironment = exportArgs: members: ''
    ${uvSetup}
    sha256sum uv.lock > "$TMPDIR/native-lock.sha256"
    ${uv}/bin/uv export --offline --frozen ${exportArgs} --no-emit-workspace \
      --format requirements.txt --output-file "$TMPDIR/exported.txt"
    sha256sum --check "$TMPDIR/native-lock.sha256"
    ${inputPython.interpreter} -I ${./split_requirements.py} "$TMPDIR/exported.txt" ${directIndex} \
      "$TMPDIR/requirements.txt" "$TMPDIR/direct-wheels.txt" ${python.interpreter}
    uv venv --no-python-downloads --python ${python.interpreter} "$out"
    # uv export already contains the complete locked graph. Resolve no
    # transitive dependencies here: registry packages can depend on a git
    # package whose locked wheel is installed in the following phase.
    uv --no-config pip install --offline --no-index --find-links ${wheelhouse} --python "$out/bin/python" --no-deps --require-hashes -r "$TMPDIR/requirements.txt"
    while read -r wheels; do
      uv --no-config pip install --offline --no-index --python "$out/bin/python" --no-deps "$wheels"/*.whl
    done < "$TMPDIR/direct-wheels.txt"
    ${lib.concatMapStrings (name: ''
      uv --no-config pip install --offline --no-index --python "$out/bin/python" --no-deps ${distributions.${name}}/*.whl
    '') (filter (name: distributions ? ${name}) members)}
    ${prepareRuntime}
  '';

  publicationSources =
    name:
    map (member: distributions.${member}) (
      filter (member: distributions ? ${member}) (memberClosure name)
    );
  publicationRepository =
    name:
    runCommand "${name}-publication-wheels" { } ''
      mkdir -p "$out"
      ${lib.concatMapStrings (source: ''
        for wheel in ${source}/*.whl; do
          ln -s "$wheel" "$out/$(basename "$wheel")"
        done
      '') (publicationSources name)}
    '';
  runtimeRepository =
    name: environment:
    runCommand "${name}-runtime-wheels"
      { nativeBuildInputs = [ (python3.withPackages (ps: [ ps.packaging ])) ]; }
      ''
        python ${./export-runtime.py} --environment ${environment} --destination "$out" \
          ${lib.concatMapStringsSep " " (source: "--own ${source}") (publicationSources name)} \
          ${lib.concatMapStringsSep " " (source: "--candidate ${source}") (
            [ wheelhouse ]
            ++ builtins.attrValues directWheels
            ++ builtins.attrValues indexWheels
            ++ map (input: input.repository) supplied
          )}
        mkdir -p "$out/nix-support"
        printf '%s\n' ${environment} > "$out/nix-support/inputs"
      '';

  packages = listToAttrs (
    map (m: {
      inherit (m) name;
      value = stdenv.mkDerivation (finalAttrs: {
        pname = m.name;
        version = memberVersion m;
        src = buildSrc;
        nativeBuildInputs = [ nativeUv ] ++ nativeBuildInputs;
        env = uvEnv;
        dontConfigure = true;
        buildPhase = ''
          runHook preBuild
          ${installEnvironment "--no-default-groups --package ${lib.escapeShellArg m.name}" (
            memberClosure m.name
          )}
          runHook postBuild
        '';
        # The scripts of the member and of the members it depends on, by
        # name, known when Nix evaluates.
        passthru.projections = purifier.pythonProjections {
          repository = publicationRepository m.name;
          producer = distributions.${m.name} or finalAttrs.finalPackage;
          runtimeRepository = runtimeRepository m.name finalAttrs.finalPackage;
          dependencies = dependencies;
          inherit runCommand python3;
        };
        passthru.scripts = lib.genAttrs (lib.concatMap (name: (outputsOf membersByName.${name}).scripts) (
          filter (name: distributions ? ${name}) (memberClosure m.name)
        )) (script: "${finalAttrs.finalPackage}/bin/${script}");
        postBuild = ''
          for script in ${lib.escapeShellArgs (builtins.attrNames finalAttrs.passthru.scripts)}; do
            if [ ! -x "$out/bin/$script" ]; then
              echo "uv-purifier: the environment of ${m.name} has no script $script, which uv-purifier.lock.json records. Run \`uv-purifier lock\`." >&2
              exit 1
            fi
          done
        '';
        dontInstall = true;
        # Installation already prepares runtime paths and RECORD hashes.
        # A later shrink/strip pass changes wheel bytes and loader semantics.
        dontFixup = true;
      });
    }) lock.members
  );

  projectName =
    let
      roots = filter (m: m.path == ".") lock.members;
    in
    if roots != [ ] then (lib.head roots).name else "uv-project";

  developmentArgs =
    (
      {
        groups ? [ ],
        allGroups ? false,
        excludeGroups ? [ ],
        extras ? [ ],
        allExtras ? false,
      }:
      [ "--all-packages" ]
      ++ lib.optionals allGroups [ "--all-groups" ]
      ++ concatMap (group: [
        "--group"
        group
      ]) (dependencyGroups ++ groups)
      ++ concatMap (group: [
        "--no-group"
        group
      ]) excludeGroups
      ++ lib.optionals allExtras [ "--all-extras" ]
      ++ concatMap (extra: [
        "--extra"
        extra
      ]) extras
    )
      developmentSelection;

  # Every member with the selected dependency groups, as `uv sync` installs
  # a project for development.
  development = stdenv.mkDerivation {
    pname = "${projectName}-development";
    version = "0";
    src = buildSrc;
    nativeBuildInputs = [ nativeUv ] ++ nativeBuildInputs;
    env = uvEnv;
    dontConfigure = true;
    buildPhase = ''
      runHook preBuild
      ${installEnvironment (lib.escapeShellArgs developmentArgs) (map (m: m.name) builtMembers)}
      runHook postBuild
    '';
    dontInstall = true;
    dontFixup = true;
  };

  tests = runCommand "${projectName}-tests" { nativeBuildInputs = nativeCheckInputs; } ''
    cp -r ${buildSrc} project
    chmod -R u+w project
    cd project
    export HOME=$TMPDIR PATH=${development}/bin:$PATH VIRTUAL_ENV=${development} PYTHONDONTWRITEBYTECODE=1
    ${lib.escapeShellArgs testCommand}
    touch $out
  '';

  release =
    runCommand "${projectName}-release"
      {
        passthru = {
          # Each file `publish` uploads, by its name, known when Nix evaluates.
          artifacts = lib.listToAttrs (
            lib.concatMap (
              m:
              map (file: {
                name = file;
                value = "${distributions.${m.name}}/${file}";
              }) (distributionFiles m)
            ) builtMembers
          );
          # The release of each member, which `publish --package` uploads.
          members = distributions;
        };
      }
      ''
        mkdir $out
        ${lib.concatMapStrings (name: ''
          ln -s ${distributions.${name}}/* $out/
        '') (builtins.attrNames distributions)}
      '';

  # Every download the lock records, with `systems` where a download is
  # needed only on some systems.
  fetches = locked.entries;

  # Fetch-only local inputs. No project interpreter or dependency wheel is
  # built in Nix; native uv installs/builds these with the user's runtime.
  nativeDirect = filter (d: !(isProvided d) && d.kind != "editable") lock.direct;
  nativeDependencyNeeds = map needs (
    filter (
      d: !(python ? activePackages) || builtins.elem d.key python.activePackages
    ) lock.distributions
  );
  nativeBuilds = buildClosure (
    map (m: m.key) builtMembers
    ++ map (d: d.key) builtDirect
    ++ concatMap (n: n.builds) nativeDependencyNeeds
  );
  nativeFileNames = lib.unique (
    concatMap (n: n.files) nativeDependencyNeeds
    ++ concatMap (key: concatMap (n: n.files) (requirementNeeds key)) nativeBuilds
  );
  nativeWheelhouse = purifier.tree (
    fetchArgs
    // {
      name = "uv-native-wheelhouse";
      entries = map (
        name:
        systemFiles.${name} or (throw "uv-purifier: no locked download of ${name} for ${platform.system}")
      ) nativeFileNames;
    }
  );
  nativeDirectSource =
    d:
    if d.wheel != null then
      if d.wheel.url != null then fetch (fileEntry d.wheel.url d.wheel.hash) else localPath d.value
    else
      directSource d;
  nativeDirectIndex = writeText "uv-native-direct.json" (
    builtins.toJSON {
      editablePaths = map (d: d.value) (filter (d: !(isProvided d) && d.kind == "editable") lock.direct);
      names = listToAttrs (
        map (d: {
          inherit (d) name;
          value = toString (nativeDirectSource d);
        }) (filter (d: d.kind == "git" || d.kind == "url") nativeDirect)
      );
      paths = listToAttrs (
        map (d: {
          name = if lib.hasPrefix "/" d.value || lib.hasPrefix "." d.value then d.value else "./${d.value}";
          value = toString (nativeDirectSource d);
        }) (filter (d: d.kind != "git" && d.kind != "url") nativeDirect)
      );
    }
  );
  nativeSources = writeText "uv-native-sources.json" (
    builtins.toJSON (
      map (d: {
        source = toString (nativeDirectSource d);
        inherit (d) version key;
        filename = if d.wheel != null then d.wheel.name else null;
      }) nativeDirect
    )
  );
  nativeInputs = runCommand "uv-purifier-native-inputs" { } ''
    mkdir "$out"
    ln -s ${nativeWheelhouse} "$out/wheelhouse"
    ln -s ${nativeDirectIndex} "$out/direct.json"
    ln -s ${nativeSources} "$out/native-sources.json"
    ln -s ${./split_requirements.py} "$out/split_requirements.py"
    ${lib.optionalString (platform.isLinux && lib.hasPrefix "/nix/store/" (toString python.interpreter))
      ''
        cat > "$out/prepare-runtime" <<'EOF'
        #!${stdenv.shell}
        exec ${python3.interpreter} -I ${./prepare-runtime.py} "$1" \
          --patchelf ${patchelf}/bin/patchelf \
          --interpreter "$(${patchelf}/bin/patchelf --print-interpreter ${python.interpreter})" \
          ${lib.escapeShellArgs (
            lib.concatMap (library: [
              "--library-dir"
              "${lib.getLib library}/lib"
            ]) runtimeLibraries
          )}
        EOF
        chmod +x "$out/prepare-runtime"
      ''
    }
    ln -s ${
      writeText "uv-editable-versions.json" (
        builtins.toJSON (
          lib.listToAttrs (
            map (m: {
              name = m.path;
              value = memberVersion m;
            }) builtMembers
          )
        )
      )
    } "$out/editable-versions.json"
    ${lib.optionalString hasInputs ''
      ln -s ${inputViews} "$out/input-views.json"
      ln -s ${oldRepositoryFile} "$out/old-repositories.json"
      ln -s ${./python_inputs.py} "$out/python-inputs.py"
      ln -s ${purifier.projectInputsTool} "$out/project-inputs-tools"
    ''}
  '';

  # What `uv-purifier prefetch` installs the project's .venv from: the
  # Python of the build, the wheelhouse, and the wheels of the direct
  # packages with the index that split_requirements.py reads.
  interactive = runCommand "uv-purifier-interactive" { } ''
    mkdir $out
    ln -s ${python.interpreter} $out/python
    ln -s ${wheelhouse} $out/wheelhouse
    ln -s ${interactiveDirectIndex} $out/direct.json
    ln -s ${./split_requirements.py} $out/split_requirements.py
    ln -s ${
      writeText "uv-purifier-editable-versions.json" (
        builtins.toJSON (
          lib.listToAttrs (
            map (m: {
              name = m.path;
              value = memberVersion m;
            }) builtMembers
          )
        )
      )
    } $out/editable-versions.json
    ${lib.optionalString platform.isLinux ''
      cat > $out/prepare-runtime <<'EOF'
      #!${stdenv.shell}
      exec ${python3.interpreter} -I ${./prepare-runtime.py} "$1" \
        --patchelf ${patchelf}/bin/patchelf \
        --interpreter "$(${patchelf}/bin/patchelf --print-interpreter ${python.interpreter})" \
        ${lib.escapeShellArgs (
          lib.concatMap (library: [
            "--library-dir"
            "${lib.getLib library}/lib"
          ]) runtimeLibraries
        )}
      EOF
      chmod +x $out/prepare-runtime
    ''}
    ${lib.optionalString hasInputs "ln -s ${buildSrc} $out/project"}
  '';
in
{
  inherit
    packages
    tests
    release
    fetches
    interactive
    nativeInputs
    lock
    ;
}
