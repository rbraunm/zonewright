"""EQ preview rendering and picking in a temporary scene that links the open scene's objects, lit and fogged as the client lights them
(bridgeClientLight). Runs under Blender's Python."""
import math
import os
import time

import bpy
import mathutils
import numpy
import OpenImageIO

import bridgeBodyGroups
import bridgeBoundaries
import bridgeCaveLight
import bridgeClientLight
import bridgeEmitterDrawing
import bridgeExport
import bridgeExportChecks
import bridgeMeshAccess
import bridgeModels
import bridgePointLights
import bridgeReviewGuides
import bridgeShadings
import bridgeStructureData
import bridgeSurfacing
import bridgeSwim
import playerScale
import skyDrawing
from buildTolerances import groundSearchAbove, groundSearchDistance, laneTintNormalZ
from previewFrame import renderHeight, renderWidth, verticalFieldOfViewDegrees

requiredZoneKeys = (
  "ambientColor", "specialAmbientColor", "bounceColor", "sunColor", "sunAzimuthDegrees", "sunElevationDegrees", "fogColor", "fogStart", "fogEnd",
  "fogDensity", "fogOn", "maxClip", "newEngineZone",
)
# The client turns its sky off when the fog it draws ends nearer than this (eqgame 0x48ae80).
skyFogEndMinimum = 101.0
previewName = "zonewrightPreview"
renderSamples = 4
minimumFieldOfViewDegrees = 10.0
maximumFieldOfViewDegrees = 120.0
maximumFrameSide = 1920
cameraClipStart = 0.5
# A frame view stands back so the framed objects' bounding sphere fits the view with this much to spare.
frameMargin = 1.1
frameMinimumRadius = 0.5
figureDistance = 15.0
figureStep = 1.0
figureClearance = 1.5
figureMinimumDistance = 3.0
figureSideOffset = 1.5
mapClearance = 100.0
valueShadings = ("objects", "curvature", "triangleDensity", "texelDensity")
viewShadings = ("client", "layout", "relief", "coverage") + valueShadings
# The sky is soft everywhere, so an equirectangular image at about a fifth of a degree a pixel draws it.
skyImageHeight = 1024
# Layout shading lights from the game's northwest (+X north, +Y west), the top left of a map, as relief maps do, so slopes read the same
# whatever the zone's sun.
layoutLightDirection = (0.5, 0.5, 0.7071)
layoutAmbient = 0.3
# Layout shading darkens every other height band by this share, so band edges read as contours at any height range.
layoutStripeShade = 0.15
# Swim volumes tint a view: cyan for water and magenta for lava, which shows over lava's oranges.
swimColors = {"water": (0.1, 0.85, 1.0), "lava": (1.0, 0.15, 0.85)}
swimAlpha = 0.3
# A lane tint's columns are read this many at a time, so a zone's whole lane never sits in memory at once.
laneTintChunk = 1 << 22
boundaryColor = (1.0, 0.12, 0.08)
zoneLineColor = (0.2, 1.0, 0.25)
guideAlpha = 0.4
boundaryThickness = 1.0
mapBoundaryPixels = 3
layoutHeightColors = ((0.0, (0.22, 0.36, 0.26)), (0.35, (0.58, 0.56, 0.36)), (0.7, (0.62, 0.45, 0.32)), (1.0, (0.92, 0.9, 0.87)))
# Relief shading is the layout drawing in quiet greys, for a plan's lines and labels to stand out over.
reliefHeightColors = ((0.0, (0.5, 0.5, 0.48)), (1.0, (0.93, 0.93, 0.91)))



def requireZone(zone):
  missing = [key for key in requiredZoneKeys if key not in zone]
  if missing:
    raise ValueError(f"Zone properties missing: {missing}; set them with setZoneProperties (a sky supplies the light and the fog color)")


