"""The zone's particle emitters in a preview, drawn as the client draws their particles at a moment of their steady state
(emitterParticles): each particle a quad facing the camera, a beam along its axis turned to the camera, or a quad lying level, showing
its texture frame times its color, blended over what lies behind (added where its definition adds) and never fogged, after the rest of
the zone, as the client's particle pass draws. Runs under Blender's Python."""
import json
import math
import os

import bpy
import mathutils
import numpy

import bridgeEnvironment
import emitterParticles
import eqEmitters

particleColorAttribute = "eqParticleColor"
materialPrefix = "zonewrightParticles"
originScale = 1e-3
notDrawnNames = 5
# The client draws no particle deeper in the view than this, less its definition's depth bias (0x100721e0 caps the particle far clip at
# 0x1013680c's 500; 0x10074987 skips particles past it).
farthestParticleDepth = 500.0
assetsCache = {}


def loadAssets(assetsPath):
  """The prepared emitter definitions and their textures (the server's emitterAssets), read once per file version."""
  stamp = os.stat(assetsPath).st_mtime_ns
  cached = assetsCache.get(assetsPath)
  if cached is None or cached[0] != stamp:
    with open(assetsPath, encoding="utf-8") as source:
      assetsCache[assetsPath] = (stamp, json.load(source))
  return assetsCache[assetsPath][1]


def sceneEmitters(scene):
  """The scene's emitters drawn in renders, as export records them (bridgeEnvironment.emitterRecord)."""
  depsgraph = bridgeEnvironment.sceneDepsgraph(scene)
  return [
    bridgeEnvironment.emitterRecord(sceneObject.evaluated_get(depsgraph))
    for sceneObject in scene.objects if bridgeEnvironment.isEmitter(sceneObject) and not sceneObject.hide_render
  ]


def particleMaterial(preview, texturePath, additive):
  """The particle pass for one texture: texture times the particle's color, its alpha times the particle's alpha, over what lies
  behind (source alpha and inverse source alpha) or added to it (source alpha and one)."""
  material = bpy.data.materials.new(f"{materialPrefix}{'Added' if additive else 'Blended'}")
  material.use_nodes = True
  material.surface_render_method = "BLENDED"
  material.use_backface_culling = False
  nodes, links = material.node_tree.nodes, material.node_tree.links
  nodes.clear()
  texture = nodes.new("ShaderNodeTexImage")
  texture.image = bpy.data.images.load(texturePath, check_existing=True)
  preview.loadedImages.append(texture.image)
  texture.image.colorspace_settings.name = "Non-Color"
  texture.image.alpha_mode = "STRAIGHT"
  texture.interpolation = "Linear"
  texture.extension = "EXTEND"
  color = nodes.new("ShaderNodeAttribute")
  color.attribute_type = "GEOMETRY"
  color.attribute_name = particleColorAttribute
  tinted = nodes.new("ShaderNodeMix")
  tinted.data_type = "RGBA"
  tinted.blend_type = "MULTIPLY"
  tinted.inputs["Factor"].default_value = 1.0
  colorInputs = [socket for socket in tinted.inputs if socket.type == "RGBA"]
  links.new(texture.outputs["Color"], colorInputs[0])
  links.new(color.outputs["Color"], colorInputs[1])
  alpha = nodes.new("ShaderNodeMath")
  alpha.operation = "MULTIPLY"
  links.new(texture.outputs["Alpha"], alpha.inputs[0])
  links.new(color.outputs["Alpha"], alpha.inputs[1])
  emission = nodes.new("ShaderNodeEmission")
  transparent = nodes.new("ShaderNodeBsdfTransparent")
  output = nodes.new("ShaderNodeOutputMaterial")
  tintedColor = next(socket for socket in tinted.outputs if socket.type == "RGBA")
  if additive:
    weighted = nodes.new("ShaderNodeVectorMath")
    weighted.operation = "SCALE"
    links.new(tintedColor, weighted.inputs[0])
    links.new(alpha.outputs["Value"], weighted.inputs["Scale"])
    links.new(weighted.outputs["Vector"], emission.inputs["Color"])
    added = nodes.new("ShaderNodeAddShader")
    links.new(transparent.outputs["BSDF"], added.inputs[0])
    links.new(emission.outputs["Emission"], added.inputs[1])
    links.new(added.outputs["Shader"], output.inputs["Surface"])
  else:
    links.new(tintedColor, emission.inputs["Color"])
    mixed = nodes.new("ShaderNodeMixShader")
    links.new(alpha.outputs["Value"], mixed.inputs["Fac"])
    links.new(transparent.outputs["BSDF"], mixed.inputs[1])
    links.new(emission.outputs["Emission"], mixed.inputs[2])
    links.new(mixed.outputs["Shader"], output.inputs["Surface"])
  preview.createdMaterials.append(material)
  return material


def quadCorners(particle, mode, camera):
  """A particle's four corners (the texture's top left, top right, bottom right, bottom left): a quad square to the view turned by its
  spin; a beam its height long along its axis, its width across, turned toward the camera; or a quad lying flat in the world's level
  plane turned by its spin, its height along (cos, sin, 0) and its width along (sin, -cos, 0) of its turn (0x10074d5f)."""
  center = mathutils.Vector(particle["center"])
  halfWidth, halfHeight = particle["width"] * 0.5, particle["height"] * 0.5
  offsets = [(-halfWidth, halfHeight), (halfWidth, halfHeight), (halfWidth, -halfHeight), (-halfWidth, -halfHeight)]
  turn = particle["turn"]
  cosine, sine = math.cos(turn), math.sin(turn)
  if mode == "screen":
    right, up = camera["right"], camera["up"]
    return [center + right * (x * cosine - y * sine) + up * (x * sine + y * cosine) for x, y in offsets]
  if mode == "flat":
    along, across = mathutils.Vector((cosine, sine, 0.0)), mathutils.Vector((sine, -cosine, 0.0))
    return [center + across * x + along * y for x, y in offsets]
  axis = mathutils.Vector(particle["axis"]).normalized()
  toCamera = camera["position"] - center
  across = axis.cross(toCamera)
  across = across.normalized() if across.length > 1e-9 else camera["right"]
  top, bottom = center + axis * halfHeight, center - axis * halfHeight
  return [top - across * halfWidth, top + across * halfWidth, bottom + across * halfWidth, bottom - across * halfWidth]


