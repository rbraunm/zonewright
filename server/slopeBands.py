"""The bands a face's steepness falls in, in degrees from level, each with a word and a color: the slope shading (bridgeViews) draws
every face in its band's color, a plan's slope layer lays the same over the relief, and legends and results name them. Faces past 90
degrees, overhangs, fall in the last band with the walls. Shared by the bridge and the server; bpy-free."""

bands = ((0, 10), (10, 25), (25, 45), (45, 80), (80, 180))
words = ("level", "gentle", "steep", "scramble", "wall or overhang")
colors = ((225, 222, 200), (190, 205, 95), (235, 165, 55), (200, 55, 45), (75, 70, 90))


def bandOf(degrees):
  """The index of the band holding a steepness in degrees from level (0 to 180: 90 upright, past it overhanging)."""
  if not 0 <= degrees <= 180:
    raise ValueError(f"A slope is 0 to 180 degrees from level, got {degrees}")
  return next(index for index, (low, high) in enumerate(bands) if low <= degrees < high or (high == 180 and degrees == 180))


def hexOf(color):
  return "#%02x%02x%02x" % color


def described():
  """Each band as results give it: its low and high degrees, its word, and its color."""
  return [{"low": low, "high": high, "word": word, "hex": hexOf(color)} for (low, high), word, color in zip(bands, words, colors)]


def legendText(index):
  """A band's legend entry: its word and its degrees; the last band is the walls and, past 90, the overhangs."""
  (low, high), word = bands[index], words[index]
  return f"{word}: {low} degrees and steeper" if high == 180 else f"{word}: {low} to {high} degrees"
