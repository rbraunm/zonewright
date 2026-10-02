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
  grass, rock = eqTerrainTextures.layerWeights(layers, numpy.zeros_like(slopes), slopes, "test")
  # Rock is whole from 40 degrees, gone at 30, half at 35 (127.5 rounded to even); grass takes what rock leaves.
  assert rock.tolist() == [[0, 0, 128, 255, 255]]
  assert grass.tolist() == [[255, 255, 127, 0, 0]]


def testLaterLayersTakeTheirShareFirst():
  layers = [anywhere | {"name": "base", "minSlope": 0, "maxSlope": 90, "slopeTolerance": 1}] + [
    {"name": name, "minHeight": low, "maxHeight": 10000.0, "heightTolerance": 100.0, "minSlope": 0, "maxSlope": 90, "slopeTolerance": 1} for name, low in (("middle", 0.0), ("top", 100.0))
  ]
  heights = numpy.array([[-200.0, 50.0, 150.0]])
  base, middle, top = eqTerrainTextures.layerWeights(layers, heights, numpy.zeros_like(heights), "test")
  # The last layer weighs first: half at 50 (127.5 to 128), whole at 150; the middle layer covers what is left at its own factor.
  assert top.tolist() == [[0, 128, 255]]
  assert middle.tolist() == [[0, 127, 0]]
  assert base.tolist() == [[255, 0, 0]]


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
