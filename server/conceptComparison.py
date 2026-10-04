"""Concept art against a render of the same view, compared as an artist compares them: one over the other to match the camera,
squinted into light and dark masses, by their strongest edges, and by palette."""
import math
from pathlib import Path

import numpy
from PIL import Image, ImageDraw, ImageFilter, ImageFont

cellWidth = 640
gap = 8
labelHeight = 20
backgroundColor = (40, 40, 40)
labelColor = (235, 235, 235)
jpegQuality = 90
renderLongSide = 960
analysisWidth = 480
# Blur radii are shares of the image's width: a squint merges everything finer than a few percent of the view into masses, and edges
# are found after a blur that leaves the big forms' outlines and drops texture grain.
squintBlurShare = 0.015
edgeBlurShare = 0.01
edgeShare = 0.06
edgeToleranceShare = 0.012
paletteSize = 6
valueBandLevels = (45, 135, 225)
conceptEdgeColor = numpy.array([0, 230, 255])
renderEdgeColor = numpy.array([255, 140, 0])
sharedEdgeColor = numpy.array([255, 255, 255])
horizonColor = (255, 225, 0)
lighterColor = numpy.array([230, 60, 40])
darkerColor = numpy.array([40, 110, 255])
dimShare = 0.35


def openImage(path, kind):
  imagePath = Path(path)
  if not imagePath.is_file():
    raise ValueError(f"No {kind} image at {path}")
  with Image.open(imagePath) as image:
    return image.convert("RGB")


def renderSize(conceptPath):
  """The render's [width, height] for a concept: its aspect, its long side renderLongSide."""
  concept = openImage(conceptPath, "concept")
  aspect = concept.width / concept.height
  if aspect >= 1:
    return [renderLongSide, max(1, round(renderLongSide / aspect))]
  return [max(1, round(renderLongSide * aspect)), renderLongSide]


def eyeLevelRow(forward, verticalFieldOfViewDegrees, height):
  """The image row of eye level for a camera without roll looking along forward, or None when it is off the frame."""
  pitch = math.asin(max(-1.0, min(1.0, forward[2])))
  if abs(pitch) >= math.pi / 2 - 1e-6:
    return None
  row = height / 2 + height / 2 * math.tan(pitch) / math.tan(math.radians(verticalFieldOfViewDegrees) / 2)
  return round(row, 1) if 0 <= row <= height else None