class PreviewScene:
  """A scene holding links to the open scene's renderable objects (guides, such as plot outlines, only when asked for) plus the
  preview's own camera and world, the client's lighting set from the zone. Where nothing is drawn shows the fog color, or the zone's
  sky (eqSky's state) once drawSky places it for the camera. Objects named in leftOut are neither linked nor drawn as lights or
  emitters, for this preview only."""

  def __init__(self, sourceScene, zone, guides=True, sky=None, leftOut=frozenset()):
    requireZone(zone)
    self.zone = zone
    self.guides = guides
    self.sky = sky
    self.leftOut = frozenset(leftOut)
    self.skyImage = None
    self.figure = None
    self.createdObjects = []
    self.createdMaterials = []
    self.loadedImages = []
    self.scene = bpy.data.scenes.new(previewName)
    for sourceObject in sourceScene.objects:
      if (
        sourceObject.type not in ("LIGHT", "CAMERA") and not sourceObject.hide_render and (guides or bridgeMeshAccess.guideProperty not in sourceObject)
        and sourceObject.name not in self.leftOut
      ):
        self.scene.collection.objects.link(sourceObject)
    bridgeClientLight.drawSidesOfAll()
    self.camera =self.addObject(bpy.data.objects.new(previewName + "Camera", bpy.data.cameras.new(previewName + "Camera")))
    self.camera.data.sensor_fit = "VERTICAL"
    self.camera.data.angle = math.radians(verticalFieldOfViewDegrees)
    self.camera.data.clip_start = cameraClipStart
    # Drawn as for a player with the far clip slider at its maximum: the far clip is the zone's maximum clip.
    self.camera.data.clip_end = zone["maxClip"]
    self.scene.camera = self.camera
    self.configureRender()
    self.configureWorld()
    bridgeClientLight.applyEnvironment(zone)

  def setFrame(self, frame):
    """Render at frame's size [width, height] and vertical field of view, as a camera matched to concept art does."""
    self.scene.render.resolution_x, self.scene.render.resolution_y = frame["size"]
    self.camera.data.angle = math.radians(frame["verticalFieldOfViewDegrees"])

  def frameSize(self):
    return self.scene.render.resolution_x, self.scene.render.resolution_y

  def fittingHalfAngle(self):
    """Half the narrower of the camera's vertical and horizontal fields of view, in radians."""
    width, height = self.frameSize()
    verticalHalf = self.camera.data.angle / 2
    return min(verticalHalf, math.atan(math.tan(verticalHalf) * width / height))

  def addObject(self, newObject):
    self.scene.collection.objects.link(newObject)
    self.createdObjects.append(newObject)
    return newObject

  def configureRender(self):
    render = self.scene.render
    render.engine = "BLENDER_EEVEE"
    render.resolution_x = renderWidth
    render.resolution_y = renderHeight
    render.resolution_percentage = 100
    render.film_transparent = False
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGB"
    render.image_settings.color_depth = "8"
    for stampProperty in bpy.types.RenderSettings.bl_rna.properties:
      if stampProperty.identifier.startswith("use_stamp"):
        setattr(render, stampProperty.identifier, False)
    eevee = self.scene.eevee
    eevee.taa_render_samples = renderSamples
    eevee.use_raytracing = False
    eevee.use_fast_gi = False
    eevee.use_shadows = False
    # Raw: output values are written as computed, as the client writes its texture-times-light bytes.
    self.scene.view_settings.view_transform = "Raw"
    self.scene.view_settings.look = "None"
    self.scene.view_settings.exposure = 0.0
    self.scene.view_settings.gamma = 1.0

  def configureWorld(self):
    world = bpy.data.worlds.new(previewName + "World")
    world.use_nodes = True
    background = world.node_tree.nodes["Background"]
    background.inputs["Color"].default_value = (*self.zone["fogColor"], 1.0)
    background.inputs["Strength"].default_value = 1.0
    self.scene.world = world

  def drawSky(self):
    """The zone's sky behind everything, as the client draws it for the camera's height, which moves its horizon band."""
    if self.sky is None or (self.zone["fogOn"] and bridgeClientLight.effectiveFog(self.zone)[1] < skyFogEndMinimum):
      return
    textures = {texture["path"]: numpy.load(texture["path"]) for satellite in self.sky["satellites"] for texture in satellite["textures"]}
    width = 2 * skyImageHeight
    colors = skyDrawing.skyColors(skyDrawing.equirectangularDirections(width, skyImageHeight), self.sky, self.camera.location.z, textures)
    pixels = numpy.ones((colors.shape[0], 4), dtype=numpy.float32)
    pixels[:, :3] = colors
    self.skyImage = bpy.data.images.new(previewName + "Sky", width, skyImageHeight, alpha=False, float_buffer=True)
    self.skyImage.colorspace_settings.name = "Non-Color"
    self.skyImage.pixels.foreach_set(pixels.ravel())
    nodes = self.scene.world.node_tree.nodes
    environment = nodes.new("ShaderNodeTexEnvironment")
    environment.image = self.skyImage
    environment.interpolation = "Linear"
    self.scene.world.node_tree.links.new(environment.outputs["Color"], nodes["Background"].inputs["Color"])

  def depsgraph(self):
    with bpy.context.temp_override(scene=self.scene, view_layer=self.scene.view_layers[0]):
      return bpy.context.evaluated_depsgraph_get()

  def rayCast(self, origin, direction, distance):
    hit, location, normal, faceIndex, hitObject, _ = self.scene.ray_cast(self.depsgraph(), origin, direction, distance=distance)
    return (location, normal, faceIndex, hitObject) if hit else None

  def addFigure(self, groundPoint, figureModel, facingHeadingDegrees):
    """The figure model faces +X and stands its origin avatarHeight above the ground; facingHeadingDegrees is 0 = +Y, clockwise."""
    origin = groundPoint + mathutils.Vector((0, 0, figureModel["avatarHeight"]))
    self.figure = self.addObject(bridgeModels.modelObject(figureModel["folder"], previewName + "Figure", figureModel["scale"], origin, 90 - facingHeadingDegrees))
    return self.figure

  def tint(self, viewPath, overlays, under=None):
    """Tint a rendered view: first with under (cover and colors over the frame, as laneDecal gives them) where no overlay that hides it
    draws, then with see-through overlays, each (alpha, place, hidesUnder) where place adds its blocks: rendered apart, alone with the
    ground held out and the water left out, each laid over the view at its alpha where it stands, whatever the shading."""
    # A swim box's top lies at the surface it was built under or a little below it (on a sloping river); drawn in one render with the
    # water, the two fight for depth or the surface hides the top. Layout and relief shading override every material, so a guide drawn
    # with the ground would draw as ground. Rendered apart, the ground still hides what lies behind or under each.
    holdout = bpy.data.collections.new(previewName + "Holdout")
    self.scene.collection.children.link(holdout)
    viewLayer = self.scene.view_layers[0]
    override = viewLayer.material_override
    render = self.scene.render
    view = readImagePixels(viewPath)
    overlayPath = os.path.splitext(viewPath)[0] + "_overlay.png"
    drawnOverlays = []
    try:
      for sceneObject in list(self.scene.collection.objects):
        if sceneObject is not self.camera:
          self.scene.collection.objects.unlink(sceneObject)
          if bridgeMeshAccess.waterProperty not in sceneObject:
            holdout.objects.link(sceneObject)
      viewLayer.layer_collection.children[holdout.name].holdout = True
      viewLayer.material_override = None
      render.film_transparent = True
      render.image_settings.color_mode = "RGBA"
      render.filepath = overlayPath
      for alpha, place, hidesUnder in overlays:
        placed = place()
        bpy.ops.render.render(write_still=True, scene=self.scene.name)
        for block in placed:
          self.scene.collection.objects.unlink(block)
        drawnOverlays.append((alpha, readImagePixels(overlayPath), hidesUnder))
      if overlays:
        os.remove(overlayPath)
    finally:
      viewLayer.material_override = override
      bpy.data.collections.remove(holdout)
    if under is not None:
      cover, colors = under
      for _, drawn, hidesUnder in drawnOverlays:
        if hidesUnder:
          cover = cover * (1 - drawn[:, :, 3])
      view[:, :, :3] = view[:, :, :3] * (1 - cover[:, :, None]) + colors * cover[:, :, None]
    for alpha, drawn, _ in drawnOverlays:
      cover = drawn[:, :, 3:] * alpha
      view[:, :, :3] = view[:, :, :3] * (1 - cover) + drawn[:, :, :3] * cover
    writeImagePixels(view, viewPath)

  def surfaceData(self, dataPath):
    """The view rendered again for what each pixel shows (OpenEXR, OpenImageIO-read): its depth from the camera's plane, its normal,
    and whether it shows the zone's surfaces at all, the scale figure held out and the water left out, so the floor under the water
    shows: (depth, normal z, covered) arrays, rows bottom to top as readImagePixels gives them."""
    render, viewLayer = self.scene.render, self.scene.view_layers[0]
    settings = render.image_settings
    holdout = bpy.data.collections.new(previewName + "FigureHoldout")
    self.scene.collection.children.link(holdout)
    water = [sceneObject for sceneObject in self.scene.collection.objects if bridgeMeshAccess.waterProperty in sceneObject]
    kept = (render.film_transparent, settings.color_mode, settings.color_depth, render.filepath)
    try:
      for sceneObject in water:
        self.scene.collection.objects.unlink(sceneObject)
      if self.figure is not None:
        self.scene.collection.objects.unlink(self.figure)
        holdout.objects.link(self.figure)
        viewLayer.layer_collection.children[holdout.name].holdout = True
      viewLayer.use_pass_z = viewLayer.use_pass_normal = True
      render.film_transparent = True
      settings.media_type = "MULTI_LAYER_IMAGE"
      settings.file_format = "OPEN_EXR_MULTILAYER"
      settings.color_depth = "32"
      render.filepath = dataPath
      bpy.ops.render.render(write_still=True, scene=self.scene.name)
    finally:
      if self.figure is not None:
        holdout.objects.unlink(self.figure)
        self.scene.collection.objects.link(self.figure)
      bpy.data.collections.remove(holdout)
      for sceneObject in water:
        self.scene.collection.objects.link(sceneObject)
      viewLayer.use_pass_z = viewLayer.use_pass_normal = False
      settings.media_type = "IMAGE"
      settings.file_format = "PNG"
      render.film_transparent, settings.color_mode, settings.color_depth, render.filepath = kept
    source = OpenImageIO.ImageInput.open(dataPath)
    if source is None:
      raise ValueError(f"The view's surface data at {dataPath} cannot be read: {OpenImageIO.geterror()}")
    try:
      passes = {}
      index = 0
      while source.seek_subimage(index, 0):
        names = list(source.spec().channelnames)
        passes |= {name.split(".", 1)[1]: pixels for name, pixels in zip(names, numpy.moveaxis(source.read_image(OpenImageIO.FLOAT), -1, 0))}
        index += 1
    finally:
      source.close()
    os.remove(dataPath)
    missing = sorted({"Combined.A", "Depth.Z", "Normal.Z"} - set(passes))
    if missing:
      raise ValueError(f"The view's surface data lacks {missing}; it holds {sorted(passes)}")
    return passes["Depth.Z"][::-1], passes["Normal.Z"][::-1], passes["Combined.A"][::-1] > 0.5

  def placeSwimVolumes(self):
    """Each swim volume as a block in its liquid's color."""
    materials = {liquid: self.emissionMaterial("Swim" + liquid, color) for liquid, color in swimColors.items()}
    return [self.addBlock("Swim", bridgeBoundaries.boxCorners(box), materials[bridgeSwim.readBox(box)["liquid"]]) for box in bridgeSwim.swimBoxes()]

  def placeBoundaries(self, thickness):
    """The boundaries (bridgeBoundaries) as red slabs `thickness` thick, so a wall shows from above too, and the zone lines as green
    blocks: guides to design with, never what the client draws."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    wallMaterial = self.emissionMaterial("Boundary", boundaryColor)
    placed = []
    for boundary in bridgeBoundaries.boundaryObjects():
      if boundary.type != "MESH":
        continue
      mesh = bpy.data.meshes.new_from_object(boundary.evaluated_get(depsgraph))
      mesh.materials.clear()
      mesh.materials.append(wallMaterial)
      slab = self.addObject(bpy.data.objects.new(previewName + "Boundary", mesh))
      slab.matrix_world = boundary.matrix_world
      solidify = slab.modifiers.new("thickness", "SOLIDIFY")
      solidify.thickness = thickness
      solidify.offset = 0.0
      placed.append(slab)
    lineMaterial = self.emissionMaterial("ZoneLine", zoneLineColor)
    return placed + [self.addBlock("ZoneLine", bridgeBoundaries.boxCorners(line), lineMaterial) for line in bridgeBoundaries.zoneLineObjects()]

  def addBlock(self, label, points, material):
    """A box from its eight corners, x changing fastest, then y, then z (bridgeBoundaries.boxCorners)."""
    faces = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    mesh = bpy.data.meshes.new(previewName + label)
    mesh.from_pydata(points, [], faces)
    mesh.materials.append(material)
    return self.addObject(bpy.data.objects.new(previewName + label, mesh))

  def emissionMaterial(self, label, color):
    material = bpy.data.materials.new(previewName + label)
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (*color, 1.0)
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(emission.outputs[0], output.inputs["Surface"])
    self.createdMaterials.append(material)
    return material

  def remove(self):
    for material in self.createdMaterials:
      bpy.data.materials.remove(material)
    for image in self.loadedImages:
      if image.users == 0:
        bpy.data.images.remove(image)
    for createdObject in self.createdObjects:
      data = createdObject.data
      bpy.data.objects.remove(createdObject)
      if isinstance(data, bpy.types.Camera):
        bpy.data.cameras.remove(data)
      elif isinstance(data, bpy.types.Light):
        bpy.data.lights.remove(data)
      elif isinstance(data, bpy.types.Mesh):
        bpy.data.meshes.remove(data)
    override = self.scene.view_layers[0].material_override
    bpy.data.worlds.remove(self.scene.world)
    if self.skyImage is not None:
      bpy.data.images.remove(self.skyImage)
    bpy.data.scenes.remove(self.scene)
    if override is not None:
      bpy.data.materials.remove(override)


def readImagePixels(path):
  """An image file's pixels as stored, RGBA from 0 to 1, rows bottom to top as Blender holds them."""
  image = bpy.data.images.load(path)
  try:
    image.colorspace_settings.name = "Non-Color"
    width, height = image.size
    pixels = numpy.empty(width * height * 4, dtype=numpy.float32)
    image.pixels.foreach_get(pixels)
  finally:
    bpy.data.images.remove(image)
  return pixels.reshape(height, width, 4)


