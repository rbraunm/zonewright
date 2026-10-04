"""A plan drawing: sketch sheets and the plan's regions, plots, and water laid in crisp lines and labels over a top-down relief render,
with a coordinate grid, a scale bar, and north up."""
import math

from PIL import Image, ImageDraw, ImageFont

planScale = 1.5
sheetColors = ((200, 40, 40), (30, 90, 200), (20, 140, 70), (170, 60, 170), (210, 120, 0), (0, 140, 150))
regionColor = (60, 60, 60)
plotColor = (150, 90, 20)
waterColor = (40, 120, 220)
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


def drawGrid(draw, frame):
  step = next((step for step in gridSteps if frame.width / step <= 12), gridSteps[-1])
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


def drawWater(draw, frame, bodies):
  for body in bodies:
    pixels = [frame.pixel(point) for point in body["positions"]]
    for triangle in body["triangles"]:
      draw.polygon([pixels[index] for index in triangle], fill=(*waterColor, 110))


def drawRegions(draw, frame, regions):
  for region in regions:
    points = [frame.pixel(point) for point in region["outline"]]
    dashedLine(draw, points + points[:1], (*regionColor, 230), 2)
    labelAt(draw, centroidOf(points), region["name"], regionColor, 13)


def drawPlots(draw, frame, plots):
  for plot in plots:
    points = [frame.pixel(point) for point in plot["corners"]]
    draw.polygon(points, outline=(*plotColor, 255), width=2)
    labelAt(draw, centroidOf(points), plot["address"], plotColor, 12)


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
  elif kind in ("point", "note"):
    anchor = (points[0][0], points[0][1] - (14 if kind == "point" else 0))
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
  drawRegions(draw, frame, overlays["regions"])
  drawPlots(draw, frame, overlays["plots"])
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
