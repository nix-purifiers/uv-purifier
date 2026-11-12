# SPDX-License-Identifier: MIT
#
# Reads a uv project's two lockfiles: uv.lock, which uv writes in the
# project, and uv-purifier.lock.json, which `uv-purifier lock` writes beside
# it or at another path. `problems` holds why they cannot be used, each
# naming the command that fixes it; read-lock.nix gives them to the purifier
# library's checkLock.
{
  # The project root, which holds uv.lock and pyproject.toml.
  src,
  lockFile ? src + "/uv-purifier.lock.json",
  # uv.lock. A copy outside `src` takes the place of the one in it.
  uvLock ? src + "/uv.lock",
}:
let
  inherit (builtins)
    attrNames
    baseNameOf
    concatStringsSep
    filter
    fromJSON
    fromTOML
    hashFile
    hashString
    head
    listToAttrs
    map
    match
    readFile
    replaceStrings
    sort
    split
    substring
    ;

  supportedVersion = import ./lock-version.nix;

  exists = builtins.pathExists lockFile;

  # A JSON file that does not parse fails evaluation here: builtins.fromJSON
  # raises an error that no Nix expression can catch, so this message is the
  # context of that error rather than one of `problems`.
  purifier =
    if !exists then
      { }
    else
      builtins.addErrorContext "while reading ${toString lockFile}, which is not valid JSON; if it was edited by hand or merged with conflicts, rewrite it with `uv-purifier lock`" (
        fromJSON (readFile lockFile)
      );

  # The hash of every field but contentHash, over the JSON builtins.toJSON
  # writes (src/uv_purifier.py, content_hash).
  contentHash = hashString "sha256" (builtins.toJSON (removeAttrs purifier [ "contentHash" ]));
  uvLockData = fromTOML (readFile uvLock);

  packageKey = package: "${package.name}==${package.version or ""}";

  # The file name of a locked URL, as the lock tool writes it: the last
  # segment of the URL's path, with the percent-escapes of PyPI file names
  # decoded.
  fileName =
    url:
    replaceStrings
      [
        "%2B"
        "%2b"
        "%21"
        "%25"
      ]
      [
        "+"
        "+"
        "!"
        "%"
      ]
      (baseNameOf (head (split "[?#]" url)));

  # A store path name from a file name. Store path names allow
  # [A-Za-z0-9+._?=-], must not start with a dot, and are at most 211
  # characters; every other character becomes "_".
  storeName =
    name:
    let
      replaced = concatStringsSep "" (
        map (part: if builtins.isList part then "_" else part) (split "[^A-Za-z0-9+._?=-]" name)
      );
      undotted = head (match "\\.*(.*)" replaced);
    in
    substring 0 200 (if undotted == "" then "file" else undotted);

  sriHash =
    uvHash:
    builtins.convertHash {
      hash = replaceStrings [ "sha256:" ] [ "" ] uvHash;
      hashAlgo = "sha256";
      toHashFormat = "sri";
    };

  # The kind of a package's source in uv.lock, and its value: the index of
  # a registry package, the repository of a git package, the URL of a URL
  # package, or the path of a local one.
  sourceKinds = [
    "registry"
    "git"
    "url"
    "path"
    "directory"
    "editable"
    "virtual"
  ];
  sourceOf =
    package:
    let
      source = package.source or { };
      kinds = filter (kind: source ? ${kind}) sourceKinds;
    in
    if kinds == [ ] then
      throw "uv-purifier: ${package.name} has a source uv-purifier does not know: ${builtins.toJSON source}"
    else
      {
        kind = head kinds;
        value = source.${head kinds};
      };

  isTree =
    package:
    builtins.elem (sourceOf package).kind [
      "directory"
      "editable"
      "virtual"
    ];

  packages = uvLockData.package or [ ];

  # A workspace lists its members in [manifest]; a single project is its own
  # member, the source tree at the project root (src/uv_purifier.py,
  # workspace_members).
  isMember =
    package:
    if uvLockData ? manifest.members then
      builtins.elem package.name uvLockData.manifest.members
    else
      isTree package && (sourceOf package).value == ".";

  members = map (package: {
    inherit (package) name;
    key = packageKey package;
    inherit (sourceOf package) kind;
    path = (sourceOf package).value;
    dependencies = map (d: d.name) (package.dependencies or [ ]);
  }) (filter isMember packages);

  # The packages that come from an index, with the file names of their
  # wheels and sdist.
  distributions = map (package: {
    inherit (package) name;
    key = packageKey package;
    wheels = map (wheel: fileName wheel.url) (package.wheels or [ ]);
    sdist = if package ? sdist then fileName package.sdist.url else null;
  }) (filter (package: (sourceOf package).kind == "registry") packages);

  # The packages that come from git, a URL or a local path, other than the
  # workspace members. The build makes a wheel of each.
  direct = map (
    package:
    let
      source = sourceOf package;
    in
    {
      inherit (package) name version;
      key = packageKey package;
      inherit (source) kind value;
      # A URL or local file that is a wheel, which installs as it is.
      wheel =
        if package ? wheels then
          let
            entry = head package.wheels;
          in
          {
            name = fileName (entry.url or entry.path or entry.filename);
            url = entry.url or null;
            hash = sriHash entry.hash;
          }
        else
          null;
      sdistHash = if package ? sdist then sriHash package.sdist.hash else null;
      # The download entry of a git checkout, in uv-purifier.lock.json.
      git =
        if source.kind == "git" then
          gitEntries.${
            (purifier.sources or { }).${packageKey package}
            or (throw "uv-purifier: uv-purifier.lock.json has no checkout of ${packageKey package}. Run `uv-purifier lock`.")
          }
        else
          null;
      subdirectory =
        if source.kind == "git" then
          let
            m = match ".*[?&]subdirectory=([^&#]*).*" source.value;
          in
          if m == null then null else head m
        else
          null;
    }
  ) (filter (package: !isMember package && (sourceOf package).kind != "registry") packages);

  # Every locked file of an index by its file name, as a file entry of the
  # shared purifier library: its path in the wheelhouse, its URL and its SRI
  # hash.
  uvFiles = builtins.concatMap (
    package:
    map (entry: {
      name = fileName entry.url;
      value = {
        path = fileName entry.url;
        inherit (entry) url;
        mirrors = [ ];
        hash = sriHash entry.hash;
      };
    }) (package.wheels or [ ] ++ (if package ? sdist then [ package.sdist ] else [ ]))
  ) (filter (package: (sourceOf package).kind == "registry") packages);

  purifierFiles = map (artifact: {
    name = artifact.path;
    value = artifact;
  }) (filter (artifact: artifact ? url) (purifier.artifacts or [ ]));

  files = listToAttrs (uvFiles ++ purifierFiles);

  gitEntries = listToAttrs (
    map (artifact: {
      name = artifact.path;
      value = artifact;
    }) (filter (artifact: artifact ? git) (purifier.artifacts or [ ]))
  );

  # The hash the lock tool records over uv.lock and the pyproject.toml files
  # of the source trees in the project (src/uv_purifier.py, inputs_hash).
  hasPrefix = prefix: text: substring 0 (builtins.stringLength prefix) text == prefix;
  insideProject = path: !(path == ".." || hasPrefix "/" path || hasPrefix "../" path);
  treeFiles = sort (a: b: a < b) (
    filter (f: builtins.pathExists (src + "/${f}")) (
      map (path: if path == "." then "pyproject.toml" else "${path}/pyproject.toml") (
        filter insideProject (map (package: (sourceOf package).value) (filter isTree packages))
      )
    )
  );
  inputsHash = hashString "sha256" (
    "uv.lock ${hashFile "sha256" uvLock}\n"
    + concatStringsSep "" (map (f: "${f} ${hashFile "sha256" (src + "/${f}")}\n") treeFiles)
  );
  # Why the lockfiles cannot be used, the first that applies, naming the
  # command that fixes it.
  problems =
    if !exists then
      [
        "uv-purifier: ${toString lockFile} does not exist. Run `uv-purifier lock` beside uv.lock, and add the file to the repository."
      ]
    else if purifier.version or null != supportedVersion then
      [
        "uv-purifier: ${toString lockFile} has lock format version ${
          toString (purifier.version or null)
        }; this library reads version ${toString supportedVersion}. Run `uv-purifier lock` with the version that provides this library."
      ]
    else if purifier.contentHash or null != contentHash then
      [
        "uv-purifier: ${toString lockFile} was changed after `uv-purifier lock` wrote it, by hand or by a merge. Rewrite it with `uv-purifier lock`."
      ]
    else if purifier.inputsHash or null != inputsHash then
      [
        "uv-purifier: ${toString lockFile} was made for another uv.lock or another pyproject.toml of a source tree in the project. Run `uv-purifier lock`, which also runs `uv lock` when pyproject.toml changed."
      ]
    else
      [ ];
in
{
  inherit
    purifier
    problems
    uvLockData
    members
    distributions
    direct
    files
    gitEntries
    insideProject
    fileName
    storeName
    ;
  builds = purifier.builds or { };
  buildRequirements = purifier.buildRequirements or [ ];
  memberNames = map (m: m.name) members;
  # Every name in `files`, for tests.
  fileNames = attrNames files;
}