def writeImagePixels(pixels, path):
  """Write pixels as readImagePixels gives them to an RGB PNG, as stored."""
  height, width = pixels.shape[:2]
  image = bpy.data.images.new(previewName + "Image", width, height, alpha=False)
  try:
    image.colorspace_settings.name = "Non-Color"
    image.pixels.foreach_set(pixels.ravel())
    image.filepath_raw = path
    image.file_format = "PNG"
    image.save()
  finally:
    bpy.data.images.remove(image)


def lookRotation(forward):
  if forward.length == 0:
    raise ValueError("The view direction has zero length")
  return forward.normalized().to_track_quat("-Z", "Y")


def headingPitchForward(headingDegrees, pitchDegrees):
  heading = math.radians(headingDegrees)
  pitch = math.radians(pitchDegrees)
  return mathutils.Vector((math.sin(heading) * math.cos(pitch), math.cos(heading) * math.cos(pitch), math.sin(pitch)))


def placeCamera(preview, view, figureModel):
  """Position the preview camera from a view; returns a description, including the scale figure when one is placed."""
  viewKeys = set(view)
  camera = preview.camera
  camera.rotation_mode = "QUATERNION"
  if viewKeys == {"camera"}:
    source = bpy.data.objects.get(view["camera"])
    if source is None or source.type != "CAMERA":
      raise ValueError(f"No camera object named '{view['camera']}'")
    # A review camera keeps its pose as its own location and rotation, which reproduce its view exactly; its world matrix decomposed
    # again differs in the last bits and moves the picture by a fraction of a pixel.
    location, rotation = bridgeReviewGuides.reviewPose(source) or source.matrix_world.decompose()[:2]
    camera.location, camera.rotation_quaternion = location, rotation
    concept = bridgeReviewGuides.savedConcept(source)
    if concept is not None:
      preview.setFrame(concept)
    description = {"eye": list(location), "forward": list(rotation @ mathutils.Vector((0, 0, -1))), "figure": None}
    saved = bridgeReviewGuides.savedFigure(source)
    if figureModel is not None and saved is not None:
      footing = standingGround(preview, bridgeMeshAccess.PlayerSurfaces(), saved["at"], "the review camera's scale figure")
      preview.addFigure(footing, figureModel, saved["facingDegrees"])
      description |= {"figure": list(footing), "figureFacingDegrees": saved["facingDegrees"]}
    return description
  if viewKeys == {"eye", "target"}:
    eye, target = mathutils.Vector(view["eye"]), mathutils.Vector(view["target"])
    camera.location, camera.rotation_quaternion = eye, lookRotation(target - eye)
    return {"eye": list(eye), "forward": list((target - eye).normalized()), "figure": None}
  if viewKeys == {"map"}:
    return placeMapCamera(preview, view["map"])
  if viewKeys == {"frame"}:
    return placeFrameCamera(preview, view["frame"])
  if viewKeys - {"figureAt", "bodyGroup"} in ({"standAt", "headingDegrees", "pitchDegrees"}, {"standOn", "headingDegrees", "pitchDegrees"}):
    surfaces = bridgeMeshAccess.PlayerSurfaces()
    ground = standingGround(preview, surfaces, view["standAt"], "standAt") if "standAt" in view else footingPoint(view["standOn"])
    groupName = view["bodyGroup"] if "bodyGroup" in view else playerScale.viewGroup(playerScale.builtForAt(bridgeBodyGroups.zoneBuiltFor(), None))
    body = playerScale.group(groupName)
    # Where the water stands over the eye, the player swims, eye at the surface.
    waterDepth = bridgeMeshAccess.waterDepthAt(bridgeMeshAccess.swimSurfaces(), surfaces, ground)
    swimming = waterDepth is not None and waterDepth > body.eyeHeight - body.swimEyeAboveSurface
    eye = ground + mathutils.Vector((0, 0, waterDepth + body.swimEyeAboveSurface if swimming else body.eyeHeight))
    forward = headingPitchForward(view["headingDegrees"], view["pitchDegrees"])
    camera.location, camera.rotation_quaternion = eye, lookRotation(forward)
    description = {
      "eye": list(eye), "forward": list(forward), "ground": list(ground), "waterDepth": None if waterDepth is None else round(waterDepth, 2),
      "swimming": swimming, "bodyGroup": groupName, "figure": None,
    }
    if figureModel is not None and "figureAt" in view:
      footing = standingGround(preview, surfaces, view["figureAt"], "figureAt")
      toEye = eye - footing
      facing = (view["headingDegrees"] + 180 if toEye.xy.length < 1e-6 else math.degrees(math.atan2(toEye.x, toEye.y))) % 360
      preview.addFigure(footing, figureModel, facing)
      description |= {"figure": list(footing), "figureFacingDegrees": facing}
    elif figureModel is not None:
      description |= {"figure": list(placeScaleFigure(preview, surfaces, ground, view["headingDegrees"], figureModel)), "figureFacingDegrees": (view["headingDegrees"] + 180) % 360}
    return description
  raise ValueError(
    f"A view is {{camera}}, {{eye, target}}, {{map}}, {{frame}}, {{standAt, headingDegrees, pitchDegrees}}, or {{standOn, headingDegrees,"
    f" pitchDegrees}}, the last two with an optional figureAt and bodyGroup; got keys {sorted(viewKeys)}"
  )


