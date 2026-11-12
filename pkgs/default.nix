# SPDX-License-Identifier: MIT
#
# The packages of the `uv-purifier` scope. The default package overlay adds
# the scope to the package set as `pkgs.uv-purifier`.
{ callPackage, lib, ... }:
{
  # The `uv-purifier` command: `lock` and `update` write the lockfiles,
  # `prefetch` installs a .venv offline, and `publish` uploads what the Nix
  # build of a workspace member made. The default package overlay gives it
  # the source of the shared purifier library.
  cli = callPackage ./cli { purifierSource = null; };

  # The builder of the purifier contract (pkgs/build-uv-project/default.nix). A
  # package written for callPackage takes the scope as `{ uv-purifier }:`
  # and calls `uv-purifier.buildUvProject { src = ./.; }`.
  buildUvProject = callPackage ./build-uv-project/default.nix { inherit (lib) purifier; };
}
