import collections
import re

import numpy

import zoneGeometry

playerHeight = 6.0
# EQEmu's navmesh agent climbs slopes up to 60 degrees; the client's own limit is unconfirmed.
walkableNormalZ = 0.5
probeColumnsPerSide = 128
slopeBandEdges = (0, 15, 30, 45, 60, 75, 90)
elevationPercentiles = (5, 25, 50, 75, 95)
topListLength = 20
probeChunkTriangles = 400000
# WLD region-type names and EQG region names start with these codes; anything else is reported as other:<code>.
regionKinds = {"WT": "water", "LA": "lava", "DRNTP": "zoneLine", "AWT": "water", "ALV": "lava", "ATP": "zoneLine", "APK": "pvp", "ASL": "slippery", "AVW": "water"}


def roundList(values, digits=1):
  return [round(float(value), digits) for value in values]


def triangleFrames(geometry):
  corners = geometry["vertices"][geometry["triangles"]]
  crossProducts = numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
  doubledAreas = numpy.linalg.norm(crossProducts, axis=1)
  with numpy.errstate(invalid="ignore", divide="ignore"):
    normalZ = numpy.where(doubledAreas > 0, crossProducts[:, 2] / doubledAreas, 0.0)
  return corners, doubledAreas / 2, normalZ


def boundsSize(bounds):
  return roundList(numpy.asarray(bounds[1]) - numpy.asarray(bounds[0]))


def measureDimensions(geometry, frames):
  vertices = geometry["vertices"]
  terrainBounds = geometry["terrainBounds"]
  allBounds = (vertices.min(0), vertices.max(0))
  footprintBounds = terrainBounds if terrainBounds is not None else allBounds
  footprint = float((footprintBounds[1][0] - footprintBounds[0][0]) * (footprintBounds[1][1] - footprintBounds[0][1]))
  return {
    "terrainMinimum": roundList(terrainBounds[0]) if terrainBounds is not None else None,
    "terrainMaximum": roundList(terrainBounds[1]) if terrainBounds is not None else None,
    "terrainSize": boundsSize(terrainBounds) if terrainBounds is not None else None,
    "allGeometrySize": boundsSize(allBounds),
    "footprint": round(footprint),
    "triangleCount": int(len(geometry["triangles"])),
    "vertexCount": int(len(vertices)),
    "trianglesPer10kSquareUnits": round(len(geometry["triangles"]) / footprint * 10000, 2) if footprint > 0 else None,
    "tileShape": geometry.get("tileShape"),
  }


def weightedPercentiles(values, weights, percentiles):
  order = numpy.argsort(values)
  cumulative = numpy.cumsum(weights[order])
  return [float(values[order][numpy.searchsorted(cumulative, cumulative[-1] * percentile / 100)]) for percentile in percentiles]


def measureSurfaces(geometry, frames):
  corners, areas, normalZ = frames
  surfaces = geometry["triangleSurfaces"]
  totalArea = float(areas.sum())
  areaByKind = {kind: round(float(areas[surfaces == code].sum())) for code, kind in enumerate(zoneGeometry.surfaceKinds)}
  solid = surfaces == zoneGeometry.surfaceCode["solid"]
  solidArea = float(areas[solid].sum())
  slopeDegrees = numpy.degrees(numpy.arccos(numpy.clip(normalZ, -1, 1)))
  slopeBands = {}
  for lower, upper in zip(slopeBandEdges, slopeBandEdges[1:]):
    inBand = solid & (slopeDegrees >= lower) & (slopeDegrees < upper if upper < 90 else slopeDegrees <= 90)
    slopeBands[f"{lower}-{upper}"] = round(float(areas[inBand].sum()) / solidArea, 4) if solidArea else 0.0
  slopeBands["overhang"] = round(float(areas[solid & (slopeDegrees > 90)].sum()) / solidArea, 4) if solidArea else 0.0
  walkable = solid & (normalZ >= walkableNormalZ)
  walkableArea = float(areas[walkable].sum())
  elevations = corners[walkable][:, :, 2].mean(axis=1)
  return {
    "totalArea": round(totalArea),
    "areaByKind": areaByKind,
    "solidSlopeShares": slopeBands,
    "walkableArea": round(walkableArea),
    "walkableElevationPercentiles": dict(zip([f"p{percentile}" for percentile in elevationPercentiles], roundList(weightedPercentiles(elevations, areas[walkable], elevationPercentiles)))) if walkableArea else None,
  }