def emitterMesh(preview, label, particles, mode, camera, material, emitterPosition):
  """One mesh of an emitter's particle quads, farthest first so nearer ones blend over them, with each corner's texture coordinates
  and color."""
  forward = camera["forward"]
  ordered = sorted(particles, key=lambda particle: -forward.dot(mathutils.Vector(particle["center"]) - camera["position"]))
  # The client draws its particles after the whole world, blended surfaces too (its frame, 0x10097420, draws the scene at 0x1008b470 and
  # the particles at 0x10072110). Blender orders blended objects by their origins, so each emitter's mesh keeps its origin just off the
  # camera, a farther emitter's farther off.
  origin = camera["position"] + (mathutils.Vector(emitterPosition) - camera["position"]) * originScale
  points, uvs, colors = [], [], []
  for particle in ordered:
    points.extend(quadCorners(particle, mode, camera))
    u, v, width, height = particle["cell"]
    # The client's texture coordinates count v down from the texture's top; Blender's count up from its bottom.
    uvs.extend([(u, 1 - v), (u + width, 1 - v), (u + width, 1 - v - height), (u, 1 - v - height)])
    colors.extend([particle["color"]] * 4)
  mesh = bpy.data.meshes.new(label)
  mesh.from_pydata([tuple(point - origin) for point in points], [], [tuple(range(index, index + 4)) for index in range(0, len(points), 4)])
  layer = mesh.uv_layers.new(name="UVMap")
  layer.data.foreach_set("uv", numpy.array(uvs, dtype=numpy.float32).ravel())
  attribute = mesh.color_attributes.new(particleColorAttribute, "FLOAT_COLOR", "CORNER")
  attribute.data.foreach_set("color", numpy.array(colors, dtype=numpy.float32).ravel())
  mesh.materials.append(material)
  meshObject = bpy.data.objects.new(label, mesh)
  meshObject.location = origin
  return preview.addObject(meshObject)


def emitterParticlesOrReason(emitter, definitions, textures, camera):
  """An emitter's particles the view shows and its definition, or why it draws none."""
  reason = eqEmitters.notMadeReason(emitter, len(definitions))
  if reason is not None:
    return None, None, reason
  index, lifespan = emitter["definition"], emitter["lifespan"]
  definition = definitions[index]
  try:
    particles = emitterParticles.steadyParticles(definition, emitter["name"], emitter["position"], lifespan, list(camera["position"]))
  except ValueError as error:
    return None, None, f"definition {index} ('{definition['name']}'): {error}"
  if not particles:
    return None, None, f"definition {index} ('{definition['name']}') makes no particles"
  if definition["texture"].lower() not in textures:
    return None, None, f"definition {index} ('{definition['name']}') names texture '{definition['texture']}', which no effect folder holds"
  shown = [
    particle for particle in particles
    if camera["forward"].dot(mathutils.Vector(particle["center"]) - camera["position"]) - definition["depthBias"] <= farthestParticleDepth
  ]
  if not shown:
    return None, None, f"farther into the view than the client draws particles ({farthestParticleDepth:g})"
  return shown, definition, None


def drawEmitters(preview, sourceScene, assetsPath):
  """Draw the source scene's emitters into the preview for its camera; returns how many were drawn and their particles, and the ones
  not drawn grouped by why, each group with its count and its first names."""
  emitters = sceneEmitters(sourceScene)
  if not emitters:
    return {"emitters": 0, "particles": 0, "notDrawn": []}
  if assetsPath is None:
    raise ValueError("The scene has emitters and no client emitter definitions were passed; the server reads them from the client (EVERQUEST_CLIENT)")
  assets = loadAssets(assetsPath)
  definitions, textures = assets["definitions"], assets["textures"]
  # The camera was just placed; its world matrix is current only once the preview's depsgraph is evaluated.
  matrix = preview.camera.evaluated_get(preview.depsgraph()).matrix_world
  camera = {
    "position": matrix.translation.copy(), "right": matrix.col[0].xyz.normalized(), "up": matrix.col[1].xyz.normalized(),
    "forward": -matrix.col[2].xyz.normalized(),
  }
  materials, drawn, particleCount, notDrawn = {}, 0, 0, {}
  for emitter in emitters:
    particles, definition, reason = emitterParticlesOrReason(emitter, definitions, textures, camera)
    if reason is not None:
      notDrawn.setdefault(reason, []).append(emitter["name"])
      continue
    key = (textures[definition["texture"].lower()], definition["additive"] == 1)
    if key not in materials:
      materials[key] = particleMaterial(preview, *key)
    emitterMesh(
      preview, f"{materialPrefix}{emitter['name']}", particles, emitterParticles.billboardModes[definition["billboard"]], camera, materials[key],
      emitter["position"],
    )
    drawn += 1
    particleCount += len(particles)
  return {
    "emitters": drawn, "particles": particleCount,
    "notDrawn": [{"reason": reason, "count": len(names), "emitters": sorted(names)[:notDrawnNames]} for reason, names in notDrawn.items()],
  }
