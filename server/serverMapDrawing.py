"""Plans of the server's map files, north up as planDrawing draws them: the collision a .map gives the server seen from above, shaded
by height and slope (faces seen edge-on from above, such as upright walls, show only as the edges between heights), with the .wtr's
region boxes outlined and numbered; two collisions side by side with the triangles that differ between them; a nav mesh's polygons
(serverNav.inspectNav's) filled by nav area, by NPC component with the islands numbered, or as a difference against another nav, in
panels side by side on one frame, each titled and with its legend; and the server's view of a zone, its collision (what stands upright
drawn as lines, so a thin wall shows), region boxes, NPC nav, and marked points in one plan."""
import collections
import math

import numpy
from PIL import Image, ImageDraw

import planDrawing
import serverMapFiles

backgroundColor = (238, 236, 232)
heightStops = ((0.0, (52, 84, 120)), (0.35, (96, 132, 86)), (0.7, (176, 156, 112)), (1.0, (246, 244, 236)))
lightDirection = numpy.array([1.0, 1.0, 1.5]) / math.sqrt(4.25)
regionColors = {1: planDrawing.swimColors["water"], 2: planDrawing.swimColors["lava"], 3: planDrawing.zoneLineColor}
otherRegionColor = (120, 60, 160)
differenceColor = (220, 30, 30)
titleColor = (20, 20, 20)
titleHeight = 34
panelGap = 8
fadedShare = 0.3
planMargin = 0.04
regionLabelLift = 10
rasterCandidates = 1_000_000
navBackground = (250, 250, 247, 255)
navGridColor = (215, 215, 210, 255)
areaColors = {
  0: (125, 175, 95), 1: (50, 125, 215), 2: (225, 95, 30), 3: (110, 110, 110), 4: (190, 60, 160), 5: (120, 190, 40), 6: (150, 210, 230),
  7: (30, 70, 160), 8: (200, 170, 60), 9: (240, 220, 0), 10: (0, 170, 170), 11: (45, 45, 45),
}
mainPieceColor = (60, 150, 60)
islandColor = (235, 145, 35)
excludedColor = (45, 45, 45)
contextColor = (220, 220, 216)
otherOnlyColor = (130, 50, 190)
islandLabelColor = (120, 50, 0)
islandLabelSize = 11
legendRowHeight = 22
swatchSize = 14
serverGroundStops = ((0.0, (140, 140, 140)), (1.0, (240, 240, 240)))
passableColor = (40, 110, 230)
passableAlpha = 115
navFillAlpha = 120
unreachedIslandColor = (150, 150, 150)
markerRadius = 6
markerLabelSize = 14
uprightColor = (150, 20, 20)
uprightDegrees = 5
uprightLineWidth = 2


def collisionFrame(serverTriangles, longSide):
  """A plan frame holding every triangle with a margin, its longer side longSide pixels and its shape the triangles' extent's (no
  narrower than half its longer side)."""
  points = serverMapFiles.inZoneAxes(serverTriangles).reshape(-1, 3).astype(numpy.float64)
  low, high = points[:, :2].min(axis=0), points[:, :2].max(axis=0)
  across, up = max(high[1] - low[1], 1e-6), max(high[0] - low[0], 1e-6)
  aspect = min(max(across / up, 0.5), 2.0)
  size = (longSide, round(longSide / aspect)) if aspect >= 1 else (round(longSide * aspect), longSide)
  width = max(across, up * size[0] / size[1]) * (1 + 2 * planMargin)
  return planDrawing.PlanFrame(((low[0] + high[0]) / 2, (low[1] + high[1]) / 2), width, size)


