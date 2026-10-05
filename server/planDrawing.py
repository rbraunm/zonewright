"""Plan and section drawings. A plan lays sketch sheets and the plan's regions, plots, and water in crisp lines and labels over a top-down
relief render, with a coordinate grid, a scale bar, and north up. A section draws where a vertical plane cuts the zone, at one scale
across and up. Translucent fills each go on their own layer, composited in turn, so a fill tints what lies under it and never erases
it; lines go over the fills and labels over everything, each label clear of those placed before it."""
import math

from PIL import Image, ImageDraw, ImageFont

planScale = 1.5
sheetColors = ((200, 40, 40), (30, 90, 200), (20, 140, 70), (170, 60, 170), (210, 120, 0), (0, 140, 150))
regionColor = (60, 60, 60)
plotColor = (150, 90, 20)
waterColor = (40, 120, 220)
liquidColors = {"water": waterColor, "waterfall": waterColor, "lava": (230, 90, 20)}
swimColors = {"water": (0, 150, 190), "lava": (200, 30, 170)}
boundaryColor = (215, 30, 25)
zoneLineColor = (20, 150, 40)
boundaryLineWidth = 4
swimLabelSize = 11
swimLabelMargin = 2
gridColor = (255, 255, 255)
labelHalo = (255, 255, 255)
gridSteps = (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000)
dashLength = 10
gapLength = 6
spotColor = (90, 30, 0)
gridLabelColor = (40, 40, 40)
entranceMarkLength = 11
entranceMarkWidth = 14


class PlanFrame:
  """World plan coordinates to pixels of the drawing: the game's north (+X) up and east (-Y) right, width across and height up."""

  def __init__(self, center, width, size):
    self.center, self.width, self.size = center, width, size
    self.height = width * size[1] / size[0]

  def pixel(self, point):
    x = (self.center[1] + self.width / 2 - point[1]) / self.width * self.size[0]
    y = (self.center[0] + self.height / 2 - point[0]) / self.height * self.size[1]
    return (x, y)

  def length(self, units):
    return units / self.width * self.size[0]


def fontOf(size):
  return ImageFont.load_default(size=size)


def labelAt(draw, position, text, color, size=15, anchor="mm"):
  draw.text(position, text, fill=color, font=fontOf(size), anchor=anchor, stroke_width=3, stroke_fill=labelHalo)


def boxesOverlap(first, second):
  return first[0] < second[2] and second[0] < first[2] and first[1] < second[3] and second[1] < first[3]


def boxContains(outer, inner):
  return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


class LabelBoard:
  """Labels placed so none covers another: each goes at its spot, or the nearest spot around it clear of the labels placed before it
  and, where it can be, of the boxes avoided (the spot heights), which give way to it otherwise."""

  def __init__(self, draw, size):
    self.draw, self.size, self.taken, self.avoided = draw, size, [], []

  def textBox(self, position, text, size, anchor="mm"):
    return self.draw.textbbox(position, text, font=fontOf(size), anchor=anchor, stroke_width=3)

  def isClear(self, box, inside=True, avoiding=True):
    within = box[0] >= 0 and box[1] >= 0 and box[2] <= self.size[0] and box[3] <= self.size[1]
    return (within or not inside) and not any(boxesOverlap(box, other) for other in self.taken + (self.avoided if avoiding else []))

  def reserve(self, box):
    self.taken.append(box)

  def avoid(self, box):
    self.avoided.append(box)

  def write(self, position, text, color, size=15, anchor="mm"):
    """Write a label at position, reserving its place."""
    self.reserve(self.textBox(position, text, size, anchor))
    labelAt(self.draw, position, text, color, size, anchor)

  def clearSpot(self, position, text, size, anchor="mm", within=None):
    """The spot nearest position, in rings a label's half width and height apart, where the label lies inside the drawing (and within
    a box, given one) clear of the labels placed and of the boxes avoided, else clear of the labels placed only; None if neither."""
    box = self.textBox(position, text, size, anchor)
    width, height = box[2] - box[0], box[3] - box[1]
    for avoiding in (True, False):
      for ring in range(4):
        for across, up in ((0, 0),) if ring == 0 else ((0, -1), (0, 1), (-1, 0), (1, 0), (-1, -1), (1, -1), (-1, 1), (1, 1)):
          spot = (position[0] + across * ring * (width / 2 + 4), position[1] + up * ring * (height + 2))
          box = self.textBox(spot, text, size, anchor)
          if self.isClear(box, avoiding=avoiding) and (within is None or boxContains(within, box)):
            return spot
    return None

  def place(self, position, text, color, size=15, anchor="mm"):
    """Write a label at clearSpot, moved the least it can; at position when nothing near is clear, never left out."""
    spot = self.clearSpot(position, text, size, anchor)
    self.write(position if spot is None else spot, text, color, size, anchor)


