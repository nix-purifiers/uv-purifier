# SPDX-License-Identifier: MIT
# First uv project-management API epoch; a test input, not a support whitelist.
{
  stdenvNoCC,
  stdenv,
  fetchzip,
  autoPatchelfHook,
}:
let
  sources = {
    x86_64-linux = {
      arch = "x86_64";
      hash = "sha256-wIwzBUBR0OdYMOS5xNr0uCLlPMgK4d31p5SoHZnVS7k=";
    };
    aarch64-linux = {
      arch = "aarch64";
      hash = "sha256-yHJ41A7rfguA8z57AUau2aStZXxDuo9dzO+WTLwsVkk=";
    };
  };
  selected = sources.${stdenvNoCC.hostPlatform.system};
in
stdenvNoCC.mkDerivation {
  pname = "uv";
  version = "0.3.0";
  src = fetchzip {
    url = "https://github.com/astral-sh/uv/releases/download/0.3.0/uv-${selected.arch}-unknown-linux-gnu.tar.gz";
    inherit (selected) hash;
  };
  nativeBuildInputs = [ autoPatchelfHook ];
  buildInputs = [ stdenv.cc.cc.lib ];
  installPhase = ''
    mkdir -p "$out/bin"
    cp uv uvx "$out/bin/"
  '';
  meta.platforms = builtins.attrNames sources;
}
