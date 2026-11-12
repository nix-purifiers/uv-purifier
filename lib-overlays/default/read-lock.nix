# SPDX-License-Identifier: MIT
#
# `lib.uv-purifier.readLock { src, lockFile ? ... }`: uv.lock and
# uv-purifier.lock.json read into the lock in memory of the purifier
# contract (github:nix-purifiers/purifier, CONTRACT.md):
#
#   {
#     purifier = "uv-purifier";
#     entries  = [ <download entry> ];
#     outputs  = { <member> = { sdist; wheel; scripts; }; };
#     problems = [ <string> ];
#   }
#
# `entries` is every download either lockfile records: a file entry for
# each wheel and sdist of uv.lock and of the build requirements, a file
# entry for each file a package from a URL comes from, and a git entry for
# each git checkout. A compiled wheel carries `systems`, the Linux and macOS
# systems its platform tags name (an empty list when it names none, such as
# a Windows wheel); every other entry carries `systems = null`, for every
# system. The builder picks among the wheels of a system by the Python it
# builds with.
#
# `outputs` is what each workspace member with a build makes: the file
# names of its sdist and wheel, and its scripts. Nix derives them from
# uv.lock and the pyproject.toml of the member (src/uv_purifier.py,
# derived_outputs), and the lockfile records a field only where the build
# makes something else. The wheel name is the one the lock run's system
# makes; on Linux the builder rewrites a compiled wheel's tags for its own
# system, and on macOS only a pure wheel has a name.
{ lib }:
{
  src,
  lockFile ? src + "/uv-purifier.lock.json",
  # uv.lock. A copy outside `src` takes the place of the one in it.
  uvLock ? src + "/uv.lock",
}:
let
  lock = import ./lock.nix { inherit src lockFile uvLock; };

  # The supported systems, by the CPU name a wheel's platform tag uses.
  systemsByArch = {
    x86_64 = "x86_64-linux";
    aarch64 = "aarch64-linux";
  };
  # The same for macOS tags, where a universal2 wheel holds arm64 too.
  macosSystemsByArch = {
    arm64 = "aarch64-darwin";
    universal2 = "aarch64-darwin";
  };

  # The systems a wheel can install on, from its platform tags: null for a
  # wheel tagged `any`, else the Linux systems of the glibc tags it names
  # and the macOS system of its macOS tags.
  wheelSystems =
    name:
    let
      parts = lib.splitString "-" (lib.removeSuffix ".whl" name);
      platforms = lib.splitString "." (lib.last parts);
      systemOf =
        tag:
        let
          m = builtins.match "(manylinux1|manylinux2010|manylinux2014|manylinux_[0-9]+_[0-9]+|linux)_(.*)" tag;
          macos = builtins.match "macosx_[0-9]+_[0-9]+_(.*)" tag;
        in
        if m != null then
          systemsByArch.${builtins.elemAt m 1} or null
        else if macos != null then
          macosSystemsByArch.${builtins.elemAt macos 0} or null
        else
          null;
      systems = lib.filter (system: system != null) (map systemOf platforms);
    in
    if lib.elem "any" platforms then null else lib.unique systems;

  withSystems =
    entry:
    entry // { systems = if lib.hasSuffix ".whl" entry.path then wheelSystems entry.path else null; };

  # A file entry of the purifier library for a URL and SRI hash of uv.lock.
  fileEntry = url: hash: {
    path = lock.fileName url;
    inherit url hash;
    mirrors = [ ];
  };

  directEntries =
    d:
    if d.kind == "git" then
      [ d.git ]
    else if d.kind == "url" then
      [
        (if d.wheel != null then fileEntry d.wheel.url d.wheel.hash else fileEntry d.value d.sdistHash)
      ]
    else
      [ ];

  entries = map withSystems (lib.attrValues lock.files ++ lib.concatMap directEntries lock.direct);

  builtMembers = lib.filter (m: m.kind != "virtual") lock.members;

  memberOutputs =
    m:
    let
      version = lib.removePrefix "${m.name}==" m.key;
      stem = lib.toLower (
        lib.concatStrings (
          map (part: if builtins.isList part then "_" else part) (builtins.split "[-_.]+" m.name)
        )
      );
      project =
        (builtins.fromTOML (builtins.readFile (src + "/${m.path}/pyproject.toml"))).project or { };
      dynamic = project.dynamic or [ ];
      derived = {
        sdist = if version == "" then null else "${stem}-${version}.tar.gz";
        wheel = if version == "" then null else "${stem}-${version}-py3-none-any.whl";
        scripts =
          if lib.elem "scripts" dynamic || lib.elem "gui-scripts" dynamic then
            null
          else
            lib.sort (a: b: a < b) (
              lib.unique (lib.attrNames (project.scripts or { }) ++ lib.attrNames (project.gui-scripts or { }))
            );
      };
    in
    derived // (lock.purifier.outputs or { }).${m.name} or { };

  outputs = lib.genAttrs (map (m: m.name) builtMembers) (
    name: memberOutputs (lib.findFirst (m: m.name == name) null builtMembers)
  );

  # A member whose outputs can be neither derived nor found in the
  # lockfile.
  unknownOutputs = lib.concatMap (
    name:
    let
      missing = lib.filter (field: outputs.${name}.${field} == null) (lib.attrNames outputs.${name});
    in
    lib.optional (missing != [ ])
      "uv-purifier: ${toString lockFile} records no ${lib.concatStringsSep ", " missing} of ${name}. Run `uv-purifier lock`."
  ) (lib.attrNames outputs);
in
{
  purifier = "uv-purifier";
  inherit entries outputs;
  problems = if lock.problems != [ ] then lock.problems else unknownOutputs;
}