def heightLabel(height):
  """A height as a whole number, never negative zero."""
  return str(round(height))


def dashedLine(draw, points, color, width):
  for start, end in zip(points[:-1], points[1:]):
    run = math.dist(start, end)
    if run == 0:
      continue
    step = dashLength + gapLength
    along = 0.0
    while along < run:
      stop = min(along + dashLength, run)
      a = (start[0] + (end[0] - start[0]) * along / run, start[1] + (end[1] - start[1]) * along / run)
      b = (start[0] + (end[0] - start[0]) * stop / run, start[1] + (end[1] - start[1]) * stop / run)
      draw.line([a, b], fill=color, width=width)
      along += step


def centroidOf(points):
  return (sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points))


def facingArrow(draw, frame, center, facingDegrees, reach, color):
  heading = math.radians(facingDegrees)
  tip = (center[0] + math.sin(heading) * reach, center[1] + math.cos(heading) * reach)
  start, end = frame.pixel(center), frame.pixel(tip)
  draw.line([start, end], fill=color, width=3)
  angle = math.atan2(end[1] - start[1], end[0] - start[0])
  for side in (2.6, -2.6):
    draw.line([end, (end[0] + 12 * math.cos(angle + side), end[1] + 12 * math.sin(angle + side))], fill=color, width=3)


def gridStepFor(width):
  return next((step for step in gridSteps if width / step <= 12), gridSteps[-1])


def gridValues(low, high, step):
  return range(math.ceil(low / step) * step, math.floor(high) + 1, step)


def gridCrossings(center, width, aspect):
  """The grid's crossings inside a plan width units across (along y) about center, height width * aspect (along x)."""
  step = gridStepFor(width)
  height = width * aspect
  xs = gridValues(center[0] - height / 2, center[0] + height / 2, step)
  ys = gridValues(center[1] - width / 2, center[1] + width / 2, step)
  return [[x, y] for x in xs for y in ys]


def gridLines(frame):
  """The grid's step and its lines' places along x (running across the drawing) and along y (running up it)."""
  step = gridStepFor(frame.width)
  xs = gridValues(frame.center[0] - frame.height / 2, frame.center[0] + frame.height / 2, step)
  ys = gridValues(frame.center[1] - frame.width / 2, frame.center[1] + frame.width / 2, step)
  return step, xs, ys


def drawGrid(draw, frame):
  _, xs, ys = gridLines(frame)
  for x in xs:
    py = frame.pixel((x, 0))[1]
    draw.line([(0, py), (frame.size[0], py)], fill=gridColor, width=1)
  for y in ys:
    px = frame.pixel((0, y))[0]
    draw.line([(px, 0), (px, frame.size[1])], fill=gridColor, width=1)


def labelGrid(board, frame):
  _, xs, ys = gridLines(frame)
  for x in xs:
    board.write((4, frame.pixel((x, 0))[1] - 3), str(x), gridLabelColor, 13, "ld")
  for y in ys:
    board.write((frame.pixel((0, y))[0] + 3, 4), str(y), gridLabelColor, 13, "la")


def drawScale(board, frame, step):
  draw = board.draw
  bar = frame.length(step)
  x0, y0 = 20, frame.size[1] - 24
  panel = [x0 - 6, y0 - 26, x0 + bar + 70, y0 + 10]
  draw.rectangle(panel, fill=(255, 255, 255, 200))
  board.reserve(panel)
  draw.line([(x0, y0), (x0 + bar, y0)], fill=(0, 0, 0), width=4)
  for x in (x0, x0 + bar):
    draw.line([(x, y0 - 6), (x, y0 + 6)], fill=(0, 0, 0), width=2)
  labelAt(draw, (x0 + bar / 2, y0 - 14), f"{step} units", (0, 0, 0), 14)
  nx, ny = frame.size[0] - 30, 50
  draw.polygon([(nx, ny - 26), (nx - 10, ny), (nx + 10, ny)], fill=(0, 0, 0))
  board.reserve([nx - 10, ny - 26, nx + 10, ny])
  board.write((nx, ny + 14), "N", (0, 0, 0), 16)


def spotMarks(board, frame, spots):
  """Each spot height's dot, its number's box, the number, and where the number is written: just beside its grid crossing."""
  marks = []
  for spot in spots:
    x, y = frame.pixel(spot["at"])
    text = heightLabel(spot["height"])
    marks.append(([x - 2, y - 2, x + 2, y + 2], board.textBox((x + 4, y + 3), text, 12, "la"), text, (x + 4, y + 3)))
  return marks


