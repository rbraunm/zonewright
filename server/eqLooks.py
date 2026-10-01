"""How the client dresses a character's head without equipment, transcribed from eqgame.exe: a Luclin model's faces, and hair and
beard items with their colors; an EQG player model's faces, eyes, and attached pieces from PlayerCustomization.txt."""
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
# 0x40ed07: the races whose EQG models take their looks from PlayerCustomization.txt (0x40ecf0).
playerRaces = frozenset(range(1, 13)) | {128, 130, 330, 522}
# 0x40b580: the skeleton bone each EQG look piece attaches at.
hairBone, beardBone, facialAttachmentBone, tattooBone = "CHEST_CHEST03", "NECK_NECK", "HEAD_HEAD", "ROOT_BONE"
customizationCounts = ("faces", "hairStyles", "eyes", "beards", "tattoos", "facialAttachments")
# 0x40c4a8: each race's offset into the client's CHR_EYE<n>_MDF eye materials; other races take none.
eyeOffsets = {6: 20, 9: 100, 10: 80, 128: 40, 130: 60, 330: 120}
# 0x40c447: the fixed right eye a male of these (race, face % 10) takes, whatever his eye color.
fixedRightEyes = {(1, 3): 209, (2, 6): 209, (8, 5): 207, (2, 4): 207}
halfling = 11
noEyeColor = 255


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


def tint(color):
  """A color as the client tints with it: black leaves the piece untinted (0x40b260)."""
  return color & 0xFFFFFF or untinted


def isEQGPlayerModel(code):
  return any(registeredCode == code and race in playerRaces and flags & eqRaces.eqgModelFlag for race, _, registeredCode, flags, _ in eqRaces.raceRegistrations)


def playerCustomizations(clientRoot):
  """Resources/PlayerCustomization.txt as eqgame.exe reads it (0x8ce3f0), by (race, heritage, sex): the base color, the color list,
  and how many of each look there are. A later row for the same key wins, as the client chains it ahead of the earlier one."""
  path = clientRoot / "Resources" / "PlayerCustomization.txt"
  if not path.is_file():
    raise ValueError(f"The client has no {path}")
  customizations = {}
  for lineNumber, line in enumerate(path.read_text(encoding="latin1").splitlines(), 1):
    if not line.strip() or line.startswith("#"):
      continue
    fields = line.split("^")
    if len(fields) != 13:
      raise ValueError(f"{path}:{lineNumber} has {len(fields)} fields, not the 13 from RACE to SEX")
    race, heritage, _, baseColor, _, colors, *counts, sex = fields
    customizations[(int(race), int(heritage), int(sex))] = {
      "baseColor": int(baseColor), "colors": [int(color, 16 if color.startswith("0x") else 10) for color in colors.split(",") if color],
    } | dict(zip(customizationCounts, map(int, counts)))
  return customizations


def eqgPlayerLooks(clientRoot, code, appearance):
  """The looks eqgame.exe gives an EQG player model as it spawns (0x40ecf0). A value past its count in the model's row is 0. The
  face and eye layers go on palette entries 0, 1, and 2 (0x40ad60, 0x40add0), both eyes taking eyeColor1. The hair, beard, tattoo,
  and facial attachment pieces (0x40ac80, 0x40acf0, 0x40ae60, 0x40b050) are (name, bone, tint, layer for their palette entry 0):
  hair and beard tinted from the color list, the others by the base color, and a tattoo past 0 laying its own layer."""
  race, gender = raceAndGender(code)
  customization = playerCustomizations(clientRoot).get((race, appearance["heritage"], gender))
  if customization is None:
    raise ValueError(f"PlayerCustomization.txt has no row for race {race}, heritage {appearance['heritage']}, sex {gender}")
  colorCount = len(customization["colors"])
  limits = {
    "faceStyle": customization["faces"], "hairStyle": customization["hairStyles"], "eyeColor1": customization["eyes"], "facialHair": customization["beards"],
    "tattoo": customization["tattoos"], "details": customization["facialAttachments"], "hairColor": colorCount, "facialHairColor": colorCount,
  }
  look = {key: appearance[key] if appearance[key] < limit else 0 for key, limit in limits.items()}
  # 0x8ceb30: an empty color list gives black, which leaves the piece untinted.
  colors = customization["colors"] or [0]
  baseTint = tint(customization["baseColor"])
  return {
    "layers": [(0, f"C_{code}_HEAD_S{look['faceStyle']:02d}_M01"), (1, f"C_{code}_RIGHTEYE_S{look['eyeColor1']:02d}_M02"), (2, f"C_{code}_LEFTEYE_S{look['eyeColor1']:02d}_M03")],
    "pieces": [
      (f"{code}_HAIR_{look['hairStyle']:02d}", hairBone, tint(colors[look["hairColor"]]), None),
      (f"{code}_FACIALHAIR_{look['facialHair']:02d}", beardBone, tint(colors[look["facialHairColor"]]), None),
      (f"{code}_TATTOO_00", tattooBone, baseTint, f"A_{code}_TATTOO_S{look['tattoo']:02d}_M01" if look["tattoo"] > 0 else None),
      (f"{code}_FACIALATT_{look['details']:02d}", facialAttachmentBone, baseTint, None),
    ],
  }


def luclinEyes(code, appearance, paletteNames):
  """The CHR_EYE<n>_MDF material each eye of a Luclin model takes as it spawns, by the palette material it replaces. Both eyes take
  eyeColor1, the right first (eqgame.exe 0x40f032, 0x40c350); EQGraphicsDX9.dll finds the eye entries by name (0x10040c50). The
  client changes neither eye for color 255 or a palette without a right eye."""
  race, gender = raceAndGender(code)
  left = right = None
  for name in paletteNames:
    if name[3:8] == "L_EYE":
      left = name
    elif name[3:8] == "R_EYE":
      right = name
    elif race == halfling and gender == 0 and name[3:7] == "R_01":
      right = name
    elif race == froglok and name[4:7] == "EYE":
      left = right = name
  color = appearance["eyeColor1"]
  if color == noEyeColor or right is None:
    return {}
  offset = eyeOffsets.get(race, 0)
  rightColor = fixedRightEyes.get((race, appearance["faceStyle"] % 10), color) if gender == 0 else color
  eyes = {right: f"CHR_EYE{rightColor + offset:03d}_MDF"}
  if left is not None:
    eyes[left] = f"CHR_EYE{color + offset:03d}_MDF"
  return eyes


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
