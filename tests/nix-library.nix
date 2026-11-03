# SPDX-License-Identifier: MIT
#
# Unit tests of the Nix library that need no build: wheel selection, and the
# check that uv-purifier.lock.json still matches uv.lock.
{
  pkgs,
  # The shared purifier library.
  purifier,
  # lib.uv-purifier.readLock.
  readLock,
}:
let
  inherit (pkgs) lib;

  # The wheel selection of the shared library, as buildUvProject uses it.
  installs = purifier.wheels {
    pythonVersion = "3.14";
    arch = "x86_64";
    glibcVersion = "2.40";
  };

  wheelCases = {
    pure = installs "attrs-25.3.0-py3-none-any.whl";
    pythonTwoAndThree = installs "six-1.17.0-py2.py3-none-any.whl";
    sameCpython = installs "pydantic_core-2.41.4-cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64.whl";
    otherCpython = installs "pydantic_core-2.41.4-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl";
    stableAbi = installs "cryptography-46.0.3-cp311-abi3-manylinux_2_34_x86_64.whl";
    stableAbiNewerThanPython = installs "cryptography-46.0.3-cp315-abi3-manylinux_2_34_x86_64.whl";
    newerGlibc = installs "numpy-2.3.4-cp314-cp314-manylinux_2_41_x86_64.whl";
    otherArch = installs "numpy-2.3.4-cp314-cp314-manylinux_2_28_aarch64.whl";
    musl = installs "numpy-2.3.4-cp314-cp314-musllinux_1_2_x86_64.whl";
    macos = installs "numpy-2.3.4-cp314-cp314-macosx_14_0_arm64.whl";
    buildTag = installs "example-1.0-1-py3-none-any.whl";
    pythonTwoOnly = installs "example-1.0-py2-none-any.whl";
  };

  wheelExpected = {
    pure = true;
    pythonTwoAndThree = true;
    sameCpython = true;
    otherCpython = false;
    stableAbi = true;
    stableAbiNewerThanPython = false;
    newerGlibc = false;
    otherArch = false;
    musl = false;
    macos = false;
    buildTag = true;
    pythonTwoOnly = false;
  };

  # The lock in memory of the simple fixture: no problems, the shape that
  # the purifier library's checkLock accepts, and `systems` by wheel tag.
  locked = readLock { src = ./fixtures/simple; };
  entryAt = name: lib.findFirst (e: e.path == name) null locked.entries;
  x86Wheel = "markupsafe-3.0.3-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl";
  armWheel = "markupsafe-3.0.3-cp314-cp314-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl";
  macosWheel = "markupsafe-3.0.3-cp314-cp314-macosx_11_0_arm64.whl";
  x86Paths = map (e: e.path) (purifier.entriesFor "x86_64-linux" locked);
  lockCases = {
    noProblems = locked.problems == [ ];
    checked = (builtins.tryEval (builtins.deepSeq (purifier.checkLock locked).entries true)).success;
    compiledWheelSystems = (entryAt x86Wheel).systems == [ "x86_64-linux" ];
    macosWheelSystems = (entryAt macosWheel).systems == [ "aarch64-darwin" ];
    otherMacosWheelSystems =
      (entryAt "markupsafe-3.0.3-cp314-cp314-macosx_10_13_x86_64.whl").systems == [ ];
    pureWheelSystems = (entryAt "attrs-25.3.0-py3-none-any.whl").systems == null;
    sdistSystems = (entryAt "docopt-0.6.2.tar.gz").systems == null;
    entriesForSystem = lib.elem x86Wheel x86Paths && !lib.elem armWheel x86Paths;
    outputs = locked.outputs.simple.scripts == [ "simple" ];
  };

  failures =
    lib.filter (name: wheelCases.${name} != wheelExpected.${name}) (lib.attrNames wheelCases)
    ++ lib.filter (name: !lockCases.${name}) (lib.attrNames lockCases);
in
pkgs.runCommand "uv-purifier-test-nix-library" { } (
  if failures == [ ] then
    "touch $out"
  else
    ''
      echo "failed: "${lib.escapeShellArg (lib.concatStringsSep ", " failures)} >&2
      exit 1
    ''
)
