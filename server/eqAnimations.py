"""How the client gives a character its animations, transcribed from eqgame.exe: the animations it loads per WLD model, the code a WLD model borrows animations from when it has none of its own, and the EQG animation each animation id plays."""
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
# eqgame.exe's animation ids in order (0xaae3e8, 40-byte rows), each with its EQG name and the animationNames code the id plays on a
# WLD model (0xaaf428, 1-based, 0 for none). An EQG model plays <name>_BA_1_<code> (EQGraphicsDX9.dll PlayAnimation, "%s_BA_1_%s").
eqgAnimationTable = (
  ("XXXX", None), ("SLPR", "C05"), ("BLPR", "C05"), ("STPR", "C02"), ("SLSC", "C06"), ("BLSC", "C06"), ("STSC", "C06"),
  ("SL2H", "C03"), ("BL2H", "C03"), ("ST2H", "C04"), ("BSTB", "C02"), ("TOSS", "C05"), ("BASH", "C07"), ("PATK", "C08"),
  ("BATK", "C09"), ("WATK", "C10"), ("KICK", "C01"), ("RKCK", "C11"), ("SHTH", None), ("UNSH", None), ("SHTF", None),
  ("UNSF", None), ("MKCK", "T07"), ("MHA1", "T08"), ("MHA2", "T09"), ("WALK", "L01"), ("BWLK", "L01"), ("NRUN", "L02"),
  ("TURN", "P03"), ("LTRN", "P03"), ("KNEL", "P05"), ("STND", "P01"), ("IDLE", "O01"), ("STDA", "P01"), ("IDLA", "O01"),
  ("STDB", "P01"), ("IDLB", "O01"), ("STDC", "P01"), ("IDLC", "O01"), ("NSIT", "P07"), ("SIDL", "O03"), ("STDG", "P02"),
  ("STNG", "P02"), ("CRCH", "L08"), ("JMPA", "L03"), ("JMPU", "L04"), ("CWLK", "L06"), ("CLMB", "L07"), ("TWTR", "L09"),
  ("SWIM", "P06"), ("STUN", None), ("DRUM", "T01"), ("LUTE", "T02"), ("HORN", "T03"), ("GCST", "T05"), ("DCST", "T04"),
  ("MCST", "T06"), ("GAPO", "T04"), ("OFSM", "T05"), ("OFLG", "T05"), ("HESM", "T04"), ("HELG", "T04"), ("OFAE", "T06"),
  ("OFPB", "T06"), ("FLCH", "D01"), ("MSHT", "D02"), ("SPAS", "D04"), ("CRMP", "D05"), ("DODG", "D01"), ("PRRY", "D01"),
  ("RPST", "D01"), ("FLDM", "D03"), ("FALL", "L05"), ("NBOW", "S28"), ("SLTE", "S25"), ("WAVE", "S03"), ("HNOD", "S06"),
  ("CLAP", "S09"), ("DOVR", "S10"), ("NPNT", "S22"), ("LAGH", "S12"), ("SHRG", "S23"), ("TRIU", "S01"), ("AGNY", "S02"),
  ("NGTV", "S04"), ("BORD", "S05"), ("PRAY", "S08"), ("BLSH", "S23"), ("COGH", "S13"), ("CRNG", "S14"), ("DNCE", "S16"),
  ("HSHK", "S17"), ("STRE", "S19"), ("SHVR", "S26"), ("HLGH", "S21"), ("IMPT", "S27"), ("SKNL", "S20"), ("CATK", "C09"),
  ("HIPS", "S18"), ("RAIS", "S24"), ("SMLE", "S29"), ("TILT", "S15"), ("KBEG", "S20"), ("SATK", "C06"),
)
eqgAnimationCodes = dict(eqgAnimationTable)
eqgAnimationPart = "BA_1"
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
  """An animation code (S03), the client's label for it (WAVE), or the EQG name of an animation id (STND), which plays that id's code."""
  name = requested.upper()
  if name in animationNames:
    return name
  matches = [code for code, label in animationLabels.items() if label == name]
  if len(matches) == 1:
    return matches[0]
  if name in eqgAnimationCodes:
    if eqgAnimationCodes[name] is None:
      raise ValueError(f"EQG animation {name} plays no animation on a WLD model")
    return eqgAnimationCodes[name]
  raise ValueError(
    f"'{requested}' is neither an animation the client loads ({', '.join(animationNames)}), one of its labels"
    f" ({', '.join(sorted(set(animationLabels.values())))}), nor an EQG animation ({', '.join(eqgAnimationCodes)})"
  )


def eqgAnimationName(requested):
  """The EQG animation a request plays on an EQG model: an EQG name (STND, WAVE) as given, or a code or label, which plays the first
  animation id the client maps to that code (P01, the default stand, plays STND)."""
  name = (requested or standAnimation).upper()
  if name in eqgAnimationCodes:
    return name
  code = animationName(name)
  eqgName = next((eqgName for eqgName, wldCode in eqgAnimationTable if wldCode == code), None)
  if eqgName is None:
    raise ValueError(f"No EQG animation plays {code}")
  return eqgName


def candidateResources(animation, code, isLuclin):
  """The animation resources the client looks for, in its order (eqgame.exe 0x407800): for a Luclin model each lettered variant under its own code, then under the borrowed code; then the unlettered animation the same way."""
  borrowed = borrowedCode(code, isLuclin)
  lettered = [(letter, [animation + letter + code, animation + letter + borrowed]) for letter in variantLetters if (code, animation, letter) not in skippedVariants] if isLuclin else []
  return lettered, [animation + code, animation + borrowed]
