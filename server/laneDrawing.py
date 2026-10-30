"""A body group's lane in plan: where its players reach, by label (walk green, swim blue, slide orange, squeeze pink, taken by a zone line
yellow, traps hatched orange), water the group wades lined blue over its floor's fill, walkable floor they never reach (open
space joins it to the lane) cross-hatched dark, alone or over a level reached under it, columns reached at a level under a higher one or
under water standing clear of it dotted (black where the group fits there, pink where only a smaller body does), ledges
(reached walkable floors side by side more than the step apart, where the plan resolves columns) dark, leak edges (onto nothing or under
the underworld) magenta, the edges where a zone line catches a fall green, zone lines hatched green when reached and dashed when not,
swim boxes dashed over the fill, seeds as white arrows (hollow when isolated), plot entrances as ticks (a red X when unreached), region
outlines in their access colors, and the problems ringed black (round the mark they lie on) and numbered over their rings or led to
them by a line; with a legend band below naming what the drawing shows. LanePlan lays it over planDrawing.drawPlan's plan;
drawLaneBesideNav draws a lane from server files over the floors it reaches beside the server's NPC nav. A view's lane tint
(renderView lane) takes the same colors, its walk split by the design grade (buildTolerances.designGradeDegrees, or the region's
gradeDegrees) into walk and climb (violet: reached floor steeper than the grade, passable only because the client allows it), a label of
the tint's own that the lane file never holds; tintLegendUnder names them under the view."""
import math

import numpy
from PIL import Image, ImageDraw

import planDrawing
import playerReach
import playerScale
import serverMapDrawing
import slopeBands

# A view's lane tint splits the lane's walk label by the design grade into walk and climb under this label of the tint's own, outside
# playerReach.labelNames: the lane file holds no slope, so a plan's lane fill draws such floor as walk, and the label is mapped only
# through labelColors and labelWords, never through labelNames.
climbTint = 6
# Each label's hue stands apart from the others' and from what plans and sections draw beside them: the caves' purple, the zone lines'
# green, the leak edges' magenta, the plan's amber view access, and the swim volumes' cyan.
labelColors = {
  playerReach.walkLabel: (35, 165, 60), playerReach.swimLabel: (25, 105, 225), playerReach.slideLabel: (240, 140, 20),
  playerReach.squeezeLabel: (230, 90, 150), playerReach.zoneLineLabel: (235, 205, 25), climbTint: (150, 60, 220),
}
labelWords = {
  playerReach.walkLabel: "walk", playerReach.swimLabel: "swim (a floor in a swim box)", playerReach.slideLabel: "slide (too steep to stand)",
  playerReach.squeezeLabel: "squeeze (only a smaller player)", playerReach.zoneLineLabel: "taken by a zone line",
  climbTint: "climb (reached walking, steeper than the design grade: passable only because the client allows it)",
}
# What a section's water bands are, by label: water the lane's group swims, water only a smaller body passes through (under too little
# open space for the group), and water a zone line takes.
bandWords = {playerReach.swimLabel: "water swum", playerReach.squeezeLabel: "water only a smaller player swims", playerReach.zoneLineLabel: "water a zone line takes"}
fillAlpha = 125
# A view's lane tint moves each pixel of reached floor this share of the way toward its label's color.
tintAlpha = 0.35
hatchAlpha = 215
hatchSpacing = 8
trapColor = (240, 120, 0)
unreachedColor = (30, 30, 30)
# Floor never reached with no reached level under it is tinted under its cross-hatch, so the grid's lines crossing it read as dimmed,
# never as dash-dot outlines; over a reached level the level's fill shows between the hatch's lines.
unreachedFillAlpha = 70
layeredColor = (25, 25, 25)
squeezeLayeredColor = labelColors[playerReach.squeezeLabel]
layeredSpacing = 8
layeredDot = 2
# Where reached walkable floors in columns side by side lie more than the step apart, a line along their shared edge, drawn where the
# plan resolves columns.
ledgeColor = (45, 35, 25)
ledgeAlpha = 230
zoneLineHatchSpacing = 6
zoneLineFillAlpha = 60
voidColor = (215, 0, 215)
catchColor = planDrawing.zoneLineColor
edgeWidth = 4
zoneLineColor = planDrawing.zoneLineColor
seedFill = (255, 255, 255)
seedOutline = (20, 20, 20)
seedLength = 20
seedWidth = 18
plotColor = planDrawing.plotColor
unreachedPlotColor = (220, 20, 20)
plotMark = 7
problemColor = (15, 15, 15)
problemHalo = (255, 255, 255)
problemRing = 9
ringRadii = {"disconnectedEntry": seedLength + 4, "unreachedPlot": 2 * plotMark}
nameGap = 5
# The band along the drawing's top edge that planDrawing.labelGrid writes the grid's coordinates in.
axisBand = 22
problemNumberSize = 17
# A problem's number set further than this from its ring is led back to it by a line.
leaderReach = 2 * problemRing
regionColors = {"play": planDrawing.regionColor, "view": planDrawing.accessColors["view"], "none": planDrawing.accessColors["none"], None: planDrawing.accessColors[None]}
legendRowHeight = 22
legendSwatch = 18
# Legend columns are as wide as the widest entry and this much apart.
legendColumnGap = 20
legendPadding = 8
legendTextSize = 14
legendBackground = (250, 250, 247)
legendTextColor = (20, 20, 20)
# What a plan's regions players do not simply play in are, by access, and its caves' floor strokes, by kind, as its legend names them.
accessWords = {"view": "view region: seen, never entered", "none": "none region: never reached", None: "region, its access undecided"}
strokeWords = {"level": "cave level way", "pad": "cave pad", "rough": "cave rough ground"}
# The relief runs between these greys, its highest kept apart from the background.
reliefLow, reliefHigh = (70, 66, 60), (200, 196, 186)
trapFlag, layeredFlag, unreachedFlag, squeezeLayeredFlag, wadedFlag = 1, 2, 4, 8, 16
# Water players wade: lines across it in the swim blue, this far apart.
wadedColor = labelColors[playerReach.swimLabel]
wadedSpacing = 6
swimBoxHalo = (255, 255, 255)
ribbonWidth = 7
ribbonCasing = (15, 15, 15)
ribbonCasingWidth = 12
teleportColor = (15, 15, 15)
decisiveRadius = 10
# Decisive moves' rings closer than this apart in pixels, edge to edge, share one ring numbering them all.
decisiveSpacing = 4
decisiveFill = (255, 255, 255)
decisiveNumberSize = 13
# The traced point is a black ring round a dot on a white halo, apart from the boundaries' red and the decisive moves' white discs.
targetColor = (15, 15, 15)
targetHalo = (255, 255, 255)
targetRadius = 9
sectionBarHeight = 6
# A section's bars stand this many pixels over their floors, so the ground's line under them shows.
sectionBarLift = 3
sectionBandAlpha = 110
sectionUnreachedColor = (110, 110, 110)
sectionLeakTick = 16
separationColor = (15, 15, 15)
separationSize = 5
separationTextSize = 13