def topSurface(zoneTriangles, frame):
  """For each pixel of the frame, the highest triangle over its center and its height there: (heights, triangle index), the height NaN
  and the index -1 where no triangle covers it. Triangles seen edge-on cover no pixel."""
  columns, rows = frame.size
  corners = zoneTriangles.astype(numpy.float64)
  pixelX = (frame.center[1] + frame.width / 2 - corners[:, :, 1]) / frame.width * columns
  pixelY = (frame.center[0] + frame.height / 2 - corners[:, :, 0]) / frame.height * rows
  lowX = numpy.clip(numpy.floor(pixelX.min(axis=1) - 0.5), 0, columns).astype(numpy.int64)
  highX = numpy.clip(numpy.ceil(pixelX.max(axis=1) - 0.5), -1, columns - 1).astype(numpy.int64)
  lowY = numpy.clip(numpy.floor(pixelY.min(axis=1) - 0.5), 0, rows).astype(numpy.int64)
  highY = numpy.clip(numpy.ceil(pixelY.max(axis=1) - 0.5), -1, rows - 1).astype(numpy.int64)
  spanX, spanY = numpy.maximum(highX - lowX + 1, 0), numpy.maximum(highY - lowY + 1, 0)
  counts = spanX * spanY
  heights = numpy.full(columns * rows, -numpy.inf)
  owners = numpy.full(columns * rows, -1, dtype=numpy.int64)
  ends = numpy.cumsum(counts)
  start = 0
  while start < len(counts):
    stop = max(start + 1, int(numpy.searchsorted(ends, (ends[start - 1] if start else 0) + rasterCandidates, side="right")))
    chosen = numpy.arange(start, stop)
    chosen = chosen[counts[chosen] > 0]
    start = stop
    if not len(chosen):
      continue
    triangle = numpy.repeat(chosen, counts[chosen])
    within = numpy.arange(len(triangle)) - numpy.repeat(numpy.cumsum(counts[chosen]) - counts[chosen], counts[chosen])
    column = lowX[triangle] + within % spanX[triangle]
    row = lowY[triangle] + within // spanX[triangle]
    centerX, centerY = column + 0.5, row + 0.5
    ax, ay = pixelX[triangle, 0], pixelY[triangle, 0]
    bx, by = pixelX[triangle, 1] - ax, pixelY[triangle, 1] - ay
    cx, cy = pixelX[triangle, 2] - ax, pixelY[triangle, 2] - ay
    px, py = centerX - ax, centerY - ay
    area = bx * cy - cx * by
    with numpy.errstate(divide="ignore", invalid="ignore"):
      u = (px * cy - cx * py) / area
      v = (bx * py - px * by) / area
      inside = (numpy.abs(area) > 1e-12) & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9)
    triangle, u, v = triangle[inside], u[inside], v[inside]
    pixel = row[inside] * columns + column[inside]
    height = corners[triangle, 0, 2] + u * (corners[triangle, 1, 2] - corners[triangle, 0, 2]) + v * (corners[triangle, 2, 2] - corners[triangle, 0, 2])
    order = numpy.lexsort((height, pixel))
    pixel, height, triangle = pixel[order], height[order], triangle[order]
    last = numpy.append(pixel[1:] != pixel[:-1], True)
    pixel, height, triangle = pixel[last], height[last], triangle[last]
    higher = height > heights[pixel]
    heights[pixel[higher]] = height[higher]
    owners[pixel[higher]] = triangle[higher]
  heights[owners < 0] = numpy.nan
  return heights.reshape(rows, columns), owners.reshape(rows, columns)


def shadedCollision(zoneTriangles, frame, stops=heightStops):
  """The top surface as RGB: a color by height (stops, between the 1st and 99th percentile of what it covers) darkened by slope away
  from a light in the north-west, the background where nothing is."""
  heights, owners = topSurface(zoneTriangles, frame)
  image = numpy.empty((*heights.shape, 3), dtype=numpy.float64)
  image[:] = backgroundColor
  covered = owners >= 0
  if covered.any():
    corners = zoneTriangles[owners[covered]].astype(numpy.float64)
    normals = numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    normals /= numpy.linalg.norm(normals, axis=1, keepdims=True)
    normals[normals[:, 2] < 0] *= -1
    shade = 0.5 + 0.5 * numpy.clip(normals @ lightDirection, 0, 1)
    low, high = numpy.percentile(heights[covered], (1, 99))
    share = numpy.clip((heights[covered] - low) / max(high - low, 1e-9), 0, 1)
    places = numpy.array([place for place, _ in stops])
    colors = numpy.array([color for _, color in stops], dtype=numpy.float64)
    ramp = numpy.stack([numpy.interp(share, places, colors[:, channel]) for channel in range(3)], axis=1)
    image[covered] = ramp * shade[:, None]
  return Image.fromarray(numpy.round(image).astype(numpy.uint8), "RGB")


