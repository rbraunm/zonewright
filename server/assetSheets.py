"""Contact sheets: many assets in one numbered image, so they are looked at and compared side by side."""
import math

from PIL import Image, ImageDraw, ImageFont

# Cell sides and how many cells a sheet of each holds.
cellSides = {128: 48, 256: 16}
labelHeight = 28
gap = 4
checkerSide = 8
backgroundColor = (34, 34, 34)
labelColor = (230, 230, 230)
jpegQuality = 88


def checkerboard(cellSide):
  board = Image.new("RGB", (cellSide, cellSide), (200, 200, 200))
  drawing = ImageDraw.Draw(board)
  for row in range(0, cellSide, checkerSide):
    for column in range(0, cellSide, checkerSide):
      if (row + column) // checkerSide % 2:
        drawing.rectangle([column, row, column + checkerSide - 1, row + checkerSide - 1], fill=(150, 150, 150))
  return board


def cellImage(imagePath, tiled, showAlpha, cellSide):
  """One cell: the image fitted to the cell, or with tiled two by two at half size so seams show; its alpha over a checkerboard with
  showAlpha, else its color alone."""
  with Image.open(imagePath) as source:
    image = source.convert("RGBA")
  if tiled:
    half = image.resize((cellSide // 2, cellSide // 2), Image.Resampling.LANCZOS)
    image = Image.new("RGBA", (cellSide, cellSide))
    for left in (0, cellSide // 2):
      for top in (0, cellSide // 2):
        image.paste(half, (left, top))
  else:
    image.thumbnail((cellSide, cellSide), Image.Resampling.LANCZOS)
  cell = checkerboard(cellSide) if showAlpha else Image.new("RGB", (cellSide, cellSide), backgroundColor)
  offset = ((cellSide - image.width) // 2, (cellSide - image.height) // 2)
  if showAlpha:
    cell.paste(image, offset, image)
  else:
    cell.paste(image.convert("RGB"), offset)
  return cell


def writeSheet(cells, columns, outputPath, cellSide):
  """cells: [{image, lines (two short label lines), tiled, showAlpha}], written as a JPEG."""
  if cellSide not in cellSides:
    raise ValueError(f"cellSide is one of {list(cellSides)}, got {cellSide}")
  if not 1 <= len(cells) <= cellSides[cellSide]:
    raise ValueError(f"A sheet of {cellSide}-pixel cells holds 1 to {cellSides[cellSide]}, got {len(cells)}")
  if not 1 <= columns <= 12:
    raise ValueError(f"columns must be 1 to 12, got {columns}")
  rows = math.ceil(len(cells) / columns)
  sheet = Image.new("RGB", (gap + columns * (cellSide + gap), gap + rows * (cellSide + labelHeight + gap)), backgroundColor)
  drawing = ImageDraw.Draw(sheet)
  font = ImageFont.load_default(size=11)
  for index, cell in enumerate(cells):
    left = gap + index % columns * (cellSide + gap)
    top = gap + index // columns * (cellSide + labelHeight + gap)
    sheet.paste(cellImage(cell["image"], cell["tiled"], cell["showAlpha"], cellSide), (left, top))
    for line, text in enumerate(cell["lines"][:2]):
      drawing.text((left, top + cellSide + 1 + 13 * line), text[:cellSide // 6], fill=labelColor, font=font)
  outputPath.parent.mkdir(parents=True, exist_ok=True)
  sheet.save(outputPath, "JPEG", quality=jpegQuality)
  return {"width": sheet.width, "height": sheet.height}