class LanePlan:
  """A lane drawn over a plan: fill (the labels, hatches, dots, ledges, and, for a whole lane plan, the region outlines as one layer, so
  the plan's boundaries draw over the outlines), strokes (swim boxes, void and catch edges, zone lines, seeds, plot entrances, problem
  rings), claim (the marks labels keep clear of), labels (problem numbers, zone line, seed and region names), and its legend of what the
  drawing showed. level {bottom, top} keeps only nodes whose height lies in it. Drawn as a layer of another plan (complete false), the
  problems, the regions, and the zone lines' names, which that plan draws its own way, are left out."""

  def __init__(self, analysis, description, level, complete):
    nodes = analysis["nodes"]
    self.lane = lane = nodes.lane
    self.analysis, self.description, self.complete = analysis, description, complete
    self.regions = description["regions"] if complete else []
    self.shown = set()
    inBand = numpy.ones(lane.nodeCount, dtype=bool) if level is None else (nodes.height >= level["bottom"]) & (nodes.height <= level["top"])
    self.label = numpy.zeros(lane.columns, dtype=numpy.uint8)
    self.flags = numpy.zeros(lane.columns, dtype=numpy.uint8)
    self.floorTop = numpy.zeros(lane.columns, dtype=numpy.uint16)
    layered = numpy.zeros(lane.columns, dtype=bool)
    squeezeLayered = numpy.zeros(lane.columns, dtype=bool)
    # Each column's highest reached floor, in cells and one up, 0 where nothing is reached.
    reachedTop = numpy.zeros(lane.columns, dtype=numpy.uint32)
    carried = None
    # A column's nodes lie together, lowest first, each floor before the water over it, so the last reached one of a run is the
    # column's top; a column split between two chunks has its higher nodes in the later one, which writes over the earlier, and its lower
    # level carried over. Water the lane's group wades is drawn by the floor under it; water standing clear of the floor and the water
    # under it (a floating box) is a level of its own.
    held = None
    for start in range(0, lane.nodeCount, playerReach.chunkNodes):
      part = slice(start, min(lane.nodeCount, start + playerReach.chunkNodes))
      flat = nodes.z[part].astype(numpy.int32) * lane.columnsX + nodes.x[part]
      band = inBand[part]
      wet = nodes.wet[part]
      floating, held = floatingWater(flat, wet, nodes.low[part], nodes.high[part], held)
      reached = numpy.flatnonzero(nodes.reached[part] & band & ~(wet & nodes.shallow[part]))
      if len(reached):
        columns = flat[reached]
        top = numpy.append(columns[1:] != columns[:-1], True)
        tops = reached[top]
        self.label[columns[top]] = nodes.label[part][tops]
        self.floorTop[columns[top]] = numpy.where(wet[tops] | ~nodes.walkable[part][tops], 0, nodes.low[part][tops])
        waded = ~wet & (nodes.box[part] >= 0) & nodes.shallow[part]
        self.flags[columns[top]] = (numpy.where(nodes.trap[part][tops], trapFlag, 0) | numpy.where(waded[tops], wadedFlag, 0)).astype(numpy.uint8)
        reachedTop[columns[top]] = nodes.low[part][tops].astype(numpy.uint32) + 1
        levels = reached[~wet[reached] | floating[reached]]
        floors = flat[levels]
        if len(floors):
          full = (nodes.state[part][levels] & playerReach.reachedFullState) != 0
          if carried is not None and carried[0] == floors[0]:
            (layered if carried[1] else squeezeLayered)[floors[0]] = True
          lower = numpy.append(floors[1:] == floors[:-1], False)
          layered[floors[lower & full]] = True
          squeezeLayered[floors[lower & ~full]] = True
          carried = (floors[-1], bool(full[-1]))
    self.flags[layered] |= layeredFlag
    self.flags[squeezeLayered & ~layered] |= squeezeLayeredFlag
    del layered, squeezeLayered
    # Walkable floor never reached that open space joins to the lane, alone in its column or over the highest level reached there.
    for start in range(0, lane.nodeCount, playerReach.chunkNodes):
      part = slice(start, min(lane.nodeCount, start + playerReach.chunkNodes))
      unreached = (
        nodes.walkable[part] & ~nodes.wet[part] & ~nodes.reached[part] & inBand[part] & ((nodes.state[part] & playerReach.openState) != 0)
        & ((nodes.shape[part] & (playerReach.absorbedShape | playerReach.boundaryShape)) == 0)
      )
      chosen = numpy.flatnonzero(unreached)
      flat = nodes.z[part][chosen].astype(numpy.int64) * lane.columnsX + nodes.x[part][chosen]
      over = nodes.low[part][chosen].astype(numpy.uint32) + 1 > reachedTop[flat]
      self.flags[flat[over]] |= unreachedFlag
    del reachedTop
    self.label = self.label.reshape(lane.columnsZ, lane.columnsX)
    self.flags = self.flags.reshape(lane.columnsZ, lane.columnsX)
    self.floorTop = self.floorTop.reshape(lane.columnsZ, lane.columnsX)
    self.fromColumns = (nodes.x[lane.events[:, 1]].astype(numpy.int64), nodes.z[lane.events[:, 1]].astype(numpy.int64)) if len(lane.events) else None

  def sample(self, frame, size):
    """The columns under each pixel's center: (inside, row, column) arrays over the drawing."""
    lane = self.lane
    across = (numpy.arange(size[0]) + 0.5) / size[0]
    down = (numpy.arange(size[1]) + 0.5) / size[1]
    zoneY = frame.center[1] + frame.width / 2 - across * frame.width
    zoneX = frame.center[0] + frame.height / 2 - down * frame.height
    columnX = numpy.floor((zoneY - lane.origin[0]) / lane.cellSize).astype(numpy.int64)
    columnZ = numpy.floor((zoneX - lane.origin[2]) / lane.cellSize).astype(numpy.int64)
    insideX = (columnX >= 0) & (columnX < lane.columnsX)
    insideZ = (columnZ >= 0) & (columnZ < lane.columnsZ)
    inside = insideZ[:, None] & insideX[None, :]
    return inside, numpy.clip(columnZ, 0, lane.columnsZ - 1)[:, None], numpy.clip(columnX, 0, lane.columnsX - 1)[None, :]

  def pixels(self, frame, size):
    """Each pixel's label and flags, and whether it lies on a ledge: where two pixels side by side lie on neighbouring columns whose
    reached walkable floors differ by more than the lane's group's step, the edge of a platform, roof, or pit the plan's fills
    alone would not show; drawn only where the plan resolves columns, so a plan zoomed out draws no bands down steep walls."""
    inside, rows, columns = self.sample(frame, size)
    label = numpy.where(inside, self.label[rows, columns], 0)
    flags = numpy.where(inside, self.flags[rows, columns], 0)
    floors = numpy.where(inside, self.floorTop[rows, columns], 0).astype(numpy.int32)
    climb = self.lane.body.stepHeight / self.lane.cellHeight
    ledge = numpy.zeros(floors.shape, dtype=bool)
    for axis in (0, 1):
      first, second = (floors[:-1], floors[1:]) if axis == 0 else (floors[:, :-1], floors[:, 1:])
      neighbours = numpy.abs(numpy.diff(rows[:, 0]))[:, None] == 1 if axis == 0 else numpy.abs(numpy.diff(columns[0]))[None, :] == 1
      apart = neighbours & (first > 0) & (second > 0) & (numpy.abs(second - first) > climb)
      if axis == 0:
        ledge[:-1] |= apart
        ledge[1:] |= apart
      else:
        ledge[:, :-1] |= apart
        ledge[:, 1:] |= apart
    return label, flags, ledge

  def fill(self, frame, size):
    label, flags, ledge = self.pixels(frame, size)
    self.shown |= {("label", int(value)) for value in numpy.unique(label) if value}
    self.shown |= {("flag", flag) for flag in (trapFlag, layeredFlag, unreachedFlag, squeezeLayeredFlag, wadedFlag) if ((flags & flag) != 0).any()}
    if ledge.any():
      self.shown.add("ledge")
    pixels = numpy.zeros((size[1], size[0], 4), dtype=numpy.uint8)
    for value, color in labelColors.items():
      pixels[label == value] = (*color, fillAlpha)
    down, across = numpy.indices((size[1], size[0]))
    trapHatch = ((across + down) % hatchSpacing < 2) & ((flags & trapFlag) != 0)
    pixels[trapHatch] = (*trapColor, hatchAlpha)
    unreached = (flags & unreachedFlag) != 0
    pixels[unreached & (label == 0)] = (*unreachedColor, unreachedFillAlpha)
    crossing = ((across - down) % hatchSpacing == 0) | ((across + down) % hatchSpacing == 0)
    pixels[crossing & unreached] = (*unreachedColor, hatchAlpha)
    waves = (down % wadedSpacing < 2) & ((flags & wadedFlag) != 0)
    pixels[waves] = (*wadedColor, hatchAlpha)
    dots = (across % layeredSpacing < layeredDot) & (down % layeredSpacing < layeredDot)
    pixels[dots & ((flags & layeredFlag) != 0)] = (*layeredColor, 255)
    pixels[dots & ((flags & squeezeLayeredFlag) != 0)] = (*squeezeLayeredColor, 255)
    pixels[ledge] = (*ledgeColor, ledgeAlpha)
    image = Image.fromarray(pixels, "RGBA")
    draw = ImageDraw.Draw(image)
    for region in self.regions:
      points = [frame.pixel(point) for point in region["outline"]]
      planDrawing.dashedLine(draw, points + points[:1], (*regionColors[region["access"]], 255), 3)
      if planDrawing.polygonArea(planDrawing.clippedToFrame(points, frame.size)) > 0:
        self.shown.add(("region", region["access"]))
    return image

  def surface(self):
    """Each column's surface, (columnsZ, columnsX): the height of its highest reached node, else of its highest node, NaN where it has
    none."""
    nodes, lane = self.analysis["nodes"], self.lane
    surface = numpy.full(lane.columns, numpy.nan, dtype=numpy.float32)
    for onlyReached in (False, True):
      for start in range(0, lane.nodeCount, playerReach.chunkNodes):
        part = slice(start, min(lane.nodeCount, start + playerReach.chunkNodes))
        chosen = numpy.flatnonzero(nodes.reached[part]) if onlyReached else numpy.arange(part.stop - part.start)
        flat = nodes.z[part][chosen].astype(numpy.int32) * lane.columnsX + nodes.x[part][chosen]
        top = numpy.append(flat[1:] != flat[:-1], True)
        surface[flat[top]] = nodes.height[part][chosen[top]]
    return surface.reshape(lane.columnsZ, lane.columnsX)

  def relief(self, frame, size):
    """The floors players walk as grey relief: each column's surface, lighter higher (between the 1st and 99th percentile of what the
    drawing holds), shaded by its slope away from a light in the north-west; the planDrawing background where the lane holds nothing."""
    inside, rows, columns = self.sample(frame, size)
    heights = numpy.where(inside, self.surface()[rows, columns], numpy.nan)
    image = numpy.empty((size[1], size[0], 3), dtype=numpy.float64)
    image[:] = serverMapDrawing.backgroundColor
    covered = numpy.isfinite(heights)
    if covered.any():
      low, high = numpy.percentile(heights[covered], (1, 99))
      share = numpy.clip((heights - low) / max(high - low, 1e-9), 0, 1)
      color = numpy.asarray(reliefLow) + (numpy.asarray(reliefHigh) - numpy.asarray(reliefLow)) * share[..., None]
      unitsPerPixel = frame.width / size[0]
      filled = numpy.where(covered, heights, heights[covered].mean())
      downSlope, acrossSlope = numpy.gradient(filled, unitsPerPixel)
      normal = numpy.stack([-acrossSlope, -downSlope, numpy.ones_like(filled)], axis=-1)
      normal /= numpy.linalg.norm(normal, axis=-1, keepdims=True)
      # In pixels the light comes from the upper left (the game's north-west): x right, y down, z up.
      light = numpy.array([-1.0, -1.0, 1.5]) / math.sqrt(4.25)
      shade = 0.55 + 0.45 * numpy.clip(normal @ light, 0, 1)
      image[covered] = (color * shade[..., None])[covered]
    return Image.fromarray(numpy.round(image).astype(numpy.uint8), "RGB")

  def inFrame(self, frame, point):
    x, y = frame.pixel(point)
    return 0 <= x < frame.size[0] and 0 <= y < frame.size[1]

  def boxInFrame(self, frame, corners):
    xs, ys = [x for x, _ in corners], [y for _, y in corners]
    return max(xs) >= 0 and min(xs) < frame.size[0] and max(ys) >= 0 and min(ys) < frame.size[1]

  def edges(self):
    """The leak and catch edges in plan: (start, end, absorbed) per event whose fall leaves its column."""
    lane = self.lane
    found = []
    if self.fromColumns is not None:
      for (fromX, fromZ), event in zip(zip(*self.fromColumns), lane.events):
        x, z = lane.columnPlaces(numpy.array([event[2]], dtype=numpy.int64))
        if (int(fromX), int(fromZ)) == (int(x[0]), int(z[0])):
          continue
        start, end = edgeBetween(lane, (int(fromX), int(fromZ)), (int(x[0]), int(z[0])))
        found.append((start, end, event[0] == playerReach.absorbedEvent))
    return found

  def zoneLineBoxes(self, frame):
    """Each zone line's box in the drawing's pixels (left, top, right, bottom), with its line and whether it is reached."""
    boxes = []
    for line, reported in zip(self.description["zoneLines"], self.analysis["zoneLines"]):
      (x0, y0), (x1, y1) = frame.pixel(line["high"][:2]), frame.pixel(line["low"][:2])
      boxes.append(((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)), line, self.isReached(reported)))
    return boxes

  def isReached(self, reported):
    """Whether the lane's body group reaches a zone line or plot entrance as analyse (reachedBy) or laneMarks (reached) reports it."""
    return self.lane.group in reported["reachedBy"] if "reachedBy" in reported else reported["reached"]

  def strokes(self, draw, frame):
    for box in self.description["swimBoxes"]:
      low, high = box["low"], box["high"]
      corners = [frame.pixel(point) for point in ((low[0], low[1]), (high[0], low[1]), (high[0], high[1]), (low[0], high[1]))]
      draw.line(corners + corners[:2], fill=(*swimBoxHalo, 255), width=5, joint="curve")
      planDrawing.dashedLine(draw, corners + corners[:1], (*planDrawing.swimColors[box["liquid"]], 255), 3)
      if self.boxInFrame(frame, corners):
        self.shown.add(("swimBox", box["liquid"]))
    for start, end, absorbed in self.edges():
      ends = [frame.pixel(start), frame.pixel(end)]
      draw.line(ends, fill=(*(catchColor if absorbed else voidColor), 255), width=edgeWidth)
      if self.boxInFrame(frame, ends):
        self.shown.add("catch" if absorbed else "void")
    for box, _, reached in self.zoneLineBoxes(frame):
      left, top, right, bottom = box
      corners = [(left, top), (right, top), (right, bottom), (left, bottom)]
      if reached:
        draw.rectangle(box, fill=(*zoneLineColor, zoneLineFillAlpha))
        hatchBox(draw, box, (*zoneLineColor, 255), zoneLineHatchSpacing)
        draw.rectangle(box, outline=(*zoneLineColor, 255), width=3)
      else:
        planDrawing.dashedLine(draw, corners + corners[:1], (*zoneLineColor, 255), 3)
      if self.boxInFrame(frame, corners):
        self.shown.add("zoneLineReached" if reached else "zoneLineUnreached")
    for plot in self.analysis["plotEntrances"]:
      x, y = frame.pixel(plot["at"])
      reached = self.isReached(plot)
      if reached:
        draw.line([(x - plotMark, y), (x + plotMark, y)], fill=(*plotColor, 255), width=3)
        draw.line([(x, y - plotMark), (x, y + plotMark)], fill=(*plotColor, 255), width=3)
      else:
        draw.line([(x - plotMark, y - plotMark), (x + plotMark, y + plotMark)], fill=(*unreachedPlotColor, 255), width=4)
        draw.line([(x - plotMark, y + plotMark), (x + plotMark, y - plotMark)], fill=(*unreachedPlotColor, 255), width=4)
      if self.inFrame(frame, plot["at"]):
        self.shown.add("plotReached" if reached else "plotUnreached")
    for seed, entry in zip(self.description["seeds"], self.analysis["entries"]):
      drawSeed(draw, frame.pixel(entry["at"]), seed.get("headingDegrees"), seed["isolated"])
      if self.inFrame(frame, entry["at"]):
        self.shown.add("isolatedSeed" if seed["isolated"] else "seed")
    if self.complete:
      for problem in self.problemsIn(frame):
        drawRing(draw, frame.pixel(problem["center"]), ringRadius(problem))

  def seedPoints(self):
    """The plan points of the seeds the lane names."""
    return [entry["at"][:2] for entry in self.analysis["entries"]]

  def problemsIn(self, frame):
    return [problem for problem in self.analysis["problems"] if self.inFrame(frame, problem["center"])]

  def reserveMarks(self, board, frame):
    """Reserve on the board the marks the grid's labels must not cover; the lane's own give way to them."""

  def claim(self, board, frame):
    """Keep labels clear of the lane's marks where they can be: every problem ring, seed, and plot mark."""
    for problem in self.problemsIn(frame) if self.complete else []:
      x, y = frame.pixel(problem["center"])
      radius = ringRadius(problem)
      board.avoid([x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2])
    for entry in self.analysis["entries"]:
      x, y = frame.pixel(entry["at"])
      board.avoid([x - seedLength, y - seedLength, x + seedLength, y + seedLength])
    for plot in self.analysis["plotEntrances"]:
      x, y = frame.pixel(plot["at"])
      board.avoid([x - plotMark - 2, y - plotMark - 2, x + plotMark + 2, y + plotMark + 2])

  def labels(self, board, frame):
    """Name what the lane marks: for a whole lane plan each zone line in its box, which later labels keep clear of, and each problem's
    number just over its ring (led back to it by a line when it lands far off); then each seed's name beside it, clear of any ring round
    it, and each region's name, where its outline runs through the drawing and enough of it shows to hold its name, just over the middle
    of the top of its outline in the drawing, or just under it where that lies in the grid's labels."""
    problems = self.problemsIn(frame) if self.complete else []
    if self.complete:
      boxes = [(box, line) for box, line, _ in self.zoneLineBoxes(frame) if box[2] > 0 and box[3] > 0 and box[0] < frame.size[0] and box[1] < frame.size[1]]
      for box, line in boxes:
        board.place(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), line["name"], zoneLineColor, 13)
      for box, _ in boxes:
        board.avoid(list(box))
    for problem in problems:
      x, y = frame.pixel(problem["center"])
      radius = ringRadius(problem)
      wanted = (x, y - radius - 3)
      text = str(problem["number"])
      spot = board.clearSpot(wanted, text, problemNumberSize, "mb")
      spot = wanted if spot is None else spot
      board.write(spot, text, problemColor, problemNumberSize, "mb")
      if math.dist(spot, wanted) > leaderReach:
        box = board.textBox(spot, text, problemNumberSize, "mb")
        nearest = (min(max(x, box[0]), box[2]), min(max(y, box[1]), box[3]))
        reach = math.dist((x, y), nearest)
        if reach > radius:
          board.draw.line([(x + (nearest[0] - x) * radius / reach, y + (nearest[1] - y) * radius / reach), nearest], fill=(*problemColor, 255), width=2)
    for entry in self.analysis["entries"]:
      ringed = [ringRadius(problem) for problem in problems if problem["kind"] == "disconnectedEntry" and problem.get("entry") == entry["name"]]
      nameBeside(board, frame.pixel(entry["at"]), max([seedLength] + ringed), "S" if entry["kind"] == "safePoint" else entry["name"], seedOutline, 13)
    for region in self.regions:
      points = [frame.pixel(point) for point in region["outline"]]
      pieces = outlineInFrame(points, frame.size)
      if pieces and planDrawing.showsName(board, planDrawing.clippedToFrame(points, frame.size), region["name"], 13):
        shown = [point for piece in pieces for point in piece]
        highest = min(y for _, y in shown)
        along = [x for x, y in shown if y <= highest + 1]
        middle = (min(along) + max(along)) / 2
        above = board.textBox((middle, highest - 4), region["name"], 13, "mb")
        if above[1] >= axisBand:
          board.place((middle, highest - 4), region["name"], regionColors[region["access"]], 13, "mb")
        else:
          board.place((middle, highest + 4), region["name"], regionColors[region["access"]], 13, "ma")

  def legend(self, frame, base=()):
    """The drawing's key, each entry (kind of mark, color, words): the labels and marks the lane drew in the frame (fill and strokes
    record them), each fill as it shows over the relief, and, for a whole lane plan, each kind of problem in the frame with its numbers;
    then base, the entries of the plan's own layers under the lane (planLayerLegend), and which way the plan's axes run."""
    body = self.lane.body
    keyed = [
      *[(("label", label), ("fill", overRelief(labelColors[label], fillAlpha), labelWords[label])) for label in labelColors],
      (("flag", wadedFlag), ("waves", wadedColor, f"water waded: no deeper than {body.minimumSwimDepth:g}")),
      (("flag", trapFlag), ("hatch", trapColor, "trap (no way back)")), (("flag", unreachedFlag), ("crossHatch", unreachedColor, "walkable, never reached")),
      (("flag", layeredFlag), ("dots", layeredColor, "reached under a higher level")),
      (("flag", squeezeLayeredFlag), ("dots", squeezeLayeredColor, "only a smaller player under it")),
      ("ledge", ("ledge", ledgeColor, f"ledge: walked floors side by side more than the step ({body.stepHeight:g}) apart")),
      ("void", ("line", voidColor, "leak edge (onto nothing or under the underworld)")), ("catch", ("line", catchColor, "a zone line catches the fall")),
      ("zoneLineReached", ("hatchedBox", zoneLineColor, "zone line reached")), ("zoneLineUnreached", ("dashedBox", zoneLineColor, "zone line not reached")),
      *[(("swimBox", liquid), ("swimBox", planDrawing.swimColors[liquid], f"{liquid} swim volume")) for liquid in sorted(planDrawing.swimColors)],
      ("seed", ("arrow", seedOutline, "seed: where players arrive")), ("isolatedSeed", ("hollowArrow", seedOutline, "isolated seed")),
      ("plotReached", ("plus", plotColor, "plot entrance reached")), ("plotUnreached", ("cross", unreachedPlotColor, "plot entrance not reached")),
      *[(("region", access), ("dashed", regionColors[access], f"{access or 'undecided'} region")) for access in ("play", "view", "none", None)],
    ]
    items = [item for key, item in keyed if key in self.shown]
    if self.complete:
      byKind = {}
      for problem in self.problemsIn(frame):
        byKind.setdefault(problem["kind"], []).append(problem["number"])
      items += [("ring", problemColor, f"{numberRuns(numbers)} {kind}") for kind, numbers in byKind.items()]
    return items + list(base) + [
      ("text", legendTextColor, f"the {self.lane.group} body group's lane ({body.playerHeight:g} tall; squeeze: only a smaller body)"),
      ("text", legendTextColor, "plan: +X (north) up, +Y left"),
    ]