def footingPoint(point):
  """A standOn view's footing, stood on as given: no ground is looked for."""
  if not isinstance(point, list) or len(point) != 3 or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in point):
    raise ValueError(f"standOn is a footing [x, y, z], got {point!r}")
  return mathutils.Vector(point)


def standingGround(preview, surfaces, point, name):
  """The ground players stand on at [x, y] (the highest there) or at [x, y, z] (from groundSearchAbove over z down to groundSearchDistance below it)."""
  if len(point) == 2:
    bottom, top = sceneHeightRange(preview)
    ground = surfaces.footingBelow(mathutils.Vector((*point, top + mapClearance)), top - bottom + 2 * mapClearance)
    if ground is None:
      raise ValueError(f"No ground below {name} {list(point)}")
    return ground
  if len(point) == 3:
    ground = surfaces.footingBelow(mathutils.Vector(point) + mathutils.Vector((0, 0, groundSearchAbove)), groundSearchAbove + groundSearchDistance)
    if ground is None:
      raise ValueError(f"No ground at {name} {list(point)}: none from {groundSearchAbove:g} above it to {groundSearchDistance:g} below it")
    return ground
  raise ValueError(f"{name} is [x, y] or [x, y, z], got {point!r}")


def sceneCorners(preview):
  """Bounding box corners of the meshes as evaluated: an object's own bound_box lags behind a shaping pass changed since the last evaluation."""
  depsgraph = preview.depsgraph()
  corners = [sceneObject.matrix_world @ mathutils.Vector(corner) for sceneObject in preview.scene.objects if sceneObject.type == "MESH" for corner in sceneObject.evaluated_get(depsgraph).bound_box]
  if not corners:
    raise ValueError("The scene has no meshes")
  return corners


def sceneHeightRange(preview):
  """The lowest and highest the scene's meshes reach, an imported zone's without the objects it parks far below itself."""
  depsgraph = preview.depsgraph()
  heights = []
  for sceneObject in preview.scene.objects:
    if sceneObject.type != "MESH":
      continue
    corners = [mathutils.Vector(corner) for corner in sceneObject.evaluated_get(depsgraph).bound_box]
    withoutParked = sceneObject.data.get(bridgeMeshAccess.heightsWithoutParkedProperty)
    if withoutParked is not None:
      corners = [mathutils.Vector((corner.x, corner.y, height)) for corner in corners for height in withoutParked]
    heights += [(sceneObject.matrix_world @ corner).z for corner in corners]
  if not heights:
    raise ValueError("The scene has no meshes")
  return min(heights), max(heights)


def applyLayoutShading(preview, bandHeight, heightColors, stripes):
  """Draw every surface unlit in a color for its height across the scene's height range (heightColors: (fraction, RGB) stops),
  banded every bandHeight units so the band edges read as contours (with stripes, every other band a shade darker, so they read where
  the scene's range is so tall a band's color barely differs from the next, as in a cave under a hill), and darker facing away from the
  layout light so slopes read; returns that height range."""
  if bandHeight <= 0:
    raise ValueError(f"bandHeight must be positive, got {bandHeight}")
  bottom, top = sceneHeightRange(preview)
  material = bpy.data.materials.new(previewName + "Layout")
  material.use_nodes = True
  nodes, links = material.node_tree.nodes, material.node_tree.links
  nodes.clear()
  geometry = nodes.new("ShaderNodeNewGeometry")
  height = nodes.new("ShaderNodeSeparateXYZ")
  links.new(geometry.outputs["Position"], height.inputs["Vector"])
  # Snapping to the nearest band rather than the band below keeps flat ground at a round height (a multiple of bandHeight) from
  # flickering between two bands with the GPU's rounding.
  halfBandUp = nodes.new("ShaderNodeMath")
  halfBandUp.operation = "ADD"
  links.new(height.outputs["Z"], halfBandUp.inputs[0])
  halfBandUp.inputs[1].default_value = bandHeight / 2
  banded = nodes.new("ShaderNodeMath")
  banded.operation = "SNAP"
  links.new(halfBandUp.outputs["Value"], banded.inputs[0])
  banded.inputs[1].default_value = bandHeight
  fraction = nodes.new("ShaderNodeMapRange")
  links.new(banded.outputs["Value"], fraction.inputs["Value"])
  fraction.inputs["From Min"].default_value = bottom
  fraction.inputs["From Max"].default_value = max(top, bottom + bandHeight)
  ramp = nodes.new("ShaderNodeValToRGB")
  elements = ramp.color_ramp.elements
  while len(elements) < len(heightColors):
    elements.new(0.5)
  for element, (position, color) in zip(elements, heightColors):
    element.position, element.color = position, (*color, 1.0)
  links.new(fraction.outputs["Result"], ramp.inputs["Fac"])
  facing = nodes.new("ShaderNodeVectorMath")
  facing.operation = "DOT_PRODUCT"
  links.new(geometry.outputs["Normal"], facing.inputs[0])
  facing.inputs[1].default_value = layoutLightDirection
  lit = nodes.new("ShaderNodeMath")
  lit.operation = "MAXIMUM"
  links.new(facing.outputs["Value"], lit.inputs[0])
  lit.inputs[1].default_value = 0.0
  shade = nodes.new("ShaderNodeMath")
  shade.operation = "MULTIPLY_ADD"
  links.new(lit.outputs["Value"], shade.inputs[0])
  shade.inputs[1].default_value = 1 - layoutAmbient
  shade.inputs[2].default_value = layoutAmbient
  if stripes:
    bandIndex = nodes.new("ShaderNodeMath")
    bandIndex.operation = "DIVIDE"
    links.new(banded.outputs["Value"], bandIndex.inputs[0])
    bandIndex.inputs[1].default_value = bandHeight
    whole = nodes.new("ShaderNodeMath")
    whole.operation = "ROUND"
    links.new(bandIndex.outputs["Value"], whole.inputs[0])
    parity = nodes.new("ShaderNodeMath")
    parity.operation = "FLOORED_MODULO"
    links.new(whole.outputs["Value"], parity.inputs[0])
    parity.inputs[1].default_value = 2.0
    striped = nodes.new("ShaderNodeMath")
    striped.operation = "MULTIPLY_ADD"
    links.new(parity.outputs["Value"], striped.inputs[0])
    striped.inputs[1].default_value = -layoutStripeShade
    striped.inputs[2].default_value = 1.0
    darkened = nodes.new("ShaderNodeMath")
    darkened.operation = "MULTIPLY"
    links.new(shade.outputs["Value"], darkened.inputs[0])
    links.new(striped.outputs["Value"], darkened.inputs[1])
    shade = darkened
  shaded = nodes.new("ShaderNodeVectorMath")
  shaded.operation = "SCALE"
  links.new(ramp.outputs["Color"], shaded.inputs[0])
  links.new(shade.outputs["Value"], shaded.inputs["Scale"])
  emission = nodes.new("ShaderNodeEmission")
  links.new(shaded.outputs["Vector"], emission.inputs["Color"])
  output = nodes.new("ShaderNodeOutputMaterial")
  links.new(emission.outputs["Emission"], output.inputs["Surface"])
  preview.scene.view_layers[0].material_override = material
  return bottom, top


