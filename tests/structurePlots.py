"""A test kit of pieces and a test plot of ground to build structures on, made through the tools as an artist would."""
import math
import struct
import zlib

from conftest import writePNG

slopeDegrees = 15.0
gorgeRims = (-100.0, -20.0)
gorgeFloor = (-80.0, -40.0)
gorgeDepth = 40.0
cliffFoot, cliffBrow, cliffHeight = 12.0, 20.0, 60.0
slopeStart = -40.0
slopeSpan = (0.0, 110.0)
plotHalf = 120.0
# Each test kit piece: kind, size, location in the kit file, and each role's material and repeat.
testKitPieces = {
  "testKitWall25": ("wall", [25, 10, 30], [100, 0, 0], {"face": ("testKitStone", 12.5), "edge": ("testKitTrim", 5)}),
  "testKitWall12": ("wall", [12.5, 10, 30], [130, 0, 0], {"face": ("testKitStone", 12.5), "edge": ("testKitTrim", 5)}),
  "testKitPost": ("post", [12, 12, 34], [160, 0, 0], {"side": ("testKitTimber", 10), "top": ("testKitTrim", 10)}),
  "testKitThinPost": ("post", [8, 8, 34], [180, 0, 0], {"side": ("testKitTimber", 10), "top": ("testKitTrim", 10)}),
  "testKitPlank": ("plank", [8, 2.5, 0.5], [200, 0, 0], {"top": ("testKitTimber", 10), "edge": ("testKitTrim", 10)}),
  "testKitDeck": ("floor", [25, 8, 1], [230, 0, 0], {"top": ("testKitTimber", 12.5), "edge": ("testKitTrim", 5), "under": ("testKitStone", 12.5)}),
  "testKitLeg": ("post", [2, 2, 10], [260, 0, 0], {"side": ("testKitTimber", 5), "top": ("testKitTrim", 5)}),
  "testKitBeam": ("beam", [25, 1.5, 3], [280, 0, 0], {"side": ("testKitTimber", 12), "end": ("testKitTrim", 12)}),
  "testKitRail": ("rail", [25, 0.8, 0.8], [310, 0, 0], {"side": ("testKitTimber", 12.5), "end": ("testKitTrim", 12.5)}),
  "testKitRope": ("ropeRail", [25, 0, 1], [340, 0, 0], {"rope": ("testKitRope", 4)}),
  "testKitTread": ("plank", [8, 2.6, 0.4], [370, 0, 0], {"top": ("testKitTimber", 10), "edge": ("testKitTrim", 10)}),
}


def writeRowsPNG(path, rows):
  """An RGBA PNG whose pixel rows, top first, each take one color."""
  def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
  width = len(rows)
  header = struct.pack(">IIBBBBB", width, len(rows), 8, 6, 0, 0, 0)
  raw = b"".join(b"\x00" + bytes(rgba) * width for rgba in rows)
  path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
  return path


async def testKitMaterials(session, folder):
  textures = folder / "textures"
  textures.mkdir(exist_ok=True)
  colors = {"testKitStone": (180, 160, 130, 255), "testKitTimber": (110, 80, 50, 255), "testKitTrim": (60, 60, 70, 255)}
  for name, color in colors.items():
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(textures / f"{name}.png", 4, 4, color))})
  rope = writeRowsPNG(textures / "testKitRope.png", [(150, 120, 80, 0), (150, 120, 80, 255), (150, 120, 80, 255), (150, 120, 80, 0)])
  await session.expectSuccess("createMaterial", {"name": "testKitRope", "diffuseTexture": str(rope), "cutout": True})


async def testKit(session, folder):
  """The test kit: its materials and pieces (testKitPieces), saved as folder/testKit.blend; returns its path."""
  kitPath = folder / "testKit.blend"
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("saveFile", {"path": str(kitPath)})
  await testKitMaterials(session, folder)
  for name, (kind, size, location, roles) in testKitPieces.items():
    await session.expectSuccess("createKitPiece", {
      "name": name, "kind": kind, "size": size, "location": location,
      "materials": {role: material for role, (material, _) in roles.items()}, "worldUnitsPerRepeat": {role: repeat for role, (_, repeat) in roles.items()},
    })
  await session.expectSuccess("saveFile", {})
  return kitPath