class RoutePlan(LanePlan):
  """A route over the lane (LanePlan as another plan's layer): the lane's fill and marks, then the route as a ribbon through its nodes'
  columns in its legs' label colors over a dark casing (a teleport dashed from its zone line to its landing), each decisive move ringed
  white where it starts and numbered, rings that would overlap at the plan's scale sharing one numbered with their run ("5-9"), and the
  traced point ringed black; labels keep clear of the ribbon and the rings. points are the route's nodes (playerReach.routeLegs), legs
  its legs, decisive its decisive moves (playerReach.decisiveLegs), target the traced point."""

  def __init__(self, analysis, description, points, legs, decisive, target):
    super().__init__(analysis, description, None, False)
    self.points, self.legs, self.decisive, self.target = points, legs, decisive, target

  def rings(self, frame):
    """Where the decisive moves are ringed: [(box, text)], each box wide enough for its text."""
    grouped = []
    for move in self.decisive:
      at = frame.pixel(self.points[self.legs[move["first"]]["first"]])
      if grouped and math.dist(at, grouped[-1][0]) < 2 * decisiveRadius + decisiveSpacing:
        grouped[-1][2] = move["number"]
      else:
        grouped.append([at, move["number"], move["number"]])
    font = planDrawing.fontOf(decisiveNumberSize)
    placed = []
    for (x, y), first, last in grouped:
      text = str(first) if first == last else f"{first}-{last}"
      half = max(decisiveRadius, font.getlength(text) / 2 + 5)
      placed.append(([x - half, y - decisiveRadius, x + half, y + decisiveRadius], text))
    return placed

  def strokes(self, draw, frame):
    super().strokes(draw, frame)
    pixels = [frame.pixel(point) for point in self.points]
    walked = [leg for leg in self.legs if leg["move"] != "teleport"]
    for leg in walked:
      draw.line(pixels[leg["first"]:leg["last"] + 1], fill=(*ribbonCasing, 255), width=ribbonCasingWidth, joint="curve")
    for leg in walked:
      color = labelColors[playerReach.labelNames.index(leg["label"])]
      draw.line(pixels[leg["first"]:leg["last"] + 1], fill=(*color, 255), width=ribbonWidth, joint="curve")
    for leg in self.legs:
      if leg["move"] == "teleport":
        planDrawing.dashedLine(draw, [pixels[leg["first"]], pixels[leg["last"]]], (*teleportColor, 255), 3)
        self.shown.add("teleport")
    drawTarget(draw, frame.pixel(self.target))
    for box, _ in self.rings(frame):
      draw.rounded_rectangle(box, radius=decisiveRadius, fill=(*decisiveFill, 255), outline=(*ribbonCasing, 255), width=2)

  def claim(self, board, frame):
    """Keep labels off the ribbon, the rings, and the traced point."""
    super().claim(board, frame)
    pixels = [frame.pixel(point) for point in self.points]
    reach = ribbonCasingWidth / 2
    for (x0, y0), (x1, y1) in zip(pixels[:-1], pixels[1:]):
      steps = max(1, math.ceil(math.dist((x0, y0), (x1, y1)) / reach))
      for step in range(steps + 1):
        x, y = x0 + (x1 - x0) * step / steps, y0 + (y1 - y0) * step / steps
        if 0 <= x < frame.size[0] and 0 <= y < frame.size[1]:
          board.reserve([x - reach, y - reach, x + reach, y + reach])
    for box, _ in self.rings(frame):
      board.reserve(box)
    x, y = frame.pixel(self.target)
    board.reserve([x - targetRadius - 2, y - targetRadius - 2, x + targetRadius + 2, y + targetRadius + 2])

  def labels(self, board, frame):
    super().labels(board, frame)
    for box, text in self.rings(frame):
      board.draw.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), text, fill=(*ribbonCasing, 255), font=planDrawing.fontOf(decisiveNumberSize), anchor="mm")

  def legend(self, frame, base=()):
    return [("ribbon", labelColors[playerReach.walkLabel], "the route, in its labels' colors")] + (
      [("dashed", teleportColor, "carried by a zone line")] if "teleport" in self.shown else []
    ) + ([("numbered", ribbonCasing, "a decisive move, numbered (a run of them shares a ring)")] if self.decisive else []) + [
      ("target", targetColor, "the traced point"),
    ] + super().legend(frame, base)


