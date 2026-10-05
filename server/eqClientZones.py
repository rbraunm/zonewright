"""The zones the RoF2 client registers, read in place from eqgame.exe: its world data's constructor (0x7DCA10-0x7E3B53) fills a table of
1000 slots by fixed calls, the zone entry's constructor inlined (0x7DC290), AddZone (0x7DC430), and AddZone with player counts
(0x7DC4B0), each a run of pushes ending in mov ecx and the call. The client loads a zone's files by the id the server sends, through
this table, so it cannot load a zone it never registered (docs/serverFiles.md, Zone row). Read with byte patterns, no disassembler,
and kept under the tooling root by the exe's SHA-256."""
import hashlib
import json
import re
import struct

# The only build these addresses are read from, whose __DATE__ and __TIME__ strings stand side by side.
buildName = ("May 10 2013", "23:30:08")
buildString = b"\x00".join(part.encode("ascii") for part in buildName) + b"\x00"
registrationCode = (0x7DCA10, 0x7E3B54)
# Each registrar's argument count; the first five are expansion, id, short name, long name, and eqstr id, the flags fifth from the end.
registrars = {0x7DC290: 9, 0x7DC430: 9, 0x7DC4B0: 11}
# AddZone takes ids up to 1000 into a slot already empty, and the client's own run-time zone takes 997 (0x5401BA).
highestZoneID = 999
runtimeZoneID = 997
# The zone properties checked against these registrations (setZoneProperties).
checkedZoneProperties = ("shortName", "zoneId", "serverTemplate")
push = rb"(?:\x6a[\x00-\xff]|\x68[\x00-\xff]{4})"
pushValue = re.compile(rb"\x6a([\x00-\xff])|\x68([\x00-\xff]{4})")


class PEImage:
  """An x86 executable's sections, to read its code and the strings its code points at by virtual address."""

  def __init__(self, data):
    if data[:2] != b"MZ":
      raise ValueError("not a Windows executable (no MZ header)")
    header = struct.unpack_from("<I", data, 0x3C)[0]
    if data[header:header + 4] != b"PE\x00\x00":
      raise ValueError("not a PE executable")
    sectionCount, = struct.unpack_from("<H", data, header + 6)
    optionalSize, = struct.unpack_from("<H", data, header + 20)
    self.imageBase, = struct.unpack_from("<I", data, header + 24 + 28)
    self.data = data
    self.sections = []
    table = header + 24 + optionalSize
    for index in range(sectionCount):
      _, virtualSize, virtualAddress, rawSize, rawPointer = struct.unpack_from("<8sIIII", data, table + 40 * index)
      self.sections.append((self.imageBase + virtualAddress, min(virtualSize, rawSize), rawPointer))

  def offset(self, address):
    for start, size, rawPointer in self.sections:
      if start <= address < start + size:
        return rawPointer + address - start
    raise ValueError(f"address 0x{address:X} lies in no section of the file")

  def string(self, address):
    start = self.offset(address)
    end = self.data.index(b"\x00", start)
    return self.data[start:end].decode("ascii")


def signedValue(match):
  small, large = match.groups()
  return struct.unpack("<b", small)[0] if small is not None else struct.unpack("<i", large)[0]


def readRegistrations(data):
  """Every zone the exe registers, in the order the client registers them, the first of an id holding its slot (AddZone and the inlined
  constructor skip a slot already filled): {id, shortName, longName, expansion, flags, eqstrID}. Refuses another build, and any call to
  a registrar it cannot read."""
  if buildString not in data:
    raise ValueError(f"eqgame.exe is not the RoF2 build of {' '.join(buildName)}, whose zone registrations this reads")
  image = PEImage(data)
  start, end = image.offset(registrationCode[0]), image.offset(registrationCode[1] - 1) + 1
  code = data[start:end]
  calls = set()
  for match in re.finditer(rb"\xe8([\x00-\xff]{4})", code):
    target = registrationCode[0] + match.end() + struct.unpack("<i", match.group(1))[0]
    if target in registrars:
      calls.add(match.start())
  registrations, read = [], set()
  for registrar, count in registrars.items():
    for match in re.finditer(rb"(" + push + b"{" + str(count).encode() + rb"})\x8b[\xc8\xce]\xe8([\x00-\xff]{4})", code):
      callAt = match.end() - 5
      if registrationCode[0] + match.end() + struct.unpack("<i", match.group(2))[0] != registrar:
        continue
      values = [signedValue(value) for value in pushValue.finditer(match.group(1))][::-1]
      flags = values[count - 4]
      registrations.append((callAt, {
        "id": values[1], "shortName": image.string(values[2] & 0xFFFFFFFF), "longName": image.string(values[3] & 0xFFFFFFFF),
        "expansion": values[0], "flags": flags & 0xFFFFFFFF, "eqstrID": values[4],
      }))
      read.add(callAt)
  unread = sorted(calls - read)
  if unread:
    raise ValueError(f"eqgame.exe calls a zone registrar at {[hex(registrationCode[0] + offset) for offset in unread]} with arguments this cannot read")
  zones, taken = [], set()
  for _, zone in sorted(registrations, key=lambda found: found[0]):
    if not 1 <= zone["id"] <= highestZoneID + 1:
      raise ValueError(f"eqgame.exe registers '{zone['shortName']}' under id {zone['id']}, outside AddZone's 1 to {highestZoneID + 1}")
    if zone["id"] not in taken:
      taken.add(zone["id"])
      zones.append(zone)
  return zones


class ClientZones:
  """The zones the client registers, by short name and by id."""

  def __init__(self, zones):
    self.zones = zones
    self.byID = {zone["id"]: zone for zone in zones}
    self.byShortName = {}
    for zone in zones:
      if zone["shortName"] in self.byShortName:
        raise ValueError(f"The client registers '{zone['shortName']}' under ids {self.byShortName[zone['shortName']]['id']} and {zone['id']}")
      self.byShortName[zone["shortName"]] = zone

  def idsByShortName(self):
    return {name: zone["id"] for name, zone in self.byShortName.items()}


def loadClientZones(clientRoot, toolingRoot):
  """The zones <clientRoot>\\eqgame.exe registers, read once per exe into cache\\clientZones-<its SHA-256, 16 hex>.json under the
  tooling root."""
  data = (clientRoot / "eqgame.exe").read_bytes()
  digest = hashlib.sha256(data).hexdigest()
  cachePath = toolingRoot / "cache" / f"clientZones-{digest[:16]}.json"
  if cachePath.is_file():
    cached = json.loads(cachePath.read_text(encoding="ascii"))
    if cached["exeSHA256"] != digest:
      raise ValueError(f"{cachePath} holds the zones of another eqgame.exe ({cached['exeSHA256']}); remove it")
    return ClientZones(cached["zones"])
  zones = readRegistrations(data)
  cachePath.parent.mkdir(parents=True, exist_ok=True)
  partial = cachePath.with_name(cachePath.name + ".partial")
  partial.write_text(json.dumps({"exeSHA256": digest, "zones": zones}, indent=1), encoding="ascii")
  partial.replace(cachePath)
  return ClientZones(zones)
