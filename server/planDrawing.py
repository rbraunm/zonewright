"""Plan and section drawings. A plan lays sketch sheets and the plan's regions, plots, and water in crisp lines and labels over a top-down
relief render, with a coordinate grid, a scale bar, and north up. A section draws where a vertical plane cuts the zone, at one scale
across and up."""
import math

from PIL import Image, ImageDraw, ImageFont

planScale = 1.5
sheetColors = ((200, 40, 40), (30, 90, 200), (20, 140, 70), (170, 60, 170), (210, 120, 0), (0, 140, 150))
regionColor = (60, 60, 60)
plotColor = (150, 90, 20)
waterColor = (40, 120, 220)
swimColors = {"water": (0, 150, 190), "lava": (220, 90, 0)}
gridColor = (255, 255, 255)
labelHalo = (255, 255, 255)
gridSteps = (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000)
dashLength = 10
gapLength = 6


class PlanFrame:
  """World plan coordinates to pixels of the drawing."""

  def __init__(self, center, width, size):
    self.center, self.width, self.size = center, width, size
    self.height = width * size[1] / size[0]

  def pixel(self, point):
    x = (point[0] - self.center[0] + self.width / 2) / self.width * self.size[0]
    y = (self.center[1] + self.height / 2 - point[1]) / self.height * self.size[1]
    return (x, y)

  def length(self, units):
    return units / self.width * self.size[0]


def fontOf(size):
  return ImageFont.load_default(size=size)


def labelAt(draw, position, text, color, size=15, anchor="mm"):
  draw.text(position, text, fill=color, font=fontOf(size), anchor=anchor, stroke_width=3, stroke_fill=labelHalo)


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


def gridCrossings(center, width, aspect):
  """The grid's crossings inside a plan width units across about center, height width * aspect."""
  step = gridStepFor(width)
  height = width * aspect
  xs = range(math.ceil((center[0] - width / 2) / step) * step, math.floor(center[0] + width / 2) + 1, step)
  ys = range(math.ceil((center[1] - height / 2) / step) * step, math.floor(center[1] + height / 2) + 1, step)
  return [[x, y] for x in xs for y in ys]


def drawGrid(draw, frame):
  step = gridStepFor(frame.width)
  left, right = frame.center[0] - frame.width / 2, frame.center[0] + frame.width / 2
  bottom, top = frame.center[1] - frame.height / 2, frame.center[1] + frame.height / 2
  for x in range(math.ceil(left / step) * step, math.floor(right) + 1, step):
    px = frame.pixel((x, 0))[0]
    draw.line([(px, 0), (px, frame.size[1])], fill=gridColor, width=1)
    labelAt(draw, (px + 3, 4), str(x), (40, 40, 40), 13, "la")
  for y in range(math.ceil(bottom / step) * step, math.floor(top) + 1, step):
    py = frame.pixel((0, y))[1]
    draw.line([(0, py), (frame.size[0], py)], fill=gridColor, width=1)
    labelAt(draw, (4, py - 3), str(y), (40, 40, 40), 13, "ld")
  return step


def drawScale(draw, frame, step):
  bar = frame.length(step)
  x0, y0 = 20, frame.size[1] - 24
  draw.rectangle([x0 - 6, y0 - 26, x0 + bar + 70, y0 + 10], fill=(255, 255, 255, 200))
  draw.line([(x0, y0), (x0 + bar, y0)], fill=(0, 0, 0), width=4)
  for x in (x0, x0 + bar):
    draw.line([(x, y0 - 6), (x, y0 + 6)], fill=(0, 0, 0), width=2)
  labelAt(draw, (x0 + bar / 2, y0 - 14), f"{step} units", (0, 0, 0), 14)
  nx, ny = frame.size[0] - 30, 50
  draw.polygon([(nx, ny - 26), (nx - 10, ny), (nx + 10, ny)], fill=(0, 0, 0))
  labelAt(draw, (nx, ny + 14), "N", (0, 0, 0), 16)


def drawSpots(draw, frame, spots):
  """The ground's height at each grid crossing, written just beside it."""
  for spot in spots:
    x, y = frame.pixel(spot["at"])
    draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(0, 0, 0, 255))
    labelAt(draw, (x + 4, y + 3), f"{spot['height']:.0f}", (90, 30, 0), 12, "la")


def spotCovers(draw, frame, spots):
  """The boxes each spot height's dot and number cover on the drawing."""
  covered = []
  for spot in spots:
    x, y = frame.pixel(spot["at"])
    numberBox = draw.textbbox((x + 4, y + 3), f"{spot['height']:.0f}", font=fontOf(12), anchor="la", stroke_width=3)
    covered.append((x - 3, y - 3, numberBox[2], numberBox[3]))
  return covered


