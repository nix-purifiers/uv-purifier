# SPDX-License-Identifier: MIT
#
# What the `uv-purifier` command builds for a project directory given as an
# absolute path, with the nixpkgs the command was built with:
# - with `package`, that workspace member's release, which `publish`
#   uploads;
# - without it, the inputs of a development environment, which `prefetch`
#   installs into the project's .venv.
#
# `lockFile`, when given, is the purifier lockfile to build from instead of
# the one in the project. A local package that uv.lock names by a path
# outside the project is read from that path.
{
  nixpkgs,
  # The source of the shared purifier library.
  purifier,
  src,
  package ? null,
  lockFile ? null,
  pythonVersion ? null,
  pythonInterpreter ? null,
  pythonGlibcVersion ? null,
  pythonFreeThreaded ? false,
  nativeUvPackage ? null,
  uvHelperPackage ? null,
}:
let
  pkgs = import nixpkgs { };
  inherit (pkgs) lib;

  # A directory without what uv, Python and git leave in it.
  source =
    path:
    builtins.path {
      inherit path;
      name = "source";
      filter =
        p: type:
        !builtins.elem (baseNameOf p) [
          ".git"
          ".venv"
          "__pycache__"
          "dist"
          ".uv-purifier"
        ];
    };

  root = /. + src;
  uvLock = builtins.fromTOML (builtins.readFile (root + "/uv.lock"));
  localKinds = [
    "path"
    "directory"
    "editable"
  ];
  localPaths = lib.concatMap (
    p:
    let
      source = p.source or { };
    in
    map (kind: source.${kind}) (builtins.filter (kind: source ? ${kind}) localKinds)
  ) (uvLock.package or [ ]);
  inputLock = if lockFile == null then root + "/uv-purifier.lock.json" else /. + lockFile;
  inputRecords =
    if builtins.pathExists inputLock then
      (builtins.fromJSON (builtins.readFile inputLock)).projectInputs or [ ]
    else
      [ ];
  providedPath =
    path: lib.any (input: lib.hasPrefix (input.nativeRepository + "/") path) inputRecords;
  outside = builtins.filter (
    path: !(providedPath path) && (path == ".." || lib.hasPrefix "../" path || lib.hasPrefix "/" path)
  ) localPaths;
  # Nix paths are normalized, so ".." in the string leaves the project.
  absolute = path: if lib.hasPrefix "/" path then /. + path else /. + "${src}/${path}";
  pathSources = lib.genAttrs outside (
    path:
    let
      target = absolute path;
    in
    if lib.pathIsDirectory target then source target else target
  );

  buildUvProject = pkgs.callPackage ../build-uv-project/default.nix (
    {
      purifier = import purifier { inherit lib; };
    }
    // lib.optionalAttrs (uvHelperPackage != null) { uv = builtins.storePath uvHelperPackage; }
  );
  projectSrc = source root;
  project = buildUvProject (
    {
      inherit pathSources;
      nativeUv = if nativeUvPackage == null then pkgs.uv else builtins.storePath nativeUvPackage;
      python =
        if pythonInterpreter != null then
          {
            interpreter = builtins.storePath pythonInterpreter;
            inherit pythonVersion;
            freeThreaded = pythonFreeThreaded;
          }
          // lib.optionalAttrs (pythonGlibcVersion != null) { glibcVersion = pythonGlibcVersion; }
        else if pythonVersion == null then
          pkgs.python3
        else
          pkgs.${"python" + builtins.replaceStrings [ "." ] [ "" ] pythonVersion}
            or (throw "uv-purifier: nixpkgs has no Python ${pythonVersion}");
      src = projectSrc;
    }
    // lib.optionalAttrs (lockFile != null) { lockFile = /. + lockFile; }
  );
in
if package == null then
  project.interactive
else if inputRecords != [ ] && project.packages ? ${package} then
  project.packages.${package}.projections.python
else
  project.release.members.${package}
    or (throw "uv-purifier: ${package} has no release; the members with one are ${toString (builtins.attrNames project.release.members)}")