class SectionLane:
  """A lane cut (playerReach.laneCut) drawn into a section: reached water as bands from its bottom to its top in its label's color, each
  end on the swim box's edge within a column of it; reached floors as bars standing just over their heights in their labels' colors,
  walkable floor never reached dashed grey, each end on the ground's steep face or the edge of the swim box holding it within a column
  of it, where the lane's columns run past a block's or a box's edge; and leak edges as magenta ticks down from the floor a fall starts
  from; each kept to the section's frame (length along, bottom to top) and placed by pixel(s, z). marks ({kind, s, z, text, and for a
  separation over columns s1, z1}) are a trace's: the reached place nearest (a white disc), the traced point (a black ring round a dot),
  and what separates them (a dark diamond named by its kind, a dashed line to a second diamond where it ends); the disc and ring lie
  over the diamonds, and every name keeps clear of the marks. The legend names what the cut and the marks hold."""

  def __init__(self, cut, marks=()):
    self.cut, self.marks = cut, list(marks)

  def drawBands(self, draw, pixel, length, bottom, top, swim):
    edges = [s for box in swim for s in box["s"]]
    for band in self.cut["bands"]:
      s0, s1 = (min(edges, key=lambda edge: abs(edge - s), default=s) for s in band["s"])
      s0, s1 = (s if abs(s - wanted) <= playerReach.reachGrid["cellSize"] else wanted for s, wanted in zip((s0, s1), band["s"]))
      s0, s1 = max(s0, 0.0), min(s1, length)
      low, high = max(band["z"][0], bottom), min(band["z"][1], top)
      if s1 > s0 and high > low:
        color = labelColors[playerReach.labelNames.index(band["label"])]
        draw.rectangle([pixel(s0, high), pixel(s1, low)], fill=(*color, sectionBandAlpha))

  def trimmed(self, mark, ground, swim):
    """A floor's span along the line, each end moved back to where the floor ends within a column of it, where the lane's whole columns
    run past: the ground's face steeper than walkable that rises to the floor's height, or the edge of a swim box (swim, the section's
    cuts of them) holding the floor, where it enters the box for a start and leaves it for an end."""
    cellSize, cellHeight = playerReach.reachGrid["cellSize"], playerReach.reachGrid["cellHeight"]
    steep = math.tan(math.radians(playerScale.groups[self.cut["group"]].steepestWalkableDegrees))
    s0, s1 = mark["s"]
    faces = []
    for a0, z0, a1, z1 in ground:
      rise = abs(z1 - z0)
      if rise > steep * abs(a1 - a0) and mark["z"] - cellHeight <= max(z0, z1) <= mark["z"] + cellHeight:
        faces.append(a0 if z0 > z1 else a1)
    holding = [box["s"] for box in swim if box["z"][0] <= mark["z"] <= box["z"][1]]
    starts = [face for face in faces + [enter for enter, _ in holding] if s0 <= face <= s0 + cellSize and face < s1]
    ends = [face for face in faces + [leave for _, leave in holding] if s1 - cellSize <= face <= s1 and face > s0]
    return (max(starts) if starts else s0), (min(ends) if ends else s1)

  def drawMarks(self, draw, pixel, length, bottom, top, ground, swim):
    """Draw the bars, dashes, ticks, and trace marks; return the boxes labels keep clear of and the names to place, each led back to
    its mark when set off it: ([box], [(position, text, color, size, mark)])."""
    avoided, names = [], []

    def spanned(mark):
      s0, s1 = self.trimmed(mark, ground, swim)
      s0, s1 = max(s0, 0.0), min(s1, length)
      return (s0, s1) if s1 > s0 and bottom <= mark["z"] <= top else None

    for mark in self.cut["unreached"]:
      kept = spanned(mark)
      if kept is not None:
        (x0, y), (x1, _) = pixel(kept[0], mark["z"]), pixel(kept[1], mark["z"])
        planDrawing.dashedLine(draw, [(x0, y - sectionBarLift), (x1, y - sectionBarLift)], (*sectionUnreachedColor, 255), 3)
        avoided.append([x0, y - sectionBarLift - 2, x1, y - sectionBarLift + 2])
    for bar in self.cut["bars"]:
      kept = spanned(bar)
      if kept is not None:
        (x0, y), (x1, _) = pixel(kept[0], bar["z"]), pixel(kept[1], bar["z"])
        color = labelColors[playerReach.labelNames.index(bar["label"])]
        box = [x0, y - sectionBarLift - sectionBarHeight, max(x1, x0 + 1), y - sectionBarLift]
        draw.rectangle(box, fill=(*color, 255), outline=(*serverMapDrawing.darker(color), 255))
        avoided.append(box)
    for leak in self.cut["leaks"]:
      if 0 <= leak["s"] <= length and bottom <= leak["z"] <= top:
        x, y = pixel(leak["s"], leak["z"])
        draw.line([(x, y), (x, y + sectionLeakTick)], fill=(*voidColor, 255), width=edgeWidth)
        avoided.append([x - edgeWidth, y, x + edgeWidth, y + sectionLeakTick])
    shown = [mark for mark in self.marks if 0 <= mark["s"] <= length and bottom <= mark["z"] <= top]
    for mark in shown:
      if mark["kind"] != "separation":
        continue
      x, y = pixel(mark["s"], mark["z"])
      ends = [(x, y)]
      if "s1" in mark and 0 <= mark["s1"] <= length and bottom <= mark["z1"] <= top:
        ends.append(pixel(mark["s1"], mark["z1"]))
        planDrawing.dashedLine(draw, ends, (*separationColor, 255), 2)
      for ex, ey in ends:
        draw.polygon([(ex, ey - separationSize), (ex + separationSize, ey), (ex, ey + separationSize), (ex - separationSize, ey)], fill=(*separationColor, 255))
        avoided.append([ex - separationSize, ey - separationSize, ex + separationSize, ey + separationSize])
      names.append(((x, y - separationSize - 3), mark["text"], separationColor, separationTextSize, (x, y)))
    for mark in shown:
      x, y = pixel(mark["s"], mark["z"])
      if mark["kind"] == "reached":
        radius = decisiveRadius / 2
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=(*decisiveFill, 255), outline=(*ribbonCasing, 255), width=2)
        avoided.append([x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2])
      elif mark["kind"] == "point":
        drawTarget(draw, (x, y))
        avoided.append([x - targetRadius - 2, y - targetRadius - 2, x + targetRadius + 2, y + targetRadius + 2])
    return avoided, names

  def legend(self):
    """What the cut and the marks hold, each entry once."""
    shown = {mark["kind"] for mark in self.marks}
    items = [
      item for kind, item in (
        ("reached", ("disc", ribbonCasing, "the reached place nearest the traced point")), ("point", ("target", targetColor, "the traced point")),
        ("separation", ("diamond", separationColor, "what separates them, by kind (a dashed line where it runs on)")),
      ) if kind in shown
    ]
    labels = {playerReach.labelNames.index(mark["label"]) for mark in self.cut["bars"]}
    items += [("bar", labelColors[label], labelWords[label]) for label in labelColors if label in labels]
    items += [("band", labelColors[playerReach.labelNames.index(label)], bandWords[playerReach.labelNames.index(label)]) for label in sorted({band["label"] for band in self.cut["bands"]})]
    if self.cut["unreached"]:
      items.append(("dashed", sectionUnreachedColor, "walkable, never reached"))
    if self.cut["leaks"]:
      items.append(("tick", voidColor, "leak edge (a fall onto nothing or under the underworld starts)"))
    return items