def subjectCorners(preview, names):
  """World bounding-box corners of named objects as evaluated: meshes, collection instances by their collection's meshes, and
  structures by their parts."""
  depsgraph = preview.depsgraph()
  corners = []
  for name in [part for given in names for part in bridgeStructureData.namedObjects(given)]:
    sceneObject = bpy.data.objects.get(name)
    if sceneObject is None:
      raise ValueError(f"No object named '{name}'")
    corners += bridgeMeshAccess.worldBoundsCorners(sceneObject, depsgraph)
  if not corners:
    raise ValueError(f"{names} have nothing to frame")
  return corners


def placeFrameCamera(preview, frame):
  """Looking at named objects from a heading and pitch, far enough back that all of them fit in the view."""
  if not isinstance(frame, dict) or set(frame) != {"objects", "headingDegrees", "pitchDegrees"} or not frame["objects"]:
    raise ValueError(f"A frame view is {{\"frame\": {{objects: [names], headingDegrees, pitchDegrees}}}}, got {frame!r}")
  corners = subjectCorners(preview, frame["objects"])
  low = mathutils.Vector([min(corner[axis] for corner in corners) for axis in range(3)])
  high = mathutils.Vector([max(corner[axis] for corner in corners) for axis in range(3)])
  center = (low + high) / 2
  radius = max((high - low).length / 2, frameMinimumRadius)
  forward = headingPitchForward(frame["headingDegrees"], frame["pitchDegrees"])
  distance = radius / math.sin(preview.fittingHalfAngle()) * frameMargin
  camera = preview.camera
  camera.location = center - forward * distance
  camera.rotation_quaternion = lookRotation(forward)
  camera.data.clip_end = max(camera.data.clip_end, distance + 2 * radius)
  # The eye and target reproduce this camera exactly; a frame view taken again re-frames on the objects as they then are.
  return {"eye": list(camera.location), "target": list(center), "forward": list(forward), "framedRadius": radius, "figure": None}


def requireMapView(mapView):
  if not isinstance(mapView, dict) or set(mapView) != {"center", "width"} or len(mapView["center"]) != 2 or mapView["width"] <= 0:
    raise ValueError(f"A map view is {{\"map\": {{\"center\": [x, y], \"width\": w}}}} with a positive width, got {mapView!r}")
  return mapView


def guideThickness(view):
  """How thick boundary guides draw: a few pixels across in a map, so a wall shows from straight above, else a unit."""
  return requireMapView(view["map"])["width"] / renderWidth * mapBoundaryPixels if "map" in view else boundaryThickness


def placeMapCamera(preview, mapView):
  """Straight down from above everything, orthographic, the game's north (+X) up and east (-Y) right, as the in-game map draws."""
  requireMapView(mapView)
  bottom, top = sceneHeightRange(preview)
  if preview.guides:
    heights = [corner[2] for corner in bridgeBoundaries.guideCorners()]
    bottom, top = min([bottom, *heights]), max([top, *heights])
  camera = preview.camera
  camera.data.type = "ORTHO"
  camera.data.sensor_fit = "HORIZONTAL"
  camera.data.ortho_scale = mapView["width"]
  camera.location = mathutils.Vector((*mapView["center"], top + mapClearance))
  camera.rotation_quaternion = mathutils.Quaternion((0.0, 0.0, 1.0), math.radians(-90.0))
  camera.data.clip_end = top - bottom + 2 * mapClearance
  return {"mapCenter": list(mapView["center"]), "mapWidth": mapView["width"], "mapHeight": mapView["width"] * renderHeight / renderWidth, "unitsPerPixel": mapView["width"] / renderWidth, "figure": None}


def placeScaleFigure(preview, surfaces, ground, headingDegrees, figureModel):
  """Walk ahead along what players stand on (surfaces, bridgeMeshAccess.PlayerSurfaces), as a player would, until figureDistance or a
  wall, or a climb or drop over the step of the figure's body group (playerScale), stops the walk; then stand the figure there."""
  stepHeight = playerScale.group(figureModel["bodyGroup"]).stepHeight
  # Probed from a whole unit over the step: from a fractional height the footing lands a rounding error off the ground a saved review
  # camera finds again under it, and the camera would not render its view exactly.
  probe = math.ceil(stepHeight)
  heading = math.radians(headingDegrees)
  ahead = mathutils.Vector((math.sin(heading), math.cos(heading), 0))
  side = mathutils.Vector((math.cos(heading), -math.sin(heading), 0))
  down = mathutils.Vector((0, 0, -1))
  position = ground.copy()
  sideHit = surfaces.cast(position + side * figureSideOffset + mathutils.Vector((0, 0, probe)), down, probe + stepHeight)
  if sideHit is not None:
    position = sideHit
  walked = 0.0
  while walked < figureDistance:
    chest = position + mathutils.Vector((0, 0, figureModel["avatarHeight"]))
    if surfaces.cast(chest, ahead, figureStep + figureClearance) is not None:
      break
    nextGround = surfaces.cast(position + ahead * figureStep + mathutils.Vector((0, 0, probe)), down, probe + stepHeight)
    if nextGround is None:
      break
    position = nextGround
    walked += figureStep
  if walked < figureMinimumDistance:
    raise ValueError(f"No room for the scale figure: the ground ahead stops after {walked:.0f} units; stand her by hand with figureAt [x, y, z] in the view")
  preview.addFigure(position, figureModel, headingDegrees + 180)
  return position


def roundVector(vector, digits=3):
  return [round(float(component), digits) for component in vector]


