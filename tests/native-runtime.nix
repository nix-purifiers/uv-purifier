# SPDX-License-Identifier: MIT
# A real C++ ELF with its runtime path removed reproduces manylinux's
# dependency on the host libstdc++, independently of any warmed uv cache.
{ pkgs }:
pkgs.runCommand "uv-purifier-native-runtime"
  {
    nativeBuildInputs = [
      pkgs.stdenv.cc
      pkgs.patchelf
      pkgs.python3
    ];
  }
  ''
    cat > fixture.cpp <<'EOF'
    #include <iostream>
    #include <string>
    int main() { std::cout << std::string("native-runtime-ok") << std::endl; }
    EOF
    c++ fixture.cpp -o cached-wheel-elf
    patchelf --remove-rpath cached-wheel-elf
    if ./cached-wheel-elf > before.out 2> before.err; then
      echo "fixture unexpectedly found its C++ runtime" >&2
      exit 1
    fi
    grep libstdc++ before.err
    sha256sum cached-wheel-elf > cache.sha256
    mkdir -p environment/bin
    mkdir environment/lib
    cat > leaf.cpp <<'EOF'
    extern "C" int leaf() { return 42; }
    EOF
    cat > bridge.cpp <<'EOF'
    extern "C" int leaf();
    extern "C" int bridge() { return leaf(); }
    EOF
    cat > transitive.cpp <<'EOF'
    #include <iostream>
    extern "C" int bridge();
    int main() { std::cout << bridge() << std::endl; }
    EOF
    c++ -shared -fPIC leaf.cpp -o environment/lib/libleaf.so
    c++ -shared -fPIC bridge.cpp -Lenvironment/lib -lleaf -o environment/lib/libbridge.so
    patchelf --remove-rpath environment/lib/libbridge.so
    c++ transitive.cpp -Lenvironment/lib -lbridge -Wl,-rpath-link,"$PWD/environment/lib" -o environment/bin/transitive
    patchelf --force-rpath --set-rpath '$ORIGIN/../lib' environment/bin/transitive
    ln cached-wheel-elf environment/bin/program
    cp cached-wheel-elf environment/bin/foreign-loader
    patchelf --set-interpreter /nonexistent/wheel-loader environment/bin/foreign-loader
    ln -s "$PWD/cached-wheel-elf" environment/bin/external-symlink
    mkdir environment/runtime_fixture.dist-info
    printf 'Name: runtime-fixture\nVersion: 1\n' > environment/runtime_fixture.dist-info/METADATA
    printf 'bin/program,,\nbin/foreign-loader,,\n' > environment/runtime_fixture.dist-info/RECORD
    python3 -I ${../pkgs/build-uv-project/prepare-runtime.py} environment \
      --patchelf ${pkgs.patchelf}/bin/patchelf \
      --interpreter "$(patchelf --print-interpreter cached-wheel-elf)" \
      --library-dir ${pkgs.stdenv.cc.cc.lib}/lib
    test "$(environment/bin/program)" = native-runtime-ok
    test "$(environment/bin/foreign-loader)" = native-runtime-ok
    # RUNPATH does not find libleaf through libbridge; preserve the original
    # wheel's transitive RPATH when adding the declared C++ runtime.
    if ! environment/bin/transitive > transitive.out; then
      readelf -d environment/bin/transitive
      readelf -d environment/lib/libbridge.so
      exit 1
    fi
    test "$(cat transitive.out)" = 42
    sha256sum -c cache.sha256
    test -L environment/bin/external-symlink
    python3 - <<'PY'
    import base64,hashlib,importlib.metadata
    distribution = importlib.metadata.Distribution.at('environment/runtime_fixture.dist-info')
    for installed in distribution.files:
        assert installed.hash.mode == 'sha256'
        digest = base64.urlsafe_b64encode(hashlib.sha256(installed.read_binary()).digest()).rstrip(b'=').decode()
        assert installed.hash.value == digest
    PY
    sha256sum environment/bin/program environment/bin/foreign-loader environment/bin/transitive environment/runtime_fixture.dist-info/RECORD > prepared.sha256
    python3 -I ${../pkgs/build-uv-project/prepare-runtime.py} environment \
      --patchelf ${pkgs.patchelf}/bin/patchelf \
      --interpreter "$(patchelf --print-interpreter cached-wheel-elf)" \
      --library-dir ${pkgs.stdenv.cc.cc.lib}/lib
    sha256sum -c prepared.sha256
    touch $out
  ''
