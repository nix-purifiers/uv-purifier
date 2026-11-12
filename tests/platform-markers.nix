# SPDX-License-Identifier: MIT
{ pkgs }:
let
  inherit (pkgs) lib;
  select = import ../pkgs/build-uv-project/platform-packages.nix { inherit lib; };
  environment = {
    sys_platform = "linux";
    os_name = "posix";
    platform_system = "Linux";
  };
  graph = select {
    inherit environment;
    memberKeys = [ "root==1" ];
    uvLock = {
      manifest.dependency-groups.tools = [ { name = "manifest-tool"; } ];
      package = [
        {
          name = "root";
          version = "1";
          dependencies = [
            {
              name = "linux";
              marker = "sys_platform == 'linux'";
            }
            {
              name = "windows";
              marker = "sys_platform == 'win32'";
            }
            {
              name = "unknown-python";
              marker = "python_version < '3.12'";
            }
            {
              name = "versioned";
              version = "1";
            }
          ];
          optional-dependencies.extra = [
            {
              name = "optional";
              marker = "os_name == 'posix'";
            }
          ];
          dev-dependencies.dev = [ { name = "docker"; } ];
        }
        {
          name = "docker";
          version = "1";
          dependencies = [
            {
              name = "windows";
              marker = "sys_platform == 'win32'";
            }
          ];
        }
        {
          name = "linux";
          version = "1";
        }
        {
          name = "windows";
          version = "1";
          dependencies = [ { name = "windows-child"; } ];
        }
        {
          name = "windows-child";
          version = "1";
        }
        {
          name = "unknown-python";
          version = "1";
        }
        {
          name = "optional";
          version = "1";
        }
        {
          name = "versioned";
          version = "1";
        }
        {
          name = "versioned";
          version = "2";
        }
        {
          name = "manifest-tool";
          version = "1";
        }
      ];
    };
  };
  cases = [
    {
      marker = "sys_platform == 'win32'";
      expected = false;
    }
    {
      marker = "sys_platform == 'linux'";
      expected = true;
    }
    {
      marker = "'linux' == sys_platform";
      expected = true;
    }
    {
      marker = "platform_system != 'Windows'";
      expected = true;
    }
    {
      marker = "sys_platform in 'linux darwin'";
      expected = true;
    }
    {
      marker = "sys_platform not in 'win32 darwin'";
      expected = true;
    }
    {
      marker = "sys_platform == 'win32' and python_version < '3.12'";
      expected = false;
    }
    {
      marker = "sys_platform == 'linux' or python_version < '3.12'";
      expected = true;
    }
    {
      marker = "sys_platform == 'win32' or python_version < '3.12'";
      expected = null;
    }
    {
      marker = "(sys_platform == 'darwin' or sys_platform == 'win32') and extra == 'test'";
      expected = false;
    }
    {
      marker = "sys_platform == 'win32' or os_name == 'posix' and platform_system == 'Linux'";
      expected = true;
    }
    {
      marker = "sys_platform == 'win32' and '3.0' == '3.0.0'";
      expected = false;
    }
    {
      marker = "'3.0' == '3.0.0'";
      expected = null;
    }
    {
      marker = "sys_platform < 'z'";
      expected = null;
    }
    {
      marker = "sys_platform == 'win32' trailing";
      expected = null;
      invalid = true;
    }
  ];
  observed = map (case: case // { actual = graph.markerTruth case.marker; }) cases;
  keys = builtins.attrNames graph.keys;
  expectedKeys = [
    "docker==1"
    "linux==1"
    "manifest-tool==1"
    "optional==1"
    "root==1"
    "unknown-python==1"
    "versioned==1"
  ];
in
assert keys == expectedKeys;
assert lib.all (case: case.actual == case.expected) observed;
pkgs.runCommand "uv-purifier-platform-markers"
  {
    nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.packaging ])) ];
  }
  ''
    python -I - ${pkgs.writeText "marker-observations.json" (builtins.toJSON observed)} <<'PY'
    import itertools, json, sys
    from packaging.markers import Marker, default_environment
    cases = json.load(open(sys.argv[1]))
    for case in cases:
        if case.get('invalid') or case['actual'] is None:
            continue
        for version, extra in itertools.product(['3.10', '3.13', '3.15'], ["", "test"]):
            environment = default_environment()
            environment.update(sys_platform='linux', os_name='posix', platform_system='Linux',
                               python_version=version, python_full_version=version+'.0', extra=extra)
            assert Marker(case['marker']).evaluate(environment) == case['actual'], case
    PY
    touch "$out"
  ''
