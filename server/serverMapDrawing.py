"""Plans of the server's map files, north up as planDrawing draws them: the collision a .map gives the server seen from above, shaded
by height and slope (faces seen edge-on from above, such as upright walls, show only as the edges between heights), with the .wtr's
region boxes outlined and numbered; and two collisions side by side with the triangles that differ between them."""
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
titleHeight = 30
panelGap = 8
fadedShare = 0.3
planMargin = 0.04
regionLabelLift = 10
rasterCandidates = 1_000_000


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


def shadedCollision(zoneTriangles, frame):
  """The top surface as RGB: a color by height (between the 1st and 99th percentile of what it covers) darkened by slope away from a
  light in the north-west, the background where nothing is."""
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
    stops = numpy.array([stop for stop, _ in heightStops])
    colors = numpy.array([color for _, color in heightStops], dtype=numpy.float64)
    ramp = numpy.stack([numpy.interp(share, stops, colors[:, channel]) for channel in range(3)], axis=1)
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


def drawCollisionComparison(first, second, regions, titles, longSide=900, tolerance=1e-3):
  """Two collisions (server-axis triangles in the server's order) in one frame side by side, each with the region boxes, and a third
  plan with the triangles that differ by more than tolerance (differingTriangles) drawn in red over the first faded: blank when the
  two agree. The third plan's title gives the count."""
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
  panels = [drawCollision(first, regions, frame), drawCollision(second, regions, frame), difference]
  labels = [*titles, f"differ by over {tolerance:g}: {len(differing):,} of {max(len(first), len(second)):,} triangles"]
  sheet = Image.new("RGB", (len(panels) * size[0] + (len(panels) - 1) * panelGap, size[1] + titleHeight), (255, 255, 255))
  sheetDraw = ImageDraw.Draw(sheet)
  for index, (panel, label) in enumerate(zip(panels, labels)):
    left = index * (size[0] + panelGap)
    sheet.paste(panel, (left, titleHeight))
    sheetDraw.text((left + size[0] / 2, titleHeight / 2), label, fill=titleColor, font=planDrawing.fontOf(16), anchor="mm")
  return sheet
