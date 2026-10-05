"""Plans of a server nav mesh from above (zone axes, north (+X) up), drawn from inspectNav's polygons: filled by nav area, by NPC
component with the islands numbered, or as a difference against another nav; panels on one frame side by side, each titled and with its
legend."""
import collections

from PIL import Image, ImageDraw

import planDrawing
import serverMapFiles

background = (250, 250, 247, 255)
gridColor = (215, 215, 210, 255)
titleColor = (20, 20, 20)
areaColors = {
  0: (125, 175, 95), 1: (50, 125, 215), 2: (225, 95, 30), 3: (110, 110, 110), 4: (190, 60, 160), 5: (120, 190, 40), 6: (150, 210, 230),
  7: (30, 70, 160), 8: (200, 170, 60), 9: (240, 220, 0), 10: (0, 170, 170), 11: (45, 45, 45),
}
mainPieceColor = (60, 150, 60)
islandColor = (235, 145, 35)
excludedColor = (45, 45, 45)
contextColor = (220, 220, 216)
differenceColor = (220, 30, 30)
otherOnlyColor = (130, 50, 190)
islandLabelColor = (120, 50, 0)
islandLabelSize = 11
titleHeight = 34
legendRowHeight = 22
swatchSize = 14
panelGap = 8
margin = 0.03


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


def planFrame(points, panelWidth):
  xs = [point[0] for point in points]
  ys = [point[1] for point in points]
  across = (max(ys) - min(ys)) * (1 + 2 * margin)
  up = (max(xs) - min(xs)) * (1 + 2 * margin)
  center = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
  return planDrawing.PlanFrame(center, across, (panelWidth, round(panelWidth * up / across)))


def drawPanel(panel, frame):
  """One panel's plan: grid, polygons, labels clear of each other (an island whose number has no clear spot keeps only its dot), scale
  and north. Returns the image and how many labels were written."""
  image = Image.new("RGBA", frame.size, background)
  draw = ImageDraw.Draw(image)
  step, xs, ys = planDrawing.gridLines(frame)
  for x in xs:
    draw.line([(0, frame.pixel((x, 0))[1]), (frame.size[0], frame.pixel((x, 0))[1])], fill=gridColor, width=1)
  for y in ys:
    draw.line([(frame.pixel((0, y))[0], 0), (frame.pixel((0, y))[0], frame.size[1])], fill=gridColor, width=1)
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
  frame = planFrame([point for panel in panels for polygon in panel["polygons"] for point in polygon["points"]], panelWidth)
  legendHeight = legendRowHeight * max(len(panel["legend"]) for panel in panels) + panelGap
  sheet = Image.new("RGB", (panelWidth * len(panels) + panelGap * (len(panels) + 1), titleHeight + frame.size[1] + legendHeight + panelGap), (255, 255, 255))
  draw = ImageDraw.Draw(sheet)
  origins = []
  labelCounts = []
  for index, panel in enumerate(panels):
    left = panelGap + index * (panelWidth + panelGap)
    origins.append([left, titleHeight])
    image, written = drawPanel(panel, frame)
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
