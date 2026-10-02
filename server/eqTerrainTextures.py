"""The textures the client builds for each terrain tile (docs/clientRendering.md, EQ terrain): for every ecosystem on the tile, a color
map (the layers' cover maps weighed by height and slope, alpha its coverage of the tile) and a detail mask (the weight of each detail
layer), computed as EQGraphicsDX9.dll does at 0x100eeed0, 0x100ee790, 0x100f4690, and 0x100ac770."""
import io
import math

import numpy
from PIL import Image

import eqTextures

# Texels per tile side: CEQTerrainSystem's distance table (0x100ec12a) gives every ring of tiles 32.
textureSide = 32
# Texture layers past the tenth are skipped by the client.
maximumLayers = 10
# Detail masks hold one weight per color channel.
maximumDetailLayers = 3
# A layer's slope in degrees, by its normal's height times 1000 (0x100ec630): acos in single precision, and 0 at straight up.
slopeTable = numpy.append(numpy.degrees(numpy.arccos(numpy.arange(1000, dtype=numpy.float32) * numpy.float32(0.001))).astype(numpy.float32), numpy.float32(0))
inverse255 = numpy.float32(1 / 255)
inverse65535 = numpy.float32(1 / 65535)
defaultMap = "default.bmp"


def coverImage(ddsBytes, side, sourceName):
  """A cover map at the tile texture size, as the client reads it from the mip chain D3DX builds by box filtering (the DXT5
  recompression of each generated level is not reproduced)."""
  image = numpy.asarray(Image.open(io.BytesIO(eqTextures.repairDDS(ddsBytes))).convert("RGB"), dtype=numpy.float64)
  height, width = image.shape[:2]
  if height != width or width % side or width & (width - 1):
    raise ValueError(f"{sourceName} is {width}x{height}; cover maps are read as square powers of two of at least {side}")
  factor = width // side
  return numpy.rint(image.reshape(side, factor, side, factor, 3).mean(axis=(1, 3))).astype(numpy.uint8)


def vertexNormals(heights, neighbors, spacing):
  """Each vertex's normal from the heights beside it (0x100f2190): differences across the vertex, reaching into the neighboring tile at
  an edge (whose shared column or row is this tile's), one-sided where there is no neighbor."""
  side = heights.shape[0]
  padded = numpy.empty((side + 2, side + 2))
  padded[1:-1, 1:-1] = heights
  left, right, down, up = neighbors
  padded[1:-1, 0] = left[:, side - 2] if left is not None else numpy.nan
  padded[1:-1, -1] = right[:, 1] if right is not None else numpy.nan
  padded[0, 1:-1] = down[side - 2, :] if down is not None else numpy.nan
  padded[-1, 1:-1] = up[1, :] if up is not None else numpy.nan
  before, after = padded[1:-1, :-2], padded[1:-1, 2:]
  differenceX = numpy.where(numpy.isnan(before), heights - after, numpy.where(numpy.isnan(after), before - heights, before - after))
  below, above = padded[:-2, 1:-1], padded[2:, 1:-1]
  differenceY = numpy.where(numpy.isnan(below), heights - above, numpy.where(numpy.isnan(above), below - heights, below - above))
  normals = numpy.stack([differenceX, differenceY, numpy.full_like(heights, 2 * spacing)], axis=-1)
  return normals / numpy.linalg.norm(normals, axis=-1, keepdims=True)


def packedNormals(normals):
  """Normals as the vertex buffer stores them (0x100a3f30): bytes truncated from 255 * (n * 0.5 + 0.5), read back as 2 * byte / 255 - 1."""
  return numpy.floor(255 * (normals * 0.5 + 0.5)).astype(numpy.uint8).astype(numpy.float64) * 2 / 255 - 1


def texelGrid(vertexSide):
  """Where each texel samples the vertex grid: the cell and the fraction across it, texel t at t * (vertices - 1) / (texels - 1)."""
  position = numpy.arange(textureSide) * ((vertexSide - 1) / (textureSide - 1))
  cell = numpy.minimum(numpy.floor(position).astype(int), vertexSide - 2)
  return cell, position - cell


