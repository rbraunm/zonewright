"""An environment emitter's particles as they stand at one moment of its steady state, from its client definition
(eqEmitterDefinitions), following EQGraphicsDX9.dll's particle code (docs/clientRendering.md, Particle emitters): when its events
spawn particles, where each starts and how it moves, and its size, color, opacity, and texture frame at its age. The random draws the
client takes from shuffled tables are drawn here from a generator seeded by the emitter's name and position, so a view draws the same
particles every time."""
import hashlib
import math

import numpy

anglesPerTurn = 512.0
maximumCapacity = 100000
# CEmitter keeps the full particle count beyond its definition's distance only down to 16 particles (constructor, 0x1006c687).
lodFloorParticles = 16
millisecondsPerSecond = 1000.0
billboardModes = {0: "screen", 1: "beam", 2: "flat"}


def turnsToRadians(angle):
  return angle * 2 * math.pi / anglesPerTurn


def capacityFor(definition, lifespanMilliseconds):
  """The particle slots CEmitterDef::CreateEmitter gives an emitter (0x1006c9ed): enough for every particle alive in its steady state."""
  rate, life, burst, perEvent = definition["eventRate"], definition["particleLife"], definition["burst"], definition["perEvent"]
  duration = lifespanMilliseconds / millisecondsPerSecond if lifespanMilliseconds > 0 else definition["duration"]
  running = duration - definition["startDelay"]
  if running <= 0:
    return 0
  if definition["capacity"]:
    slots = definition["capacity"]
  elif running > life:
    events = int(life * rate)
    slots = (events + 1) * perEvent if burst <= perEvent else events * perEvent + burst
  else:
    slots = int(running * rate) * perEvent + burst
  return min(max(slots, definition["capacity"]), maximumCapacity)


def lodFloor(definition, capacity):
  steady = capacity if definition["burst"] <= definition["perEvent"] else capacity - definition["burst"] + definition["perEvent"]
  return lodFloorParticles / steady if steady > lodFloorParticles else 1.0


def frameAxes(definition):
  """The emitter's spawn axes: A up, B along +x, C along +y (an unattached CEmitter), turned by the definition's tilts (0x10072fe0)."""
  axisA, axisB, axisC = numpy.array([0.0, 0.0, 1.0]), numpy.array([1.0, 0.0, 0.0]), numpy.array([0.0, 1.0, 0.0])
  if definition["tiltB"]:
    angle = turnsToRadians(definition["tiltB"])
    axisA, axisB = axisA * math.cos(angle) + axisB * math.sin(angle), axisB * math.cos(angle) - axisA * math.sin(angle)
  if definition["tiltC"]:
    angle = turnsToRadians(definition["tiltC"])
    axisA, axisC = axisA * math.cos(angle) + axisC * math.sin(angle), axisC * math.cos(angle) - axisA * math.sin(angle)
  return axisA, axisB, axisC


def between(random, low, high):
  return low + (high - low) * random