houseCenter = (500.0, 300.0)
# The test house, placed in the test kit about houseCenter: each instance's piece, [x, y] from the center, and facing. Walls stand on a
# 50 x 37.5 rectangle with a post at each corner, so the footprint is the posts' outer corners, 62 x 49.5.
houseWalls = {
  "houseNorthDoor": ("testKitWall25Door", [-12.5, 18.75], 0), "houseNorth": ("testKitWall25", [12.5, 18.75], 0),
  "houseSouthWest": ("testKitWall25", [-12.5, -18.75], 180), "houseSouthEast": ("testKitWall25", [12.5, -18.75], 180),
  "houseEastSouth": ("testKitWall25", [25, -6.25], 90), "houseEastNorth": ("testKitWall12", [25, 12.5], 90),
  "houseWestSouth": ("testKitWall12", [-25, -12.5], 270), "houseWestNorth": ("testKitWall25", [-25, 6.25], 270),
}
housePosts = {f"housePost{corner}": ("testKitPost", [x, y], 0) for corner, (x, y) in {"NE": (25, 18.75), "SE": (25, -18.75), "SW": (-25, -18.75), "NW": (-25, 18.75)}.items()}
houseFootprintHalf = (31.0, 24.75)
houseDoor = {"name": "front", "at": [houseCenter[0] - 12.5, houseCenter[1] + 18.75, 0.0], "facingDegrees": 0}
houseParts = {"exterior": list(houseWalls) + list(housePosts), "interior": ["houseFloor"], "roof": ["houseRoof"]}
houseDoorHeight = 16.0


async def placeHousePiece(session, name, piece, offset, facing, z=0.0):
  await session.expectSuccess("placeKitPiece", {
    "name": name, "kitPath": None, "piece": piece, "location": [houseCenter[0] + offset[0], houseCenter[1] + offset[1], z], "facingDegrees": facing,
  })


async def testPrefab(session, folder):
  """The test kit with a door section, a floor, a roof over the house's walls, and the test house assembled as prefab testKitHouse
  (exterior: walls and corner posts; interior: the floor, its top at the walls' base; roof); returns the kit path."""
  kitPath = await testKit(session, folder)
  wall = {"face": ("testKitStone", 12.5), "edge": ("testKitTrim", 5)}
  extras = {
    "testKitWall25Door": ("wall", [25, 10, 30], [100, 60, 0], wall),
    "testKitFloor": ("floor", [60, 47.5, 1], [100, 120, 0], {"top": ("testKitTimber", 12.5), "edge": ("testKitTrim", 5), "under": ("testKitStone", 12.5)}),
  }
  for name, (kind, size, location, roles) in extras.items():
    await session.expectSuccess("createKitPiece", {
      "name": name, "kind": kind, "size": size, "location": location,
      "materials": {role: material for role, (material, _) in roles.items()}, "worldUnitsPerRepeat": {role: repeat for role, (_, repeat) in roles.items()},
    })
  await session.expectSuccess("cutOpening", {"piece": "testKitWall25Door", "kind": "door", "along": 0, "width": 10, "height": houseDoorHeight})
  for name, (piece, offset, facing) in (houseWalls | housePosts).items():
    await placeHousePiece(session, name, piece, offset, facing)
  await placeHousePiece(session, "houseFloor", "testKitFloor", [0, 0], 0, -1.0)
  roofRoles = {"roof": "testKitTimber", "under": "testKitStone", "gable": "testKitStone", "edge": "testKitTrim"}
  roof = await session.expectSuccess("addRoof", {
    "name": "testKitGableRoof", "kind": "gable", "pitchDegrees": 35, "overhang": 3, "thickness": 1, "over": list(houseWalls), "location": [100, 200, 0],
    "materials": roofRoles, "worldUnitsPerRepeat": dict.fromkeys(roofRoles, 5),
  })
  await session.expectSuccess("placeKitPiece", {"name": "houseRoof", "kitPath": None, "piece": "testKitGableRoof", "location": roof["plateOver"], "facingDegrees": 0})
  await session.expectSuccess("assemblePrefab", {"name": "testKitHouse", "parts": houseParts, "entrances": [houseDoor]})
  await session.expectSuccess("saveFile", {})
  return kitPath