def labFromRGB(pixels):
  """CIE L*a*b* (D65) of sRGB pixels in 0-255, any leading shape."""
  linear = pixels.astype(numpy.float64) / 255.0
  linear = numpy.where(linear <= 0.04045, linear / 12.92, ((linear + 0.055) / 1.055) ** 2.4)
  xyz = linear @ numpy.array([[0.4124, 0.2126, 0.0193], [0.3576, 0.7152, 0.1192], [0.1805, 0.0722, 0.9505]])
  xyz /= numpy.array([0.95047, 1.0, 1.08883])
  cubeRoot = numpy.where(xyz > 216 / 24389, numpy.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
  return numpy.stack([116 * cubeRoot[..., 1] - 16, 500 * (cubeRoot[..., 0] - cubeRoot[..., 1]), 200 * (cubeRoot[..., 1] - cubeRoot[..., 2])], axis=-1)


def blurred(lightness, radius):
  image = Image.fromarray(numpy.clip(lightness / 100.0 * 255.0, 0, 255).astype(numpy.uint8))
  return numpy.asarray(image.filter(ImageFilter.GaussianBlur(radius)), dtype=numpy.float64) / 255.0 * 100.0


def valueThirds(squinted):
  """0 dark, 1 middle, 2 light: each image split at its own thirds, so the pattern of masses compares, not the exposure."""
  low, high = numpy.quantile(squinted, [1 / 3, 2 / 3])
  return (squinted > low).astype(numpy.int8) + (squinted > high).astype(numpy.int8)


def strongestEdges(lightness):
  smooth = blurred(lightness, edgeBlurShare * lightness.shape[1])
  padded = numpy.pad(smooth, 1, mode="edge")
  across = (padded[:-2, 2:] + 2 * padded[1:-1, 2:] + padded[2:, 2:]) - (padded[:-2, :-2] + 2 * padded[1:-1, :-2] + padded[2:, :-2])
  down = (padded[2:, :-2] + 2 * padded[2:, 1:-1] + padded[2:, 2:]) - (padded[:-2, :-2] + 2 * padded[:-2, 1:-1] + padded[:-2, 2:])
  magnitude = numpy.hypot(across, down)
  return magnitude > numpy.quantile(magnitude, 1 - edgeShare)


def grown(mask, radius):
  """The mask grown by radius pixels across and down."""
  result = mask
  for axis in (0, 1):
    spread = result.copy()
    for offset in range(1, radius + 1):
      later, earlier = [slice(None), slice(None)], [slice(None), slice(None)]
      later[axis], earlier[axis] = slice(offset, None), slice(None, -offset)
      spread[tuple(later)] |= result[tuple(earlier)]
      spread[tuple(earlier)] |= result[tuple(later)]
    result = spread
  return result


def palette(image):
  """The image's paletteSize dominant colors, median-cut, each {color [r, g, b], share of the pixels}, most common first."""
  quantized = image.quantize(colors=paletteSize, method=Image.Quantize.MEDIANCUT)
  colors = quantized.getpalette()[: 3 * paletteSize]
  counts = sorted(quantized.getcolors(), reverse=True)
  total = sum(count for count, _ in counts)
  return [{"color": colors[3 * index: 3 * index + 3], "share": count / total} for count, index in counts]


def nearestDistances(fromSwatches, toSwatches):
  """Each swatch's CIE76 color difference from the nearest of the others."""
  fromLab = labFromRGB(numpy.array([swatch["color"] for swatch in fromSwatches]))
  toLab = labFromRGB(numpy.array([swatch["color"] for swatch in toSwatches]))
  return numpy.linalg.norm(fromLab[:, None, :] - toLab[None, :, :], axis=2).min(axis=1)


def colorSummary(lab):
  lab = lab.reshape(-1, 3)
  return {
    "lightness": round(float(lab[:, 0].mean()), 1),
    "lightnessSpread": round(float(numpy.quantile(lab[:, 0], 0.9) - numpy.quantile(lab[:, 0], 0.1)), 1),
    "chroma": round(float(numpy.hypot(lab[:, 1], lab[:, 2]).mean()), 1),
    "redGreen": round(float(lab[:, 1].mean()), 1),
    "yellowBlue": round(float(lab[:, 2].mean()), 1),
  }


def hexColor(color):
  return "#" + "".join(f"{int(channel):02x}" for channel in color)


def paletteCell(conceptSwatches, renderSwatches, size):
  cell = Image.new("RGB", size, backgroundColor)
  draw = ImageDraw.Draw(cell)
  font = ImageFont.load_default(size=14)
  width, height = size
  barHeight = (height - 3 * 24) // 2
  for row, (label, swatches) in enumerate((("concept", conceptSwatches), ("render", renderSwatches))):
    top = 24 + row * (barHeight + 24)
    draw.text((8, top - 20), label, fill=labelColor, font=font)
    left = 8.0
    for swatch in swatches:
      right = left + (width - 16) * swatch["share"]
      draw.rectangle([round(left), top, max(round(left), round(right) - 1), top + barHeight], fill=tuple(swatch["color"]))
      left = right
  return cell


def numbersCell(lines, size):
  cell = Image.new("RGB", size, backgroundColor)
  draw = ImageDraw.Draw(cell)
  font = ImageFont.load_default(size=15)
  for index, line in enumerate(lines):
    draw.text((10, 10 + index * 22), line, fill=labelColor, font=font)
  return cell


def compare(conceptPath, renderPath, outputPath, forward, verticalFieldOfViewDegrees):
  """Write the comparison sheet to outputPath (JPEG) and return its numbers."""
  concept, render = openImage(conceptPath, "concept"), openImage(renderPath, "render")
  analysisSize = (analysisWidth, max(1, round(analysisWidth * render.height / render.width)))
  conceptLab = labFromRGB(numpy.asarray(concept.resize(analysisSize, Image.Resampling.LANCZOS)))
  renderSmall = render.resize(analysisSize, Image.Resampling.LANCZOS)
  renderLab = labFromRGB(numpy.asarray(renderSmall))

  squintRadius = squintBlurShare * analysisWidth
  conceptSquint, renderSquint = blurred(conceptLab[..., 0], squintRadius), blurred(renderLab[..., 0], squintRadius)
  conceptThirds, renderThirds = valueThirds(conceptSquint), valueThirds(renderSquint)
  sameThird = float((conceptThirds == renderThirds).mean())
  massCorrelation = float(numpy.corrcoef(conceptSquint.ravel(), renderSquint.ravel())[0, 1])

  conceptEdges, renderEdges = strongestEdges(conceptLab[..., 0]), strongestEdges(renderLab[..., 0])
  tolerance = max(1, round(edgeToleranceShare * analysisWidth))
  conceptEdgesMet = float((conceptEdges & grown(renderEdges, tolerance)).sum() / max(1, conceptEdges.sum()))
  renderEdgesMet = float((renderEdges & grown(conceptEdges, tolerance)).sum() / max(1, renderEdges.sum()))

  conceptSwatches = palette(concept.resize(analysisSize, Image.Resampling.LANCZOS))
  renderSwatches = palette(renderSmall)
  conceptToRender, renderToConcept = nearestDistances(conceptSwatches, renderSwatches), nearestDistances(renderSwatches, conceptSwatches)
  conceptPaletteDistance = float(sum(distance * swatch["share"] for distance, swatch in zip(conceptToRender, conceptSwatches)))
  renderPaletteDistance = float(sum(distance * swatch["share"] for distance, swatch in zip(renderToConcept, renderSwatches)))
  conceptColor, renderColor = colorSummary(conceptLab), colorSummary(renderLab)

  horizon = eyeLevelRow(forward, verticalFieldOfViewDegrees, render.height)
  cellSize = (cellWidth, max(1, round(cellWidth * render.height / render.width)))

  def scaled(image):
    return image.resize(cellSize, Image.Resampling.LANCZOS)

  conceptCell, renderCell = scaled(concept), scaled(render)
  overlayCell = Image.blend(renderCell, conceptCell, 0.5)
  if horizon is not None:
    horizonY = round(horizon / render.height * cellSize[1])
    ImageDraw.Draw(overlayCell).line([(0, horizonY), (cellSize[0], horizonY)], fill=horizonColor, width=2)

  levels = numpy.array(valueBandLevels, dtype=numpy.uint8)
  renderMasses = levels[renderThirds]
  dimmed = numpy.repeat((renderMasses.astype(numpy.float64) * dimShare)[..., None], 3, axis=2)
  massDifference = numpy.where((renderThirds > conceptThirds)[..., None], lighterColor, numpy.where((renderThirds < conceptThirds)[..., None], darkerColor, dimmed))

  edgePixels = numpy.full(conceptEdges.shape + (3,), 20, dtype=numpy.uint8)
  edgePixels[conceptEdges] = conceptEdgeColor
  edgePixels[renderEdges] = renderEdgeColor
  edgePixels[conceptEdges & renderEdges] = sharedEdgeColor

  numbers = [
    f"masses: {sameThird:.0%} in the same third, correlation {massCorrelation:.2f}",
    f"edges: {conceptEdgesMet:.0%} of the concept's met, {renderEdgesMet:.0%} of the render's",
    f"palettes: the concept's colors {conceptPaletteDistance:.0f} dE from the render's,",
    f"  the render's {renderPaletteDistance:.0f} dE from the concept's",
    "concept / render:",
    f"  lightness {conceptColor['lightness']:.0f} / {renderColor['lightness']:.0f}, spread {conceptColor['lightnessSpread']:.0f} / {renderColor['lightnessSpread']:.0f}",
    f"  chroma {conceptColor['chroma']:.0f} / {renderColor['chroma']:.0f}",
    f"  red-green {conceptColor['redGreen']:.0f} / {renderColor['redGreen']:.0f}, yellow-blue {conceptColor['yellowBlue']:.0f} / {renderColor['yellowBlue']:.0f}",
  ]
  cells = [
    (conceptCell, "concept"),
    (renderCell, "render"),
    (overlayCell, "the concept at half over the render" + ("" if horizon is None else "; the render's eye level yellow")),
    (scaled(Image.fromarray(levels[conceptThirds])), "concept squinted: its dark, middle, and light thirds"),
    (scaled(Image.fromarray(renderMasses)), "render squinted"),
    (scaled(Image.fromarray(massDifference.astype(numpy.uint8))), "where the render's masses are lighter (red) or darker (blue)"),
    (scaled(Image.fromarray(edgePixels)), "strongest edges: concept cyan, render orange, both white"),
    (paletteCell(conceptSwatches, renderSwatches, cellSize), "palettes, by share of the picture"),
    (numbersCell(numbers, cellSize), "numbers"),
  ]
  writeSheet(cells, 3, cellSize, outputPath)
  return {
    "eyeLevelRow": horizon,
    "masses": {"sameThird": round(sameThird, 3), "correlation": round(massCorrelation, 3)},
    "edges": {"conceptEdgesMet": round(conceptEdgesMet, 3), "renderEdgesMet": round(renderEdgesMet, 3)},
    "palette": {
      "conceptDistance": round(conceptPaletteDistance, 1),
      "renderDistance": round(renderPaletteDistance, 1),
      "concept": [{"color": hexColor(swatch["color"]), "share": round(swatch["share"], 3)} for swatch in conceptSwatches],
      "render": [{"color": hexColor(swatch["color"]), "share": round(swatch["share"], 3)} for swatch in renderSwatches],
    },
    "color": {"concept": conceptColor, "render": renderColor},
  }


def writeSheet(cells, columns, cellSize, outputPath):
  width, height = cellSize
  rows = -(-len(cells) // columns)
  sheet = Image.new("RGB", (gap + columns * (width + gap), gap + rows * (height + labelHeight + gap)), backgroundColor)
  draw = ImageDraw.Draw(sheet)
  font = ImageFont.load_default(size=14)
  for index, (image, label) in enumerate(cells):
    left = gap + index % columns * (width + gap)
    top = gap + index // columns * (height + labelHeight + gap)
    sheet.paste(image, (left, top))
    draw.text((left, top + height + 2), label, fill=labelColor, font=font)
  sheet.save(outputPath, "JPEG", quality=jpegQuality)
