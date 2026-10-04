"""Sheets of preview renders: the same view before and after a change with what changed between them, and views around a subject."""
from pathlib import Path

import numpy
from PIL import Image, ImageDraw, ImageFont

cellSize = (640, 360)
jpegQuality = 90
gap = 8
labelHeight = 20
backgroundColor = (40, 40, 40)
labelColor = (235, 235, 235)
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


def writeGrid(cells, columns, outputPath):
  """cells: [(image, label)] laid out in rows of `columns`, each scaled to cellSize, labeled under it; written as JPEG."""
  rows = -(-len(cells) // columns)
  width, height = cellSize
  sheet = Image.new("RGB", (gap + columns * (width + gap), gap + rows * (height + labelHeight + gap)), backgroundColor)
  draw = ImageDraw.Draw(sheet)
  font = ImageFont.load_default(size=14)
  for index, (image, label) in enumerate(cells):
    left = gap + index % columns * (width + gap)
    top = gap + index // columns * (height + labelHeight + gap)
    sheet.paste(image.resize(cellSize, Image.Resampling.LANCZOS), (left, top))
    draw.text((left, top + height + 2), label, fill=labelColor, font=font)
  sheet.save(outputPath, "JPEG", quality=jpegQuality)
  return {"width": sheet.width, "height": sheet.height}


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