def sectionLayerLegend(cuts):
  """A section's own layers its cuts hold, as legend entries: the ground, water, swim volumes, boundaries, zone lines, sketch massing,
  areas, and paths, plot pads, caves (floor, vault, and marks), and the path's points."""
  items = [("line", planDrawing.groundColor, "ground players stand on (all that collides)")] if cuts["ground"] else []
  if cuts["water"]:
    items.append(("line", planDrawing.waterColor, "water surface"))
  for liquid in sorted({box["liquid"] for box in cuts["swim"]}):
    items.append(("dashedBox", planDrawing.swimColors[liquid], f"{liquid} swim volume"))
  if cuts["boundaries"]:
    items.append(("line", planDrawing.boundaryColor, "boundary"))
  if cuts["zoneLines"]:
    items.append(("box", planDrawing.zoneLineColor, "zone line"))
  if cuts["massing"]:
    items.append(("line", planDrawing.massingColor, "sketch massing"))
  if cuts["sketch"]:
    items.append(("dashed", planDrawing.sheetColors[0], "sketch areas (dashed) and paths, in their sheets' colors"))
  if cuts["plots"]:
    items.append(("line", planDrawing.plotColor, "plot pad"))
  if any(entry["floor"] for entry in cuts["caves"]):
    items += [("line", planDrawing.caveColor, "cave floor, as designed"), ("dashed", planDrawing.caveColor, "cave vault, as designed")]
  if any(entry["crossings"] for entry in cuts["caves"]):
    items.append(("dashedBox", planDrawing.caveColor, "cave run crossing the line"))
  if any(entry["marks"] for entry in cuts["caves"]):
    items.append(("caveMark", planDrawing.caveColor, "cave landing, junction, or grade"))
  if cuts["bends"]:
    items.append(("bend", planDrawing.bendColor, "the line's points, numbered"))
  return items


