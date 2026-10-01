"""How the client dresses a Luclin character's head without equipment, transcribed from eqgame.exe: faces, and hair and beard items with their colors."""
import eqRaces

# 0x40a290: each race's block of Luclin hair and beard items; a female's block starts 30 higher, and other races' at 0.
itemBases = {1: 360, 2: 540, 3: 480, 4: 240, 5: 300, 6: 180, 7: 60, 8: 120, 9: 660, 10: 720, 11: 780, 12: 0, 128: 420, 130: 600, 330: 840}
femaleItemOffset = 30
# 0x40d85a and 0x40d8f6: hair items count from 1000 and beards from 2000 within the block, attached at these points (0x40bb14, 0x40bb58).
hairItemStart, beardItemStart = 1000, 2000
hairPoint, beardPoint = "HAIR_POINT_DAG", "BEARD_POINT_DAG"
# 0x40a240: the beard type of a male race (others 0); type 2 takes no beard (0x40d8e7), as a dwarf female does.
maleBeardTypes = {1: 1, 2: 1, 3: 1, 4: 0, 5: 2, 6: 2, 7: 2, 8: 1, 9: 0, 10: 0, 11: 1, 12: 1}
noBeardType = 2
vahShir, froglok = 130, 330
# 0x40aaa1: a Vah Shir takes hair and beard items only with face 7.
vahShirItemFace = 7
# 0xac1a70: hair and beard colors by index, 0xRRGGBB, applied as a tint (0x40b260).
hairColors = (
  0x2E1A0C, 0x432916, 0x4E3123, 0x7F513B, 0x650B06, 0xB93714, 0xD75532, 0x8B721E, 0xCCB361, 0xE1DD6C, 0xFBFF81, 0xFDFAC9,
  0xFFFFFF, 0xDEDEDE, 0x808080, 0x6F8690, 0x3E585A, 0x293E40, 0x121214, 0xC9E5FD, 0xC9FDFD, 0xE9C9FD, 0xCEFDC9, 0x559B48,
)
untinted = 0xFFFFFF
noStyle = 255


def raceAndGender(code):
  """The one (race, gender) eqgame.exe registers a model code for."""
  pairs = sorted({(race, gender) for race, gender, registeredCode, _, _ in eqRaces.raceRegistrations if registeredCode == code})
  if len(pairs) != 1:
    raise ValueError(f"eqgame.exe registers the model {code} for {pairs}, not one race and gender")
  return pairs[0]


def hairColor(index):
  """The tint for a color index; the client leaves an item untinted for an index past its table (0x40d939)."""
  if index < 0:
    raise ValueError(f"Hair and beard colors are 0 or more, not {index}")
  return hairColors[index] if index < len(hairColors) else untinted


def headItems(code, appearance):
  """The hair and beard items the client attaches to a WLD character (eqgame.exe 0x40d853, 0x40d8e0), as (item, point suffix,
  color) for the points that the model's skeleton must have for the client to attach them."""
  if appearance["hairStyle"] == noStyle and appearance["facialHair"] == noStyle:
    return []
  race, gender = raceAndGender(code)
  if race == froglok or (race == vahShir and appearance["faceStyle"] != vahShirItemFace):
    return []
  base = itemBases.get(race, 0) + (femaleItemOffset if gender == 1 else 0)
  items = []
  if appearance["hairStyle"] != noStyle:
    items.append((f"IT{hairItemStart + base + appearance['hairStyle']}", hairPoint, hairColor(appearance["hairColor"])))
  beardType = (noBeardType if race == 8 else 0) if gender == 1 else maleBeardTypes.get(race, 0)
  if appearance["facialHair"] != noStyle and beardType != noBeardType:
    items.append((f"IT{beardItemStart + base + appearance['facialHair']}", beardPoint, hairColor(appearance["facialHairColor"])))
  return items


def faceSwaps(code, faceStyle, materialNames):
  """A Luclin face swaps each head material <code>HE000<part>_MDF for <code>HE<face // 10><face % 10><part>_MDF where the file
  has it (eqgame.exe 0x40d1a0, the face split by 0x40c2a0)."""
  if faceStyle == 0:
    return {}
  set_, face = divmod(faceStyle, 10)
  swaps = {}
  for name in materialNames:
    if name.startswith(f"{code}HE000") and name.endswith("_MDF") and len(name) == len(code) + len("HE0001_MDF"):
      candidate = f"{code}HE{set_:02d}{face}{name[len(code) + 5]}_MDF"
      if candidate in materialNames:
        swaps[name] = candidate
  return swaps
