"""The zone's point lights in a preview, added per vertex as the client adds them (clientPointLights). Every mesh a light reaches is
drawn from a temporary copy carrying, per corner, the light its vertex takes, which the client light group adds before clamping; the
copies replace their originals in the preview scene only. Runs under Blender's Python."""
import bpy
import mathutils
import numpy

import bridgeClientLight
import bridgeEnvironment
import bridgeExport
import bridgeMeshAccess
import clientPointLights

previewCopySuffix = "PointLit"


def sceneLights(scene):
  """The scene's zone lights as the client keeps them: point lights carrying an EQ radius, drawn in renders."""
  depsgraph = bridgeEnvironment.sceneDepsgraph(scene)
  lights = []
  for sceneObject in scene.objects:
    if sceneObject.type == "LIGHT" and not sceneObject.hide_render and bridgeEnvironment.radiusProperty in sceneObject.data:
      lights.append(bridgeEnvironment.lightRecord(sceneObject.evaluated_get(depsgraph)))
  return lights


def readMesh(mesh, matrix):
  """A mesh's vertex positions, corner vertices, and corner normals in world space (normals as the client's model effect turns them,
  through the inverse transpose, then made unit length as export writes them)."""
  positions = numpy.empty(len(mesh.vertices) * 3)
  mesh.vertices.foreach_get("co", positions)
  cornerVertices = numpy.empty(len(mesh.loops), dtype=numpy.int64)
  mesh.loops.foreach_get("vertex_index", cornerVertices)
  stored = mesh.attributes.get(bridgeClientLight.normalAttribute)
  if stored is not None:
    pointNormals = numpy.empty(len(mesh.vertices) * 3)
    stored.data.foreach_get("vector", pointNormals)
    normals = pointNormals.reshape(-1, 3)[cornerVertices]
  else:
    cornerNormals = numpy.empty(len(mesh.loops) * 3)
    mesh.corner_normals.foreach_get("vector", cornerNormals)
    normals = cornerNormals.reshape(-1, 3)
  transform = numpy.array(matrix)
  positions = positions.reshape(-1, 3) @ transform[:3, :3].T + transform[:3, 3]
  normals = normals @ numpy.linalg.inv(transform[:3, :3])
  normals /= numpy.maximum(numpy.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
  return positions, cornerVertices, normals


def takesAllLightsPerVertex(mesh):
  attribute = mesh.attributes.get(bridgeClientLight.takesAllLightsAttribute)
  if attribute is None:
    return None
  values = numpy.empty(len(mesh.vertices), dtype=bool)
  attribute.data.foreach_get("value", values)
  return values


class Unit:
  """One thing the preview draws whose light the client chooses together: a mesh object, or a collection instance with every mesh it
  places, with its kind (region, zone, or model) and what decides its lights."""

  def __init__(self, owner, kind, origin):
    self.owner = owner
    self.kind = kind
    self.origin = origin
    self.parts = []

  def bounds(self):
    corners = numpy.array([part["positions"].min(0) for part in self.parts] + [part["positions"].max(0) for part in self.parts])
    return corners.min(0), corners.max(0)


def renderedMeshes(preview, depsgraph):
  """Each mesh the preview renders, as (owner, evaluated mesh object, world matrix) while the depsgraph instance is valid: the owner is
  the scene object, or the collection instance placing it."""
  for instance in depsgraph.object_instances:
    evaluated = instance.object
    if evaluated.type != "MESH":
      continue
    owner = instance.parent.original if instance.is_instance and instance.parent is not None else evaluated.original
    if owner.name in preview.scene.objects:
      yield owner, evaluated, instance.matrix_world


def reachedOwners(preview, depsgraph, lights):
  """The names of the scene objects some light reaches, by the world bounds of everything each places."""
  corners = {}
  for owner, evaluated, matrix in renderedMeshes(preview, depsgraph):
    corners.setdefault(owner.name, []).extend(matrix @ mathutils.Vector(corner) for corner in evaluated.bound_box)
  reached = set()
  for name, points in corners.items():
    low, high = numpy.array(points).min(0), numpy.array(points).max(0)
    if any(clientPointLights.reachesBox(light, low, high) for light in lights):
      reached.add(name)
  return reached


def collectUnits(preview, depsgraph, terrainNames, reached):
  """The preview's meshes some light reaches as they render, grouped by what owns them, each part's world positions and normals read
  while the depsgraph instance is valid."""
  units = {}
  for owner, evaluated, matrix in renderedMeshes(preview, depsgraph):
    if owner.name not in reached:
      continue
    if owner.name not in units:
      if owner.get(bridgeMeshAccess.clientContentProperty) in ("zone", "zoneFile"):
        kind = "zone"
      elif owner.name in terrainNames:
        kind = "region"
      else:
        kind = "model"
      units[owner.name] = Unit(owner, kind, numpy.array(owner.evaluated_get(depsgraph).matrix_world.translation))
    mesh = evaluated.data
    positions, cornerVertices, normals = readMesh(mesh, matrix)
    units[owner.name].parts.append({
      "member": evaluated.original, "matrix": matrix.copy(), "positions": positions, "cornerVertices": cornerVertices,
      "normals": normals, "takesAllLights": takesAllLightsPerVertex(mesh),
    })
  return list(units.values())


def unitLight(unit, lights):
  """Each part's per-corner light. A model (anything placed that is not terrain, and spawns, doors, and objects the server places,
  none of which carries baked light) takes the three lights scoring highest at its origin among those reaching its bounds, from every
  light; a region takes three per vertex from the lights marked for baked geometry; a client zone's vertices take three each, from
  every light where the import marked them (a placed model without baked light) and from the marked lights elsewhere."""
  low, high = unit.bounds()
  reaching = [light for light in lights if clientPointLights.reachesBox(light, low, high)]
  if not reaching:
    return None
  results = []
  for part in unit.parts:
    cornerPositions = part["positions"][part["cornerVertices"]]
    if unit.kind == "model":
      chosen = clientPointLights.selectForDraw(reaching, unit.origin, low, high, True)
      results.append(clientPointLights.addedLight(cornerPositions, part["normals"], chosen))
    elif unit.kind == "region":
      results.append(clientPointLights.addedLightPerVertex(cornerPositions, part["normals"], clientPointLights.eligible(reaching, False)))
    else:
      if part["takesAllLights"] is None:
        results.append(None)
        continue
      takesAll = part["takesAllLights"][part["cornerVertices"]]
      light = clientPointLights.addedLightPerVertex(cornerPositions, part["normals"], clientPointLights.eligible(reaching, False))
      if takesAll.any():
        everyLight = clientPointLights.addedLightPerVertex(cornerPositions[takesAll], part["normals"][takesAll], reaching)
        light[takesAll] = everyLight
      results.append(light)
  return results


def litCopy(preview, part, light, label):
  """A temporary object drawing a part as placed, its corners carrying light (or none), with its original's properties."""
  member = part["member"]
  mesh = bpy.data.meshes.new_from_object(member.evaluated_get(preview.depsgraph()), preserve_all_data_layers=True, depsgraph=preview.depsgraph())
  mesh.name = f"{label}{previewCopySuffix}"
  if light is not None:
    attribute = mesh.color_attributes.new(bridgeClientLight.pointLightAttribute, "FLOAT_COLOR", "CORNER")
    colors = numpy.ones((len(light), 4), dtype=numpy.float32)
    colors[:, :3] = light
    attribute.data.foreach_set("color", colors.ravel())
  copy = bpy.data.objects.new(f"{label}{previewCopySuffix}", mesh)
  copy.matrix_world = part["matrix"]
  for key in member.keys():
    copy[key] = member[key]
  return preview.addObject(copy)


def applyPointLights(preview, sourceScene, carriedLights):
  """Draw the source scene's zone lights, and the lights the view's character carries (clientPointLights.carriedLight), on the preview's
  meshes; returns what was lit and what could not be."""
  lights = sceneLights(sourceScene) + list(carriedLights)
  if not lights:
    return {"lights": 0, "litObjects": 0, "notLit": []}
  terrainCollection = bpy.data.collections.get(bridgeExport.terrainCollectionName)
  terrainNames = {member.name for member in terrainCollection.all_objects} if terrainCollection is not None else set()
  depsgraph = preview.depsgraph()
  units = collectUnits(preview, depsgraph, terrainNames, reachedOwners(preview, depsgraph, lights))
  litObjects, notLit = 0, []
  for unit in units:
    lightPerPart = unitLight(unit, lights)
    if lightPerPart is None:
      continue
    if all(light is None for light in lightPerPart):
      notLit.append({"object": unit.owner.name, "reason": "a client zone imported before point lights were drawn; import it again"})
      continue
    for index, (part, light) in enumerate(zip(unit.parts, lightPerPart)):
      litCopy(preview, part, light, f"{unit.owner.name}{index}")
    preview.scene.collection.objects.unlink(unit.owner)
    litObjects += 1
  return {"lights": len(lights), "litObjects": litObjects, "notLit": notLit}