def planLayerLegend(overlays, frame):
  """A plan's own layers the frame shows under a lane, as legend entries, each fill as it shows over the relief: the relief, the
  ground's spot heights, the slope layer's bands when it is on, water by liquid, regions (their outlines and the fills of those players
  do not simply play in, by access), caves (runs, floor heights, junctions, floor strokes by kind), plots, entries (filled, isolated, or
  keeping the heading), zone lines, boundaries' walls and their lids and floors, and sketch shapes (the lane draws the swim volumes and
  names them in its own entries)."""
  def shows(points):
    pixels = [frame.pixel(point) for point in points]
    xs, ys = [x for x, _ in pixels], [y for _, y in pixels]
    return bool(pixels) and max(xs) >= 0 and min(xs) < frame.size[0] and max(ys) >= 0 and min(ys) < frame.size[1]

  items = [("relief", None, "relief: lighter is higher")]
  if any(shows([spot["at"]]) for spot in overlays["spots"]):
    items.append(("spot", planDrawing.spotColor, "the ground's height at a grid crossing"))
  if overlays["slope"]:
    items += [("fill", overRelief(color, planDrawing.slopeAlpha), slopeBands.legendText(index)) for index, color in enumerate(slopeBands.colors)]
  liquids = sorted({body["liquid"] for body in overlays["water"] if shows([point for x0, y0, x1, y1 in body["runs"] for point in ((x0, y0), (x1, y1))] + [point for loop in body["loops"] for point in loop])})
  items += [("fill", overRelief(planDrawing.liquidColors[liquid], planDrawing.waterAlpha), f"{liquid} surface") for liquid in liquids]
  regions = [region for region in overlays["regions"] if shows(region["outline"])]
  if regions:
    items.append(("dashed", planDrawing.regionColor, "region, named"))
  accesses = {region["access"] for region in regions}
  items += [("accessHatch", planDrawing.accessColors[access], words) for access, words in accessWords.items() if access in accesses]
  caves = [cave for cave in overlays["caves"] if shows(cave["left"] + cave["right"])]
  if caves:
    items.append(("caveRun", planDrawing.caveColor, "cave run: walls at its floor's height, middle dashed, over its pale floor"))
    if any(cave["floors"] for cave in caves):
      items.append(("caveFloor", planDrawing.caveColor, "cave floor height at a path point"))
    if any(cave["junction"] is not None or cave["opening"] is not None for cave in caves):
      items.append(("caveJunction", planDrawing.caveColor, "where a cave branch meets its parent"))
    kinds = {stroke["kind"] for cave in caves for stroke in cave["strokes"]}
    items += [("dashed" if kind == "rough" else "stroke", planDrawing.strokeColors[kind], words) for kind, words in strokeWords.items() if kind in kinds]
  if any(shows(plot["corners"]) for plot in overlays["plots"]):
    items.append(("plot", planDrawing.plotColor, "plot, by address, its mark out of the entrance side"))
  shownEntries = [entry for entry in overlays["entries"] if shows([entry["at"]])]
  if any(entry["headingDegrees"] != "keep" and not entry["isolated"] for entry in shownEntries):
    items.append(("entryArrow", planDrawing.entryColor, "entry: where players arrive, toward their heading"))
  if any(entry["headingDegrees"] != "keep" and entry["isolated"] for entry in shownEntries):
    items.append(("isolatedEntryArrow", planDrawing.entryColor, "isolated entry"))
  if any(entry["headingDegrees"] == "keep" for entry in shownEntries):
    items.append(("entryRing", planDrawing.entryColor, "entry keeping the player's heading"))
  if any(shows(line["corners"]) for line in overlays["zoneLines"]):
    items.append(("box", planDrawing.zoneLineColor, "zone line"))
  if any(shows([point for line in boundary["lines"] for point in line]) for boundary in overlays["boundaries"]):
    items.append(("line", planDrawing.boundaryColor, "boundary wall"))
  if any(shows([point for area in boundary["areas"] for point in area]) for boundary in overlays["boundaries"]):
    items.append(("fill", overRelief(planDrawing.boundaryColor, planDrawing.boundaryAreaAlpha), "boundary lid or floor"))
  if any(shows(shape["plan"]) for sheet in overlays["sheets"] for shape in sheet["shapes"]):
    items.append(("dashed", planDrawing.sheetColors[0], "sketch areas (dashed), footprints, paths, and points, in their sheets' colors"))
  return items


def tintLegendUnder(path, labels, designGradeDegrees):
  """Add a legend band under a view the lane tints (renderView lane): each label it tints, the design grade walk and climb are told
  apart by, and what is left untinted."""
  with Image.open(path) as opened:
    view = opened.convert("RGB")
  items = [("fill", labelColors[label], labelWords[label]) for label in labels]
  withLegend(view, items + [
    ("text", legendTextColor, "floor tinted toward how players reach it; untinted: never reached, or not floor"),
    ("text", legendTextColor, f"the design grade is {designGradeDegrees:g} degrees, or the region's gradeDegrees: walk no steeper than it, climb steeper"),
  ]).save(path)


def numberRuns(numbers):
  """Numbers as runs: 1, 3-5, 8."""
  runs, start = [], None
  for index, number in enumerate(numbers):
    if start is None:
      start = number
    if index + 1 == len(numbers) or numbers[index + 1] != number + 1:
      runs.append(str(start) if start == number else f"{start}-{number}")
      start = None
  return ", ".join(runs)


def drawTarget(draw, at):
  x, y = at
  draw.ellipse([x - targetRadius - 2, y - targetRadius - 2, x + targetRadius + 2, y + targetRadius + 2], outline=(*targetHalo, 255), width=7)
  draw.ellipse([x - targetRadius, y - targetRadius, x + targetRadius, y + targetRadius], outline=(*targetColor, 255), width=3)
  draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(*targetColor, 255))


def drawRing(draw, at, radius):
  x, y = at
  draw.ellipse([x - radius - 1, y - radius - 1, x + radius + 1, y + radius + 1], outline=(*problemHalo, 255), width=5)
  draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=(*problemColor, 255), width=3)


def edgeBetween(lane, first, second):
  """The plan segment (zone x, y) between two side by side columns, given as (x, z) indices along Recast x (zone y) and z (zone x)."""
  if first[0] != second[0]:
    zoneY = lane.origin[0] + max(first[0], second[0]) * lane.cellSize
    zoneX = lane.origin[2] + first[1] * lane.cellSize
    return (zoneX, zoneY), (zoneX + lane.cellSize, zoneY)
  zoneX = lane.origin[2] + max(first[1], second[1]) * lane.cellSize
  zoneY = lane.origin[0] + first[0] * lane.cellSize
  return (zoneX, zoneY), (zoneX, zoneY + lane.cellSize)


def drawSeed(draw, at, headingDegrees, isolated):
  """A seed: a white arrow along its heading (a white disc without one), hollow when isolated."""
  x, y = at
  if headingDegrees is None:
    box = [x - seedWidth / 2, y - seedWidth / 2, x + seedWidth / 2, y + seedWidth / 2]
    draw.ellipse(box, fill=None if isolated else (*seedFill, 255), outline=(*seedOutline, 255), width=2)
    return
  ahead = planDrawing.pixelDirection(headingDegrees)
  side = (-ahead[1], ahead[0])
  tip = (x + ahead[0] * seedLength, y + ahead[1] * seedLength)
  back = (x - ahead[0] * seedLength / 2, y - ahead[1] * seedLength / 2)
  arrow = [tip, (back[0] + side[0] * seedWidth / 2, back[1] + side[1] * seedWidth / 2), (back[0] - side[0] * seedWidth / 2, back[1] - side[1] * seedWidth / 2)]
  draw.polygon(arrow, fill=None if isolated else (*seedFill, 255), outline=(*seedOutline, 255), width=2)


reliefMiddle = tuple((low + high) / 2 for low, high in zip(reliefLow, reliefHigh))


def overRelief(color, alpha):
  """A color laid at alpha over the relief's middle grey, as a fill shows on the plan."""
  return tuple(round(channel * alpha / 255 + middle * (1 - alpha / 255)) for channel, middle in zip(color, reliefMiddle))


def outlineInFrame(points, size):
  """The pieces of a closed outline (pixel points) that lie inside a drawing of size, each [start, end]."""
  pieces = []
  for start, end in zip(points, points[1:] + points[:1]):
    enter, leave = 0.0, 1.0
    for axis in (0, 1):
      delta = end[axis] - start[axis]
      if delta == 0:
        if not 0 <= start[axis] <= size[axis]:
          enter, leave = 1.0, 0.0
        continue
      first, second = -start[axis] / delta, (size[axis] - start[axis]) / delta
      enter, leave = max(enter, min(first, second)), min(leave, max(first, second))
    if enter < leave:
      pieces.append([(start[0] + (end[0] - start[0]) * share, start[1] + (end[1] - start[1]) * share) for share in (enter, leave)])
  return pieces