def slopeHeight(y):
  return (slopeStart - y) * math.tan(math.radians(slopeDegrees))


def gorgeHeight(x):
  """The test plot's ground across the gorge: its walls fall 2 for every 1 in from the rims to its floor."""
  inside = min(x - gorgeRims[0], gorgeRims[1] - x)
  return 0.0 if inside <= 0 else max(-gorgeDepth, -2 * inside)


def plotGround(x, y):
  """The test plot's ground at a point away from the cliff's west end: the gorge, the cliff, the slope, or flat."""
  if gorgeRims[0] < x < gorgeRims[1]:
    return gorgeHeight(x)
  if x >= 0 and y >= cliffFoot:
    return min(cliffHeight, (y - cliffFoot) * cliffHeight / (cliffBrow - cliffFoot))
  if slopeSpan[0] <= x <= slopeSpan[1] and y < slopeStart:
    return slopeHeight(y)
  return 0.0


def cliffFaceY(z):
  """Where the test plot's cliff face stands at a height."""
  return cliffFoot + (cliffBrow - cliffFoot) * z / cliffHeight


readFaces = """
import bpy, numpy
found = []
for name in names:
  sceneObject = bpy.data.objects[name]
  mesh = sceneObject.data
  matrix = numpy.array(sceneObject.matrix_world)
  positions = numpy.array([list(vertex.co) for vertex in mesh.vertices]).reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
  attribute = mesh.attributes.get('zonewrightPassable')
  for polygon in mesh.polygons:
    material = sceneObject.material_slots[polygon.material_index].material if polygon.material_index < len(sceneObject.material_slots) else None
    found.append({
      'object': name, 'material': None if material is None else material.name, 'passable': bool(attribute.data[polygon.index].value) if attribute is not None else False,
      'points': positions[list(polygon.vertices)].round(6).tolist(),
    })
result = found
"""


async def faces(session, *names):
  """Every face of the named meshes in the world: its material, whether it is flagged passable, and its corners."""
  return (await session.expectSuccess("runPython", {"code": f"names = {list(names)!r}\n" + readFaces}))["result"]


readParts = """
import bpy, numpy
import bridgeMeshAccess
found = {}
for name in names:
  sceneObject = bpy.data.objects[name]
  points = []
  for part, matrix in bridgeMeshAccess.objectParts(sceneObject):
    world = numpy.array(matrix)
    points += (numpy.array([list(vertex.co) for vertex in part.data.vertices]).reshape(-1, 3) @ world[:3, :3].T + world[:3, 3]).round(6).tolist()
  found[name] = {
    'type': sceneObject.type, 'instance': sceneObject.instance_collection.name if sceneObject.instance_collection is not None else None,
    'mesh': sceneObject.data.name if sceneObject.type == 'MESH' else None, 'points': points,
    'local': numpy.array([list(vertex.co) for vertex in sceneObject.data.vertices]).round(6).tolist() if sceneObject.type == 'MESH' else None,
    'matrix': [list(row) for row in sceneObject.matrix_world],
  }
result = found
"""


async def parts(session, names):
  """Each named object's type, the collection it instances or its mesh, its vertices in the world, and its own mesh's vertices."""
  return (await session.expectSuccess("runPython", {"code": f"names = {list(names)!r}\n" + readParts}))["result"]


def pointsNear(faceList, x, y, radius):
  return [point for face in faceList for point in face["points"] if math.hypot(point[0] - x, point[1] - y) <= radius]


