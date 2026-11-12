# SPDX-License-Identifier: MIT
#
# The `uv-purifier` library namespace of the purifier contract. It holds
# `readLock { src, lockFile ? ... }` (lib-overlays/default/read-lock.nix), which reads
# the lockfiles into the lock in memory with no package set. The builder
# is the package `buildUvProject` that the default package overlay adds.
{ ... }:
{

  overlay = final: prev: {
    uv-purifier = (prev.uv-purifier or { }) // {
      readLock = import ./read-lock.nix { lib = final; };
    };
  };

}