def bilinear(values, cell, fraction):
  rows, columns = numpy.meshgrid(cell, cell, indexing="ij")
  fractionY, fractionX = numpy.meshgrid(fraction, fraction, indexing="ij")
  low = values[rows, columns] * (1 - fractionX) + values[rows, columns + 1] * fractionX
  high = values[rows + 1, columns] * (1 - fractionX) + values[rows + 1, columns + 1] * fractionX
  return low * (1 - fractionY) + high * fractionY


def layerFactor(layer, heights, slopes):
  """How fully a texture layer covers each texel by its height and slope ranges, falling off across their tolerances (0x100ee790)."""
  minimum, maximum, tolerance = layer["minHeight"], layer["maxHeight"], layer["heightTolerance"]
  with numpy.errstate(divide="ignore", invalid="ignore"):
    factor = numpy.where(heights > maximum, 1 - (heights - maximum) / tolerance, numpy.where(heights < minimum, 1 - (minimum - heights) / tolerance, 1.0))
    inside = (heights <= maximum + tolerance) & (heights >= minimum - tolerance)
    minimum, maximum, tolerance = layer["minSlope"], layer["maxSlope"], layer["slopeTolerance"]
    factor = numpy.where(slopes < minimum, factor * (1 - (minimum - slopes) / tolerance), numpy.where(slopes > maximum, factor * (1 - (slopes - maximum) / tolerance), factor))
    inside &= (slopes >= minimum - tolerance) & (slopes <= maximum + tolerance)
  return numpy.where(inside, factor, 0.0)


def layerWeights(layers, heights, slopes, sourceName):
  """Each texture layer's weight per texel, 0-255: the last layer first, each later one taking its share of what earlier ones left,
  and the first layer the rest."""
  for layer in layers:
    for key in ("blendMap", "layeringMap"):
      if layer.get(key, defaultMap) != defaultMap:
        raise ValueError(f"{sourceName}: layer {layer['name']} has {key} {layer[key]}, which is not read yet")
  weights = [numpy.zeros(heights.shape, dtype=numpy.int64) for _ in layers]
  taken = numpy.zeros(heights.shape, dtype=numpy.int64)
  for index in range(min(len(layers), maximumLayers) - 1, 0, -1):
    weights[index] = numpy.rint(layerFactor(layers[index], heights, slopes) * (255 - taken)).astype(numpy.int64)
    taken += weights[index]
  weights[0] = 255 - taken
  return weights


def colorMap(layers, weights, covers):
  """The color map's color (0x100eeed0): each layer's cover map times its weight, the first layer's written, the others added (bytes
  wrapping), a weight below 3 adding nothing and one of 253 or more taking the cover map whole."""
  color = numpy.zeros((textureSide, textureSide, 3), dtype=numpy.int64)
  for index, (layer, weight) in enumerate(zip(layers, weights)):
    cover = covers[layer["coverMap"]].astype(numpy.int64)
    scaled = numpy.rint(cover * (weight.astype(numpy.float32) * inverse255)[..., None]).astype(numpy.int64)
    whole = (weight >= 253)[..., None]
    partial = ((weight >= 3) & (weight < 253))[..., None]
    if index == 0:
      color = numpy.where(whole, cover, numpy.where(partial, scaled, 0))
    else:
      color = numpy.where(whole, cover, numpy.where(partial, (color + scaled) & 0xFF, color))
  return color.astype(numpy.uint8)


def maskSample(mask):
  """A tile's ecosystem mask at the texture size, texel t reading mask texel round(t * maskSide / textureSide + 0.5) (0x100f4690)."""
  side = mask.shape[0]
  index = numpy.rint(numpy.arange(textureSide) * (side / textureSide) + 0.5).astype(int)
  if index.max() >= side:
    raise ValueError(f"A {side}x{side} ecosystem mask is read past its edge at {textureSide} texels")
  return mask[numpy.ix_(index, index)].astype(numpy.int64)