def floatingWater(flat, wet, low, high, held):
  """Which of a run of nodes (their columns, wetness, and lows and highs in cells, in lane order) are water standing clear of what holds
  it up in its column: its floor and the water under it there. held is (column, height) carried from the run before, or None; returns the
  flags and what to carry on."""
  count = len(flat)
  newColumn = numpy.ones(count, dtype=bool)
  newColumn[1:] = flat[1:] != flat[:-1]
  if held is not None:
    newColumn[0] = held[0] != flat[0]
  segment = numpy.cumsum(~wet | newColumn).astype(numpy.int64)
  span = 1 << 16
  running = numpy.maximum.accumulate(segment * span + numpy.where(wet, high, low).astype(numpy.int64))
  support = numpy.full(count, -1, dtype=numpy.int64)
  continued = numpy.zeros(count, dtype=bool)
  continued[1:] = segment[1:] == segment[:-1]
  support[continued] = running[:-1][continued[1:]] - segment[continued] * span
  if held is not None:
    carried = segment == 0
    support[carried] = numpy.maximum(support[carried], held[1])
  floating = wet & (low.astype(numpy.int64) > support)
  return floating, (int(flat[-1]), int(running[-1] - segment[-1] * span))


def ringRadius(problem):
  """A problem's ring: wide enough to go round the mark it lies on (a seed's arrow, a plot entrance's cross)."""
  return ringRadii.get(problem["kind"], problemRing)


def hatchBox(draw, box, color, spacing):
  """Diagonal lines spacing apart across a box (left, top, right, bottom) in pixels."""
  left, top, right, bottom = box
  for offset in range(math.floor(left - bottom), math.ceil(right - top) + 1, spacing):
    first, last = max(top, left - offset), min(bottom, right - offset)
    if last > first:
      draw.line([(first + offset, first), (last + offset, last)], fill=color, width=1)


def nameBeside(board, at, reach, text, color, size):
  """Write a mark's name reach pixels from it, right, left, under, or over it, the first clear of the labels and avoided boxes; right of
  it, moved the least it can, where none is."""
  x, y = at
  spots = (((x + reach + nameGap, y), "lm"), ((x - reach - nameGap, y), "rm"), ((x, y + reach + nameGap), "ma"), ((x, y - reach - nameGap), "mb"))
  for spot, anchor in spots:
    if board.isClear(board.textBox(spot, text, size, anchor)):
      board.write(spot, text, color, size, anchor)
      return
  board.place(spots[0][0], text, color, size, "lm")


def drawLegendItem(draw, left, top, item):
  """One legend entry at (left, top): its mark in a swatch, then its words."""
  kind, color, text = item
  right, bottom = left + legendSwatch, top + legendSwatch
  middle = (top + bottom) / 2
  if kind == "fill":
    draw.rectangle([left, top, right, bottom], fill=(*color, 255), outline=serverMapDrawing.darker(color))
  elif kind in ("hatch", "crossHatch"):
    draw.rectangle([left, top, right, bottom], fill=overRelief(color, unreachedFillAlpha) if kind == "crossHatch" else None, outline=(170, 170, 170))
    for offset in range(-legendSwatch + 3, legendSwatch, 6):
      start, end = max(offset, 0), min(offset + legendSwatch, legendSwatch)
      draw.line([(left + start, bottom - (start - offset)), (left + end, bottom - (end - offset))], fill=color, width=2 if kind == "hatch" else 1)
      if kind == "crossHatch":
        draw.line([(left + start, top + (start - offset)), (left + end, top + (end - offset))], fill=color, width=1)
  elif kind == "dots":
    draw.rectangle([left, top, right, bottom], outline=(170, 170, 170))
    for dx in range(3, legendSwatch, layeredSpacing):
      for dy in range(3, legendSwatch, layeredSpacing):
        draw.rectangle([left + dx, top + dy, left + dx + 1, top + dy + 1], fill=color)
  elif kind == "line":
    draw.line([(left, middle), (right, middle)], fill=color, width=edgeWidth)
  elif kind == "ledge":
    draw.line([(left, middle), (right, middle)], fill=color, width=2)
  elif kind == "dashed":
    planDrawing.dashedLine(draw, [(left, middle), (right, middle)], color, 3)
  elif kind == "box":
    draw.rectangle([left + 2, top + 2, right - 2, bottom - 2], fill=tuple(round(channel * 0.6 + 255 * 0.4) for channel in color), outline=color, width=3)
  elif kind == "hatchedBox":
    draw.rectangle([left + 2, top + 2, right - 2, bottom - 2], fill=overRelief(color, zoneLineFillAlpha))
    hatchBox(draw, (left + 2, top + 2, right - 2, bottom - 2), color, zoneLineHatchSpacing)
    draw.rectangle([left + 2, top + 2, right - 2, bottom - 2], outline=color, width=3)
  elif kind in ("dashedBox", "swimBox"):
    corners = [(left + 2, top + 2), (right - 2, top + 2), (right - 2, bottom - 2), (left + 2, bottom - 2)]
    if kind == "swimBox":
      draw.rectangle([left, top, right, bottom], fill=(205, 205, 205))
      draw.rectangle([left + 2, top + 2, right - 2, bottom - 2], outline=swimBoxHalo, width=5)
    for start, end in zip(corners, corners[1:] + corners[:1]):
      draw.line([start, ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)], fill=color, width=3)
  elif kind == "relief":
    for step in range(legendSwatch + 1):
      share = step / legendSwatch
      draw.line([(left + step, top), (left + step, bottom)], fill=tuple(round(low + (high - low) * share) for low, high in zip(reliefLow, reliefHigh)))
  elif kind == "waves":
    draw.rectangle([left, top, right, bottom], fill=overRelief(labelColors[playerReach.walkLabel], fillAlpha), outline=serverMapDrawing.darker(labelColors[playerReach.walkLabel]))
    for y in range(top + 2, bottom, wadedSpacing):
      draw.line([(left, y), (right, y)], fill=color, width=2)
  elif kind in ("arrow", "hollowArrow"):
    # On the legend's near-white band a white arrow and a hollow one look alike; on the walked ground seeds stand on, they do not.
    draw.rectangle([left, top, right, bottom], fill=overRelief(labelColors[playerReach.walkLabel], fillAlpha))
    draw.polygon([(right - 1, middle), (left + 2, top + 2), (left + 2, bottom - 2)], fill=None if kind == "hollowArrow" else seedFill, outline=color, width=2)
  elif kind in ("entryArrow", "isolatedEntryArrow"):
    draw.rectangle([left, top, right, bottom], fill=tuple(round(channel) for channel in reliefMiddle))
    if kind == "entryArrow":
      draw.polygon([(right - 1, middle), (left + 2, top + 2), (left + 2, bottom - 2)], fill=color, outline=(255, 255, 255), width=1)
    else:
      draw.polygon([(right - 1, middle), (left + 2, top + 2), (left + 2, bottom - 2)], fill=(255, 255, 255), outline=color, width=2)
  elif kind == "entryRing":
    draw.ellipse([left + 2, top + 2, right - 2, bottom - 2], outline=color, width=3)
  elif kind == "spot":
    draw.ellipse([left + 1, middle - 2, left + 5, middle + 2], fill=(0, 0, 0))
    draw.text((left + 7, middle + 1), "7", fill=color, font=planDrawing.fontOf(12), anchor="lm")
  elif kind == "accessHatch":
    draw.rectangle([left, top, right, bottom], fill=overRelief(color, planDrawing.accessFillAlpha))
    for offset in range(-legendSwatch + 3, legendSwatch, 6):
      start, end = max(offset, 0), min(offset + legendSwatch, legendSwatch)
      draw.line([(left + start, bottom - (start - offset)), (left + end, bottom - (end - offset))], fill=overRelief(color, planDrawing.accessHatchAlpha), width=planDrawing.hatchWidth)
  elif kind == "caveRun":
    draw.rectangle([left, top + 3, right, bottom - 3], fill=overRelief(planDrawing.caveFloorFill[:3], planDrawing.caveFloorFill[3]))
    for y in (top + 3, bottom - 3):
      draw.line([(left, y), (right, y)], fill=color, width=2)
    planDrawing.dashedLine(draw, [(left, middle), (right, middle)], color, 2)
  elif kind == "caveFloor":
    draw.rectangle([left + 3, top + 2, right - 3, bottom - 2], outline=color, width=1)
    draw.text(((left + right) / 2, middle), "8", fill=color, font=planDrawing.fontOf(11), anchor="mm")
  elif kind == "caveJunction":
    draw.ellipse([left + 4, top + 4, right - 4, bottom - 4], fill=(255, 255, 255), outline=color, width=2)
  elif kind == "stroke":
    draw.rectangle([left + 1, top + 3, right - 1, bottom - 3], fill=overRelief(color, 70), outline=color, width=2)
  elif kind == "plot":
    draw.rectangle([left + 1, top + 5, right - 1, bottom - 1], outline=color, width=2)
    center = (left + right) / 2
    draw.polygon([(center, top), (center - 5, top + 5), (center + 5, top + 5)], fill=color)
  elif kind == "plus":
    draw.line([(left + 2, middle), (right - 2, middle)], fill=color, width=3)
    draw.line([((left + right) / 2, top + 2), ((left + right) / 2, bottom - 2)], fill=color, width=3)
  elif kind == "cross":
    draw.line([(left + 2, top + 2), (right - 2, bottom - 2)], fill=color, width=3)
    draw.line([(left + 2, bottom - 2), (right - 2, top + 2)], fill=color, width=3)
  elif kind == "ring":
    draw.ellipse([left + 1, top + 1, right - 1, bottom - 1], outline=color, width=3)
  elif kind == "ribbon":
    draw.line([(left, middle), (right, middle)], fill=ribbonCasing, width=ribbonCasingWidth - 2)
    draw.line([(left + 1, middle), (right - 1, middle)], fill=color, width=ribbonWidth - 2)
  elif kind == "numbered":
    draw.rounded_rectangle([left + 1, top + 1, right - 1, bottom - 1], radius=legendSwatch / 2 - 1, fill=decisiveFill, outline=color, width=2)
    draw.text(((left + right) / 2, middle), "1", fill=color, font=planDrawing.fontOf(11), anchor="mm")
  elif kind == "caveMark":
    center = (left + right) / 2
    draw.polygon([(center, bottom - 3), (center - 6, top + 3), (center + 6, top + 3)], fill=color)
  elif kind == "bend":
    center = (left + right) / 2
    planDrawing.dashedLine(draw, [(center, top), (center, bottom - 6)], color, 1)
    draw.polygon([(center, bottom - 8), (center - 5, bottom), (center + 5, bottom)], fill=color)
  elif kind == "target":
    draw.ellipse([left + 2, top + 2, right - 2, bottom - 2], outline=color, width=3)
    draw.ellipse([(left + right) / 2 - 2, middle - 2, (left + right) / 2 + 2, middle + 2], fill=color)
  elif kind == "disc":
    draw.ellipse([left + 4, top + 4, right - 4, bottom - 4], fill=decisiveFill, outline=color, width=2)
  elif kind == "diamond":
    center = (left + right) / 2
    draw.polygon([(center, top + 3), (right - 4, middle), (center, bottom - 3), (left + 4, middle)], fill=color)
  elif kind == "bar":
    draw.rectangle([left, middle - sectionBarHeight / 2, right, middle + sectionBarHeight / 2], fill=color, outline=serverMapDrawing.darker(color))
  elif kind == "band":
    draw.rectangle([left, top, right, bottom], fill=bandSwatch(color))
  elif kind == "tick":
    draw.line([((left + right) / 2, top), ((left + right) / 2, bottom)], fill=color, width=edgeWidth)
  elif kind in ("dot", "hollowDot"):
    center = (left + right) / 2
    draw.ellipse([center - 4, middle - 4, center + 4, middle + 4], fill=color if kind == "dot" else (255, 255, 255), outline=color, width=1)
  elif kind != "text":
    raise ValueError(f"No legend mark '{kind}'")
  draw.text((left if kind == "text" else right + 8, top), text, fill=legendTextColor, font=planDrawing.fontOf(legendTextSize))


