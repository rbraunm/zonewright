import io
import struct
import zlib

pfsMagic = b"PFS "
directoryCRC = 0x61580AC9
maximumBlockBytes = 65536
keyPolynomial = 0x04C11DB7


def keyTable():
  table = []
  for index in range(256):
    value = index << 24
    for _ in range(8):
      value = ((value << 1) ^ keyPolynomial) if value & 0x80000000 else value << 1
    table.append(value & 0xFFFFFFFF)
  return table


filenameKeyTable = keyTable()


def filenameKey(name):
  """The key an archive files an entry under and the client finds a name by (EQGraphicsDX9.dll 0x100dae40, table 0x101750b0): a CRC-32
  with polynomial 0x04C11DB7, unreflected, from 0, over the name and its terminating zero."""
  value = 0
  for byte in name.encode("latin1") + b"\0":
    value = ((value << 8) ^ filenameKeyTable[((value >> 24) ^ byte) & 0xFF]) & 0xFFFFFFFF
  return value


class EQArchive:
  """Read-only view of a PFS archive (.s3d, .eqg); entry names are lowercased. Reads seek into the file on demand, or into
  archiveBytes, the archive's content, when given (archivePath then only names it). The client files entries by key only
  (EQGraphicsDX9.dll 0x100dd090) and reads a name through its key, so a name the directory lists without an entry under its key
  (greatdivide_chr.s3d lists growthplane_chr.wld, velketor_chr.s3d kael_chr.wld) is not in entries, as the client cannot read it
  either."""

  def __init__(self, archivePath, archiveBytes=None):
    self.archivePath = archivePath
    self.archiveBytes = archiveBytes
    with self.openArchive() as archiveFile:
      header = archiveFile.read(12)
      if len(header) < 12 or header[4:8] != pfsMagic:
        raise ValueError(f"{archivePath}: not a PFS archive")
      directoryOffset = struct.unpack_from("<I", header, 0)[0]
      archiveFile.seek(directoryOffset)
      entryCount = struct.unpack("<I", archiveFile.read(4))[0]
      entryTable = archiveFile.read(entryCount * 12)
      if len(entryTable) != entryCount * 12:
        raise ValueError(f"{archivePath}: entry table runs past the end of the archive")
      entries = [struct.unpack_from("<III", entryTable, 12 * index) for index in range(entryCount)]
      directoryEntries = [entry for entry in entries if entry[0] == directoryCRC]
      if len(directoryEntries) != 1:
        raise ValueError(f"{archivePath}: expected one filename directory, found {len(directoryEntries)}")
      names = self.readFilenames(self.inflate(archiveFile, directoryEntries[0][1], directoryEntries[0][2]))
    keyed = {key: (offset, size) for key, offset, size in entries if key != directoryCRC}
    nameKeys = {name: filenameKey(name) for name in names}
    self.entries = {name.lower(): keyed[key] for name, key in nameKeys.items() if key in keyed}

  def openArchive(self):
    return self.archivePath.open("rb") if self.archiveBytes is None else io.BytesIO(self.archiveBytes)

  def inflate(self, archiveFile, offset, size):
    archiveFile.seek(offset)
    inflated = bytearray()
    while len(inflated) < size:
      blockHeader = archiveFile.read(8)
      if len(blockHeader) != 8:
        raise ValueError(f"{self.archivePath}: truncated block header at {archiveFile.tell()}")
      compressedSize, rawSize = struct.unpack("<II", blockHeader)
      if compressedSize == 0 or rawSize > maximumBlockBytes:
        raise ValueError(f"{self.archivePath}: invalid block at {archiveFile.tell() - 8}")
      compressed = archiveFile.read(compressedSize)
      if len(compressed) != compressedSize:
        raise ValueError(f"{self.archivePath}: truncated block at {archiveFile.tell() - len(compressed)}")
      block = zlib.decompress(compressed)
      if len(block) != rawSize:
        raise ValueError(f"{self.archivePath}: block inflated to {len(block)} bytes, expected {rawSize}")
      inflated.extend(block)
    if len(inflated) != size:
      raise ValueError(f"{self.archivePath}: entry inflated to {len(inflated)} bytes, expected {size}")
    return bytes(inflated)

  def readFilenames(self, directoryBytes):
    nameCount = struct.unpack_from("<I", directoryBytes, 0)[0]
    position = 4
    names = []
    for _ in range(nameCount):
      nameLength = struct.unpack_from("<I", directoryBytes, position)[0]
      position += 4
      names.append(directoryBytes[position:position + nameLength].rstrip(b"\0").decode("latin1"))
      position += nameLength
    return names

  def names(self):
    return sorted(self.entries)

  def read(self, name):
    if name.lower() not in self.entries:
      raise KeyError(f"{self.archivePath}: no entry '{name}'")
    offset, size = self.entries[name.lower()]
    with self.openArchive() as archiveFile:
      return self.inflate(archiveFile, offset, size)