def spawnShape(definition, generator, spawnIndex, perRow, rows):
  """Where a particle starts in the emitter's cylinder coordinates (angle in 512ths, radius out from axis A, height along it) and the
  direction its outward velocity takes (radial, along A), by the definition's shape (0x10073204)."""
  shape = definition["shape"]
  radiusX, radiusY, height = definition["shapeRadius"], definition["shapeRadiusY"], definition["shapeHeight"]

  def onEllipse(angle, scale=1.0):
    if radiusY == 0:
      return angle, radiusX * scale
    x, y = math.cos(turnsToRadians(angle)) * radiusX * scale, math.sin(turnsToRadians(angle)) * radiusY * scale
    return math.degrees(math.atan2(y, x)) / 360 * anglesPerTurn, math.hypot(x, y)

  randomAngle = generator.random() * anglesPerTurn
  if shape == 0:
    angle, radius, up = 0.0, 0.0, 0.0
  elif shape == 1:
    angle, radius = onEllipse(anglesPerTurn / perRow * spawnIndex)
    up = 0.0
  elif shape == 2:
    angle, radius = onEllipse(anglesPerTurn / perRow * spawnIndex)
    up = (spawnIndex // perRow) % rows * height / rows - height * 0.5
  elif shape == 3:
    angle, radius = onEllipse(randomAngle)
    up = (generator.random() - 0.5) * height
  elif shape == 4:
    while True:
      x, y = generator.uniform(-1.0, 1.0), generator.uniform(-1.0, 1.0)
      if x * x + y * y <= 1.0:
        break
    if radiusY == 0:
      angle, radius = math.degrees(math.atan2(y, x)) / 360 * anglesPerTurn, math.hypot(x, y) * radiusX
    else:
      angle, radius = math.degrees(math.atan2(y * radiusY, x * radiusX)) / 360 * anglesPerTurn, math.hypot(x * radiusX, y * radiusY)
    up = 0.0
  elif shape == 5:
    along = generator.uniform(-1.0, 1.0)
    across = math.sqrt(1.0 - along * along)
    angle, radius = onEllipse(randomAngle, across)
    up = along * (height if height else radiusX)
  elif shape == 6:
    # The face is the FPU's rounding of a draw times 6, so 6 comes up too and takes the top face, as 5 does (0x100737e4).
    first, second, face = generator.uniform(-1.0, 1.0), generator.uniform(-1.0, 1.0), int(numpy.rint(generator.random() * 6))
    x, y, z = {0: (-1.0, first, second), 1: (1.0, first, second), 2: (first, -1.0, second), 3: (first, 1.0, second), 4: (first, second, -1.0)}.get(face, (first, second, 1.0))
    x, y, z = x * radiusX, y * (radiusY if radiusY else radiusX), z * (height if height else radiusX)
    angle, radius, up = math.degrees(math.atan2(y, x)) / 360 * anglesPerTurn, math.hypot(x, y), z
  elif shape == 7:
    reach = generator.random()
    angle, radius = onEllipse(randomAngle, reach)
    up = reach * height
  elif shape == 8:
    tube = turnsToRadians(generator.random() * anglesPerTurn)
    angle, ringRadius = onEllipse(randomAngle)
    radius, up = ringRadius + math.cos(tube) * height, math.sin(tube) * height
  elif shape == 9:
    angle, radius = onEllipse(randomAngle)
    up = 0.0
  else:
    raise ValueError(f"Definition {definition['index']} '{definition['name']}' has spawn shape {shape}; the client draws shapes 0-9")
  outward = (1.0, 0.0)
  if shape in (5, 6) and (radius or up):
    length = math.hypot(radius, up)
    outward = (radius / length, up / length)
  return angle, radius, up, outward


def frameCell(definition, age):
  """The texture frame a particle shows at its age (frames at framesPerSecond, rounded as the FPU rounds, 0x100754de) as its cell in
  the texture: (u, v, width, height) with v down from the top, as the client's texture coordinates run."""
  frames = definition["frames"]
  if frames <= 1:
    return 0.0, 0.0, 1.0, 1.0
  frame = int(numpy.rint(definition["framesPerSecond"] * age)) % frames
  if frames <= 4:
    return (frame % 2) * 0.5, (frame // 2) * 0.5, 0.5, 0.5
  if frames <= 8:
    return (frame % 4) * 0.25, (frame // 4) * 0.5, 0.25, 0.5
  if frames <= 16:
    return (frame % 4) * 0.25, (frame // 4) * 0.25, 0.25, 0.25
  return 0.0, 0.0, 1.0, 1.0


def fade(value, over):
  return value / over if over > 0 and value < over else 1.0


def spawnSpread(definition):
  """What a definition's spawn offsets and shape are scaled by. One scaled by its emitter (emitterScaled) takes the emitter's scale,
  which the environment emitter path passes as 0 (0x1006af68 through 0x100705e0 to the CEmitter constructor's +0x88), so its particles
  all start at the emitter."""
  return 0.0 if definition["emitterScaled"] == 1 else 1.0


def seedFor(emitterName, position):
  digest = hashlib.sha256(f"{emitterName}|{position[0]:.3f}|{position[1]:.3f}|{position[2]:.3f}".encode("utf-8")).digest()
  return int.from_bytes(digest[:8], "little")


def steadyParticles(definition, emitterName, position, lifespanMilliseconds, cameraPosition):
  """The particles an emitter shows at a moment of its steady state, oldest first: each with its center, its age, its billboard's width
  and height (sizes in world units), its turn (radians), its RGBA color (0-1), and its texture cell. Refuses what is not traced: a
  billboard mode other than 0-2, and billboards turned to their velocity on screen."""
  if definition["billboard"] not in billboardModes:
    raise ValueError(f"billboard mode {definition['billboard']}, whose orientation is not traced")
  if definition["velocityAligned"]:
    raise ValueError("billboards turned to their screen velocity, which is not traced")
  if lifespanMilliseconds <= 0:
    return []
  life, rate = definition["particleLife"], definition["eventRate"]
  capacity = capacityFor(definition, lifespanMilliseconds)
  if life <= 0 or rate <= 0 or capacity == 0:
    return []
  origin = numpy.asarray(position, dtype=float)
  distance = float(numpy.linalg.norm(origin - numpy.asarray(cameraPosition, dtype=float)))
  lodFactor = max(definition["lodDistance"] / distance if distance > definition["lodDistance"] > 0 else 1.0, lodFloor(definition, capacity))
  generator = numpy.random.default_rng(seedFor(emitterName, origin))
  axisA, axisB, axisC = frameAxes(definition)
  spread = spawnSpread(definition)
  # Offsets move the spawn point along the untilted axes, before the tilts turn them (0x10072f14).
  base = origin + spread * (numpy.array([0.0, 0.0, 1.0]) * definition["offsetA"] + numpy.array([1.0, 0.0, 0.0]) * definition["offsetB"] + numpy.array([0.0, 1.0, 0.0]) * definition["offsetC"])
  perEvent = definition["perEvent"]
  perRow, rows = perEvent, 1
  if definition["shape"] == 2:
    perRow, rows = evenCylinderLayout(perEvent)
  phase = generator.random()
  startColor = numpy.array([definition["startRed"], definition["startGreen"], definition["startBlue"]], dtype=float)
  endColor = numpy.array([definition["endRed"], definition["endGreen"], definition["endBlue"]], dtype=float)
  particles = []
  event = 0
  spawnIndex = 0
  while (age := (event + phase) / rate) < life and len(particles) < capacity:
    wanted = perEvent * lodFactor
    count = int(wanted) + (1 if generator.random() < wanted - int(wanted) else 0)
    for _ in range(count):
      if len(particles) >= capacity:
        break
      particles.append(particleAt(definition, generator, base, axisA, axisB, axisC, age, spawnIndex, perRow, rows, startColor, endColor))
      spawnIndex += 1
    event += 1
  particles.reverse()
  return [particle for particle in particles if particle is not None]


def evenCylinderLayout(count):
  """Rows and particles per row for shape 2, as the client splits an event's particles (0x10072c5f)."""
  if count < 9:
    return 16, 8
  for limit, rows in ((0x12, 3), (0x20, 4), (0x48, 6), (0x80, 8), (0xC8, 10), (0x120, 12), (0x200, 16), (0x480, 24), (0xC80, 40)):
    if count <= limit:
      return count // rows, rows
  rows = 60 if count <= 0x1C20 else 100
  return count // rows, rows


def particleAt(definition, generator, base, axisA, axisB, axisC, age, spawnIndex, perRow, rows, startColor, endColor):
  initialTurn = generator.random() * millisecondsPerSecond if definition["randomRotation"] else 0.0
  spin = between(generator.random(), definition["spinMinimum"], definition["spinMaximum"])
  sizeRandom = generator.random()
  height = between(sizeRandom, definition["heightMinimum"], definition["heightMaximum"])
  width = between(sizeRandom if definition["linkedSize"] == 1 else generator.random(), definition["widthMinimum"], definition["widthMaximum"])
  angle, radius, up, (outwardRadial, outwardUp) = spawnShape(definition, generator, spawnIndex, perRow, rows)
  radius, up = radius * spawnSpread(definition), up * spawnSpread(definition)
  velocityA = between(generator.random(), definition["velocityAMinimum"], definition["velocityAMaximum"])
  velocityB = between(generator.random(), definition["velocityBMinimum"], definition["velocityBMaximum"])
  velocityC = between(generator.random(), definition["velocityCMinimum"], definition["velocityCMaximum"])
  velocityOut = between(generator.random(), definition["velocityOutMinimum"], definition["velocityOutMaximum"])
  swirl = between(generator.random(), definition["swirlMinimum"], definition["swirlMaximum"])

  def travelled(velocity, acceleration):
    return velocity * age + 0.5 * acceleration * age * age

  out = travelled(velocityOut, definition["accelerationOut"])
  radius += outwardRadial * out
  up += travelled(velocityA, definition["accelerationA"]) + outwardUp * out
  # A particle whose radius has crossed the axis is not drawn unless the definition allows it (0x10074810).
  if radius < 0 and definition["crossesAxis"] != 1:
    return None
  angle += travelled(swirl, definition["swirlAcceleration"])
  offset = axisB * travelled(velocityB, definition["accelerationB"]) + axisC * travelled(velocityC, definition["accelerationC"])
  offset = offset + numpy.array([definition["driftX"] * age, 0.0, -0.5 * definition["gravity"] * age * age])
  turn = turnsToRadians(angle)
  center = base + offset + axisA * up + (axisB * math.cos(turn) + axisC * math.sin(turn)) * radius
  remaining = definition["particleLife"] - age
  sizeFactor = definition["sizeScale"] * fade(age, definition["sizeFadeIn"]) * fade(remaining, definition["sizeFadeOut"])
  alpha = min(fade(age, definition["alphaFadeIn"]) * fade(remaining, definition["alphaFadeOut"]), definition["maximumAlpha"])
  if numpy.array_equal(startColor, endColor):
    color = startColor
  else:
    color = startColor + numpy.rint((endColor - startColor) * (age / definition["particleLife"]))
  return {
    "center": center.tolist(), "age": age, "axis": axisA.tolist(),
    "width": width * sizeFactor * fade(remaining, definition["widthFadeOut"]), "height": height * sizeFactor * fade(remaining, definition["heightFadeOut"]),
    "turn": turnsToRadians(initialTurn + spin * age), "color": [*(numpy.clip(color, 0, 255) / 255).tolist(), float(numpy.rint(alpha * 255) / 255)],
    "cell": frameCell(definition, age),
  }