def probeColumns(geometry, frames):
  """Vertical lines on a grid over the footprint: every solid surface each crosses, as (column, z, normalZ)."""
  corners, areas, normalZ = frames
  bounds = geometry["terrainBounds"] if geometry["terrainBounds"] is not None else (geometry["vertices"].min(0), geometry["vertices"].max(0))
  spacing = max(float(max(bounds[1][0] - bounds[0][0], bounds[1][1] - bounds[0][1])) / probeColumnsPerSide, 1.0)
  originX, originY = float(bounds[0][0]) + spacing / 2, float(bounds[0][1]) + spacing / 2
  columnsX = int((bounds[1][0] - originX) // spacing) + 1
  columnsY = int((bounds[1][1] - originY) // spacing) + 1
  solidIndices = numpy.flatnonzero((geometry["triangleSurfaces"] == zoneGeometry.surfaceCode["solid"]) & (areas > 0))
  hitColumns, hitHeights, hitNormals = [], [], []
  for chunkStart in range(0, len(solidIndices), probeChunkTriangles):
    chunk = solidIndices[chunkStart:chunkStart + probeChunkTriangles]
    triangle = corners[chunk]
    firstX = numpy.clip(numpy.ceil((triangle[:, :, 0].min(1) - originX) / spacing), 0, columnsX).astype(numpy.int64)
    lastX = numpy.clip(numpy.floor((triangle[:, :, 0].max(1) - originX) / spacing), -1, columnsX - 1).astype(numpy.int64)
    firstY = numpy.clip(numpy.ceil((triangle[:, :, 1].min(1) - originY) / spacing), 0, columnsY).astype(numpy.int64)
    lastY = numpy.clip(numpy.floor((triangle[:, :, 1].max(1) - originY) / spacing), -1, columnsY - 1).astype(numpy.int64)
    widths = numpy.maximum(lastX - firstX + 1, 0)
    counts = widths * numpy.maximum(lastY - firstY + 1, 0)
    if counts.sum() == 0:
      continue
    pairTriangles = numpy.repeat(numpy.arange(len(chunk)), counts)
    offsets = numpy.arange(counts.sum()) - numpy.repeat(numpy.cumsum(counts) - counts, counts)
    columnX = firstX[pairTriangles] + offsets % widths[pairTriangles]
    columnY = firstY[pairTriangles] + offsets // widths[pairTriangles]
    pointX = originX + columnX * spacing
    pointY = originY + columnY * spacing
    a, b, c = triangle[pairTriangles, 0], triangle[pairTriangles, 1], triangle[pairTriangles, 2]
    edgeCX, edgeCY = c[:, 0] - a[:, 0], c[:, 1] - a[:, 1]
    edgeBX, edgeBY = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
    toPointX, toPointY = pointX - a[:, 0], pointY - a[:, 1]
    denominator = edgeCX * edgeBY - edgeBX * edgeCY
    # Degenerate (zero-area in plan) triangles divide by zero; the denominator test drops them.
    with numpy.errstate(invalid="ignore", divide="ignore"):
      weightC = (toPointX * edgeBY - edgeBX * toPointY) / denominator
      weightB = (edgeCX * toPointY - toPointX * edgeCY) / denominator
      inside = (denominator != 0) & (weightC >= 0) & (weightB >= 0) & (weightC + weightB <= 1)
      heights = a[:, 2] + weightC * (c[:, 2] - a[:, 2]) + weightB * (b[:, 2] - a[:, 2])
    hitColumns.append((columnY * columnsX + columnX)[inside])
    hitHeights.append(heights[inside])
    hitNormals.append(normalZ[chunk][pairTriangles][inside])
  empty = numpy.array([], dtype=numpy.float64)
  return {
    "columnCount": columnsX * columnsY,
    "spacing": spacing,
    "columns": numpy.concatenate(hitColumns) if hitColumns else numpy.array([], dtype=numpy.int64),
    "heights": numpy.concatenate(hitHeights) if hitHeights else empty,
    "normals": numpy.concatenate(hitNormals) if hitNormals else empty,
  }


def measureVerticality(geometry, frames):
  probe = probeColumns(geometry, frames)
  order = numpy.lexsort((probe["heights"], probe["columns"]))
  columns, heights, normals = probe["columns"][order], probe["heights"][order], probe["normals"][order]
  levelHistogram = collections.Counter()
  enclosedColumns = 0
  floorColumns = 0
  starts = numpy.flatnonzero(numpy.r_[True, columns[1:] != columns[:-1]]) if len(columns) else numpy.array([], dtype=numpy.int64)
  ends = numpy.r_[starts[1:], len(columns)]
  for start, end in zip(starts, ends):
    columnHeights = heights[start:end]
    floors = columnHeights[normals[start:end] >= walkableNormalZ]
    if len(floors) == 0:
      continue
    floorColumns += 1
    levelHistogram[min(1 + int((numpy.diff(floors) > playerHeight).sum()), 4)] += 1
    if (columnHeights > floors[0] + playerHeight).any():
      enclosedColumns += 1
  return {
    "probeColumns": probe["columnCount"],
    "probeSpacing": round(probe["spacing"], 1),
    "floorShare": round(floorColumns / probe["columnCount"], 4),
    "enclosedShare": round(enclosedColumns / floorColumns, 4) if floorColumns else None,
    "levelShares": {("4+" if levels == 4 else str(levels)): round(levelHistogram[levels] / floorColumns, 4) for levels in (1, 2, 3, 4)} if floorColumns else None,
  }


def measureContent(geometry, frames):
  _, areas, _ = frames
  textureAreas = numpy.bincount(geometry["triangleTextures"][geometry["triangleTextures"] >= 0], weights=areas[geometry["triangleTextures"] >= 0], minlength=len(geometry["textureNames"]))
  texturedArea = float(textureAreas.sum())
  topTextures = numpy.argsort(textureAreas)[::-1][:topListLength]
  placementCounts = geometry["placementCounts"]
  return {
    "textureCount": len(geometry["textureNames"]),
    "topTexturesByArea": [{"texture": geometry["textureNames"][index], "areaShare": round(float(textureAreas[index]) / texturedArea, 4)} for index in topTextures if textureAreas[index] > 0] if texturedArea else [],
    "placementCount": sum(placementCounts.values()),
    "distinctModelCount": len(placementCounts),
    "topModels": [{"model": model, "count": count} for model, count in placementCounts.most_common(topListLength)],
    "missingModels": geometry["missingModels"],
    "missingAssetArchives": geometry["missingAssetArchives"],
    "droppedTrianglesByModel": geometry["droppedTriangles"],
  }


def regionKind(regionName):
  prefix = re.match(r"[A-Za-z]+", regionName)
  if prefix is None:
    return "other"
  token = prefix.group(0).upper()
  for knownPrefix, kind in regionKinds.items():
    if token.startswith(knownPrefix):
      return kind
  return f"other:{token}"


def measureRegions(geometry, frames):
  kinds = collections.Counter(regionKind(name) for name in geometry["regionNames"])
  return {"regionCount": len(geometry["regionNames"]), "regionsByKind": dict(sorted(kinds.items()))}


# Steeper than this is a cliff or wall rather than a slope.
cliffDegrees = 50
# A material region of at most this many triangles is an island: speckle rather than a surfaced area.
islandTriangles = 2


def weldedVertexIDs(vertices):
  """One id per distinct position: zone files split vertices along material borders, which hides those borders from shared-index adjacency."""
  _, ids = numpy.unique(numpy.round(vertices, 2), axis=0, return_inverse=True)
  return ids.ravel()


def sharedEdges(vertexIDs, triangles):
  """Pairs of triangles that share an edge."""
  corners = vertexIDs[triangles]
  sides = numpy.sort(numpy.stack([corners[:, [0, 1]], corners[:, [1, 2]], corners[:, [2, 0]]], axis=1).reshape(-1, 2), axis=1)
  owners = numpy.repeat(numpy.arange(len(triangles)), 3)
  keys = sides[:, 0].astype(numpy.int64) * (int(vertexIDs.max()) + 1) + sides[:, 1]
  order = numpy.argsort(keys, kind="stable")
  matching = numpy.flatnonzero(keys[order][1:] == keys[order][:-1])
  return owners[order[matching]], owners[order[matching + 1]]


def connectedLabels(count, first, second):
  """Connected components over the given pairs, by hooking roots to the smaller label and jumping pointers until stable."""
  labels = numpy.arange(count)
  while True:
    lowest = numpy.minimum(labels[first], labels[second])
    numpy.minimum.at(labels, labels[first], lowest)
    numpy.minimum.at(labels, labels[second], lowest)
    while True:
      jumped = labels[labels]
      if numpy.array_equal(jumped, labels):
        break
      labels = jumped
    if numpy.array_equal(labels[first], labels[second]):
      return labels


def islandShare(vertices, triangles, materials):
  """The share of triangles in material regions of at most islandTriangles triangles."""
  if not len(triangles):
    return None
  first, second = sharedEdges(weldedVertexIDs(vertices), triangles)
  same = materials[first] == materials[second]
  labels = connectedLabels(len(triangles), first[same], second[same])
  sizes = numpy.bincount(labels, minlength=len(triangles))
  return round(float((sizes[labels] <= islandTriangles).mean()), 4)


def measureConstruction(geometry, frames):
  """How the zone is built: its terrain (the zone's own meshes: an EQG zone's .ter, a classic zone's region meshes, an EQ terrain zone's
  tiles) against what is placed on it. Placed classic objects are not measured, and an EQ terrain zone surfaces its tiles by ecosystem
  rather than by face, so those measures are None there."""
  _, areas, normalZ = frames
  terrain = ~geometry["triangleIsObject"]
  footprintBounds = geometry["terrainBounds"] if geometry["terrainBounds"] is not None else (geometry["vertices"].min(0), geometry["vertices"].max(0))
  footprint = float((footprintBounds[1][0] - footprintBounds[0][0]) * (footprintBounds[1][1] - footprintBounds[0][1]))
  steep = numpy.degrees(numpy.arccos(numpy.clip(normalZ, -1, 1))) >= cliffDegrees
  terrainArea = float(areas[terrain].sum())
  steepTerrain, steepObjects = float(areas[terrain & steep].sum()), float(areas[~terrain & steep].sum())
  faceSurfaced = geometry["format"] != "eqtzp"
  textures = geometry["triangleTextures"][terrain]
  return {
    "terrainTriangles": int(terrain.sum()),
    "terrainTrianglesPer10kSquareUnits": round(int(terrain.sum()) / footprint * 10000, 2) if footprint > 0 else None,
    "terrainTextures": int(len(numpy.unique(textures[textures >= 0]))) if faceSurfaced else None,
    "terrainIslandTriangleShare": islandShare(geometry["vertices"], geometry["triangles"][terrain], geometry["triangleTextures"][terrain]) if faceSurfaced else None,
    "terrainSteepShare": round(steepTerrain / terrainArea, 4) if terrainArea else None,
    "steepOnTerrainShare": round(steepTerrain / (steepTerrain + steepObjects), 4) if geometry["format"] != "wld" and steepTerrain + steepObjects else None,
    "terrainPaintedShare": round(float(areas[terrain & geometry["trianglePainted"]].sum()) / terrainArea, 4) if faceSurfaced and terrainArea else None,
  }


# Bump a group's version when its method or output changes; only that group is recomputed.
measuredGroups = {
  "dimensions": (1, measureDimensions),
  "surfaces": (1, measureSurfaces),
  "verticality": (1, measureVerticality),
  "content": (2, measureContent),
  "regions": (1, measureRegions),
  "construction": (1, measureConstruction),
}


def measureGroups(geometry, groupNames):
  frames = triangleFrames(geometry)
  return {groupName: {"version": measuredGroups[groupName][0], "value": measuredGroups[groupName][1](geometry, frames)} for groupName in groupNames}