def regionOutline(region):
  """A .wtr box's footprint in plan: its eight corners placed as the server places them (turned about X, Y, then Z by its rotation in
  degrees, scaled, moved), wrapped in their convex hull."""
  turns = [math.radians(value) for value in region["rotation"]]
  cosine, sine = numpy.cos(turns), numpy.sin(turns)
  aroundX = numpy.array([[1, 0, 0], [0, cosine[0], -sine[0]], [0, sine[0], cosine[0]]])
  aroundY = numpy.array([[cosine[1], 0, sine[1]], [0, 1, 0], [-sine[1], 0, cosine[1]]])
  aroundZ = numpy.array([[cosine[2], -sine[2], 0], [sine[2], cosine[2], 0], [0, 0, 1]])
  signs = numpy.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)])
  corners = (signs * numpy.array(region["halfExtents"])) @ (aroundZ @ aroundY @ aroundX).T * numpy.array(region["scale"]) + numpy.array(region["position"])
  return convexHull([tuple(point) for point in corners[:, :2]])


def convexHull(points):
  points = sorted(set(points))
  if len(points) < 3:
    return points

  def chain(ordered):
    hull = []
    for point in ordered:
      while len(hull) >= 2 and (hull[-1][0] - hull[-2][0]) * (point[1] - hull[-2][1]) - (hull[-1][1] - hull[-2][1]) * (point[0] - hull[-2][0]) <= 0:
        hull.pop()
      hull.append(point)
    return hull[:-1]
  return chain(points) + chain(points[::-1])


def regionColor(region):
  return regionColors.get(region["type"], otherRegionColor)


def drawCollision(serverTriangles, regions, frame):
  """The collision plan (server-axis triangles, as mapCollision gives them) with the .wtr's region boxes (readWater's) outlined in
  their type's color and numbered above them in file order with their type, over a coordinate grid with a scale bar and north."""
  image = shadedCollision(serverMapFiles.inZoneAxes(serverTriangles), frame).convert("RGBA")
  layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
  draw = ImageDraw.Draw(layer)
  planDrawing.drawGrid(draw, frame)
  outlines = [[frame.pixel(point) for point in regionOutline(region)] for region in regions]
  for region, outline in zip(regions, outlines):
    draw.polygon(outline, outline=(*regionColor(region), 255), width=3)
  board = planDrawing.LabelBoard(draw, frame.size)
  planDrawing.labelGrid(board, frame)
  planDrawing.drawScale(board, frame, planDrawing.gridStepFor(frame.width))
  for index, (region, outline) in enumerate(zip(regions, outlines)):
    above = (planDrawing.centroidOf(outline)[0], min(y for _, y in outline) - regionLabelLift)
    board.place(above, f"{index} {serverMapFiles.waterRegionTypeNames.get(region['type'], region['type'])}", regionColor(region), 13)
  return Image.alpha_composite(image, layer).convert("RGB")


def differingTriangles(first, second, tolerance):
  """Indices of the triangles, matched in order, whose corners differ by more than tolerance in any coordinate; those past the shorter
  list's end all differ."""
  shared = min(len(first), len(second))
  moved = numpy.flatnonzero((numpy.abs(first[:shared].astype(numpy.float64) - second[:shared]) > tolerance).any(axis=(1, 2)))
  return numpy.concatenate([moved, numpy.arange(shared, max(len(first), len(second)))])