def requireFrame(frame):
  """A render frame other than the preview's own: {size: [width, height], verticalFieldOfViewDegrees}."""
  if (
    not isinstance(frame, dict) or set(frame) != {"size", "verticalFieldOfViewDegrees"} or len(frame["size"]) != 2
    or not all(isinstance(side, int) and 16 <= side <= maximumFrameSide for side in frame["size"])
    or not minimumFieldOfViewDegrees <= frame["verticalFieldOfViewDegrees"] <= maximumFieldOfViewDegrees
  ):
    raise ValueError(
      f"A frame is {{size: [width, height] of 16 to {maximumFrameSide} pixels, verticalFieldOfViewDegrees {minimumFieldOfViewDegrees:g} to"
      f" {maximumFieldOfViewDegrees:g}}}, got {frame!r}"
    )
  return frame


def requireScrollingLiquids(sourceScene):
  """Refuse a view at a liquid time when a rendered liquid material was made before previews scrolled liquids: it would draw still."""
  materials = {
    slot.material for sceneObject in sourceScene.objects if sceneObject.type == "MESH" and not sceneObject.hide_render for slot in sceneObject.material_slots
    if slot.material is not None and (bridgeMeshAccess.liquidProperty in slot.material or bridgeMeshAccess.clientLiquidProperty in slot.material)
  }
  still = sorted(material.name for material in materials if not bridgeSurfacing.scrollsInPreview(material))
  if still:
    raise ValueError(
      f"Liquid materials {still} were made before previews scrolled liquids and would draw still: make them again (createLiquidMaterial,"
      " then editWater material; an imported zone, importZone again)"
    )


def liquidTimeModulo(liquidTime):
  """The effect time each liquid effect's preshader takes from the client's clock: modulo 100 (100 * frac(|0.01 t|))."""
  if not liquidTime >= 0:
    raise ValueError(f"liquidTime is seconds on the client's effect clock, 0 or more, got {liquidTime!r}")
  return math.fmod(liquidTime, 100.0)


def laneTintColumns(preview, tint):
  """The lane's reached floors a view holds, from a lane tint (tint: playerReach.laneTint's file of columns and the lane's grid): those
  within the camera's far clip and its field, a column's diagonal to spare, sorted by column (key, z times the grid's columns along x
  plus x) and floor: (key, low, label) arrays."""
  columns = numpy.load(tint["path"], mmap_mode="r")
  origin, cellSize, cellHeight = tint["origin"], tint["cellSize"], tint["cellHeight"]
  camera = preview.camera
  eye = numpy.array(camera.location)
  axes = numpy.array(camera.rotation_quaternion.to_matrix())
  clip = camera.data.clip_end
  spare = cellSize * math.sqrt(2)
  width, height = preview.frameSize()
  kept = []
  for start in range(0, len(columns), laneTintChunk):
    part = numpy.asarray(columns[start:start + laneTintChunk])
    points = numpy.stack([origin[2] + (part["z"] + 0.5) * cellSize, origin[0] + (part["x"] + 0.5) * cellSize, origin[1] + part["low"].astype(numpy.float64) * cellHeight], axis=1)
    local = (points - eye) @ axes
    depth = -local[:, 2]
    if camera.data.type == "ORTHO":
      halfWidth = camera.data.ortho_scale / 2
      inside = (numpy.abs(local[:, 0]) <= halfWidth + spare) & (numpy.abs(local[:, 1]) <= halfWidth * height / width + spare)
      inside &= (depth >= -spare) & (depth <= clip + spare)
    else:
      tangent = math.tan(camera.data.angle / 2)
      across = tangent * width / height
      inside = (depth >= -spare) & (numpy.linalg.norm(points - eye, axis=1) <= clip + spare)
      inside &= numpy.abs(local[:, 0]) <= numpy.maximum(depth, 0) * across + spare * (1 + across)
      inside &= numpy.abs(local[:, 1]) <= numpy.maximum(depth, 0) * tangent + spare * (1 + tangent)
    kept.append(part[inside])
  shown = numpy.concatenate(kept) if kept else numpy.zeros(0, dtype=columns.dtype)
  key = shown["z"].astype(numpy.int64) * tint["columnsX"] + shown["x"]
  low = shown["low"].astype(numpy.int64)
  order = numpy.lexsort((low, key))
  return key[order], low[order], shown["label"][order].astype(numpy.int64)


def laneDecal(preview, tint, columns, dataPath):
  """Where a view shows the lane's reached floor, pixel by pixel (columns: laneTintColumns): the view's surface data (surfaceData)
  places each pixel's point from its depth by the camera's projection, and a pixel showing the zone on a face whose normal z is at
  least laneTintNormalZ takes its column's reached floor that it lies on: within a cell height of the floor, which the lane knows to a
  cell height, and down the floor's reach at the pixel's slope under it besides (tint's slopeReach: the column's floor is the highest
  ground in it). Returns the cover (tint's alpha where tinted) and colors over the frame, rows bottom to top as readImagePixels gives
  them, and the labels tinted."""
  depth, normalZ, covered = preview.surfaceData(dataPath)
  height, width = depth.shape
  camera = preview.camera
  projection = numpy.array(camera.calc_matrix_camera(preview.depsgraph(), x=width, y=height))
  rows, across = numpy.indices((height, width), dtype=numpy.float64)
  ndcX = 2 * (across + 0.5) / width - 1
  ndcY = 2 * (rows + 0.5) / height - 1
  if camera.data.type == "ORTHO":
    localX, localY = (ndcX - projection[0, 3]) / projection[0, 0], (ndcY - projection[1, 3]) / projection[1, 1]
  else:
    localX, localY = (ndcX + projection[0, 2]) * depth / projection[0, 0], (ndcY + projection[1, 2]) * depth / projection[1, 1]
  local = numpy.stack([localX, localY, -depth, numpy.ones_like(depth)], axis=-1)
  world = local @ numpy.array(camera.matrix_world).T
  origin, cellSize, cellHeight = tint["origin"], tint["cellSize"], tint["cellHeight"]
  keys, lows, labels = columns
  columnZ = numpy.floor((world[..., 0] - origin[2]) / cellSize).astype(numpy.int64)
  columnX = numpy.floor((world[..., 1] - origin[0]) / cellSize).astype(numpy.int64)
  facing = covered & (normalZ >= laneTintNormalZ) & (columnX >= 0) & (columnX < tint["columnsX"]) & (columnZ >= 0)
  pixelKey = columnZ * tint["columnsX"] + columnX
  first, last = numpy.searchsorted(keys, pixelKey, "left"), numpy.searchsorted(keys, pixelKey, "right")
  reach = numpy.interp(normalZ, *numpy.array(tint["slopeReach"]).T)
  chosen = numpy.full(depth.shape, -1, dtype=numpy.int64)
  floors = origin[1] + lows * cellHeight
  for offset in range(int((last - first)[facing].max()) if facing.any() else 0):
    candidate = first + offset
    pending = facing & (chosen < 0) & (candidate < last)
    floor = floors[numpy.minimum(candidate, len(floors) - 1)]
    lies = pending & (world[..., 2] <= floor + cellHeight) & (world[..., 2] >= floor - reach - cellHeight)
    chosen[lies] = candidate[lies]
  tinted = chosen >= 0
  colors = numpy.zeros((height, width, 3), dtype=numpy.float32)
  shownLabels = sorted({int(label) for label in numpy.unique(labels[chosen[tinted]])})
  for label in shownLabels:
    colors[tinted & (labels[numpy.maximum(chosen, 0)] == label)] = tint["colors"][str(label)]
  return numpy.where(tinted, tint["alpha"], 0.0).astype(numpy.float32), colors, shownLabels


