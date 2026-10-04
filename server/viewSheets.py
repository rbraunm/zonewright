"""Sheets of preview renders: the same view before and after a change with what changed between them, views around a subject, review
cameras, and frames along a route; and object names written on a render."""
from pathlib import Path

import numpy
from PIL import Image, ImageDraw, ImageFont

import planDrawing

cellSize = (640, 360)
jpegQuality = 90
gap = 8
labelHeight = 20
lineHeight = 17
backgroundColor = (40, 40, 40)
labelColor = (235, 235, 235)
nameColor = (20, 20, 20)
nameSize = 15
markRadius = 3
markColor = (255, 255, 255)
# A pixel counts as changed when a channel moved more than this (of 255); identical input renders byte-identical, so anything above
# antialiasing noise is a real change.
changeThreshold = 6
dimShare = 0.35
changeColor = numpy.array([230, 30, 30])


def openRender(path):
  renderPath = Path(path)
  if not renderPath.is_file():
    raise ValueError(f"No render at {path}")
  with Image.open(renderPath) as image:
    return image.convert("RGB")


def wrappedLines(draw, text, font, width):
  """Text broken at spaces into lines no wider than width (a word wider than that keeps a line of its own)."""
  lines = []
  for word in text.split(" "):
    if lines and draw.textlength(f"{lines[-1]} {word}", font=font) <= width:
      lines[-1] = f"{lines[-1]} {word}"
    else:
      lines.append(word)
  return lines


def fitted(image):
  """The image scaled to fit a cell, keeping its aspect, centered on the sheet's background."""
  scale = min(cellSize[0] / image.width, cellSize[1] / image.height)
  size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
  cell = Image.new("RGB", cellSize, backgroundColor)
  cell.paste(image.resize(size, Image.Resampling.LANCZOS), ((cellSize[0] - size[0]) // 2, (cellSize[1] - size[1]) // 2))
  return cell


def writeGrid(cells, columns, outputPath):
  """cells: [(image, label)] laid out in rows of `columns`, each fitted to cellSize, labeled under it (wrapped to its width, every row
  as tall as the longest label needs); written as JPEG."""
  rows = -(-len(cells) // columns)
  width, height = cellSize
  font = ImageFont.load_default(size=14)
  measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
  labels = [wrappedLines(measure, label, font, width) for _, label in cells]
  labelArea = labelHeight + lineHeight * (max(len(lines) for lines in labels) - 1)
  sheet = Image.new("RGB", (gap + columns * (width + gap), gap + rows * (height + labelArea + gap)), backgroundColor)
  draw = ImageDraw.Draw(sheet)
  for index, ((image, _), lines) in enumerate(zip(cells, labels)):
    left = gap + index % columns * (width + gap)
    top = gap + index // columns * (height + labelArea + gap)
    sheet.paste(fitted(image), (left, top))
    for line, text in enumerate(lines):
      draw.text((left, top + height + 2 + line * lineHeight), text, fill=labelColor, font=font)
  sheet.save(outputPath, "JPEG", quality=jpegQuality)
  return {"width": sheet.width, "height": sheet.height}


def writeNames(path, places):
  """Write each object's name by its place on a render ([{object, at: [x, y]}]): a mark on the place and the name just above it,
  moved the least it can to stay clear of the other names and marks."""
  with Image.open(path) as opened:
    image = opened.convert("RGB")
  draw = ImageDraw.Draw(image)
  board = planDrawing.LabelBoard(draw, image.size)
  for place in places:
    x, y = place["at"]
    mark = [x - markRadius, y - markRadius, x + markRadius, y + markRadius]
    draw.ellipse(mark, fill=markColor, outline=nameColor)
    board.avoid(mark)
  for place in places:
    x, y = place["at"]
    board.place((x, y - markRadius - nameSize // 2 - 3), place["object"], nameColor, nameSize)
  image.save(path, "PNG")


def compareSheet(beforePath, afterPath, outputPath, labels):
  """Before, after, and a change map (the after view dimmed, changed pixels in red), with how much changed and where."""
  before, after = openRender(beforePath), openRender(afterPath)
  if before.size != after.size:
    raise ValueError(f"The renders differ in size ({before.size} and {after.size}); compare two renders of the same view")
  beforePixels = numpy.asarray(before, dtype=numpy.int16)
  afterPixels = numpy.asarray(after, dtype=numpy.int16)
  changed = numpy.abs(afterPixels - beforePixels).max(axis=2) > changeThreshold
  grey = numpy.asarray(after.convert("L"), dtype=numpy.float64)[..., None] * dimShare
  changeMap = numpy.where(changed[..., None], changeColor, numpy.repeat(grey, 3, axis=2)).astype(numpy.uint8)
  share = float(changed.mean())
  rows, columns = numpy.nonzero(changed)
  bounds = None if not len(rows) else [int(columns.min()), int(rows.min()), int(columns.max()), int(rows.max())]
  writeGrid([(before, labels[0]), (after, labels[1]), (Image.fromarray(changeMap), f"changed: {share:.1%} of the view")], 3, outputPath)
  return {"changedShare": round(share, 4), "changedBounds": bounds}