def drawCollisionComparison(first, firstRegions, second, secondRegions, titles, longSide=900, tolerance=1e-3):
  """Two collisions (server-axis triangles in the server's order) in one frame side by side, each with its own .wtr's region boxes,
  and a third plan with the triangles that differ by more than tolerance (differingTriangles) drawn in red over the first faded: blank
  when the two agree. The third plan's title gives the count."""
  frame = collisionFrame(numpy.concatenate([first, second]), longSide)
  size = frame.size
  differing = differingTriangles(first, second, tolerance)
  longer = first if len(first) >= len(second) else second
  faded = Image.blend(shadedCollision(serverMapFiles.inZoneAxes(first), frame), Image.new("RGB", size, (255, 255, 255)), 1 - fadedShare)
  marks = Image.new("RGBA", size, (0, 0, 0, 0))
  draw = ImageDraw.Draw(marks)
  for triangle in serverMapFiles.inZoneAxes(longer[differing]).astype(numpy.float64):
    draw.polygon([frame.pixel(corner[:2]) for corner in triangle], fill=(*differenceColor, 255), outline=(*differenceColor, 255))
  difference = Image.alpha_composite(faded.convert("RGBA"), marks).convert("RGB")
  panels = [drawCollision(first, firstRegions, frame), drawCollision(second, secondRegions, frame), difference]
  labels = [*titles, f"differ by over {tolerance:g}: {len(differing):,} of {max(len(first), len(second)):,} triangles"]
  sheet = Image.new("RGB", (len(panels) * size[0] + (len(panels) - 1) * panelGap, size[1] + titleHeight), (255, 255, 255))
  sheetDraw = ImageDraw.Draw(sheet)
  for index, (panel, label) in enumerate(zip(panels, labels)):
    left = index * (size[0] + panelGap)
    sheet.paste(panel, (left, titleHeight))
    sheetDraw.text((left + size[0] / 2, titleHeight / 2), label, fill=titleColor, font=planDrawing.fontOf(16), anchor="mm")
  return sheet


def darker(color):
  return tuple(round(channel * 0.7) for channel in color)


def polygonsOf(inspection):
  return [polygon for tile in inspection["tiles"] for polygon in tile["polygons"]]


def areaPanel(title, inspection):
  polygons = polygonsOf(inspection)
  counts = collections.Counter(polygon["area"] for polygon in polygons)
  return {
    "title": title,
    "polygons": [{"points": polygon["outline"], "fill": areaColors[polygon["area"]]} for polygon in polygons],
    "labels": [],
    "legend": [(areaColors[area], f"{serverMapFiles.navAreaNames[area]}: {count:,} polygons") for area, count in sorted(counts.items())],
  }


def componentFill(component):
  return excludedColor if component < 0 else (mainPieceColor if component == 0 else islandColor)


def componentPanel(title, inspection):
  """The main piece green, islands orange and numbered at their centers, and polygons the server's ground filter never walks dark."""
  islands = inspection["islands"]
  main = inspection["mainPiece"]
  return {
    "title": title,
    "polygons": [{"points": polygon["outline"], "fill": componentFill(polygon["component"])} for polygon in polygonsOf(inspection)],
    "labels": [{"at": island["center"][:2], "text": str(island["number"])} for island in islands],
    "legend": [
      (mainPieceColor, f"main piece{' (stand-in: largest)' if main['standIn'] else ''}: {main['polygons']:,} polygons"),
      (islandColor, f"{len(islands):,} islands, {sum(island['snapRisk'] for island in islands)} at snap risk"),
      (excludedColor, f"Disabled and zone line: {inspection['excludedPolygons']:,} polygons"),
    ],
  }


def unmatched(polygons, others):
  """polygons that others' tile lacks, each matched once by its area and outline."""
  left = collections.Counter((polygon["area"], tuple(polygon["outline"])) for polygon in others)
  lacking = []
  for polygon in polygons:
    shape = (polygon["area"], tuple(polygon["outline"]))
    if left[shape]:
      left[shape] -= 1
    else:
      lacking.append(polygon)
  return lacking


def differencePanel(title, inspection, other):
  """inspection's polygons faint, with each the other nav lacks filled red over them, and each only the other has outlined purple over
  those: tiles matched by their (x, y, layer), polygons within a tile by their area and outline."""
  ours = {tile["key"]: tile["polygons"] for tile in inspection["tiles"]}
  theirs = {tile["key"]: tile["polygons"] for tile in other["tiles"]}
  onlyOurs = [polygon for key, polygons in ours.items() for polygon in unmatched(polygons, theirs.get(key, []))]
  onlyTheirs = [polygon for key, polygons in theirs.items() for polygon in unmatched(polygons, ours.get(key, []))]
  return {
    "title": title,
    "polygons": [{"points": polygon["outline"], "fill": contextColor} for polygon in polygonsOf(inspection)]
      + [{"points": polygon["outline"], "fill": differenceColor} for polygon in onlyOurs]
      + [{"points": polygon["outline"], "fill": None, "outline": otherOnlyColor} for polygon in onlyTheirs],
    "labels": [],
    "legend": [(differenceColor, f"{len(onlyOurs):,} polygons the other lacks"), (otherOnlyColor, f"{len(onlyTheirs):,} polygons only the other has")],
  }


