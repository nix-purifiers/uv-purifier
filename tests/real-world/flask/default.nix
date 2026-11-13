# SPDX-License-Identifier: MIT
#
# Flask (github.com/pallets/flask), pinned to a commit, as an integration
# test on a real uv project: a flit-core build, 79 locked packages with
# compiled wheels (markupsafe, cffi, cryptography) and sdists to build, and
# dependency groups for tests, typing and docs.
#
# uv-purifier.lock.json beside this file is what `uv-purifier lock` wrote
# in a checkout of that commit. Flask's own source holds no such file, so
# this builds Flask the way a packaging flake builds a project its author
# does not change: with the lockfile kept outside the source.
#
# The check builds Flask's package and release, runs Flask's test suite
# through the builder's `tests`, and then installs a development
# environment with `uv-purifier prefetch` and runs the test suite in it,
# offline, through uv.
{
  pkgs,
  buildUvProject,
  cli,
}:
let
  src = pkgs.fetchFromGitHub {
    owner = "pallets";
    repo = "flask";
    rev = "49943b5a94413a530761350f04e896f76e8d620e";
    sha256 = "07ax2qyr3d7x7sayws07dv8dgbi9s8mrxj3lnlqwl6p0h417pvh7";
  };

  project = buildUvProject {
    inherit src;
    lockFile = ./uv-purifier.lock.json;
    testCommand = [
      "pytest"
      "-q"
      "-p"
      "no:cacheprovider"
    ];
  };
in
pkgs.runCommand "uv-purifier-test-flask"
  {
    nativeBuildInputs = [
      cli
      pkgs.uv
    ];
  }
  ''
    ${project.packages.flask.scripts.flask} --version
    test -f ${project.release.artifacts."flask-3.2.0.dev0-py3-none-any.whl"}
    test -f ${project.release.artifacts."flask-3.2.0.dev0.tar.gz"}
    test -e ${project.tests}

    cp -r ${src} flask
    chmod -R u+w flask
    cp ${./uv-purifier.lock.json} flask/uv-purifier.lock.json
    cd flask
    export HOME=$TMPDIR UV_CACHE_DIR=$TMPDIR/uv-cache PYTHONDONTWRITEBYTECODE=1
    export UV_PYTHON=${project.interactive}/python
    uv-purifier prefetch --inputs ${project.interactive}
    UV_OFFLINE=1 uv run --no-sync pytest -q -p no:cacheprovider
    touch $out
  ''
