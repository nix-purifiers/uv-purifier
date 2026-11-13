# SPDX-License-Identifier: MIT
"""Splits a requirements file that `uv export` wrote into the requirements
an index serves and the packages that come from elsewhere.

uv export writes a package from an index as `name==version` with its hashes,
a package from git or a URL as `name @ <url>`, and a package from a local
path as the path itself. With no network, uv installs only the first kind
from the wheelhouse. The Nix library builds a wheel for each of the others,
and this script swaps them for those wheels.

Usage: split_requirements.py <exported> <direct.json> <requirements-out> <wheels-out>

direct.json maps each package from elsewhere to the directory that holds its
wheel. A package from git or a URL is keyed by its name, and a package from a
local path by its path as uv.lock records it. The script writes the
requirements that remain, and the wheel directories of the packages it took
out, one per line.
"""

import json
import os
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

PINNED = re.compile(r"^[A-Za-z0-9._-]+(\[[^\]]*\])?==")


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def blocks(lines):
    """Groups a requirements file into requirements: a requirement's first
    line, the lines its trailing backslashes continue onto, and the comments
    after it."""
    current = []
    for line in lines:
        starts = line and not line[0].isspace() and not line.startswith("#")
        if starts and current:
            yield current
            current = []
        current.append(line)
    if current:
        yield current


def head(block):
    for line in block:
        if line and not line[0].isspace() and not line.startswith("#"):
            return line.rstrip().removesuffix("\\").strip()
    return None


def marker_environment(python):
    # The helper's packaging library evaluates markers, but its runtime must
    # never supply the target's Python version, implementation or OS facts.
    return json.loads(subprocess.check_output([python, "-I", "-c", '''
import json, os, platform, sys
version = sys.implementation.version
implementation_version = "%d.%d.%d" % version[:3]
if version.releaselevel != "final":
    implementation_version += {"alpha": "a", "beta": "b", "candidate": "rc"}.get(version.releaselevel, version.releaselevel) + str(version.serial)
print(json.dumps({
    "implementation_name": sys.implementation.name,
    "implementation_version": implementation_version,
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "platform_python_implementation": platform.python_implementation(),
    "python_version": "%d.%d" % sys.version_info[:2],
    "sys_platform": sys.platform,
}))
'''], text=True))


def active(requirement, environment):
    from packaging.requirements import Requirement
    text = requirement.removeprefix("-e ").strip()
    if not PINNED.match(text) and " @ " not in text:
        text = "uv-local-source @ " + text
    marker = Requirement(text).marker
    return marker is None or marker.evaluate(environment)


def main(exported, direct_json, requirements_out, wheels_out, python=None):
    with open(direct_json) as f:
        direct = json.load(f)
    by_name = {normalize(k): v for k, v in direct.get("names", {}).items()}
    by_path = {os.path.normpath(k): v for k, v in direct.get("paths", {}).items()}
    editable_paths = {os.path.normpath(path) for path in direct.get("editablePaths", [])}
    with open(exported) as f:
        lines = f.read().splitlines()
    environment = marker_environment(python) if python is not None else None
    kept, wheels, missing = [], [], []
    for block in blocks(lines):
        requirement = head(block)
        if requirement is not None and environment is not None and not active(requirement, environment):
            continue
        if requirement is None or PINNED.match(requirement):
            kept.extend(block)
            continue
        requirement = requirement.removeprefix("-e ").strip()
        if " @ " in requirement:
            name = normalize(requirement.split(" @ ", 1)[0].split("[", 1)[0].strip())
            wheel = by_name.get(name)
        else:
            path = requirement.split(";", 1)[0].strip()
            url = urlsplit(path)
            if url.scheme == "file" and url.netloc in ("", "localhost"):
                path = unquote(url.path)
            if os.path.normpath(path) in editable_paths:
                # Native prefetch installs this source through PEP 660.
                # Its editable hook need not support building a release wheel.
                continue
            wheel = by_path.get(os.path.normpath(path))
        if wheel is None:
            missing.append(requirement)
        else:
            wheels.append(wheel)
    if missing:
        sys.exit("uv-purifier: no wheel was built for these requirements: " + ", ".join(missing))
    with open(requirements_out, "w") as f:
        f.write("\n".join(kept) + "\n")
    with open(wheels_out, "w") as f:
        f.write("".join(w + "\n" for w in wheels))


if __name__ == "__main__":
    main(*sys.argv[1:6])