def navFrame(points, panelWidth):
  xs = [point[0] for point in points]
  ys = [point[1] for point in points]
  across = (max(ys) - min(ys)) * (1 + 2 * planMargin)
  up = (max(xs) - min(xs)) * (1 + 2 * planMargin)
  center = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
  return planDrawing.PlanFrame(center, across, (panelWidth, round(panelWidth * up / across)))


def drawNavPanel(panel, frame):
  """One panel's plan: grid, polygons, labels clear of each other (an island whose number has no clear spot keeps only its dot), scale
  and north. Returns the image and how many labels were written."""
  image = Image.new("RGBA", frame.size, navBackground)
  draw = ImageDraw.Draw(image)
  step, xs, ys = planDrawing.gridLines(frame)
  for x in xs:
    draw.line([(0, frame.pixel((x, 0))[1]), (frame.size[0], frame.pixel((x, 0))[1])], fill=navGridColor, width=1)
  for y in ys:
    draw.line([(frame.pixel((0, y))[0], 0), (frame.pixel((0, y))[0], frame.size[1])], fill=navGridColor, width=1)
  for polygon in panel["polygons"]:
    if polygon["fill"] is None:
      draw.polygon([frame.pixel(point) for point in polygon["points"]], outline=polygon["outline"], width=2)
    else:
      draw.polygon([frame.pixel(point) for point in polygon["points"]], fill=polygon["fill"], outline=darker(polygon["fill"]))
  board = planDrawing.LabelBoard(draw, frame.size)
  planDrawing.labelGrid(board, frame)
  planDrawing.drawScale(board, frame, step)
  written = 0
  for label in panel["labels"]:
    x, y = frame.pixel(label["at"])
    draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=islandLabelColor)
    spot = board.clearSpot((x, y - 9), label["text"], islandLabelSize)
    if spot is not None:
      board.write(spot, label["text"], islandLabelColor, islandLabelSize)
      written += 1
  return image, written


def drawNav(outputPath, panels, panelWidth=760):
  """Panels side by side on one plan frame fitted to all their polygons; each titled above (with how many of its labels found room) and
  with its legend below. Written as PNG; returns the sheet's size, the frame (its center and width in zone units, and its size in pixels),
  each panel's top left corner on the sheet, its written label count, and its legend."""
  frame = navFrame([point for panel in panels for polygon in panel["polygons"] for point in polygon["points"]], panelWidth)
  legendHeight = legendRowHeight * max(len(panel["legend"]) for panel in panels) + panelGap
  sheet = Image.new("RGB", (panelWidth * len(panels) + panelGap * (len(panels) + 1), titleHeight + frame.size[1] + legendHeight + panelGap), (255, 255, 255))
  draw = ImageDraw.Draw(sheet)
  origins = []
  labelCounts = []
  for index, panel in enumerate(panels):
    left = panelGap + index * (panelWidth + panelGap)
    origins.append([left, titleHeight])
    image, written = drawNavPanel(panel, frame)
    labelCounts.append(written)
    sheet.paste(image.convert("RGB"), (left, titleHeight))
    title = panel["title"] + (f" ({written} of {len(panel['labels'])} numbers fit)" if panel["labels"] else "")
    draw.text((left, panelGap), title, fill=titleColor, font=planDrawing.fontOf(17))
    for row, (color, text) in enumerate(panel["legend"]):
      top = titleHeight + frame.size[1] + 6 + row * legendRowHeight
      draw.rectangle([left, top, left + swatchSize, top + swatchSize], fill=color, outline=darker(color))
      draw.text((left + swatchSize + 8, top - 1), text, fill=titleColor, font=planDrawing.fontOf(15))
  sheet.save(outputPath, "PNG")
  return {
    "width": sheet.width, "height": sheet.height,
    "frame": {"center": list(frame.center), "width": frame.width, "size": list(frame.size)},
    "panelOrigins": origins, "labelsWritten": labelCounts, "legends": [[text for _, text in panel["legend"]] for panel in panels],
  }


