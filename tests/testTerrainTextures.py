import sys
from pathlib import Path

import numpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqTerrainTextures

side = eqTerrainTextures.textureSide
anywhere = {"minHeight": -10000.0, "maxHeight": 10000.0, "heightTolerance": 10.0}


def testLayerWeightsFallOffAcrossTheSlopeTolerance():
  layers = [anywhere | {"name": "grass", "minSlope": 0, "maxSlope": 90, "slopeTolerance": 1}, anywhere | {"name": "rock", "minSlope": 40, "maxSlope": 90, "slopeTolerance": 10}]
  slopes = numpy.array([[0.0, 30.0, 35.0, 40.0, 60.0]])
  grass, rock = eqTerrainTextures.layerWeights(layers, numpy.zeros_like(slopes), slopes, [None, None])
  # Rock is whole from 40 degrees, gone at 30, half at 35 (127.5, truncated); grass takes what rock leaves.
  assert rock.tolist() == [[0, 0, 127, 255, 255]]
  assert grass.tolist() == [[255, 255, 128, 0, 0]]


def testLaterLayersTakeTheirShareFirst():
  layers = [anywhere | {"name": "base", "minSlope": 0, "maxSlope": 90, "slopeTolerance": 1}] + [
    {"name": name, "minHeight": low, "maxHeight": 10000.0, "heightTolerance": 100.0, "minSlope": 0, "maxSlope": 90, "slopeTolerance": 1} for name, low in (("middle", 0.0), ("top", 100.0))
  ]
  heights = numpy.array([[-200.0, 50.0, 150.0]])
  base, middle, top = eqTerrainTextures.layerWeights(layers, heights, numpy.zeros_like(heights), [None, None, None])
  # The last layer weighs first: half at 50 (127.5 to 127), whole at 150; the middle layer covers what is left at its own factor.
  assert top.tolist() == [[0, 127, 255]]
  assert middle.tolist() == [[0, 128, 0]]
  assert base.tolist() == [[255, 0, 0]]


def testBlendMapTurnsTheFalloffIntoAThresholdOnItsValues():
  layers = [anywhere | {"name": "base", "minSlope": 0, "maxSlope": 90, "slopeTolerance": 1}, anywhere | {"name": "rock", "minSlope": 40, "maxSlope": 90, "slopeTolerance": 10, "blendSoftness": 100}]
  # At 33 degrees rock's factor is 0.3: t = (1 - factor) * 255 truncated is 178, its threshold 0.01 * 178^2 - 255 truncated 61, and with
  # softness 100 the blend value past it is the factor in 255ths, through the client's single-precision 1/255.
  slopes = numpy.array([[33.0, 33.0, 33.0, 50.0, 0.0]])
  blend = numpy.array([[50, 61, 100, 0, 255]], dtype=numpy.uint8)
  base, rock = eqTerrainTextures.layerWeights(layers, numpy.zeros_like(slopes), slopes, [None, blend])
  # 50 falls short (the texel is left out), 61 meets it (factor 0), 100 is 39 past it (factor 39/255). A whole factor (50 degrees) and
  # no factor (0 degrees) pass the blend map untouched.
  assert rock.tolist() == [[0, 0, 39, 255, 0]]
  assert base.tolist() == [[255, 255, 216, 0, 255]]


def testBlendThresholdsUseTheDoublePrecisionHundredth():
  # t 100 ((1 - factor) * 255 = 100.5): 0.01 as a double is slightly over a hundredth, so 0.01 * 100 * 100 - 255 is just over -155 and
  # truncates to -154, which a blend value of 0 is 154 past.
  factor = numpy.array([[1 - 100.5 / 255]])
  assert eqTerrainTextures.blendedFactor(factor, numpy.array([[0]], dtype=numpy.uint8), 100)[0, 0] == 154 * float(eqTerrainTextures.inverse255)
  # Softness 20 slopes the threshold by t * float32(0.08) + 1, truncated: 7 at t 75, where 0.01 * 75^2 - 255 is -198.75 (-198).
  factor = numpy.array([[1 - 75.5 / 255]])
  assert eqTerrainTextures.blendedFactor(factor, numpy.array([[0]], dtype=numpy.uint8), 20)[0, 0] == 1.0
  assert eqTerrainTextures.blendedFactor(factor, numpy.array([[0]], dtype=numpy.uint8), 99)[0, 0] == 198 * float(eqTerrainTextures.inverse255)


def testCoverageStacksLaterEcosystemsOverEarlierOnes():
  full = numpy.full((side, side), 255, dtype=numpy.int64)
  tile = {"layers": [{"mask": None}, {"mask": numpy.full((64, 64), 255, dtype=numpy.uint8)}, {"mask": numpy.full((64, 64), 128, dtype=numpy.uint8)}]}
  base, middle, top = eqTerrainTextures.coverages(tile, [[full], [full], [full]])
  # The client scales mask times weight by 1/65535, not 1/65025: the last ecosystem covers 255 * 128 / 65535 * 255 = 127.0, the middle
  # one 255 * 255 / 65535 * 255 = 253.0 of the 128 left (127.0), and the first ecosystem the remaining 1.
  assert (top == 127).all() and (middle == 127).all() and (base == 1).all()


def testDetailMaskAveragesEdgesWithTheNeighboringTile():
  own = numpy.full((side, side), 200, dtype=numpy.int64)
  left = numpy.full((side, side), 101, dtype=numpy.int64)
  below = numpy.full((side, side), 0, dtype=numpy.int64)
  mask = eqTerrainTextures.detailMask([own], [[left], None, [below], None])
  red = mask[..., 0].astype(int)
  # The left edge (corner included, left first) averages with the left tile, the bottom edge with the tile below; no right or upper
  # neighbor leaves those edges as they are.
  assert red[5, 0] == 150 and red[0, 0] == 150
  assert red[0, 5] == 100
  assert red[5, side - 1] == 200 and red[side - 1, 5] == 200 and red[5, 5] == 200
  assert (mask[..., 1] == 0).all() and (mask[..., 2] == 0).all()


def testSlopeTableIsTheNormalsAngleFromUp():
  assert eqTerrainTextures.slopeTable[1000] == 0
  assert abs(eqTerrainTextures.slopeTable[500] - 60) < 1e-4
  assert abs(eqTerrainTextures.slopeTable[0] - 90) < 1e-4