def exportLeavesOut():
  """The names of everything a zone export leaves out (bridgeExport.classifyObjects' excluded list: reference zones, placed client
  spawns, doors, and objects, guides, and the rest), which the archive cannot hold."""
  return frozenset(entry["object"] for entry in bridgeExport.classifyObjects()[1])


def requireFigureOn(figureOn, view, figureModel):
  """A figure stood by hand on a footing as given: {at: [x, y, z], facingDegrees}, in an eye view, with the figure's model."""
  if (
    not isinstance(figureOn, dict) or set(figureOn) != {"at", "facingDegrees"} or not isinstance(figureOn["at"], list) or len(figureOn["at"]) != 3
    or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in [*figureOn["at"], figureOn["facingDegrees"]])
  ):
    raise ValueError(f"figureOn is {{at: [x, y, z], facingDegrees}}, got {figureOn!r}")
  if set(view) != {"eye", "target"} or figureModel is None:
    raise ValueError("figureOn stands the scale figure in an {eye, target} view, with her model")
  return figureOn


def renderView(
  sourceScene, zone, sky, view, outputPath, figureModel, shading, bandHeight, guides, swimVolumes, labels, emitters, frame=None, liquidTime=None,
  carriedLight=None, laneTint=None, exportedOnly=False, figureOn=None, normalMaps=False,
):
  """Render a view; in client shading the zone's point lights and emitters are drawn too (emitters: the server's prepared emitter
  assets, or None without a client), its liquids as they stand at liquidTime on the effect clock (0 when it is None), and the light
  the view's character carries (a clientPointLights.carriedLight record, or None); with laneTint (a lane's tint file and grid, its
  labels' colors by number, and the floor's reach by slope), the view is tinted where it shows floor the lane reached (laneDecal), under
  the swim volumes' tint and hidden by the guides'. exportedOnly leaves out of this render alone, objects, lights, and emitters alike,
  everything a zone export leaves out (exportLeavesOut), as the exported archive draws; figureOn ({at, facingDegrees}) stands the
  scale figure exactly there in an eye view, as viewPose read where a view stood her; normalMaps lights createMaterial materials by
  their normal maps, which the client never does (bridgeClientLight.selectNormalMaps)."""
  if shading not in viewShadings:
    raise ValueError(f"shading must be one of {list(viewShadings)}, got '{shading}'")
  if carriedLight is not None and shading != "client":
    raise ValueError(f"A carried light draws only in client shading, not '{shading}'")
  if normalMaps and shading != "client":
    raise ValueError(f"Normal maps draw only in client shading, not '{shading}'")
  if frame is not None and ("map" in view or "camera" in view):
    raise ValueError("A frame is set for an eye, standAt, or frame view; a map keeps the preview's frame and a review camera its own")
  if figureOn is not None:
    requireFigureOn(figureOn, view, figureModel)
  if liquidTime is not None:
    tau = liquidTimeModulo(liquidTime)
    requireScrollingLiquids(sourceScene)
  # A map, or a drawing for reading shape, coverage, or values, is neither fogged nor has a sky.
  shapeOnly = "map" in view or shading != "client"
  leftOut = exportLeavesOut() if exportedOnly else frozenset()
  preview = PreviewScene(sourceScene, zone | {"fogOn": False} if shapeOnly else zone, guides, None if shapeOnly else sky, leftOut)
  try:
    if liquidTime is not None:
      preview.scene[bridgeSurfacing.liquidTimeProperty] = tau
    if frame is not None:
      preview.setFrame(requireFrame(frame))
    description = placeCamera(preview, view, figureModel)
    if figureOn is not None:
      preview.addFigure(mathutils.Vector(figureOn["at"]), figureModel, figureOn["facingDegrees"])
      description |= {"figure": list(figureOn["at"]), "figureFacingDegrees": figureOn["facingDegrees"]}
    if exportedOnly:
      description["leftOut"] = len(leftOut)
    if normalMaps:
      bridgeClientLight.selectNormalMaps(True)
      description["normalMaps"] = {"materials": bridgeSurfacing.normalMappedMaterials()}
    if preview.camera.data.type == "PERSP":
      description["verticalFieldOfViewDegrees"] = round(math.degrees(preview.camera.data.angle), 4)
    preview.drawSky()
    if shading != "client" and preview.camera.data.type != "ORTHO":
      preview.camera.data.clip_end = max((corner - preview.camera.location).length for corner in sceneCorners(preview)) + mapClearance
    if laneTint is not None:
      laneColumns = laneTintColumns(preview, laneTint)
    if labels is not None or shading in valueShadings:
      description |= bridgeShadings.prepareView(preview, shading, labels, os.path.splitext(outputPath)[0] + "_pass.exr")
    if shading == "client":
      description["daylight"] = {"daylitTerrain": bridgeCaveLight.applyDaylight(preview)}
      description["pointLights"] = bridgePointLights.applyPointLights(preview, sourceScene, [] if carriedLight is None else [carriedLight])
      description["emitters"] = bridgeEmitterDrawing.drawEmitters(preview, sourceScene, emitters)
    if shading == "coverage":
      description["coverage"] = bridgeExportChecks.drawCoverage(preview)
    elif shading in ("layout", "relief"):
      description["heightRange"] = list(applyLayoutShading(preview, bandHeight, layoutHeightColors if shading == "layout" else reliefHeightColors, shading == "layout"))
      description["bandHeight"] = bandHeight
    preview.scene.render.filepath = outputPath
    start = time.perf_counter()
    bpy.ops.render.render(write_still=True, scene=preview.scene.name)
    decal = None
    if laneTint is not None:
      cover, colors, shownLabels = laneDecal(preview, laneTint, laneColumns, os.path.splitext(outputPath)[0] + "_surface.exr")
      decal = (cover, colors)
      description["laneTint"] = {
        "columns": len(laneColumns[0]), "tintedShare": round(float((cover > 0).mean()), 4), "labels": shownLabels, "farClip": round(preview.camera.data.clip_end, 2),
      }
    overlays = []
    if swimVolumes and bridgeSwim.swimBoxes():
      overlays.append((swimAlpha, preview.placeSwimVolumes, False))
    if guides and bridgeBoundaries.guideCorners():
      overlays.append((guideAlpha, lambda: preview.placeBoundaries(guideThickness(view)), True))
    if overlays or decal is not None:
      preview.tint(outputPath, overlays, decal)
    renderSeconds = time.perf_counter() - start
    width, height = preview.frameSize()
  finally:
    if normalMaps:
      bridgeClientLight.selectNormalMaps(False)
    preview.remove()
  return {key: roundVector(value) if isinstance(value, list) else value for key, value in description.items()} | {
    "outputPath": outputPath,
    "width": width,
    "height": height,
    "renderSeconds": round(renderSeconds, 2),
  }


def viewPose(sourceScene, zone, view, figureModel):
  """A perspective view's pose as an {eye, target} view takes it again exactly, read without rendering: its eye, a target a unit ahead,
  its frame, and where it stands the scale figure ({at, facingDegrees}, or None), at full precision."""
  preview = PreviewScene(sourceScene, zone, False, None)
  try:
    description = placeCamera(preview, view, figureModel)
    camera = preview.camera
    if camera.data.type != "PERSP":
      raise ValueError(f"A pose is read from a perspective view, not {view!r}")
    forward = camera.rotation_quaternion @ mathutils.Vector((0, 0, -1))
    width, height = preview.frameSize()
    return {
      "eye": list(camera.location), "target": list(camera.location + forward),
      "frame": {"size": [width, height], "verticalFieldOfViewDegrees": math.degrees(camera.data.angle)},
      "figure": None if description["figure"] is None else {"at": list(description["figure"]), "facingDegrees": description["figureFacingDegrees"]},
    }
  finally:
    preview.remove()