def serverPolygonColor(component, islandReach):
  if component < 0:
    return excludedColor
  if component == 0:
    return mainPieceColor
  return islandColor if islandReach is None or islandReach[component - 1] else unreachedIslandColor


def uprightSegments(zoneTriangles, frame):
  """The triangles standing within uprightDegrees of vertical, which the top surface shows little or nothing of (a thin wall, nothing),
  each as its longest span in plan: rows of (x0, y0, x1, y1) in the frame's pixels."""
  corners = zoneTriangles.astype(numpy.float64)
  normals = numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
  lengths = numpy.linalg.norm(normals, axis=1)
  upright = (lengths > 0) & (numpy.abs(normals[:, 2]) <= math.sin(math.radians(uprightDegrees)) * lengths)
  x, y = frame.pixel((corners[upright, :, 0], corners[upright, :, 1]))
  pairs = numpy.array([[0, 1], [1, 2], [2, 0]])
  spans = numpy.hypot(x[:, pairs[:, 0]] - x[:, pairs[:, 1]], y[:, pairs[:, 0]] - y[:, pairs[:, 1]])
  longest = pairs[spans.argmax(axis=1)]
  rows = numpy.arange(len(longest))
  return numpy.stack([x[rows, longest[:, 0]], y[rows, longest[:, 0]], x[rows, longest[:, 1]], y[rows, longest[:, 1]]], axis=1)


def islandLegend(islands, islandReach):
  atRisk = sum(island["snapRisk"] for island in islands)
  if islandReach is None:
    return [(islandColor, f"{len(islands):,} NPC islands, {atRisk} at snap risk; players' reach not checked")]
  reached = sum(islandReach)
  return [
    (islandColor, f"{reached:,} NPC islands players reach"),
    (unreachedIslandColor, f"{len(islands) - reached:,} NPC islands players do not reach ({atRisk} of all at snap risk)"),
  ]


