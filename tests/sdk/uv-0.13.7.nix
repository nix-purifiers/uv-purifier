# SPDX-License-Identifier: MIT
# Official current stable release checked through GitHub's release API on 2026-11-06.
# Exact executable test point; no maintenance interval is inferred.
{
  stdenvNoCC,
  stdenv,
  fetchurl,
  autoPatchelfHook,
  lib,
}:
let
  sources = {
    x86_64-linux = {
      target = "x86_64-unknown-linux-gnu";
      sha256 = "c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83";
    };
    aarch64-linux = {
      target = "aarch64-unknown-linux-gnu";
      sha256 = "71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc";
    };
    aarch64-darwin = {
      target = "aarch64-apple-darwin";
      sha256 = "81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731";
    };
  };
  selected = sources.${stdenvNoCC.hostPlatform.system};
in
stdenvNoCC.mkDerivation {
  pname = "uv";
  version = "0.13.7";
  src = fetchurl {
    url = "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-${selected.target}.tar.gz";
    inherit (selected) sha256;
  };
  nativeBuildInputs = lib.optionals stdenvNoCC.hostPlatform.isLinux [ autoPatchelfHook ];
  buildInputs = lib.optionals stdenvNoCC.hostPlatform.isLinux [ stdenv.cc.cc.lib ];
  installPhase = ''
    mkdir -p "$out/bin"
    cp uv uvx "$out/bin/"
  '';
  meta.platforms = builtins.attrNames sources;
}
