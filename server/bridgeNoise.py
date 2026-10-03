"""Smooth noise shared by shaping, surfacing, and selectors. Runs under Blender's Python."""
import math

import mathutils
import mathutils.noise
import numpy

noiseBasis = "PERLIN_ORIGINAL"
# The standard deviation of a PERLIN_ORIGINAL sample, and of each component of its noise vector, measured over 20000 random points;
# dividing by it makes an amplitude the typical move.
noiseSpread = 0.278


def noiseSamplePoints(positions, featureSize, seed):
  if featureSize <= 0:
    raise ValueError(f"featureSize must be positive, got {featureSize}")
  return positions / featureSize + numpy.random.default_rng(seed).uniform(-1000, 1000, 3)


def fractalNoise(points, octaves, roughness):
  """Perlin noise summed over octaves, each twice the frequency of the last and `roughness` times its amplitude, scaled to a standard
  deviation of 1. The octaves are nearly independent, so their spreads add in quadrature."""
  total = numpy.zeros(len(points))
  amplitude, frequency, squareSum = 1.0, 1.0, 0.0
  for _ in range(octaves):
    total += amplitude * numpy.array([mathutils.noise.noise(mathutils.Vector(point * frequency), noise_basis=noiseBasis) for point in points])
    squareSum += amplitude * amplitude
    amplitude *= roughness
    frequency *= 2
  return total / (noiseSpread * math.sqrt(squareSum))
