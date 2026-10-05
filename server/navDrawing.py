"""Plans of a server nav mesh from above (zone axes, north (+X) up): its polygons filled by nav area, by NPC component with the islands
numbered, or as a difference against another nav; panels on one frame side by side, each titled and with its legend."""
from PIL import Image, ImageDraw

import planDrawing
import serverNav

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
islandLabelColor = (120, 50, 0)
islandLabelSize = 11
titleHeight = 34
legendRowHeight = 22
swatchSize = 14
margin = 0.03


def darker(color):
  return tuple(round(channel * 0.7) for channel in color)


def areaPanel(title, outlines):
  """outlines: [{points: [(x, y)], area}] in zone plan axes."""
  counts = {}
  for outline in outlines:
    counts[outline["area"]] = counts.get(outline["area"], 0) + 1
  return {
    "title": title,
    "polygons": [{"points": outline["points"], "fill": areaColors[outline["area"]]} for outline in outlines],
    "labels": [],
    "legend": [(areaColors[area], f"{serverNav.navAreaNames[area]}: {count:,} polygons") for area, count in sorted(counts.items())],
  }


def componentPanel(title, outlines, polygonComponents, inspection):
  """The main piece green, islands orange and numbered at their centers, and polygons the server's ground filter never walks dark.
  polygonComponents runs over the outlines in the same order: 0 for the main piece, n for island n, -1 for none."""
  fills = []
  for outline, component in zip(outlines, polygonComponents, strict=True):
    fills.append({"points": outline["points"], "fill": excludedColor if component < 0 else (mainPieceColor if component == 0 else islandColor)})
  islands = inspection["islands"]
  main = inspection["mainPiece"]
  return {
    "title": title,
    "polygons": fills,
    "labels": [{"at": island["center"][:2], "text": str(island["number"])} for island in islands],
    "legend": [
      (mainPieceColor, f"main piece{' (stand-in: largest)' if main['standIn'] else ''}: {main['polygons']:,} polygons"),
      (islandColor, f"{len(islands):,} islands, {sum(island['snapRisk'] for island in islands)} at snap risk"),
      (excludedColor, f"Disabled and zone line: {inspection['excludedPolygons']:,} polygons"),
    ],
  }


def differencePanel(title, outlines, differing):
  """All polygons faint, and the differing ones ([{points}]) red over them."""
  return {
    "title": title,
    "polygons": [{"points": outline["points"], "fill": contextColor} for outline in outlines] + [{"points": polygon["points"], "fill": differenceColor} for polygon in differing],
    "labels": [],
    "legend": [(differenceColor, f"{len(differing):,} polygons differ")],
  }


def planFrame(outlines, panelWidth):
  xs = [point[0] for outline in outlines for point in outline["points"]]
  ys = [point[1] for outline in outlines for point in outline["points"]]
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


def drawNav(outputPath, panels, outlines, panelWidth=760):
  """Panels side by side on one plan frame fitted to the outlines; each titled above (with how many of its labels found room) and with its
  legend below. Written as PNG; returns the sheet's size and each panel's written label count."""
  frame = planFrame(outlines, panelWidth)
  legendHeight = legendRowHeight * max(len(panel["legend"]) for panel in panels) + 8
  sheet = Image.new("RGB", (panelWidth * len(panels) + 8 * (len(panels) + 1), titleHeight + frame.size[1] + legendHeight + 8), (255, 255, 255))
  draw = ImageDraw.Draw(sheet)
  labelCounts = []
  for index, panel in enumerate(panels):
    left = 8 + index * (panelWidth + 8)
    image, written = drawPanel(panel, frame)
    labelCounts.append(written)
    sheet.paste(image.convert("RGB"), (left, titleHeight))
    title = panel["title"] + (f" ({written} of {len(panel['labels'])} numbers fit)" if panel["labels"] else "")
    draw.text((left, 8), title, fill=titleColor, font=planDrawing.fontOf(17))
    for row, (color, text) in enumerate(panel["legend"]):
      top = titleHeight + frame.size[1] + 6 + row * legendRowHeight
      draw.rectangle([left, top, left + swatchSize, top + swatchSize], fill=color, outline=darker(color))
      draw.text((left + swatchSize + 8, top - 1), text, fill=titleColor, font=planDrawing.fontOf(15))
  sheet.save(outputPath, "PNG")
  return {"width": sheet.width, "height": sheet.height, "labelsWritten": labelCounts}