def drawSpots(board, marks):
  """The ground's height at each grid crossing (spotMarks), where no other label is: a spot gives way to every label."""
  for dot, label, text, position in marks:
    if board.isClear(dot, inside=False, avoiding=False) and board.isClear(label, avoiding=False):
      board.draw.ellipse(dot, fill=(0, 0, 0, 255))
      board.reserve(dot)
      board.write(position, text, spotColor, 12, "la")


def drawWater(draw, frame, bodies):
  """Each body's water as players see it (row runs and edge loops, as planOverlays gives them), filled in its liquid's color."""
  for body in bodies:
    fill = (*liquidColors[body["liquid"]], 110)
    for x0, y0, x1, y1 in body["runs"]:
      draw.polygon([frame.pixel(point) for point in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))], fill=fill)
    for loop in body["loops"]:
      draw.polygon([frame.pixel(point) for point in loop], fill=fill)


def drawSwim(draw, frame, boxes):
  """Swim volumes as dashed rectangles in plan."""
  for box in boxes:
    (low, high) = box["corners"]
    points = [frame.pixel(point) for point in ((low[0], low[1]), (high[0], low[1]), (high[0], high[1]), (low[0], high[1]))]
    dashedLine(draw, points + points[:1], (*swimColors[box["liquid"]], 255), 2)


def swimLabels(board, frame, boxes):
  """Where each swim volume in the drawing is named, inside its part of the drawing and clear of the labels placed (clearSpot), in
  full where that fits, else by its number: [(name, text, position)], each reserved on the board, and the boxes left unnamed."""
  shown = []
  for box in boxes:
    (low, high) = box["corners"]
    points = [frame.pixel(point) for point in ((low[0], low[1]), (high[0], high[1]))]
    left, top = max(0, min(x for x, _ in points)), max(0, min(y for _, y in points))
    right, bottom = min(frame.size[0], max(x for x, _ in points)), min(frame.size[1], max(y for _, y in points))
    if right > left and bottom > top:
      shown.append((box["name"], (left, top, right, bottom)))
  labels = []
  for name, (left, top, right, bottom) in sorted(shown, key=lambda pair: (pair[1][2] - pair[1][0]) * (pair[1][3] - pair[1][1]), reverse=True):
    middle = ((left + right) / 2, (top + bottom) / 2)
    within = (left + swimLabelMargin, top + swimLabelMargin, right - swimLabelMargin, bottom - swimLabelMargin)
    number = name[len(name.rstrip("0123456789")):]
    for text in [name] + ([number] if number else []):
      spot = board.clearSpot(middle, text, swimLabelSize, within=within)
      if spot is not None:
        labels.append((name, text, spot))
        board.reserve(board.textBox(spot, text, swimLabelSize))
        break
  named = {name for name, _, _ in labels}
  return labels, [name for name, _ in shown if name not in named]


def labelSwim(board, frame, boxes):
  """Name the swim volumes where swimLabels places them; returns the boxes in the drawing left unnamed."""
  liquids = {box["name"]: box["liquid"] for box in boxes}
  labels, unnamed = swimLabels(board, frame, boxes)
  for name, text, position in labels:
    labelAt(board.draw, position, text, swimColors[liquids[name]], swimLabelSize)
  return unnamed


def drawBoundaries(draw, frame, boundaries):
  """Boundaries in plan: lids and floors filled faintly red, walls as red lines along their foot."""
  for boundary in boundaries:
    for area in boundary["areas"]:
      draw.polygon([frame.pixel(point) for point in area], fill=(*boundaryColor, 70))
    for start, end in boundary["lines"]:
      draw.line([frame.pixel(start), frame.pixel(end)], fill=(*boundaryColor, 255), width=boundaryLineWidth)


def drawZoneLines(draw, frame, zoneLines):
  """Zone lines in plan: their boxes' footprints (four corners in order around) filled faintly green and outlined."""
  for line in zoneLines:
    draw.polygon([frame.pixel(point) for point in line["corners"]], fill=(*zoneLineColor, 90), outline=(*zoneLineColor, 255), width=2)


def boundaryLabelSpot(frame, boundary):
  """Where a boundary is named in plan: the middle of its middle wall line, or of its areas."""
  if boundary["lines"]:
    start, end = boundary["lines"][len(boundary["lines"]) // 2]
    return frame.pixel(((start[0] + end[0]) / 2, (start[1] + end[1]) / 2))
  return centroidOf([frame.pixel(point) for area in boundary["areas"] for point in area])


def drawCaves(draw, frame, caves):
  """Caves' runs in plan: their walls at floor height, their middles dotted, their floor strokes' outlines, and their junctions."""
  for cave in caves:
    for stroke in cave["strokes"]:
      points = [frame.pixel(point) for point in stroke["outline"]]
      if stroke["kind"] == "rough":
        dashedLine(draw, points + points[:1], (*strokeColors["rough"], 255), 2)
      else:
        draw.polygon(points, fill=(*strokeColors[stroke["kind"]], 70), outline=(*strokeColors[stroke["kind"]], 255), width=2)
    for side in ("left", "right"):
      draw.line([frame.pixel(point) for point in cave[side]], fill=(*caveColor, 255), width=2)
    dashedLine(draw, [frame.pixel(point) for point in cave["middle"]], (*caveColor, 150), 1)
    if cave["junction"] is not None:
      x, y = frame.pixel(cave["junction"])
      draw.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(255, 255, 255, 255), outline=(*caveColor, 255), width=2)