def renderPasses(sourceScene, zone, view, outputFolder, passNames, frame=None, carriedLight=None):
  """Render a view once per named bridgeClientLight pass as raw floats (numpy .npy, rows top to bottom, RGBA, alpha 0 where nothing
  is drawn) for calibration: the drawn color (lit), the base (texture) color, the normal, the baked light, the share of scene light,
  the distance, the point lights' light, and the fog's visibility, at frame ({size, verticalFieldOfViewDegrees}, the preview's own when
  None), with the zone's point lights and the light the view's character carries (a clientPointLights.carriedLight record, or None)
  drawn as a client-shaded view draws them."""
  unknown = sorted(set(passNames) - set(bridgeClientLight.passes))
  if unknown:
    raise ValueError(f"Unknown passes {unknown}; known: {list(bridgeClientLight.passes)}")
  preview = PreviewScene(sourceScene, zone)
  written = {}
  try:
    if frame is not None:
      preview.setFrame(requireFrame(frame))
    description = placeCamera(preview, view, None)
    bridgeCaveLight.applyDaylight(preview)
    description["pointLights"] = bridgePointLights.applyPointLights(preview, sourceScene, [] if carriedLight is None else [carriedLight])
    preview.scene.render.film_transparent = True
    preview.scene.render.image_settings.file_format = "OPEN_EXR"
    preview.scene.render.image_settings.color_mode = "RGBA"
    preview.scene.render.image_settings.color_depth = "32"
    width, height = preview.frameSize()
    for passName in passNames:
      bridgeClientLight.selectPass(passName)
      exrPath = os.path.join(outputFolder, f"{passName}.exr")
      preview.scene.render.filepath = exrPath
      bpy.ops.render.render(write_still=True, scene=preview.scene.name)
      image = bpy.data.images.load(exrPath)
      try:
        pixels = numpy.empty(image.size[0] * image.size[1] * 4, dtype=numpy.float32)
        image.pixels.foreach_get(pixels)
      finally:
        bpy.data.images.remove(image)
      arrayPath = os.path.join(outputFolder, f"{passName}.npy")
      numpy.save(arrayPath, pixels.reshape(height, width, 4)[::-1])
      os.remove(exrPath)
      written[passName] = arrayPath
  finally:
    bridgeClientLight.selectPass("lit")
    preview.remove()
  return {key: roundVector(value) if isinstance(value, list) else value for key, value in description.items()} | {"passes": written}


def pick(sourceScene, zone, view, pixel):
  preview = PreviewScene(sourceScene, zone)
  try:
    placeCamera(preview, view, None)
    width, height = preview.frameSize()
    if len(pixel) != 2 or not (0 <= pixel[0] < width and 0 <= pixel[1] < height):
      raise ValueError(f"pixel {pixel} is outside the {width}x{height} render")
    topRight, _, bottomLeft, topLeft = preview.camera.data.view_frame(scene=preview.scene)
    across = (pixel[0] + 0.5) / width
    down = (pixel[1] + 0.5) / height
    localPoint = topLeft + (topRight - topLeft) * across + (bottomLeft - topLeft) * down
    rotation = preview.camera.rotation_quaternion
    if preview.camera.data.type == "ORTHO":
      origin = preview.camera.location + rotation @ mathutils.Vector((localPoint.x, localPoint.y, 0))
      direction = rotation @ mathutils.Vector((0, 0, -1))
    else:
      origin = preview.camera.location.copy()
      direction = (rotation @ localPoint).normalized()
    hit = preview.rayCast(origin, direction, preview.camera.data.clip_end)
    if hit is None:
      return {"hit": False, "origin": roundVector(origin), "direction": roundVector(direction)}
    location, normal, faceIndex, hitObject = hit
    evaluated = hitObject.evaluated_get(preview.depsgraph())
    materialIndex = evaluated.data.polygons[faceIndex].material_index
    material = evaluated.material_slots[materialIndex].material if materialIndex < len(evaluated.material_slots) else None
    return {
      "hit": True,
      "object": hitObject.name,
      "position": roundVector(location),
      "normal": roundVector(normal),
      "distance": round((location - origin).length, 3),
      "material": material.name if material else None,
      "origin": roundVector(origin),
      "direction": roundVector(direction),
    }
  finally:
    preview.remove()


thumbnailSide = 256
# Neutral daylight for looking at a model on its own: a grey ambient and a white sun from the front right, no fog.
thumbnailZone = {
  "ambientColor": [0.5, 0.5, 0.5], "specialAmbientColor": [0.0, 0.0, 0.0], "bounceColor": [0.0, 0.0, 0.0], "sunColor": [0.55, 0.55, 0.55],
  "sunAzimuthDegrees": 135.0, "sunElevationDegrees": 45.0, "fogColor": [0.3, 0.33, 0.37], "fogStart": 0.0, "fogEnd": 1000000.0, "fogDensity": 0.0, "fogOn": False, "maxClip": 1000000.0,
  "newEngineZone": False,
}
# EQ models face +X: seen from in front, to the right, and above.
thumbnailViewDirection = mathutils.Vector((1.0, -0.8, 0.6)).normalized()


def renderModelThumbnails(models, outputFolder):
  """One square render per model ([{folder}]), drawn as the client draws it in neutral daylight and framed to fit from three-quarters
  above; returns the files in order."""
  scratch = bpy.data.scenes.new(previewName + "Models")
  written = []
  try:
    for index, model in enumerate(models):
      modelObject = bridgeModels.modelObject(model["folder"], f"{previewName}Model{index}", 1, (0, 0, 0), 0)
      scratch.collection.objects.link(modelObject)
      preview = PreviewScene(scratch, thumbnailZone)
      try:
        preview.scene.render.resolution_x = preview.scene.render.resolution_y = thumbnailSide
        corners = [modelObject.matrix_world @ mathutils.Vector(corner) for corner in modelObject.evaluated_get(preview.depsgraph()).bound_box]
        center = sum(corners, mathutils.Vector()) / len(corners)
        radius = max(max((corner - center).length for corner in corners), 0.01)
        distance = radius / math.sin(math.radians(verticalFieldOfViewDegrees / 2)) * 1.05
        preview.camera.location = center + thumbnailViewDirection * distance
        preview.camera.rotation_mode = "QUATERNION"
        preview.camera.rotation_quaternion = lookRotation(-thumbnailViewDirection)
        preview.camera.data.clip_start = distance / 100
        preview.camera.data.clip_end = distance + 2 * radius
        outputPath = os.path.join(outputFolder, f"model{index}.png")
        preview.scene.render.filepath = outputPath
        bpy.ops.render.render(write_still=True, scene=preview.scene.name)
        written.append({"file": outputPath, "radius": round(radius, 2)})
      finally:
        preview.remove()
        mesh = modelObject.data
        bpy.data.objects.remove(modelObject)
        bpy.data.meshes.remove(mesh)
  finally:
    bpy.data.scenes.remove(scratch)
  return {"thumbnails": written}