def clearLabelAt(draw, frame, center, text, color, size, covered):
  """Label text at the point nearest center, a quarter of a grid step apart, whose box overlaps nothing covered, and cover it there."""
  quarter = frame.length(gridStepFor(frame.width)) / 4
  offsets = sorted(((dx * quarter, dy * quarter) for dx in range(-4, 5) for dy in range(-4, 5)), key=lambda offset: math.hypot(*offset))
  for dx, dy in offsets:
    box = draw.textbbox((center[0] + dx, center[1] + dy), text, font=fontOf(size), anchor="mm", stroke_width=3)
    if not any(box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3] for other in covered):
      labelAt(draw, (center[0] + dx, center[1] + dy), text, color, size)
      covered.append(box)
      return
  # Crowded all around (a tight zoom on a grid of spot heights): the label keeps its own place.
  labelAt(draw, center, text, color, size)


def drawWater(draw, frame, bodies):
  for body in bodies:
    pixels = [frame.pixel(point) for point in body["positions"]]
    for triangle in body["triangles"]:
      draw.polygon([pixels[index] for index in triangle], fill=(*waterColor, 110))


def drawSwim(draw, frame, boxes, covered):
  """Swim volumes as dashed rectangles in plan, named small."""
  for box in boxes:
    (low, high) = box["corners"]
    points = [frame.pixel(point) for point in ((low[0], low[1]), (high[0], low[1]), (high[0], high[1]), (low[0], high[1]))]
    dashedLine(draw, points + points[:1], (*swimColors[box["liquid"]], 255), 2)
    clearLabelAt(draw, frame, centroidOf(points), box["name"], swimColors[box["liquid"]], 11, covered)


def drawRegions(draw, frame, regions, covered):
  for region in regions:
    points = [frame.pixel(point) for point in region["outline"]]
    dashedLine(draw, points + points[:1], (*regionColor, 230), 2)
    clearLabelAt(draw, frame, centroidOf(points), region["name"], regionColor, 13, covered)


def drawPlots(draw, frame, plots, covered):
  for plot in plots:
    points = [frame.pixel(point) for point in plot["corners"]]
    draw.polygon(points, outline=(*plotColor, 255), width=2)
    clearLabelAt(draw, frame, centroidOf(points), plot["address"], plotColor, 12, covered)


