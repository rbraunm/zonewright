"""How the client gives a WLD character its animations, transcribed from eqgame.exe: the animations it loads per model, and the code a model borrows animations from when it has none of its own."""
import re

# The animation table eqgame.exe fills at 0x408040 (0xb787a0, 20-byte entries) and walks at 0x407800, in table order.
animationNames = (
  "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11",
  "D01", "D02", "D03", "D04", "D05",
  "L01", "L02", "L03", "L04", "L05", "L06", "L07", "L08", "L09",
  "O01", "S01", "S02", "S03", "S04", "S05",
  "P01", "P02", "P03", "P04", "P05", "P06", "P07",
  "T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09",
  "S06", "S07", "S08", "S09", "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19", "S20", "S21", "S22",
  "S23", "S24", "S25", "S26", "S27", "S28",
  "P08", "O02", "O03", "P09", "L11", "L12", "S29", "L10",
)
# The client's own labels for its animations (eqgame.exe strings such as "S03: WAVE"); labels it marks "?" are left out.
animationLabels = {
  "C01": "KICK", "C02": "STAB", "C04": "IMPALE ATK", "C05": "OVRHAND ATK", "C06": "LEFT HND ATK", "C07": "BASH", "C08": "PUNCH",
  "C09": "BOW", "C10": "SWIM ATK", "C11": "MONK RND KICK", "D02": "NORMAL DMG", "D03": "FALL DMG", "D04": "DEATH SHUDDER",
  "D05": "FALL DOWN", "L01": "WALK", "L02": "RUN", "L03": "JUMP ACROSS", "L04": "JUMP", "L05": "FREE FALL", "L06": "CROUCH WALK",
  "L08": "CROUCH", "L09": "TREAD WATER", "O01": "IDLE", "P01": "STAND STILL", "P03": "TURN RIGHT", "P06": "SWIM FORWD",
  "S01": "OH YAH!", "S02": "AGONY", "S03": "WAVE", "S04": "UP YOURS", "T01": "PLAY DRUM", "T02": "PLAY LUTE", "T03": "PLAY HORN",
  "T04": "DEFENSE SPELL", "T05": "GENERAL SPELL", "T06": "MISSILE SPELL", "T07": "FLYING KICK", "T08": "MONK HND ATK 2",
}
variantLetters = "ABCDEFGH"
standAnimation = "P01"
# 0x407800: a Luclin model only takes an unlettered animation of at least this many tracks, unless its code starts GP or is KES.
fewestUnletteredTracks = 50
# 0x407800 skips variant B of the animation in table entry 50 (S08) for the halfling female.
skippedVariants = {("HOF", "S08", "B")}
# An animation's root track: <anim><letter><code><anim><letter>_<code>_TRACK for Luclin models, <anim><code>_TRACK for classic ones.
rootTrackPatterns = (re.compile(r"^([A-Z]\d\d[A-H]?)([A-Z0-9]{3})\1_\2_TRACK$"), re.compile(r"^([A-Z]\d\d)([A-Z0-9]{3})_TRACK$"))


def keepGender(prefix):
  return lambda code, isLuclin: prefix + code[2]


def whole(replacement):
  return lambda code, isLuclin: replacement


# eqgame.exe 0x406a60, in its order; the first prefix that matches decides. isLuclin is whether the model has a <code>TUNIC_POINT_DAG bone.
codeRules = (
  (("ERO", "SIR", "HAG", "ABH"), whole("ELF")),
  (("BRI", "COK"), whole("DWM")),
  (("DR2", "CCD", "BWD", "PRI"), whole("TRK")),
  (("DA", "HI", "HA", "HH", "QC", "NG", "FP", "EG", "BR", "HL", "ZO", "FEM", "FEF", "IV", "GHM", "GHF", "GF", "GE"), keepGender("EL")),
  (("HU", "ER"), lambda code, isLuclin: ("HU" if isLuclin else "EL") + code[2]),
  (("BA",), lambda code, isLuclin: code if isLuclin else "EL" + code[2]),
  (("GNM", "GNF", "CLM", "CLF", "HO", "RIM", "RIF", "KAM", "KAF", "KA", "GD", "COM", "COF"), keepGender("DW")),
  (("TRM", "OG", "TRF", "GRM", "GRF", "OKM", "OKF"), whole("OGF")),
  (("BGM", "TRI", "ENA"), whole("ELM")),
  (("SKE",), lambda code, isLuclin: "HUM" if isLuclin else "ELM"),
  (("MIN", "CPM", "YAK", "FAN"), whole("GNN")),
  (("GHU",), whole("GOB")),
  (("FRG",), whole("FRO")),
  (("LIF", "PUM", "TIG", "STC", "MTC"), whole("LIM")),
  (("CPF",), whole("CPM")),
  (("SNE",), whole("SNA")),
  (("PIF",), whole("FAF")),
  (("SKU", "ARM"), whole("RAT")),
  (("GRI", "SPH", "FMO"), whole("DRK")),
  (("GOL", "GOM"), whole("GIA")),
  (("WO",), whole("WOL")),
  (("KOB",), whole("WER")),
  (("IMP", "GAM"), whole("GAR")),
  (("BET", "SPM"), whole("SPI")),
  (("ICM", "ICN", "ICF", "IKS", "IKF"), whole("IKM")),
  (("BGG",), whole("KGO")),
  (("SSK",), whole("SRW")),
  (("IKH",), whole("REA")),
  (("WUR", "GDR"), whole("DRA")),
  (("BTM",), whole("RHI")),
  (("SDE",), whole("DML")),
  (("TOT",), whole("SCA")),
  (("SPC",), whole("SPE")),
  (("STG", "STM"), whole("FSG")),
  (("VRF",), whole("VRM")),
  (("AVM",), whole("AVI")),
  (("TEM",), whole("TEN")),
  (("WAM", "WAF"), whole("WAL")),
  (("SHN", "SHF"), whole("SHM")),
  (("GMM",), whole("GMN")),
  (("WEL", "FEL"), whole("AEL")),
  (("SDF",), whole("SDM")),
  (("KES", "VSG", "VSK"), whole("KEM")),
  (("NYD",), whole("NYM")),
  (("SPL",), whole("SPD")),
  (("FRF",), whole("FRM")),
)


def borrowedCode(code, isLuclin):
  """The code whose animations a model falls back on when it has none of its own (its own code when no rule applies)."""
  for prefixes, replacement in codeRules:
    if code.startswith(prefixes):
      return replacement(code, isLuclin)
  return code


def animationName(requested):
  """An animation code (S03) or the client's label for it (WAVE)."""
  name = requested.upper()
  if name in animationNames:
    return name
  matches = [code for code, label in animationLabels.items() if label == name]
  if len(matches) != 1:
    raise ValueError(f"'{requested}' is neither an animation the client loads ({', '.join(animationNames)}) nor one of its labels ({', '.join(sorted(set(animationLabels.values())))})")
  return matches[0]


def candidateResources(animation, code, isLuclin):
  """The animation resources the client looks for, in its order (eqgame.exe 0x407800): for a Luclin model each lettered variant under its own code, then under the borrowed code; then the unlettered animation the same way."""
  borrowed = borrowedCode(code, isLuclin)
  lettered = [(letter, [animation + letter + code, animation + letter + borrowed]) for letter in variantLetters if (code, animation, letter) not in skippedVariants] if isLuclin else []
  return lettered, [animation + code, animation + borrowed]