def labelCaves(board, frame, caves):
  """Name each run by the middle of its middle line, and write its floor height at each of its path points."""
  for cave in caves:
    for spot in cave["floors"]:
      board.place(frame.pixel(spot["at"]), f"{spot['floor']:g}", caveColor, 11)
    middle = cave["middle"][len(cave["middle"]) // 2]
    board.place(frame.pixel(middle), cave["label"], caveColor, 13)


def drawRegions(draw, frame, regions):
  for region in regions:
    points = [frame.pixel(point) for point in region["outline"]]
    dashedLine(draw, points + points[:1], (*regionColor, 230), 2)


def drawPlots(draw, frame, plots):
  """Each plot's outline and a mark pointing out of the middle of its entrance side."""
  for plot in plots:
    points = [frame.pixel(point) for point in plot["corners"]]
    draw.polygon(points, outline=(*plotColor, 255), width=2)
    middle = ((points[0][0] + points[1][0]) / 2, (points[0][1] + points[1][1]) / 2)
    heading = math.radians(plot["facingDegrees"])
    out = (math.sin(heading), -math.cos(heading))
    side = (-out[1], out[0])
    tip = (middle[0] + out[0] * entranceMarkLength, middle[1] + out[1] * entranceMarkLength)
    draw.polygon([tip, (middle[0] + side[0] * entranceMarkWidth / 2, middle[1] + side[1] * entranceMarkWidth / 2), (middle[0] - side[0] * entranceMarkWidth / 2, middle[1] - side[1] * entranceMarkWidth / 2)], fill=(*plotColor, 255))


def fillShape(draw, frame, shape, color):
  """A shape's translucent part: an area's or footprint's fill, a path's width."""
  points = [frame.pixel(point) for point in shape["plan"]]
  if shape["kind"] == "area":
    draw.polygon(points, fill=(*color, 40))
  elif shape["kind"] == "footprint":
    draw.polygon(points, fill=(*color, 110))
  elif shape["kind"] == "path" and "width" in shape:
    draw.line(points, fill=(*color, 70), width=max(2, round(frame.length(shape["width"]))), joint="curve")


def strokeShape(draw, frame, shape, color):
  """A shape's lines and marks: an area's dashed edge, a footprint's edge, a path's line, a point's dot, and the way it faces."""
  points = [frame.pixel(point) for point in shape["plan"]]
  kind = shape["kind"]
  if kind == "area":
    dashedLine(draw, points + points[:1], (*color, 255), 3)
  elif kind == "footprint":
    draw.polygon(points, outline=(*color, 255), width=3)
  elif kind == "path":
    draw.line(points, fill=(*color, 255), width=2)
  elif kind == "point":
    x, y = points[0]
    draw.ellipse([x - 6, y - 6, x + 6, y + 6], fill=(*color, 255), outline=(255, 255, 255, 255), width=2)
  if "facingDegrees" in shape and kind in ("area", "footprint", "point"):
    plan = shape["plan"]
    center = centroidOf(plan)
    reach = 0.35 * max(max(p[0] for p in plan) - min(p[0] for p in plan), max(p[1] for p in plan) - min(p[1] for p in plan), frame.width / 40)
    facingArrow(draw, frame, center, shape["facingDegrees"], reach, (*color, 255))


def clippedToFrame(points, size):
  """The part of a polygon inside the drawing, 0 to size across and down."""
  for axis, bound, keepBelow in ((0, 0, False), (0, size[0], True), (1, 0, False), (1, size[1], True)):
    kept = []
    for index, current in enumerate(points):
      previous = points[index - 1]
      currentIn, previousIn = (point[axis] <= bound if keepBelow else point[axis] >= bound for point in (current, previous))
      if currentIn != previousIn:
        share = (bound - previous[axis]) / (current[axis] - previous[axis])
        kept.append(tuple(previous[other] + (current[other] - previous[other]) * share for other in range(2)))
      if currentIn:
        kept.append(current)
    points = kept
  return points


def polygonArea(points):
  return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))) / 2


