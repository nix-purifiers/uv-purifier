# SPDX-License-Identifier: MIT
#
# The host `uv` handles build/export/publication frontends. `nativeUv` owns
# project resolution and installation, and defaults to that same package.
# Both immutable package identities propagate into CLI-triggered Nix builds.
{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
  uv,
  nativeUv ? uv,
  git,
  nix-prefetch-git,
  path,
  # The source of the shared purifier library, which the default package
  # overlay passes.
  purifierSource,
}:
let
  python = python3.withPackages (ps: [
    ps.packaging
    ps.tomli-w
  ]);
  library =
    if purifierSource == null then
      throw "uv-purifier: the cli package needs the source of the purifier library; take it from the flake's package overlay"
    else
      purifierSource;
in
stdenvNoCC.mkDerivation {
  pname = "uv-purifier";
  version = import ../version.nix;

  src = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../src
      ../build-uv-project
      ./cli.nix
      ../../lib-overlays/default
    ];
  };

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin $out/share/uv-purifier
    cp src/uv_purifier.py $out/share/uv-purifier/
    mkdir -p $out/share/uv-purifier/pkgs/cli
    cp -r pkgs/build-uv-project $out/share/uv-purifier/pkgs/
    cp pkgs/cli/cli.nix $out/share/uv-purifier/pkgs/cli/
    cp -r lib-overlays $out/share/uv-purifier/
    makeWrapper ${python.interpreter} $out/bin/uv-purifier \
      --add-flags "-I $out/share/uv-purifier/uv_purifier.py" \
      --prefix PATH : ${
        lib.makeBinPath [
          nativeUv
          git
          nix-prefetch-git
        ]
      } \
      --set UV_PURIFIER_NIX_LIB $out/share/uv-purifier/pkgs/build-uv-project \
      --set UV_PURIFIER_NIXPKGS ${path} \
      --set UV_PURIFIER_LIBRARY ${library} \
      --set UV_PURIFIER_PROJECT_INPUTS_TOOLS ${library}/tools \
      --set UV_PURIFIER_HELPER_UV ${uv}/bin/uv \
      --set UV_PURIFIER_HELPER_UV_PACKAGE ${uv} \
      --set UV_PURIFIER_NATIVE_UV_PACKAGE ${nativeUv}
    runHook postInstall
  '';

  meta = {
    description = "Lock and publish uv projects that Nix builds";
    license = lib.licenses.mit;
    mainProgram = "uv-purifier";
  };
}
