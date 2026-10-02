"""Environment emitters: the particle effects a zone places, listed in the client's <zone>_EnvironmentEmitters.txt (any case) under a
Name^EmitterDefIdx^X^Y^Z^Lifespan header (some files add AlwaysVisible before Lifespan), positions in the zone file's axes, as Blender
holds them. EmitterDefIdx picks an effect from the client's emitter definitions (actoremittersnew.edd, which is not read)."""

listSuffix = "_environmentemitters.txt"
shortHeader = ("Name", "EmitterDefIdx", "X", "Y", "Z", "Lifespan")
longHeader = ("Name", "EmitterDefIdx", "X", "Y", "Z", "AlwaysVisible", "Lifespan")


def emitterListPath(clientRoot, zoneName):
  """The zone's emitter list, or None when the client has none."""
  wanted = zoneName.lower() + listSuffix
  matches = [path for path in clientRoot.iterdir() if path.name.lower() == wanted]
  if len(matches) > 1:
    raise ValueError(f"{clientRoot} holds {len(matches)} emitter lists for '{zoneName}': {[path.name for path in matches]}")
  return matches[0] if matches else None


def optionalInteger(value):
  return int(value) if value.strip() else None


def parseEmitters(text, sourceName):
  lines = [line for line in text.splitlines() if line.strip()]
  if not lines:
    raise ValueError(f"{sourceName}: empty emitter list")
  header = tuple(field.strip() for field in lines[0].split("^"))
  if header not in (shortHeader, longHeader):
    raise ValueError(f"{sourceName}: header {'^'.join(header)} is neither {'^'.join(shortHeader)} nor {'^'.join(longHeader)}")
  emitters = []
  for lineNumber, line in enumerate(lines[1:], start=2):
    fields = line.split("^")
    if len(fields) != len(header):
      raise ValueError(f"{sourceName}: line {lineNumber} has {len(fields)} fields under a {len(header)}-field header: {line}")
    values = dict(zip(header, fields))
    emitters.append({
      "name": values["Name"].strip(), "definition": int(values["EmitterDefIdx"]),
      "position": (float(values["X"]), float(values["Y"]), float(values["Z"])),
      "alwaysVisible": optionalInteger(values["AlwaysVisible"]) if "AlwaysVisible" in values else None,
      "lifespan": optionalInteger(values["Lifespan"]),
    })
  return emitters


def emitterListText(emitters):
  """A list under the long header when every emitter has alwaysVisible, else the short one (most of the client's lists use it); CRLF,
  as the client's files are."""
  withAlwaysVisible = {emitter.get("alwaysVisible") is not None for emitter in emitters}
  if len(withAlwaysVisible) > 1:
    raise ValueError("Either every emitter has alwaysVisible or none does; one list has one header")
  long = withAlwaysVisible == {True}
  lines = ["^".join(longHeader if long else shortHeader)]
  for emitter in emitters:
    if "^" in emitter["name"] or not emitter["name"]:
      raise ValueError(f"Emitter name {emitter['name']!r} must be non-empty and free of '^'")
    if not isinstance(emitter["lifespan"], int):
      raise ValueError(f"Emitter '{emitter['name']}' needs an integer lifespan, got {emitter['lifespan']!r}")
    fields = [emitter["name"], str(emitter["definition"]), *(f"{value:.6f}" for value in emitter["position"])]
    lines.append("^".join(fields + ([str(emitter["alwaysVisible"])] if long else []) + [str(emitter["lifespan"])]))
  return "\r\n".join(lines) + "\r\n"
