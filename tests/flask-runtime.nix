# SPDX-License-Identifier: MIT
# The source and both locks are captured before this sandboxed runtime check.
{
  pkgs,
  project,
  src,
  cli,
  uv,
  python,
  receipt,
  requireCSource ? false,
}:
pkgs.runCommand "uv-flask-native-and-ordinary-runtime"
  {
    nativeBuildInputs = [
      cli
      uv
    ];
  }
  ''
    cp -r ${src} flask
    chmod -R u+w flask
    cd flask
    export HOME=$TMPDIR UV_CACHE_DIR=$TMPDIR/native-cache UV_PYTHON_DOWNLOADS=never
    export UV_PYTHON=${python.interpreter} PYTHONDONTWRITEBYTECODE=1
    sha256sum pyproject.toml uv.lock uv-purifier.lock.json > "$TMPDIR/before.sha256"
    CC=/foreign-host-compiler CXX=/foreign-host-cxx SDKROOT=/foreign-host-sdk \
      NIX_CFLAGS_COMPILE=-I/foreign-host-headers \
      uv-purifier prefetch --inputs ${project.interactive}
    uv --offline run --no-sync pytest -q -p no:cacheprovider
    sha256sum --check "$TMPDIR/before.sha256"
    ${pkgs.lib.optionalString requireCSource ''
      .venv/bin/python -I ${./native-extension.py}
      # A fresh cache prevents the earlier compiled wheel masking this control.
      uv venv --python ${python.interpreter} "$TMPDIR/no-compiler"
      if CC=/missing-native-compiler UV_CACHE_DIR="$TMPDIR/no-compiler-cache" \
        uv pip install --offline --no-index --no-binary librt \
          --find-links ${project.interactive}/wheelhouse \
          --python "$TMPDIR/no-compiler/bin/python" librt==0.13.0 \
          > "$TMPDIR/no-compiler.log" 2>&1; then
        echo 'C sdist unexpectedly built without its selected compiler' >&2
        exit 1
      fi
      cat "$TMPDIR/no-compiler.log"
      grep missing-native-compiler "$TMPDIR/no-compiler.log"
    ''}

    export UV_CACHE_DIR=$TMPDIR/ordinary-cache
    test ! -e "$UV_CACHE_DIR"
    uv venv --python ${python.interpreter} "$TMPDIR/ordinary"
    uv pip install --offline --no-index \
      --find-links ${project.packages.flask.projections.python} \
      --python "$TMPDIR/ordinary/bin/python" flask
    "$TMPDIR/ordinary/bin/python" -I - <<'PYTHON'
    from flask import Flask
    from importlib.metadata import version
    app = Flask(__name__)
    @app.get('/')
    def index():
        return 'purified flask'
    assert app.test_client().get('/').data == b'purified flask'
    assert '+nix.' in version('flask')
    PYTHON

    mkdir "$TMPDIR/incomplete"
    for wheel in ${project.packages.flask.projections.python}/*.whl; do
      case "$(basename "$wheel")" in
        werkzeug-*) ;;
        *) ln -s "$wheel" "$TMPDIR/incomplete/$(basename "$wheel")" ;;
      esac
    done
    export UV_CACHE_DIR=$TMPDIR/negative-cache
    test ! -e "$UV_CACHE_DIR"
    uv venv --python ${python.interpreter} "$TMPDIR/negative"
    if uv pip install --offline --no-index --find-links "$TMPDIR/incomplete" \
      --python "$TMPDIR/negative/bin/python" flask > "$TMPDIR/negative.log" 2>&1; then
      echo 'installation from incomplete publication unexpectedly succeeded' >&2
      exit 1
    fi
    cat "$TMPDIR/negative.log"
    grep -i werkzeug "$TMPDIR/negative.log"
    mkdir "$out"
    cp ${project.interactive}/compiler-env.json "$out/compiler-env.json"
    ln -s ${project.interactive}/bin "$out/compiler-bin"
    cp ${pkgs.writeText "uv-flask-runtime-receipt.json" (builtins.toJSON receipt)} "$out/receipt.json"
  ''