def drawShape(draw, frame, shape, color):
  points = [frame.pixel(point) for point in shape["plan"]]
  label = shape.get("label") or shape["name"]
  kind = shape["kind"]
  if kind == "area":
    draw.polygon(points, fill=(*color, 40))
    dashedLine(draw, points + points[:1], (*color, 255), 3)
  elif kind == "footprint":
    draw.polygon(points, fill=(*color, 110), outline=(*color, 255), width=3)
    if "height" in shape:
      label += f" (h {shape['height']:g})"
  elif kind == "path":
    if "width" in shape:
      draw.line(points, fill=(*color, 70), width=max(2, round(frame.length(shape["width"]))), joint="curve")
    draw.line(points, fill=(*color, 255), width=2)
  elif kind == "point":
    x, y = points[0]
    draw.ellipse([x - 6, y - 6, x + 6, y + 6], fill=(*color, 255), outline=(255, 255, 255, 255), width=2)
  if "facingDegrees" in shape and kind in ("area", "footprint", "point"):
    plan = shape["plan"]
    center = centroidOf(plan)
    reach = 0.35 * max(max(p[0] for p in plan) - min(p[0] for p in plan), max(p[1] for p in plan) - min(p[1] for p in plan), frame.width / 40)
    facingArrow(draw, frame, center, shape["facingDegrees"], reach, (*color, 255))
  if kind == "path":
    middle = points[len(points) // 2 - 1] if len(points) > 1 else points[0]
    after = points[len(points) // 2]
    anchor = ((middle[0] + after[0]) / 2, (middle[1] + after[1]) / 2)
  elif kind == "point":
    labelAt(draw, (points[0][0] + 10, points[0][1]), label, color, 15, "lm")
    return
  elif kind == "note":
    anchor = points[0]
  elif kind == "area":
    # An area's name sits just inside its top, clear of the footprints and points usually drawn in its middle.
    anchor = (centroidOf(points)[0], min(point[1] for point in points) + 14)
  else:
    anchor = centroidOf(points)
  labelAt(draw, anchor, label, color, 15 if kind != "note" else 14)


def drawPlan(basePath, outputPath, center, width, overlays):
  """Lay the overlays over the base render, scaled up so lines and labels stay crisp, and save the drawing."""
  with Image.open(basePath) as base:
    size = (round(base.width * planScale), round(base.height * planScale))
    image = base.convert("RGB").resize(size, Image.Resampling.BICUBIC).convert("RGBA")
  frame = PlanFrame(center, width, size)
  layer = Image.new("RGBA", size, (0, 0, 0, 0))
  draw = ImageDraw.Draw(layer)
  step = drawGrid(draw, frame)
  drawWater(draw, frame, overlays["water"])
  covered = spotCovers(draw, frame, overlays["spots"])
  drawSwim(draw, frame, overlays["swim"], covered)
  drawRegions(draw, frame, overlays["regions"], covered)
  drawPlots(draw, frame, overlays["plots"], covered)
  drawSpots(draw, frame, overlays["spots"])
  legend = []
  for index, sheet in enumerate(overlays["sheets"]):
    color = sheetColors[index % len(sheetColors)]
    legend.append((sheet["sheet"], color))
    for shape in sheet["shapes"]:
      drawShape(draw, frame, shape, color)
  drawScale(draw, frame, step)
  for row, (name, color) in enumerate(legend):
    labelAt(draw, (frame.size[0] - 16, 80 + row * 20), f"sketch {name}", color, 15, "ra")
  image = Image.alpha_composite(image, layer).convert("RGB")
  image.save(outputPath)
  return {"gridStep": step, "size": list(size), "sheetColors": {name: list(color) for name, color in legend}}


sectionSize = (1440, 810)
sectionPadding = (60, 40)
groundColor = (110, 70, 40)
massingColor = (90, 90, 90)
sectionBackground = (238, 236, 232)


def drawSection(outputPath, cuts, start, end, bottom, top):
  """Draw a section's cuts: the ground's profile, water surfaces, swim volumes as boxes, sketch massing, and plot pads, at one scale
  across and up, with a height grid, the distance along the line, and its two ends named by their coordinates."""
  width, height = sectionSize
  padX, padY = sectionPadding
  length = cuts["length"]
  scale = min((width - 2 * padX) / length, (height - 2 * padY) / (top - bottom))
  left = padX + ((width - 2 * padX) - length * scale) / 2
  baseline = height - padY - ((height - 2 * padY) - (top - bottom) * scale) / 2

  def pixel(s, z):
    return (left + s * scale, baseline - (z - bottom) * scale)

  image = Image.new("RGBA", sectionSize, (*sectionBackground, 255))
  draw = ImageDraw.Draw(image, "RGBA")
  step = gridStepFor(max(length, top - bottom))
  for z in range(math.ceil(bottom / step) * step, math.floor(top) + 1, step):
    y = pixel(0, z)[1]
    draw.line([(left, y), (left + length * scale, y)], fill=(205, 205, 205, 255), width=1)
    labelAt(draw, (left - 6, y), str(z), (60, 60, 60), 13, "rm")
  for s in range(0, math.floor(length) + 1, step):
    x = pixel(s, 0)[0]
    draw.line([(x, pixel(0, top)[1]), (x, pixel(0, bottom)[1])], fill=(218, 218, 218, 255), width=1)
    labelAt(draw, (x, pixel(0, bottom)[1] + 12), str(s), (60, 60, 60), 12)
  for box in cuts["swim"]:
    corners = [pixel(box["s"][0], box["z"][1]), pixel(box["s"][1], box["z"][0])]
    color = swimColors[box["liquid"]]
    draw.rectangle([corners[0], corners[1]], fill=(*color, 60))
    outline = [corners[0], (corners[1][0], corners[0][1]), corners[1], (corners[0][0], corners[1][1])]
    dashedLine(draw, outline + outline[:1], (*color, 255), 2)
    labelAt(draw, ((corners[0][0] + corners[1][0]) / 2, corners[1][1] - 9), box["name"], color, 11)
  for entry in cuts["massing"]:
    for s0, z0, s1, z1 in entry["segments"]:
      draw.line([pixel(s0, z0), pixel(s1, z1)], fill=(*massingColor, 255), width=3)
    labelAt(draw, pixel(*middleOf(entry["segments"])), entry["label"], massingColor, 13)
  for s0, z0, s1, z1 in cuts["ground"]:
    draw.line([pixel(s0, z0), pixel(s1, z1)], fill=(*groundColor, 255), width=3)
  for entry in cuts["water"]:
    for s0, z0, s1, z1 in entry["segments"]:
      draw.line([pixel(s0, z0), pixel(s1, z1)], fill=(*waterColor, 255), width=3)
    labelAt(draw, pixel(*middleOf(entry["segments"])), entry["name"], waterColor, 12)
  for entry in cuts["plots"]:
    for s0, z0, s1, z1 in entry["segments"]:
      draw.line([pixel(s0, z0), pixel(s1, z1)], fill=(*plotColor, 255), width=4)
    labelAt(draw, pixel(*middleOf(entry["segments"])), entry["name"], plotColor, 12)
  fromLabel, toLabel = (f"[{point[0]:g}, {point[1]:g}]" for point in (start, end))
  labelAt(draw, (left, padY / 2), fromLabel, (0, 0, 0), 14, "lm")
  labelAt(draw, (left + length * scale, padY / 2), toLabel, (0, 0, 0), 14, "rm")
  bar = step * scale
  draw.line([(width - padX - bar, height - 16), (width - padX, height - 16)], fill=(0, 0, 0, 255), width=4)
  labelAt(draw, (width - padX - bar / 2, height - 28), f"{step} units", (0, 0, 0), 13)
  image.convert("RGB").save(outputPath)
  return {"gridStep": step, "unitsPerPixel": round(1 / scale, 4)}


def middleOf(segments):
  s0, z0, s1, z1 = segments[len(segments) // 2]
  return (s0 + s1) / 2, max(z0, z1) + 0.0