def drawServerPlan(outputPath, mapContent, waterRecords, regionLabels, inspection, markers, islandReach=None, longSide=1000):
  """The server's view of a zone from its files alone, north up: the .map's collision in grey relief, what of it stands upright (walls,
  the sides of blocks) as dark red lines along it, and what it holds that the server never collides with tinted blue; the nav's
  polygons over them (serverNav.inspectNav's), the main piece green, islands orange where players reach them and grey where they do not
  (islandReach, by island number; None draws every island orange and the legend says players' reach was not checked), numbered, and
  what the server's ground filter never walks (Disabled, zone line) dark; the .wtr's boxes outlined in their type's color (water cyan,
  lava magenta, zone lines green), each with its label (regionLabels, in .wtr order); and markers ({at: zone [x, y], label}), such as
  the safe point. Written as PNG with its legend below; returns the sheet's size, the plan's frame (its center and width in zone units,
  its size in pixels, at the sheet's top left), the legend, and how many island numbers found room."""
  islands = inspection["islands"]
  if islandReach is not None and len(islandReach) != len(islands):
    raise ValueError(f"islandReach holds {len(islandReach)} flags for {len(islands)} islands")
  if len(regionLabels) != len(waterRecords):
    raise ValueError(f"{len(regionLabels)} region labels for {len(waterRecords)} .wtr records")
  collision = serverMapFiles.collisionTriangles(mapContent)
  passable = serverMapFiles.passableTriangles(mapContent)
  frame = collisionFrame(collision, longSide)
  image = shadedCollision(serverMapFiles.inZoneAxes(collision), frame, serverGroundStops).convert("RGBA")
  if len(passable):
    _, owners = topSurface(serverMapFiles.inZoneAxes(passable), frame)
    tint = numpy.zeros((*owners.shape, 4), dtype=numpy.uint8)
    tint[owners >= 0] = (*passableColor, passableAlpha)
    image = Image.alpha_composite(image, Image.fromarray(tint, "RGBA"))
  fills = Image.new("RGBA", frame.size, (0, 0, 0, 0))
  fillDraw = ImageDraw.Draw(fills)
  for polygon in polygonsOf(inspection):
    color = serverPolygonColor(polygon["component"], islandReach)
    fillDraw.polygon([frame.pixel(point) for point in polygon["outline"]], fill=(*color, navFillAlpha), outline=(*darker(color), 255))
  image = Image.alpha_composite(image, fills)
  layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
  draw = ImageDraw.Draw(layer)
  planDrawing.drawGrid(draw, frame)
  upright = uprightSegments(serverMapFiles.inZoneAxes(collision), frame)
  for segment in upright:
    draw.line(segment.tolist(), fill=(*uprightColor, 255), width=uprightLineWidth)
  outlines = [[frame.pixel(point) for point in regionOutline(record)] for record in waterRecords]
  for record, outline in zip(waterRecords, outlines):
    draw.polygon(outline, outline=(*regionColor(record), 255), width=3)
  board = planDrawing.LabelBoard(draw, frame.size)
  planDrawing.labelGrid(board, frame)
  planDrawing.drawScale(board, frame, planDrawing.gridStepFor(frame.width))
  spots = [frame.pixel(marker["at"][:2]) for marker in markers]
  for x, y in spots:
    draw.ellipse([x - markerRadius, y - markerRadius, x + markerRadius, y + markerRadius], fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=3)
    board.reserve([x - markerRadius, y - markerRadius, x + markerRadius, y + markerRadius])
  for marker, (x, y) in zip(markers, spots):
    board.place((x + markerRadius + 4, y), marker["label"], (0, 0, 0), markerLabelSize, "lm")
  for record, outline, label in zip(waterRecords, outlines, regionLabels):
    board.place((planDrawing.centroidOf(outline)[0], min(y for _, y in outline) - regionLabelLift), label, regionColor(record), 13)
  written = 0
  for island in islands:
    x, y = frame.pixel(island["center"][:2])
    draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(*islandLabelColor, 255))
    spot = board.clearSpot((x, y - 9), str(island["number"]), islandLabelSize)
    if spot is not None:
      board.write(spot, str(island["number"]), islandLabelColor, islandLabelSize)
      written += 1
  plan = Image.alpha_composite(image, layer).convert("RGB")
  typeCounts = collections.Counter(record["type"] for record in waterRecords)
  main = inspection["mainPiece"]
  legend = [((150, 150, 150), f"collision (.map): {len(collision):,} triangles, lighter higher")]
  legend += [(uprightColor, f"upright collision (.map): {len(upright):,} triangles, as lines")] if len(upright) else []
  legend += [(passableColor, f"never collided with (.map): {len(passable):,} triangles")] if len(passable) else []
  legend += [(regionColor({"type": kind}), f"{serverMapFiles.waterRegionTypeNames.get(kind, kind)} boxes (.wtr): {count}") for kind, count in sorted(typeCounts.items())]
  legend += [(mainPieceColor, f"NPC main piece{' (stand-in: largest)' if main['standIn'] else ''}: {main['polygons']:,} polygons")]
  legend += islandLegend(islands, islandReach)
  legend += [(excludedColor, f"Disabled and zone line: {inspection['excludedPolygons']:,} polygons")]
  sheet = Image.new("RGB", (plan.width, plan.height + 2 * panelGap + legendRowHeight * len(legend)), (255, 255, 255))
  sheet.paste(plan, (0, 0))
  sheetDraw = ImageDraw.Draw(sheet)
  for row, (color, text) in enumerate(legend):
    top = plan.height + panelGap + row * legendRowHeight
    sheetDraw.rectangle([panelGap, top, panelGap + swatchSize, top + swatchSize], fill=color, outline=darker(color))
    sheetDraw.text((panelGap + swatchSize + 8, top - 1), text, fill=titleColor, font=planDrawing.fontOf(15))
  sheet.save(outputPath, "PNG")
  return {
    "width": sheet.width, "height": sheet.height, "frame": {"center": list(frame.center), "width": frame.width, "size": list(frame.size)},
    "legend": [text for _, text in legend], "islandNumbersWritten": written,
  }
