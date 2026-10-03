"""The open scene's zone gathered as the zone survey gathers a client zone, so it is measured by the same methods: the terrain
collection's meshes are its terrain, every other rendered mesh and collection instance is placed on it. Runs under Blender's Python."""
import os

import math

import bpy
import mathutils
import mathutils.bvhtree
import numpy

import bridgeExport
from playerScale import playerHeight, walkableNormalZ

# How far a route looks to each side for a drop or a wall, and how finely; how far above for a ceiling; and how far below it still
# finds footing.
routeSideReach = 60.0
routeSideStep = 2.0
routeHeadroomReach = 60.0
routeFootingReach = 60.0
routeProfileRows = 60


def triangulated(sceneObject, depsgraph, matrix):
  """World positions, triangles, and each triangle's material name, from the mesh as evaluated."""
  evaluated = sceneObject.evaluated_get(depsgraph)
  mesh = evaluated.to_mesh()
  try:
    mesh.calc_loop_triangles()
    coordinates = numpy.empty(len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", coordinates)
    positions = coordinates.reshape(-1, 3) @ numpy.array(matrix)[:3, :3].T + numpy.array(matrix)[:3, 3]
    triangles = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    materialIndices = numpy.empty(len(mesh.loop_triangles), dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("material_index", materialIndices)
  finally:
    evaluated.to_mesh_clear()
  slotNames = [slot.material.name if slot.material else None for slot in sceneObject.material_slots]
  return positions, triangles.reshape(-1, 3), [slotNames[index] if index < len(slotNames) else None for index in materialIndices]


def collectConstruction(outputPath):
  """Writes the zone's triangles to outputPath (.npz) and returns its texture names and placement count."""
  scene = bpy.context.scene
  depsgraph = bpy.context.evaluated_depsgraph_get()
  terrainCollection = bpy.data.collections.get(bridgeExport.terrainCollectionName)
  if terrainCollection is None or not any(member.type == "MESH" for member in terrainCollection.all_objects):
    raise ValueError(f"The scene has no '{bridgeExport.terrainCollectionName}' collection with meshes; its meshes are the zone's terrain")
  terrainNames = {member.name for member in terrainCollection.all_objects}
  parts, placements = [], 0
  for sceneObject in scene.objects:
    if sceneObject.hide_render or sceneObject.type in ("LIGHT", "CAMERA"):
      continue
    if sceneObject.type == "MESH":
      isTerrain = sceneObject.name in terrainNames
      parts.append((not isTerrain, triangulated(sceneObject, depsgraph, sceneObject.matrix_world)))
      placements += not isTerrain
    elif sceneObject.type == "EMPTY" and sceneObject.instance_type == "COLLECTION" and sceneObject.instance_collection is not None:
      collection = sceneObject.instance_collection
      offset = mathutils.Matrix.Translation(-collection.instance_offset)
      for member in collection.all_objects:
        if member.type == "MESH" and not member.hide_render:
          parts.append((True, triangulated(member, depsgraph, sceneObject.matrix_world @ offset @ member.matrix_world)))
      placements += 1
  textureNames, vertexChunks, triangleChunks, textureChunks, objectChunks, vertexCount = {}, [], [], [], [], 0
  for isObject, (positions, triangles, materialNames) in parts:
    vertexChunks.append(positions)
    triangleChunks.append(triangles + vertexCount)
    textureChunks.append(numpy.array([-1 if name is None else textureNames.setdefault(name, len(textureNames)) for name in materialNames], dtype=numpy.int64))
    objectChunks.append(numpy.full(len(triangles), isObject))
    vertexCount += len(positions)
  os.makedirs(os.path.dirname(outputPath), exist_ok=True)
  numpy.savez(outputPath, vertices=numpy.concatenate(vertexChunks), triangles=numpy.concatenate(triangleChunks), triangleTextures=numpy.concatenate(textureChunks), triangleIsObject=numpy.concatenate(objectChunks))
  return {"arrays": outputPath, "textureNames": list(textureNames), "placements": placements}


class SolidSurfaces:
  """Ray casts against every rendered mesh in the scene (terrain, rock masses, placed objects), not regions or other helpers."""

  def __init__(self):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    self.members = [
      (sceneObject.matrix_world.copy(), sceneObject.matrix_world.inverted(), mathutils.bvhtree.BVHTree.FromObject(sceneObject, depsgraph))
      for sceneObject in bpy.context.scene.objects if sceneObject.type == "MESH" and not sceneObject.hide_render
    ]
    if not self.members:
      raise ValueError("The scene has no rendered meshes to walk on")

  def cast(self, origin, direction, distance):
    """The nearest world hit point within distance, or None."""
    hit = self.castWithNormal(origin, direction, distance)
    return hit[0] if hit else None

  def footingBelow(self, origin, distance):
    """The first surface below origin within distance that faces up; an underside met first means origin lies inside rock, and the
    search goes on through it."""
    down = mathutils.Vector((0, 0, -1))
    while True:
      hit = self.castWithNormal(origin, down, distance)
      if hit is None:
        return None
      if hit[1].z > 0:
        return hit[0]
      distance -= origin.z - hit[0].z + 0.01
      origin = hit[0] + down * 0.01

  def castWithNormal(self, origin, direction, distance):
    """The nearest world hit point within distance and the normal of the face hit there, or None."""
    nearest = None
    for matrix, inverse, tree in self.members:
      location, normal, _, _ = tree.ray_cast(inverse @ origin, (inverse.to_3x3() @ direction).normalized())
      if location is None:
        continue
      hit = matrix @ location
      along = (hit - origin).length
      if along <= distance and (nearest is None or along < nearest[0]):
        nearest = (along, hit, (matrix.to_3x3().inverted().transposed() @ normal).normalized())
    return nearest[1:] if nearest else None


def routeSamples(path, sampleSpacing):
  """Points every sampleSpacing units along the route, measured across the ground, with the route's horizontal direction at each."""
  points, directions = [], []
  for start, end in zip(path[:-1], path[1:]):
    run = mathutils.Vector((end[0] - start[0], end[1] - start[1], 0.0))
    if run.length < 1e-6:
      raise ValueError(f"The route stands still between {start} and {end}; each point must lie elsewhere across the ground")
    steps = max(1, math.ceil(run.length / sampleSpacing))
    for step in range(steps):
      share = step / steps
      points.append(mathutils.Vector(start) + share * (mathutils.Vector(end) - mathutils.Vector(start)))
      directions.append(run.normalized())
  points.append(mathutils.Vector(path[-1]))
  directions.append(directions[-1])
  return points, directions


def sideClearance(surfaces, footing, side, climb):
  """How far to one side the footing runs before it drops more than a player's height or rises into a wall."""
  for offset in numpy.arange(routeSideStep, routeSideReach + routeSideStep / 2, routeSideStep):
    probe = footing + side * float(offset)
    hit = surfaces.footingBelow(probe + mathutils.Vector((0, 0, playerHeight + climb)), 2 * playerHeight + climb)
    if hit is None or hit.z > footing.z + climb + routeSideStep:
      return float(offset) - routeSideStep
  return None


def walkRoute(path, sampleSpacing):
  if len(path) < 2 or any(len(point) != 3 for point in path):
    raise ValueError(f"A route is at least two [x, y, z] points, got {path!r}")
  if sampleSpacing <= 0:
    raise ValueError(f"sampleSpacing must be positive, got {sampleSpacing}")
  surfaces = SolidSurfaces()
  steepestWalkable = math.degrees(math.acos(walkableNormalZ))
  climb = sampleSpacing * math.tan(math.radians(steepestWalkable)) + 0.5
  up = mathutils.Vector((0, 0, 1))
  points, directions = routeSamples(path, sampleSpacing)
  footing = surfaces.footingBelow(points[0] + up * climb, routeFootingReach + climb)
  if footing is None:
    raise ValueError(f"No footing within {routeFootingReach:g} below the route's start {list(path[0])}")
  rows, problems, travelled = [], [], 0.0
  for index, (point, direction) in enumerate(zip(points, directions)):
    if index:
      probe = mathutils.Vector((point.x, point.y, footing.z + climb))
      hit = surfaces.footingBelow(probe, routeFootingReach + climb)
      if hit is None:
        over = surfaces.castWithNormal(probe, up, routeHeadroomReach)
        if over is not None and over[1].z > 0:
          problems.append(f"rise: the ground climbs steeper than {steepestWalkable:.0f} degrees past {roundVector(footing)} toward [{point.x:.1f}, {point.y:.1f}]")
        else:
          problems.append(f"drop: no footing within {routeFootingReach:g} below {roundVector(footing)} going on to [{point.x:.1f}, {point.y:.1f}]")
        break
      run = math.hypot(hit.x - footing.x, hit.y - footing.y)
      slope = math.degrees(math.atan2(abs(hit.z - footing.z), run))
      travelled += run
      footing = hit
    else:
      slope = 0.0
    ceiling = surfaces.cast(footing + up * 0.05, up, routeHeadroomReach)
    headroom = None if ceiling is None else ceiling.z - footing.z
    side = mathutils.Vector((-direction.y, direction.x, 0.0))
    left, right = sideClearance(surfaces, footing, side, climb), sideClearance(surfaces, footing, -side, climb)
    rows.append({"distance": round(travelled, 1), "at": roundVector(footing), "slopeDegrees": round(slope, 1), "headroom": None if headroom is None else round(headroom, 1), "left": left, "right": right})
    if slope > steepestWalkable:
      problems.append(f"slope {slope:.0f} degrees at {roundVector(footing)}, steeper than {steepestWalkable:.0f}")
    if headroom is not None and headroom < playerHeight:
      problems.append(f"headroom {headroom:.1f} at {roundVector(footing)}, under a player's {playerHeight:g}")
  widths = [(row["left"] if row["left"] is not None else routeSideReach) + (row["right"] if row["right"] is not None else routeSideReach) for row in rows]
  narrowest = int(numpy.argmin(widths))
  steepest = max(range(len(rows)), key=lambda row: rows[row]["slopeDegrees"])
  covered = [row for row in rows if row["headroom"] is not None]
  stride = max(1, math.ceil(len(rows) / routeProfileRows))
  return {
    "length": round(travelled, 1), "samples": len(rows), "walkable": not problems, "problems": problems,
    "steepest": {"slopeDegrees": rows[steepest]["slopeDegrees"], "at": rows[steepest]["at"]},
    "narrowest": {"width": round(widths[narrowest], 1), "at": rows[narrowest]["at"], "left": rows[narrowest]["left"], "right": rows[narrowest]["right"]},
    "lowestHeadroom": min(({"headroom": row["headroom"], "at": row["at"]} for row in covered), key=lambda item: item["headroom"]) if covered else None,
    "profile": rows[::stride],
  }


def roundVector(vector):
  return [round(float(component), 1) for component in vector]


commands = {
  "collectConstruction": (collectConstruction, False),
  "walkRoute": (walkRoute, False),
}