def labelShape(board, frame, shape, color):
  points = [frame.pixel(point) for point in shape["plan"]]
  label = shape.get("label") or shape["name"]
  kind = shape["kind"]
  if kind == "footprint" and "height" in shape:
    label += f" (h {shape['height']:g})"
  if kind == "path":
    middle = points[len(points) // 2 - 1] if len(points) > 1 else points[0]
    after = points[len(points) // 2]
    board.place(((middle[0] + after[0]) / 2, (middle[1] + after[1]) / 2), label, color, 15)
  elif kind == "point":
    board.place((points[0][0] + 10, points[0][1]), label, color, 15, "lm")
  elif kind == "note":
    board.place(points[0], label, color, 14)
  elif kind == "area":
    shown = clippedToFrame(points, frame.size)
    if polygonArea(shown) > 0:
      # Just inside the top of what the drawing shows of it, clear of the footprints and points usually drawn in its middle.
      left, right = min(point[0] for point in shown), max(point[0] for point in shown)
      board.place(((left + right) / 2, min(point[1] for point in shown) + 14), label, color, 15)
  else:
    board.place(centroidOf(points), label, color, 15)


# Shapes' labels are placed smallest shapes first: they have the least room to move before leaving what they name.
labelOrder = ("point", "footprint", "note", "path", "area")


def drawPlan(basePath, outputPath, center, width, overlays):
  """Lay the overlays over the base render, scaled up so lines and labels stay crisp, and save the drawing: water, swim volumes,
  boundaries, zone lines, regions, then every sheet's area fills, footprint fills and path widths each composited in turn, then every
  line and mark, then the labels: swim volumes' names inside them first, then the sketch's, the plots', the boundaries', the zone
  lines', and the regions', each set beside the ground's spot heights where it can be, and the spot heights giving way to them where it
  cannot."""
  with Image.open(basePath) as base:
    size = (round(base.width * planScale), round(base.height * planScale))
    image = base.convert("RGB").resize(size, Image.Resampling.BICUBIC).convert("RGBA")
  frame = PlanFrame(center, width, size)

  def overlay(paint):
    nonlocal image
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    painted = paint(ImageDraw.Draw(layer))
    image = Image.alpha_composite(image, layer)
    return painted

  sheets = [(sheet, sheetColors[index % len(sheetColors)]) for index, sheet in enumerate(overlays["sheets"])]
  shapes = [(shape, color) for sheet, color in sheets for shape in sheet["shapes"]]
  overlay(lambda draw: drawGrid(draw, frame))
  overlay(lambda draw: drawWater(draw, frame, overlays["water"]))
  overlay(lambda draw: drawSwim(draw, frame, overlays["swim"]))
  overlay(lambda draw: drawBoundaries(draw, frame, overlays["boundaries"]))
  overlay(lambda draw: drawZoneLines(draw, frame, overlays["zoneLines"]))
  overlay(lambda draw: drawRegions(draw, frame, overlays["regions"]))
  overlay(lambda draw: drawCaves(draw, frame, overlays["caves"]))
  for kinds in (("area",), ("footprint", "path")):
    for shape, color in shapes:
      if shape["kind"] in kinds:
        overlay(lambda draw, shape=shape, color=color: fillShape(draw, frame, shape, color))

  def strokes(draw):
    for shape, color in shapes:
      strokeShape(draw, frame, shape, color)
    drawPlots(draw, frame, overlays["plots"])

  overlay(strokes)
  step = gridStepFor(frame.width)

  def labels(draw):
    board = LabelBoard(draw, size)
    spots = spotMarks(board, frame, overlays["spots"])
    for dot, number, _, _ in spots:
      board.avoid(dot)
      board.avoid(number)
    labelGrid(board, frame)
    drawScale(board, frame, step)
    for row, (sheet, color) in enumerate(sheets):
      board.write((frame.size[0] - 16, 80 + row * 20), f"sketch {sheet['sheet']}", color, 15, "ra")
    unnamedSwimVolumes = labelSwim(board, frame, overlays["swim"])
    for kind in labelOrder:
      for shape, color in shapes:
        if shape["kind"] == kind:
          labelShape(board, frame, shape, color)
    for plot in overlays["plots"]:
      board.place(centroidOf([frame.pixel(point) for point in plot["corners"]]), plot["address"], plotColor, 12)
    for boundary in overlays["boundaries"]:
      board.place(boundaryLabelSpot(frame, boundary), boundary["name"], boundaryColor, 13)
    for line in overlays["zoneLines"]:
      board.place(centroidOf([frame.pixel(point) for point in line["corners"]]), line["name"], zoneLineColor, 13)
    for region in overlays["regions"]:
      board.place(centroidOf([frame.pixel(point) for point in region["outline"]]), region["name"], regionColor, 13)
    labelCaves(board, frame, overlays["caves"])
    drawSpots(board, spots)
    return unnamedSwimVolumes

  unnamedSwimVolumes = overlay(labels)
  image.convert("RGB").save(outputPath)
  return {
    "gridStep": step, "size": list(size), "sheetColors": {sheet["sheet"]: list(color) for sheet, color in sheets},
    "unnamedSwimVolumes": unnamedSwimVolumes,
  }


sectionSize = (1440, 810)
sectionPadding = (60, 40)
groundColor = (110, 70, 40)
massingColor = (90, 90, 90)
sectionBackground = (238, 236, 232)
sectionLabelLift = 8
padWidth = 5
padTick = 7
entranceMarkSection = 10
crossingMark = 5
bendColor = (70, 70, 160)
caveColor = (120, 40, 140)
strokeColors = {"level": (30, 150, 60), "pad": (220, 120, 0), "rough": (130, 90, 60)}
strokeWidth = 5
# A cave's profile stacks one panel per run, each at most this tall (and at least the shorter), a few pixels apart.
profilePanelHeight = 540
profilePanelMinimum = 220
profileGap = 6


def clippedSegment(segment, length, bottom, top):
  """The part of a segment [s0, z0, s1, z1] inside the drawing's frame (0 to length along, bottom to top), or None."""
  s0, z0, s1, z1 = segment
  enter, leave = 0.0, 1.0
  for origin, delta, low, high in ((s0, s1 - s0, 0.0, length), (z0, z1 - z0, bottom, top)):
    if delta == 0:
      if not low <= origin <= high:
        return None
      continue
    first, second = (low - origin) / delta, (high - origin) / delta
    enter, leave = max(enter, min(first, second)), min(leave, max(first, second))
  if enter > leave:
    return None
  return (s0 + enter * (s1 - s0), z0 + enter * (z1 - z0), s0 + leave * (s1 - s0), z0 + leave * (z1 - z0))


def closedLoops(segments):
  """How many closed shapes a section's ground segments make (a room or a passage cut across, a hole through an arch): groups of
  segments joined end to end where every end meets exactly one other. A segment with no length (where the plane passes through a
  vertex) joins nothing."""
  ends = {}
  segments = [segment for segment in segments if (round(segment[0], 2), round(segment[1], 2)) != (round(segment[2], 2), round(segment[3], 2))]
  for index, (s0, z0, s1, z1) in enumerate(segments):
    for end in ((round(s0, 2), round(z0, 2)), (round(s1, 2), round(z1, 2))):
      ends.setdefault(end, []).append(index)
  parent = list(range(len(segments)))

  def root(index):
    while parent[index] != index:
      parent[index] = parent[parent[index]]
      index = parent[index]
    return index

  for members in ends.values():
    for other in members[1:]:
      parent[root(other)] = root(members[0])
  groups, openGroups = set(), set()
  for index in range(len(segments)):
    groups.add(root(index))
  for members in ends.values():
    if len(members) != 2:
      openGroups.update(root(member) for member in members)
  return len(groups - openGroups)


def sectionFit(cuts, size):
  """The scale (pixels per unit) that fits a section's length and height into a drawing of size."""
  return min((size[0] - 2 * sectionPadding[0]) / cuts["length"], (size[1] - 2 * sectionPadding[1]) / (cuts["top"] - cuts["bottom"]))


def sectionImage(cuts, size, scale, title=None):
  """Draw a section's cuts at scale (pixels per unit) on an image of size, clipped to its frame: the ground's profile, water surfaces,
  swim volumes and zone lines as boxes, sketch massing, sketch area floors (dashed) and paths in their sheets' colors, plot pads with
  their entrances, boundaries in red, and the caves' runs (their floors solid, vaults dashed, floor strokes as bars, landings and
  junctions marked, a run crossing the line as a dashed box), at one scale across and up, with a height grid, the distance along the
  line, its bends marked by their point numbers, and its two ends named by their coordinates; each label just above what it names."""
  width, height = size
  padX, padY = sectionPadding
  length, bottom, top = cuts["length"], cuts["bottom"], cuts["top"]
  left = padX + ((width - 2 * padX) - length * scale) / 2
  baseline = height - padY - ((height - 2 * padY) - (top - bottom) * scale) / 2

  def pixel(s, z):
    return (left + s * scale, baseline - (z - bottom) * scale)

  image = Image.new("RGB", size, sectionBackground)
  draw = ImageDraw.Draw(image, "RGBA")
  board = LabelBoard(draw, size)
  labels = []

  def lines(segments, color, lineWidth, dashed=False):
    """Draw the segments' parts inside the frame, and return those parts."""
    kept = [clipped for segment in segments if (clipped := clippedSegment(segment, length, bottom, top)) is not None]
    for s0, z0, s1, z1 in kept:
      if dashed:
        dashedLine(draw, [pixel(s0, z0), pixel(s1, z1)], (*color, 255), lineWidth)
      else:
        draw.line([pixel(s0, z0), pixel(s1, z1)], fill=(*color, 255), width=lineWidth)
    return kept

  def extent(kept):
    """The pixel box around drawn parts."""
    corners = [pixel(s, z) for s0, z0, s1, z1 in kept for s, z in ((s0, z0), (s1, z1))]
    return [min(x for x, _ in corners), min(y for _, y in corners), max(x for x, _ in corners), max(y for _, y in corners)]

  def nameAbove(kept, text, color, size, overTop):
    """Name drawn parts just above them: over the middle of their top (a block), or over the middle of their longest part (a line)."""
    if not kept:
      return
    if overTop:
      box = extent(kept)
      labels.append((((box[0] + box[2]) / 2, box[1] - sectionLabelLift), text, color, size))
      return
    s0, z0, s1, z1 = max(kept, key=lambda piece: math.hypot(piece[2] - piece[0], piece[3] - piece[1]))
    x, y = pixel((s0 + s1) / 2, (z0 + z1) / 2)
    labels.append(((x, y - sectionLabelLift), text, color, size))

  step = gridStepFor(max(length, top - bottom))
  for z in range(math.ceil(bottom / step) * step, math.floor(top) + 1, step):
    y = pixel(0, z)[1]
    draw.line([(left, y), (left + length * scale, y)], fill=(205, 205, 205, 255), width=1)
    board.write((left - 6, y), str(z), (60, 60, 60), 13, "rm")
  for s in range(0, math.floor(length) + 1, step):
    x = pixel(s, 0)[0]
    draw.line([(x, pixel(0, top)[1]), (x, pixel(0, bottom)[1])], fill=(218, 218, 218, 255), width=1)
    board.write((x, pixel(0, bottom)[1] + 12), str(s), (60, 60, 60), 12)
  for bend in cuts["bends"]:
    x = pixel(bend["s"], 0)[0]
    dashedLine(draw, [(x, pixel(0, top)[1]), (x, pixel(0, bottom)[1])], (*bendColor, 160), 1)
    draw.polygon([(x, pixel(0, bottom)[1] + 2), (x - 5, pixel(0, bottom)[1] + 10), (x + 5, pixel(0, bottom)[1] + 10)], fill=(*bendColor, 255))
    board.write((x, pixel(0, bottom)[1] + 22), bend["label"], bendColor, 12)
  first, last = cuts["points"][0], cuts["points"][-1]
  board.write((left, padY / 2), title or f"[{first[0]:g}, {first[1]:g}]", (0, 0, 0), 14, "lm")
  if title is None:
    board.write((left + length * scale, padY / 2), f"[{last[0]:g}, {last[1]:g}]", (0, 0, 0), 14, "rm")
  bar = step * scale
  draw.line([(width - padX - bar, height - 16), (width - padX, height - 16)], fill=(0, 0, 0, 255), width=4)
  board.place((width - padX - bar / 2, height - 28), f"{step} units", (0, 0, 0), 13)
  for box in cuts["swim"]:
    corners = [pixel(box["s"][0], box["z"][1]), pixel(box["s"][1], box["z"][0])]
    color = swimColors[box["liquid"]]
    draw.rectangle([corners[0], corners[1]], fill=(*color, 60))
    outline = [corners[0], (corners[1][0], corners[0][1]), corners[1], (corners[0][0], corners[1][1])]
    dashedLine(draw, outline + outline[:1], (*color, 255), 2)
    labels.append((((corners[0][0] + corners[1][0]) / 2, corners[1][1] - 9), box["name"], color, 11))
  for box in cuts["zoneLines"]:
    corners = [pixel(box["s"][0], box["z"][1]), pixel(box["s"][1], box["z"][0])]
    draw.rectangle([corners[0], corners[1]], fill=(*zoneLineColor, 70), outline=(*zoneLineColor, 255), width=2)
    labels.append((((corners[0][0] + corners[1][0]) / 2, corners[0][1] - sectionLabelLift), box["name"], zoneLineColor, 12))
  for entry in cuts["caves"]:
    for crossing in entry["crossings"]:
      (s0, s1), (z0, z1) = crossing["s"], crossing["z"]
      kept = [piece for piece in ([s0, z0, s1, z0], [s1, z0, s1, z1], [s1, z1, s0, z1], [s0, z1, s0, z0]) if clippedSegment(piece, length, bottom, top) is not None]
      nameAbove(lines(kept, caveColor, 2, dashed=True), entry["label"], caveColor, 12, True)
  lines(cuts["ground"], groundColor, 3)
  for entry in cuts["caves"]:
    for stroke in entry["strokes"]:
      lines([stroke["segment"]], strokeColors[stroke["kind"]], strokeWidth, dashed=stroke["kind"] == "rough")
    if entry["floor"]:
      lines(entry["floor"], caveColor, 2 if entry["followed"] else 1)
      nameAbove(lines(entry["vault"], caveColor, 2 if entry["followed"] else 1, dashed=True), entry["label"], caveColor, 13, False)
    for mark in entry["marks"]:
      if 0 <= mark["s"] <= length and bottom <= mark["z"] <= top:
        x, y = pixel(mark["s"], mark["z"])
        draw.polygon([(x, y - 3), (x - 6, y - 13), (x + 6, y - 13)], fill=(*caveColor, 255))
        labels.append(((x, y - 15), mark["label"], caveColor, 12))
  for entry in cuts["boundaries"]:
    nameAbove(lines(entry["segments"], boundaryColor, boundaryLineWidth), entry["name"], boundaryColor, 12, True)
  for entry in cuts["massing"]:
    kept = lines(entry["segments"], massingColor, 3)
    if kept:
      board.reserve(extent(kept))
    nameAbove(kept, entry["label"], massingColor, 13, True)
  for entry in cuts["sketch"]:
    color = sheetColors[cuts["sheets"].index(entry["sheet"]) % len(sheetColors)]
    kept = lines([piece for piece in entry["pieces"] if piece[0] != piece[2]], color, 3, dashed=entry["kind"] == "area")
    for s, z, _, _ in (piece for piece in entry["pieces"] if piece[0] == piece[2]):
      if 0 <= s <= length and bottom <= z <= top:
        x, y = pixel(s, z)
        draw.polygon([(x, y - crossingMark), (x + crossingMark, y), (x, y + crossingMark), (x - crossingMark, y)], fill=(*color, 255))
        kept.append((s, z, s, z))
    nameAbove(kept, entry["label"], color, 13, False)
  for entry in cuts["water"]:
    nameAbove(lines(entry["segments"], waterColor, 3), entry["name"], waterColor, 12, False)
  for entry in cuts["plots"]:
    kept = lines([[entry["s"][0], entry["z"], entry["s"][1], entry["z"]]], plotColor, padWidth)
    for s0, z, s1, _ in kept:
      for s in (s0, s1):
        x, y = pixel(s, z)
        draw.line([(x, y - padTick), (x, y + padTick)], fill=(*plotColor, 255), width=2)
      if entry["entrance"] is not None and s0 - 1e-6 <= entry["entrance"] <= s1 + 1e-6:
        x, y = pixel(entry["entrance"], z)
        y -= padTick + entranceMarkSection / 2
        tip = x + entry["outward"] * entranceMarkSection
        draw.polygon([(tip, y), (x, y - entranceMarkSection / 2), (x, y + entranceMarkSection / 2)], fill=(*plotColor, 255))
    if kept:
      box = extent(kept)
      board.reserve([box[0] - entranceMarkSection, box[1] - padTick - entranceMarkSection, box[2] + entranceMarkSection, box[3] + padTick])
    nameAbove(kept, entry["name"], plotColor, 12, True)
  for position, text, color, size in labels:
    board.place(position, text, color, size, "mb")
  return image, {"gridStep": step, "unitsPerPixel": round(1 / scale, 4)}


def drawSection(outputPath, cuts):
  """Draw a section (sectionImage) at the scale that fits it, and save it."""
  image, drawn = sectionImage(cuts, sectionSize, sectionFit(cuts, sectionSize))
  image.save(outputPath)
  return drawn


def drawProfiles(outputPath, panels):
  """Draw a cave's runs' sections ([(title, cuts)], each along a run) one under another at one scale, each panel as tall as its run's
  heights need and all as wide as the longest, and save them as one picture."""
  scale = min(min(sectionFit(cuts, (sectionSize[0], profilePanelHeight)) for _, cuts in panels), sectionFit(panels[0][1], sectionSize))
  images = []
  for title, cuts in panels:
    height = min(profilePanelHeight, max(profilePanelMinimum, round((cuts["top"] - cuts["bottom"]) * scale + 2 * sectionPadding[1])))
    images.append(sectionImage(cuts, (sectionSize[0], height), scale, title)[0])
  sheet = Image.new("RGB", (sectionSize[0], sum(image.height for image in images) + profileGap * (len(images) - 1)), (255, 255, 255))
  top = 0
  for image in images:
    sheet.paste(image, (0, top))
    top += image.height + profileGap
  sheet.save(outputPath)
  return {"unitsPerPixel": round(1 / scale, 4), "panels": [title for title, _ in panels], "size": list(sheet.size)}
