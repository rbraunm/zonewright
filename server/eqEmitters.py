"""Environment emitters: the particle effects a zone places, listed in the client's <zone>_EnvironmentEmitters.txt (any case), read as
eqgame.exe reads it (0x4a1c30, 0x4a1e00): the first line is skipped, and each line's fields are taken by position, whatever the header
says: name, the EnvironmentEmittersNew.edd definition index, x, y, z (the zone file's axes, as Blender holds them), the lifespan in
milliseconds (an emitter is made only when it is above 0), and an optional seventh field the client takes as always visible when it is
not 0. Of the client's two headers, Name^EmitterDefIdx^X^Y^Z^Lifespan and Name^EmitterDefIdx^X^Y^Z^AlwaysVisible^Lifespan, the second
labels its last two columns the other way round from how the client reads them; its files hold 4000000 under AlwaysVisible."""

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
    emitters.append({
      "name": fields[0].strip(), "definition": int(fields[1]), "position": (float(fields[2]), float(fields[3]), float(fields[4])),
      "lifespan": optionalInteger(fields[5]), "alwaysVisible": optionalInteger(fields[6]) if len(fields) == 7 else None,
    })
  return emitters


def emitterListText(emitters):
  """A list under the client's seven-field header when every emitter has alwaysVisible, else its six-field one (most of the client's
  lists use it), each line's fields in the order the client reads them; CRLF, as the client's files are."""
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
    fields = [emitter["name"], str(emitter["definition"]), *(f"{value:.6f}" for value in emitter["position"]), str(emitter["lifespan"])]
    lines.append("^".join(fields + ([str(emitter["alwaysVisible"])] if long else [])))
  return "\r\n".join(lines) + "\r\n"
