"""EQ models built from the server's model cache (model.npz plus textures) into Blender objects. Runs under Blender's Python."""
import math
import os

import bpy
import mathutils
import numpy

import bridgeMeshAccess
import bridgeObjects

alphaThreshold = 0.5
missingTextureColor = (1.0, 0.0, 1.0, 1.0)
untinted = 0xFFFFFF


def missingTextureMaterial(textureName):
  """Faces whose texture no linked archive holds: flat magenta, so the gap is visible in every render."""
  materialName = f"eq_missing_{textureName}"
  material = bpy.data.materials.get(materialName)
  if material is None:
    material = bpy.data.materials.new(materialName)
    material.use_nodes = True
    shader = material.node_tree.nodes["Principled BSDF"]
    shader.inputs["Base Color"].default_value = missingTextureColor
    shader.inputs["Roughness"].default_value = 1.0
    shader.inputs["Specular IOR Level"].default_value = 0.0
  return material


def linearChannel(value):
  channel = value / 255
  return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def modelMaterial(folder, textureName, cutout, tint):
  """One material per texture, cutout mode, and tint, reused across objects built from the same cache folder. A tint other than white
  multiplies the texture's color, as the client tints hair (0xRRGGBB)."""
  materialName = f"eq_{os.path.basename(folder)}_{textureName}{'_cutout' if cutout else ''}{'' if tint == untinted else f'_{tint:06x}'}"
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
  if tint == untinted:
    material.node_tree.links.new(diffuse.outputs["Color"], shader.inputs["Base Color"])
  else:
    multiply = nodes.new("ShaderNodeMix")
    multiply.data_type = "RGBA"
    multiply.blend_type = "MULTIPLY"
    multiply.inputs["Factor"].default_value = 1.0
    colorInputs = [socket for socket in multiply.inputs if socket.type == "RGBA"]
    material.node_tree.links.new(diffuse.outputs["Color"], colorInputs[0])
    colorInputs[1].default_value = (*(linearChannel((tint >> shift) & 0xFF) for shift in (16, 8, 0)), 1.0)
    material.node_tree.links.new(next(socket for socket in multiply.outputs if socket.type == "RGBA"), shader.inputs["Base Color"])
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
  missing = {str(name) for name in data["missingTextures"]}
  slots = {}
  materialIndices = numpy.empty(len(triangles), dtype=numpy.int32)
  for index, (textureName, cutout, tint) in enumerate(zip(data["textureNames"], data["cutouts"], data["tints"])):
    key = (str(textureName), bool(cutout), int(tint))
    if key not in slots:
      slots[key] = len(slots)
      mesh.materials.append(missingTextureMaterial(key[0]) if key[0] in missing else modelMaterial(folder, *key))
    materialIndices[index] = slots[key]
  mesh.polygons.foreach_set("material_index", materialIndices)
  mesh.update()
  mesh.validate()
  return mesh


def modelObject(folder, name, scale, location, rotationDegrees):
  """An object built from a model cache, scaled, turned rotationDegrees about Z (counter-clockwise from above), its model origin at `location`."""
  modelObjectInstance = bpy.data.objects.new(name, buildModelMesh(folder, name))
  modelObjectInstance.scale = (scale, scale, scale)
  modelObjectInstance.rotation_euler = (0, 0, math.radians(rotationDegrees))
  modelObjectInstance.location = location
  return modelObjectInstance


def placeModel(modelFolder, name, location, rotationDegrees, scale, avatarHeight, snapToGround, collection):
  """Place a cached EQ model with its origin at `location`; snapToGround instead stands the origin avatarHeight above the surface below, as the client stands a spawn."""
  bridgeObjects.requireNewName(name)
  origin = mathutils.Vector(location)
  ground = None
  if snapToGround:
    hit = bridgeMeshAccess.rayCast(origin + mathutils.Vector((0, 0, 1)), (0, 0, -1), 1000)
    if hit is None:
      raise ValueError(f"No ground below {list(location)} for '{name}'")
    ground = hit[0]
    origin = ground + mathutils.Vector((0, 0, avatarHeight))
  placed = modelObject(modelFolder, name, scale, origin, rotationDegrees)
  bridgeObjects.targetCollection(collection).objects.link(placed)
  bpy.context.view_layer.update()
  return bridgeObjects.describeTransform(placed) | {"ground": ground and bridgeObjects.roundVector(ground), "dimensions": bridgeObjects.roundVector(placed.dimensions)}


commands = {
  "placeModel": (placeModel, True),
}
