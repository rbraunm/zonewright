"""The client's environment emitter definitions and their textures, prepared once under the tooling root for previews to draw from: a
JSON file of the decoded definitions and, per texture name, a copy Blender reads (eqTextures), rebuilt when the client's files change."""
import hashlib
import json

import eqEmitterDefinitions
import eqTextures

assetsFormat = 1


def fileStamp(path):
  status = path.stat()
  return [path.name, status.st_size, status.st_mtime_ns]


def prepareAssets(clientRoot, toolingRoot):
  """The path of the prepared assets for the client's environment emitters; a texture no effect folder holds is left out, so a
  definition naming it is reported as not drawn."""
  definitionsPath = eqEmitterDefinitions.environmentDefinitionsPath(clientRoot)
  definitions = eqEmitterDefinitions.parseDefinitions(definitionsPath.read_bytes(), definitionsPath.name)
  files = eqEmitterDefinitions.textureFiles(clientRoot)
  sources = {}
  for definition in definitions:
    name = definition["texture"].lower()
    if name and name not in sources:
      sources[name] = files.get(name)
  stamp = {"format": assetsFormat, "definitions": fileStamp(definitionsPath), "textures": {name: fileStamp(path) for name, path in sources.items() if path is not None}}
  digest = hashlib.sha256(json.dumps(stamp, sort_keys=True).encode("utf-8")).hexdigest()[:16]
  folder = toolingRoot / "emitters"
  assetsPath = folder / f"environment-{digest}.json"
  if assetsPath.is_file():
    return assetsPath
  textureFolder = folder / "textures" / digest
  textureFolder.mkdir(parents=True, exist_ok=True)
  textures = {}
  for name, path in sources.items():
    if path is None:
      continue
    readableName, readable = eqTextures.readableTexture(path.name.lower(), path.read_bytes())
    target = textureFolder / readableName
    target.write_bytes(readable)
    textures[name] = str(target)
  temporary = assetsPath.with_suffix(".tmp")
  temporary.write_text(json.dumps({"stamp": stamp, "definitions": definitions, "textures": textures}), encoding="utf-8")
  temporary.replace(assetsPath)
  return assetsPath
