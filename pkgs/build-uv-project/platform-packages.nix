# SPDX-License-Identifier: MIT
# A conservative upper bound on the packages a universal lock can use on
# this OS. Unknown predicates remain reachable; uv owns full marker evaluation.
{ lib }:
{
  uvLock,
  memberKeys,
  environment,
}:
let
  inherit (builtins)
    head
    length
    isList
    isString
    isAttrs
    ;
  first = xs: if xs == [ ] then null else head xs;
  invalid = {
    valid = false;
    truth = null;
    rest = [ ];
  };
  conjunction =
    a: b:
    if a == false || b == false then
      false
    else if a == true && b == true then
      true
    else
      null;
  disjunction =
    a: b:
    if a == true || b == true then
      true
    else if a == false && b == false then
      false
    else
      null;

  value =
    token:
    let
      size = builtins.stringLength token;
      quoted = builtins.match ''("[^"]*"|'[^']*')'' token != null;
      identifier = builtins.elem token [
        "implementation_name"
        "implementation_version"
        "os_name"
        "platform_machine"
        "platform_release"
        "platform_system"
        "platform_version"
        "python_full_version"
        "platform_python_implementation"
        "python_version"
        "sys_platform"
        "extra"
        "extras"
        "dependency_groups"
      ];
      known =
        identifier
        && builtins.elem token [
          "sys_platform"
          "os_name"
          "platform_system"
        ]
        && environment ? ${token};
    in
    {
      valid = (quoted && !(lib.hasInfix "\\" token)) || identifier;
      inherit quoted;
      variable = identifier;
      known = quoted || known;
      osVariable = known;
      text =
        if quoted then
          builtins.substring 1 (size - 2) token
        else if known then
          environment.${token}
        else
          null;
    };

  comparison =
    left: operator: right:
    # Do not substitute string comparison for PEP 440 version comparison.
    # Only OS variables supplied by the builder have known values here.
    if !(left.known && right.known && (left.osVariable || right.osVariable)) then
      null
    else if operator == "==" then
      left.text == right.text
    else if operator == "!=" then
      left.text != right.text
    else if operator == "in" then
      lib.hasInfix left.text right.text
    else if operator == "not in" then
      !(lib.hasInfix left.text right.text)
    else
      null;

  atom =
    tokens:
    if first tokens == "(" then
      let
        parsed = expression (lib.drop 1 tokens);
      in
      if parsed.valid && first parsed.rest == ")" then
        parsed // { rest = lib.drop 1 parsed.rest; }
      else
        invalid
    else if length tokens < 3 then
      invalid
    else
      let
        negate = builtins.elemAt tokens 1 == "not";
        operator = if negate then "not in" else builtins.elemAt tokens 1;
        offset = if negate then 3 else 2;
        left = value (head tokens);
        right =
          if length tokens > offset then value (builtins.elemAt tokens offset) else { valid = false; };
      in
      if
        left.valid
        && right.valid
        && ((left.variable && right.quoted) || (left.quoted && right.variable))
        && (!negate || builtins.elemAt tokens 2 == "in")
        && builtins.elem operator [
          "=="
          "!="
          "<"
          ">"
          "<="
          ">="
          "~="
          "==="
          "in"
          "not in"
        ]
      then
        {
          valid = true;
          truth = comparison left operator right;
          rest = lib.drop (offset + 1) tokens;
        }
      else
        invalid;

  andExpression =
    tokens:
    let
      loop =
        parsed:
        if parsed.valid && first parsed.rest == "and" then
          let
            next = atom (lib.drop 1 parsed.rest);
          in
          loop (next // { truth = conjunction parsed.truth next.truth; })
        else
          parsed;
    in
    loop (atom tokens);
  expression =
    tokens:
    let
      loop =
        parsed:
        if parsed.valid && first parsed.rest == "or" then
          let
            next = andExpression (lib.drop 1 parsed.rest);
          in
          loop (next // { truth = disjunction parsed.truth next.truth; })
        else
          parsed;
    in
    loop (andExpression tokens);

  markerTruth =
    marker:
    if !isString marker then
      null
    else
      let
        parts = builtins.split ''("[^"]*"|'[^']*'|===|==|!=|<=|>=|~=|[()<>]|[[:alpha:]_][[:alnum:]_]*|[^[:space:]])'' marker;
        tokens = lib.concatMap (part: if isList part then [ (head part) ] else [ ]) parts;
        parsed = expression tokens;
      in
      if parsed.valid && parsed.rest == [ ] then parsed.truth else null;

  packages = uvLock.package or [ ];
  packageKey = package: "${package.name}==${package.version or ""}";
  byKey = lib.groupBy packageKey packages;
  groupEdges = groups: lib.concatLists (builtins.attrValues groups);
  edges =
    package:
    (package.dependencies or [ ])
    ++ groupEdges (package.optional-dependencies or { })
    ++ groupEdges (package.dev-dependencies or { });
  resolve =
    edge:
    if !(isAttrs edge && edge ? name) then
      # Unknown root/edge schema must never remove a possibly active package.
      map (package: { key = packageKey package; }) packages
    else if edge ? marker && markerTruth edge.marker == false then
      [ ]
    else
      map (package: { key = packageKey package; }) (
        builtins.filter (
          package: package.name == edge.name && (!(edge ? version) || (package.version or "") == edge.version)
        ) packages
      );
  manifest = uvLock.manifest or { };
  roots =
    map (key: { inherit key; }) memberKeys
    ++ lib.concatMap resolve (
      (manifest.requirements or [ ]) ++ groupEdges (manifest.dependency-groups or { })
    );
  reachable = builtins.genericClosure {
    startSet = if roots == [ ] then map (package: { key = packageKey package; }) packages else roots;
    operator =
      item: lib.concatMap (package: lib.concatMap resolve (edges package)) (byKey.${item.key} or [ ]);
  };
in
{
  inherit markerTruth;
  keys = lib.genAttrs (map (item: item.key) reachable) (_: true);
}
