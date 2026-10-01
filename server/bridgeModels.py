"""EQ models built from the server's model cache (model.npz plus textures) into Blender objects. Runs under Blender's Python."""
import math
import os

import bpy
import mathutils
import numpy

import bridgeMeshAccess
import bridgeObjects

alphaThreshold = 0.5


def modelMaterial(folder, textureName, cutout):
  """One material per texture and cutout mode, reused across objects built from the same cache folder."""
  materialName = f"eq_{os.path.basename(folder)}_{textureName}{'_cutout' if cutout else ''}"
  material = bpy.data.materials.get(materialName)
  texturePath = os.path.join(folder, textureName)
  if material is not None and material.node_tree.nodes["eqDiffuse"].image.filepath == texturePath:
    return material
  material = bpy.data.materials.new(materialName)
  material.use_nodes = True
  nodes = material.node_tree.nodes
  shader = nodes["Principled BSDF"]
  shader.inputs["Roughness"].default_value = 1.0
  shader.inputs["Specular IOR Level"].default_value = 0.0
  diffuse = nodes.new("ShaderNodeTexImage")
  diffuse.name = "eqDiffuse"
  diffuse.image = bpy.data.images.load(texturePath, check_existing=True)
  material.node_tree.links.new(diffuse.outputs["Color"], shader.inputs["Base Color"])
  if cutout:
    threshold = nodes.new("ShaderNodeMath")
    threshold.operation = "GREATER_THAN"
    threshold.inputs[1].default_value = alphaThreshold
    material.node_tree.links.new(diffuse.outputs["Alpha"], threshold.inputs[0])
    material.node_tree.links.new(threshold.outputs["Value"], shader.inputs["Alpha"])
    material.surface_render_method = "DITHERED"
  return material


def buildModelMesh(folder, meshName):
  data = numpy.load(os.path.join(folder, "model.npz"))
  vertices, triangles, uvs = data["vertices"], data["triangles"], data["uvs"]
  mesh = bpy.data.meshes.new(meshName)
  mesh.vertices.add(len(vertices))
  mesh.vertices.foreach_set("co", vertices.astype(numpy.float32).ravel())
  mesh.loops.add(len(triangles) * 3)
  mesh.loops.foreach_set("vertex_index", triangles.astype(numpy.int32).ravel())
  mesh.polygons.add(len(triangles))
  mesh.polygons.foreach_set("loop_start", numpy.arange(0, len(triangles) * 3, 3, dtype=numpy.int32))
  mesh.polygons.foreach_set("loop_total", numpy.full(len(triangles), 3, dtype=numpy.int32))
  uvLayer = mesh.uv_layers.new(name="UVMap")
  uvLayer.data.foreach_set("uv", uvs[triangles.ravel()].astype(numpy.float32).ravel())
  slots = {}
  materialIndices = numpy.empty(len(triangles), dtype=numpy.int32)
  for index, (textureName, cutout) in enumerate(zip(data["textureNames"], data["cutouts"])):
    key = (str(textureName), bool(cutout))
    if key not in slots:
      slots[key] = len(slots)
      mesh.materials.append(modelMaterial(folder, *key))
    materialIndices[index] = slots[key]
  mesh.polygons.foreach_set("material_index", materialIndices)
  mesh.update()
  mesh.validate()
  return mesh


def modelObject(folder, name, scale, footHeight, location, headingDegrees):
  """An object whose feet stand at `location`, turned so the model's front (+X) faces `headingDegrees` (0 = +Y, clockwise)."""
  modelObjectInstance = bpy.data.objects.new(name, buildModelMesh(folder, name))
  modelObjectInstance.scale = (scale, scale, scale)
  modelObjectInstance.rotation_euler = (0, 0, math.radians(90 - headingDegrees))
  modelObjectInstance.location = mathutils.Vector(location) - mathutils.Vector((0, 0, footHeight * scale))
  return modelObjectInstance


def placeSpawn(modelFolder, name, location, headingDegrees, scale, footHeight, snapToGround, collection):
  bridgeObjects.requireNewName(name)
  standAt = mathutils.Vector(location)
  if snapToGround:
    hit = bridgeMeshAccess.rayCast(standAt + mathutils.Vector((0, 0, 1)), (0, 0, -1), 1000)
    if hit is None:
      raise ValueError(f"No ground below {list(location)} for spawn '{name}'")
    standAt = hit[0]
  spawn = modelObject(modelFolder, name, scale, footHeight, standAt, headingDegrees)
  bridgeObjects.targetCollection(collection).objects.link(spawn)
  bpy.context.view_layer.update()
  return bridgeObjects.describeTransform(spawn) | {"feet": bridgeObjects.roundVector(standAt), "height": round(float(spawn.dimensions.z), 3)}


commands = {
  "placeSpawn": (placeSpawn, True),
}