async def testPlot(session, folder):
  """The test plot: ground 240 x 240 every 4 in `terrain`, a gorge 80 wide at its rims (x -100 to -20) and 40 deep (its floor x -80 to
  -40) the full length in y, a cliff 60 tall facing south for x 0 to 120 (its face from y 12 at z 0 to y 20 at z 60, its top to y 120),
  a 15-degree slope rising toward -y south of y -40 for x 0 to 110, flat z 0 elsewhere; saved as folder/testPlot.blend."""
  plotPath = folder / "testPlot.blend"
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [2 * plotHalf, 2 * plotHalf], "spacing": 4, "location": [0, 0, 0], "collection": "terrain"})
  far = 4 * plotHalf
  await session.expectSuccess("sculptOutline", {
    "objectName": "ground", "mode": "carve", "outline": [[gorgeRims[0], -far], [gorgeRims[1], -far], [gorgeRims[1], far], [gorgeRims[0], far]], "base": 0,
    "profile": [[0, 0], [gorgeFloor[0] - gorgeRims[0], -gorgeDepth]], "conformBreaks": False,
  })
  await session.expectSuccess("sculptOutline", {
    "objectName": "ground", "mode": "fill", "outline": [[0, cliffFoot], [far, cliffFoot], [far, far], [0, far]], "base": 0,
    "profile": [[0, 0], [cliffBrow - cliffFoot, cliffHeight]], "conformBreaks": False,
  })
  # A fill along a path holds its end's height past each end (a graded route would run its grade on, digging the flat ground north);
  # its reach runs a unit past the slope's sides so the vertices on them are inside it.
  middle = (slopeSpan[0] + slopeSpan[1]) / 2
  await session.expectSuccess("sculptAlongPath", {
    "objectName": "ground", "mode": "fill", "path": [[middle, slopeStart, 0], [middle, -plotHalf, slopeHeight(-plotHalf)]],
    "radius": (slopeSpan[1] - slopeSpan[0]) / 2 + 1, "strength": 1.0, "profile": [[0, 0], [1, 0]],
  })
  (folder / "textures").mkdir(exist_ok=True)
  groundTexture = writePNG(folder / "textures" / "testPlotGround.png", 4, 4, (120, 110, 90, 255))
  await session.expectSuccess("createMaterial", {"name": "testPlotGround", "diffuseTexture": str(groundTexture)})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "testPlotGround"})
  await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "box", "worldUnitsPerRepeat": 16})
  await session.expectSuccess("saveFile", {"path": str(plotPath)})
  return plotPath


# The zone's own material named as the test kit's stone, and the block it covers, at a repeat six times finer than the kit's.
zoneStoneColor = (40, 90, 160, 255)
zoneStoneRepeat = 2.0


async def sameNamedMaterials(session, folder):
  """The test kit; the test plot with a material of its own named as the kit's testKitStone (another texture, zoneStone.png) on a block
  in the plaza, and a testKitWall25 placed beside it; saved. Returns the kit path."""
  kitPath = await testKit(session, folder)
  await testPlot(session, folder)
  zoneStone = writePNG(folder / "textures" / "zoneStone.png", 4, 4, zoneStoneColor)
  await session.expectSuccess("createMaterial", {"name": "testKitStone", "diffuseTexture": str(zoneStone)})
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "zoneBlock", "size": [8, 8, 8], "location": [60, -14, 0]})
  await session.expectSuccess("assignMaterial", {"objectName": "zoneBlock", "materialName": "testKitStone"})
  await session.expectSuccess("projectUVs", {"objectName": "zoneBlock", "method": "box", "worldUnitsPerRepeat": zoneStoneRepeat})
  await session.expectSuccess("placeKitPiece", {"name": "plazaWall", "kitPath": str(kitPath), "piece": "testKitWall25", "location": [30, -14, 0], "facingDegrees": 0})
  await session.expectSuccess("saveFile", {})
  return kitPath