def bandSwatch(color):
  """Water swum as a section draws it: the band over a water swim volume's fill over the section's background."""
  boxed = [background + (box - background) * planDrawing.swimBoxAlpha / 255 for background, box in zip(planDrawing.sectionBackground, planDrawing.swimColors["water"])]
  return tuple(round(under + (channel - under) * sectionBandAlpha / 255) for under, channel in zip(boxed, color))


def legendLayout(width, items):
  """Where each legend entry goes in a band `width` wide: rows of as many columns as fit, each as wide as the widest entry; (places,
  height)."""
  font = planDrawing.fontOf(legendTextSize)
  column = max(font.getlength(text) + (0 if kind == "text" else legendSwatch + 8) for kind, _, text in items) + legendColumnGap
  columns = max(1, int((width - 2 * legendPadding) // column))
  places = [(legendPadding + (index % columns) * column, legendPadding + (index // columns) * legendRowHeight) for index in range(len(items))]
  rows = -(-len(items) // columns)
  return places, 2 * legendPadding + rows * legendRowHeight


def withLegend(image, items):
  """The image with a band under it holding the legend's entries."""
  places, height = legendLayout(image.width, items)
  sheet = Image.new("RGB", (image.width, image.height + height), legendBackground)
  sheet.paste(image.convert("RGB"), (0, 0))
  draw = ImageDraw.Draw(sheet)
  for (left, top), item in zip(places, items):
    drawLegendItem(draw, left, image.height + top, item)
  return sheet


def legendUnder(path, items):
  """Add a legend (its items) as a band under the drawing at path."""
  with Image.open(path) as opened:
    drawing = opened.convert("RGB")
  withLegend(drawing, items).save(path)


def ringProblem(image, share, fieldOfViewDegrees, number):
  """Ring a problem in the middle of a view looking at it: the ring as wide as the problem (share, its radius over its distance) in a
  view of fieldOfViewDegrees vertically, numbered beside it; a problem wider than the view is ringed at its center only."""
  draw = ImageDraw.Draw(image)
  radius = share / math.tan(math.radians(fieldOfViewDegrees) / 2) * image.height / 2
  radius = problemRing * 2 if radius > 0.45 * image.height else max(problemRing * 2, radius)
  center = (image.width / 2, image.height / 2)
  drawRing(draw, center, radius)
  planDrawing.labelAt(draw, (center[0] + radius * 0.71 + 4, center[1] - radius * 0.71 - 4), str(number), problemColor, 22, "lb")


def drawLaneBesideNav(outputPath, serverTriangles, lanePlan, inspection, titles, panelWidth=900):
  """A lane from a zone's server files over the floors it reaches (LanePlan.relief: the highest reached floor of each column, so a lid or
  ceiling over the pass hides nothing) and, on the same frame, the server's NPC nav by component (serverMapDrawing.componentPanel of
  inspection), side by side, each titled and with its legend. Written as PNG; returns the sheet's size, the frame, and the legends."""
  frame = serverMapDrawing.collisionFrame(serverTriangles, panelWidth)
  left = lanePanel(frame, lanePlan.relief(frame, frame.size), lanePlan)
  navPanel = serverMapDrawing.componentPanel(titles[1], inspection)
  right, _ = serverMapDrawing.drawNavPanel(navPanel, frame)
  relief = [("fill", reliefHigh, "relief: the highest reached floor, else the highest floor"), ("fill", serverMapDrawing.backgroundColor, "no floor at all")]
  legends = [relief + lanePlan.legend(frame), [("fill", color, text) for color, text in navPanel["legend"]]]
  layouts = [legendLayout(frame.size[0], legend) for legend in legends]
  gap = serverMapDrawing.panelGap
  legendHeight = max(height for _, height in layouts)
  sheet = Image.new("RGB", (2 * frame.size[0] + 3 * gap, serverMapDrawing.titleHeight + frame.size[1] + legendHeight + gap), (255, 255, 255))
  draw = ImageDraw.Draw(sheet)
  for index, (panel, title, legend, (places, _)) in enumerate(zip((left, right), titles, legends, layouts)):
    corner = gap + index * (frame.size[0] + gap)
    sheet.paste(panel.convert("RGB"), (corner, serverMapDrawing.titleHeight))
    draw.text((corner, gap), title, fill=serverMapDrawing.titleColor, font=planDrawing.fontOf(17))
    for (x, y), item in zip(places, legend):
      drawLegendItem(draw, corner + x, serverMapDrawing.titleHeight + frame.size[1] + y, item)
  sheet.save(outputPath, "PNG")
  return {"width": sheet.width, "height": sheet.height, "frame": {"center": list(frame.center), "width": frame.width, "size": list(frame.size)}, "legends": [[text for _, _, text in legend] for legend in legends]}


def lanePanel(frame, base, lanePlan):
  """A lane over a base image in a frame, with the grid, scale and north: the image."""
  image = Image.alpha_composite(base.convert("RGBA"), lanePlan.fill(frame, frame.size))
  layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
  draw = ImageDraw.Draw(layer)
  planDrawing.drawGrid(draw, frame)
  lanePlan.strokes(draw, frame)
  board = planDrawing.LabelBoard(draw, frame.size)
  planDrawing.labelGrid(board, frame)
  planDrawing.drawScale(board, frame, planDrawing.gridStepFor(frame.width))
  lanePlan.claim(board, frame)
  lanePlan.labels(board, frame)
  return Image.alpha_composite(image, layer)