def coverages(tile, weightsByEcosystem):
  """Each ecosystem's coverage of the tile, 0-255 (0x100f4690): the last ecosystem its mask, each earlier one its mask of what later ones
  left, and the first ecosystem the rest, so the passes add to the whole."""
  layers = tile["layers"]
  result = [None] * len(layers)
  taken = numpy.zeros((textureSide, textureSide), dtype=numpy.int64)
  for index in range(len(layers) - 1, 0, -1):
    mask = maskSample(layers[index]["mask"])
    alpha = numpy.zeros_like(taken)
    for weight in weightsByEcosystem[index]:
      alpha = (alpha + numpy.rint((weight * mask) * inverse65535 * 255.0).astype(numpy.int64)) & 0xFF
    alpha = numpy.rint((255 - taken).astype(numpy.float32) * inverse255 * alpha).astype(numpy.int64)
    alpha = numpy.where(alpha >= 253, 255, numpy.where(alpha < 3, 0, alpha))
    result[index] = alpha
    taken = (taken + alpha) & 0xFF
  result[0] = (255 - taken) & 0xFF
  return result


def detailMask(weights, neighborWeights):
  """The detail mask's channels (0x100ac770): red, green, and blue the first three layers' weights, each edge texel averaged with the
  neighboring tile's texel beside it where that tile has the same ecosystem (left, right, below, above, in that order of precedence)."""
  left, right, down, up = neighborWeights
  last = textureSide - 1
  channels = []
  for index in range(maximumDetailLayers):
    if index >= len(weights):
      channels.append(numpy.zeros((textureSide, textureSide), dtype=numpy.int64))
      continue
    own = weights[index]
    mixed = own.copy()
    pending = numpy.ones(own.shape, dtype=bool)
    for neighbor, edge, besideEdge in ((left, (slice(None), 0), (slice(None), last)), (right, (slice(None), last), (slice(None), 0)), (down, (0, slice(None)), (last, slice(None))), (up, (last, slice(None)), (0, slice(None)))):
      if neighbor is None:
        continue
      take = pending[edge]
      mixed[edge] = numpy.where(take, (own[edge] + neighbor[index][besideEdge]) // 2, mixed[edge])
      pending[edge] &= ~take
    channels.append(mixed)
  return numpy.stack(channels, axis=-1).astype(numpy.uint8)


def tileTextures(terrain, ecosystems, covers):
  """For each tile, for each ecosystem on it in order: its color map and detail mask (RGBA, rows along y), keyed by tile origin."""
  tilesByGrid = {(tile["longitude"], tile["latitude"]): tile for tile in terrain["tiles"]}
  spacing = terrain["header"]["unitsPerVertex"]
  vertexSide = terrain["header"]["quadsPerTile"] + 1
  cell, fraction = texelGrid(vertexSide)
  weights = {}
  for key, tile in tilesByGrid.items():
    longitude, latitude = key
    neighbors = [tilesByGrid.get(grid) for grid in ((longitude - 1, latitude), (longitude + 1, latitude), (longitude, latitude - 1), (longitude, latitude + 1))]
    normals = vertexNormals(tile["heights"].astype(numpy.float64), [None if neighbor is None else neighbor["heights"].astype(numpy.float64) for neighbor in neighbors], spacing)
    heights = bilinear(tile["heights"].astype(numpy.float64), cell, fraction)
    slopes = slopeTable[numpy.floor(bilinear(normals[..., 2], cell, fraction) * 1000 + 0.5).astype(int)]
    weights[key] = [layerWeights(ecosystems[layer["ecosystem"]], heights, slopes, layer["ecosystem"]) for layer in tile["layers"]]
  textures = {}
  for key, tile in tilesByGrid.items():
    longitude, latitude = key
    alphas = coverages(tile, weights[key])
    perEcosystem = []
    for index, layer in enumerate(tile["layers"]):
      ecosystem = layer["ecosystem"]
      neighborWeights = []
      for grid in ((longitude - 1, latitude), (longitude + 1, latitude), (longitude, latitude - 1), (longitude, latitude + 1)):
        neighbor = tilesByGrid.get(grid)
        slot = None if neighbor is None else next((position for position, candidate in enumerate(neighbor["layers"]) if candidate["ecosystem"] == ecosystem), None)
        neighborWeights.append(None if slot is None else weights[grid][slot])
      color = colorMap(ecosystems[ecosystem], weights[key][index], covers)
      mask = detailMask(weights[key][index], neighborWeights)
      alpha = alphas[index].astype(numpy.uint8)[..., None]
      perEcosystem.append({"ecosystem": ecosystem, "colorMap": numpy.concatenate([color, alpha], axis=-1), "detailMask": numpy.concatenate([mask, alpha], axis=-1)})
    textures[(tile["x"], tile["y"])] = perEcosystem
  return textures
