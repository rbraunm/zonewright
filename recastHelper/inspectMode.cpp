#include <algorithm>
#include <cmath>
#include <cstring>
#include <numeric>
#include <vector>

#include "DetourAlloc.h"
#include "DetourCommon.h"
#include "DetourNavMesh.h"
#include "DetourNavMeshQuery.h"
#include "helperIO.h"

namespace {

const char* inspectMagic = "ZWINSP01";
const int notInComponent = -1;

struct InspectInput {
  std::vector<unsigned char> payload;
  unsigned int searchNodes;
  unsigned int pathPolygons;
  float nearestHalfExtents[3];
  float pathHalfExtents[3];
  float snapHeight;
  unsigned int includeFlags;
  unsigned int excludeFlags;
  float areaCosts[DT_MAX_AREAS];
  bool hasSafePoint;
  float safePoint[3];
  std::vector<float> targets;
};

InspectInput readInspectInput(const std::string& path) {
  InputReader reader(path, inspectMagic);
  InspectInput input;
  input.payload.resize(reader.readUInt32());
  reader.readBytes(input.payload.data(), input.payload.size());
  input.searchNodes = reader.readUInt32();
  input.pathPolygons = reader.readUInt32();
  reader.readFloats(input.nearestHalfExtents, 3);
  reader.readFloats(input.pathHalfExtents, 3);
  input.snapHeight = reader.readFloat();
  input.includeFlags = reader.readUInt32();
  input.excludeFlags = reader.readUInt32();
  reader.readFloats(input.areaCosts, DT_MAX_AREAS);
  input.hasSafePoint = reader.readUInt32() != 0;
  reader.readFloats(input.safePoint, 3);
  input.targets.resize(static_cast<size_t>(reader.readUInt32()) * 3);
  reader.readFloats(input.targets.data(), input.targets.size());
  reader.requireEnd();
  if (input.searchNodes == 0 || input.pathPolygons == 0) {
    throw HelperError("the inspect input asks for 0 search nodes or 0 path polygons");
  }
  return input;
}

class PayloadReader {
public:
  explicit PayloadReader(const std::vector<unsigned char>& bytes) : bytes(bytes), offset(0) {}

  void read(void* destination, size_t count, const std::string& what) {
    if (bytes.size() - offset < count) {
      throw HelperError("the nav payload ends at byte " + std::to_string(bytes.size()) + " inside " + what + " at offset " + std::to_string(offset));
    }
    std::memcpy(destination, bytes.data() + offset, count);
    offset += count;
  }

  const std::vector<unsigned char>& bytes;
  size_t offset;
};

struct LoadedMesh {
  dtNavMesh* mesh = nullptr;
  std::vector<const dtMeshTile*> tiles;
  std::vector<int> payloadIndexOfSlot;
  std::vector<int> firstPolygon;
  int polygonCount = 0;

  ~LoadedMesh() {
    dtFreeNavMesh(mesh);
  }
};

void loadAsTheServerDoes(const std::vector<unsigned char>& payload, LoadedMesh& loaded) {
  PayloadReader reader(payload);
  uint32_t tileCount;
  dtNavMeshParams parameters;
  reader.read(&tileCount, 4, "the tile count");
  reader.read(&parameters, sizeof(parameters), "the mesh parameters");
  loaded.mesh = dtAllocNavMesh();
  if (dtStatusFailed(loaded.mesh->init(&parameters))) {
    throw HelperError("dtNavMesh::init refuses the payload's parameters (" + std::to_string(parameters.maxTiles) + " tiles of " +
      std::to_string(parameters.maxPolys) + " polygons)");
  }
  loaded.payloadIndexOfSlot.assign(static_cast<size_t>(parameters.maxTiles), -1);
  for (uint32_t index = 0; index < tileCount; ++index) {
    uint32_t reference;
    int32_t size;
    const std::string name = "payload tile " + std::to_string(index);
    reader.read(&reference, 4, name + "'s reference");
    reader.read(&size, 4, name + "'s size");
    if (reference == 0 || size <= 0) {
      throw HelperError(name + " has reference " + std::to_string(reference) + " and size " + std::to_string(size) +
        "; the server drops the whole mesh on a zero");
    }
    if (static_cast<size_t>(size) < sizeof(dtMeshHeader)) {
      throw HelperError(name + " is " + std::to_string(size) + " bytes, too short for a Detour tile header");
    }
    unsigned char* data = static_cast<unsigned char*>(dtAlloc(static_cast<size_t>(size), DT_ALLOC_PERM));
    reader.read(data, static_cast<size_t>(size), name + "'s data");
    const dtMeshHeader* header = reinterpret_cast<const dtMeshHeader*>(data);
    if (header->polyCount > parameters.maxPolys) {
      const std::string message = name + " (" + std::to_string(header->x) + ", " + std::to_string(header->y) + ") has " +
        std::to_string(header->polyCount) + " polygons, more than the " + std::to_string(parameters.maxPolys) + " its polygon bits address";
      dtFree(data);
      throw HelperError(message);
    }
    dtTileRef added = 0;
    const dtStatus status = loaded.mesh->addTile(data, size, DT_TILE_FREE_DATA, reference, &added);
    if (dtStatusFailed(status)) {
      dtFree(data);
      throw HelperError(name + " with reference " + std::to_string(reference) + " fails dtNavMesh::addTile (status " + std::to_string(status) +
        "); the server would lose it without a word");
    }
    const dtMeshTile* tile = loaded.mesh->getTileByRef(added);
    loaded.payloadIndexOfSlot[loaded.mesh->decodePolyIdTile(added)] = static_cast<int>(index);
    loaded.tiles.push_back(tile);
    loaded.firstPolygon.push_back(loaded.polygonCount);
    loaded.polygonCount += tile->header->polyCount;
  }
  if (reader.offset != payload.size()) {
    throw HelperError("the nav payload holds " + std::to_string(payload.size() - reader.offset) + " bytes after its last tile");
  }
}

int globalIndex(const LoadedMesh& loaded, dtPolyRef reference) {
  const int payloadIndex = loaded.payloadIndexOfSlot[loaded.mesh->decodePolyIdTile(reference)];
  return loaded.firstPolygon[static_cast<size_t>(payloadIndex)] + static_cast<int>(loaded.mesh->decodePolyIdPoly(reference));
}

int findRoot(std::vector<int>& parents, int item) {
  while (parents[static_cast<size_t>(item)] != item) {
    parents[static_cast<size_t>(item)] = parents[static_cast<size_t>(parents[static_cast<size_t>(item)])];
    item = parents[static_cast<size_t>(item)];
  }
  return item;
}

float planArea(const dtMeshTile* tile, const dtPoly& polygon) {
  float doubled = 0;
  for (int corner = 0; corner < polygon.vertCount; ++corner) {
    const float* first = &tile->verts[polygon.verts[corner] * 3];
    const float* second = &tile->verts[polygon.verts[(corner + 1) % polygon.vertCount] * 3];
    doubled += first[0] * second[2] - second[0] * first[2];
  }
  return std::fabs(doubled) * 0.5f;
}

void polygonBounds(const dtMeshTile* tile, const dtPoly& polygon, float* low, float* high) {
  dtVcopy(low, &tile->verts[polygon.verts[0] * 3]);
  dtVcopy(high, low);
  for (int corner = 1; corner < polygon.vertCount; ++corner) {
    dtVmin(low, &tile->verts[polygon.verts[corner] * 3]);
    dtVmax(high, &tile->verts[polygon.verts[corner] * 3]);
  }
}

struct Component {
  int firstPolygon = 0;
  int polygons = 0;
  float area = 0;
  float low[3] = {0, 0, 0};
  float high[3] = {0, 0, 0};
  float largestArea = -1;
  float center[3] = {0, 0, 0};
  bool snapRisk = false;
};

class MainPieceQuery : public dtPolyQuery {
public:
  MainPieceQuery(const LoadedMesh& loaded, const std::vector<int>& componentOf) : loaded(loaded), componentOf(componentOf) {}

  void process(const dtMeshTile*, dtPoly**, dtPolyRef* references, int count) override {
    for (int index = 0; index < count; ++index) {
      if (componentOf[static_cast<size_t>(globalIndex(loaded, references[index]))] == 0) {
        touchesMainPiece = true;
      }
    }
  }

  bool touchesMainPiece = false;

private:
  const LoadedMesh& loaded;
  const std::vector<int>& componentOf;
};

std::string probeJSON(const char* result, int pathPolygons, dtStatus status, int goalComponent) {
  return std::string("{\"result\": \"") + result + "\", \"pathPolygons\": " + std::to_string(pathPolygons) +
    ", \"outOfNodes\": " + ((status & DT_OUT_OF_NODES) ? "true" : "false") +
    ", \"pathBufferFull\": " + ((status & DT_BUFFER_TOO_SMALL) ? "true" : "false") +
    ", \"goalComponent\": " + std::to_string(goalComponent) + "}";
}

}

void runInspect(const std::string& inputPath, const std::string& reportPath) {
  const InspectInput input = readInspectInput(inputPath);
  reportProgress("loading the nav as the server does", 0, 1);
  LoadedMesh loaded;
  loadAsTheServerDoes(input.payload, loaded);

  dtQueryFilter filter;
  filter.setIncludeFlags(static_cast<unsigned short>(input.includeFlags));
  filter.setExcludeFlags(static_cast<unsigned short>(input.excludeFlags));
  for (int area = 0; area < DT_MAX_AREAS; ++area) {
    filter.setAreaCost(area, input.areaCosts[area]);
  }

  reportProgress("labelling nav components", 0, 1);
  std::vector<int> parents(static_cast<size_t>(loaded.polygonCount));
  std::iota(parents.begin(), parents.end(), 0);
  std::vector<bool> walkable(static_cast<size_t>(loaded.polygonCount), false);
  int excludedPolygons = 0;
  for (size_t tileIndex = 0; tileIndex < loaded.tiles.size(); ++tileIndex) {
    const dtMeshTile* tile = loaded.tiles[tileIndex];
    const dtPolyRef base = loaded.mesh->getPolyRefBase(tile);
    for (int polygon = 0; polygon < tile->header->polyCount; ++polygon) {
      const dtPoly& poly = tile->polys[polygon];
      const int index = loaded.firstPolygon[tileIndex] + polygon;
      if (poly.getType() != DT_POLYTYPE_GROUND || !filter.passFilter(base | static_cast<dtPolyRef>(polygon), tile, &poly)) {
        ++excludedPolygons;
        continue;
      }
      walkable[static_cast<size_t>(index)] = true;
    }
  }
  for (size_t tileIndex = 0; tileIndex < loaded.tiles.size(); ++tileIndex) {
    const dtMeshTile* tile = loaded.tiles[tileIndex];
    for (int polygon = 0; polygon < tile->header->polyCount; ++polygon) {
      const int index = loaded.firstPolygon[tileIndex] + polygon;
      if (!walkable[static_cast<size_t>(index)]) {
        continue;
      }
      for (unsigned int link = tile->polys[polygon].firstLink; link != DT_NULL_LINK; link = tile->links[link].next) {
        const int neighbour = globalIndex(loaded, tile->links[link].ref);
        if (walkable[static_cast<size_t>(neighbour)]) {
          const int first = findRoot(parents, index);
          const int second = findRoot(parents, neighbour);
          if (first != second) {
            parents[static_cast<size_t>(std::max(first, second))] = std::min(first, second);
          }
        }
      }
    }
  }

  std::vector<int> rootComponent(static_cast<size_t>(loaded.polygonCount), notInComponent);
  std::vector<Component> components;
  std::vector<int> componentOf(static_cast<size_t>(loaded.polygonCount), notInComponent);
  for (size_t tileIndex = 0; tileIndex < loaded.tiles.size(); ++tileIndex) {
    const dtMeshTile* tile = loaded.tiles[tileIndex];
    for (int polygon = 0; polygon < tile->header->polyCount; ++polygon) {
      const int index = loaded.firstPolygon[tileIndex] + polygon;
      if (!walkable[static_cast<size_t>(index)]) {
        continue;
      }
      const int root = findRoot(parents, index);
      if (rootComponent[static_cast<size_t>(root)] == notInComponent) {
        rootComponent[static_cast<size_t>(root)] = static_cast<int>(components.size());
        components.emplace_back();
        components.back().firstPolygon = index;
      }
      const int number = rootComponent[static_cast<size_t>(root)];
      componentOf[static_cast<size_t>(index)] = number;
      Component& component = components[static_cast<size_t>(number)];
      const dtPoly& poly = tile->polys[polygon];
      float low[3], high[3];
      polygonBounds(tile, poly, low, high);
      if (component.polygons == 0) {
        dtVcopy(component.low, low);
        dtVcopy(component.high, high);
      } else {
        dtVmin(component.low, low);
        dtVmax(component.high, high);
      }
      ++component.polygons;
      const float area = planArea(tile, poly);
      component.area += area;
      if (area > component.largestArea) {
        component.largestArea = area;
        dtVset(component.center, 0, 0, 0);
        for (int corner = 0; corner < poly.vertCount; ++corner) {
          dtVadd(component.center, component.center, &tile->verts[poly.verts[corner] * 3]);
        }
        dtVscale(component.center, component.center, 1.0f / poly.vertCount);
      }
    }
  }
  if (components.empty()) {
    throw HelperError("the nav holds no polygon the server's ground filter lets an NPC walk");
  }

  dtNavMeshQuery* query = dtAllocNavMeshQuery();
  if (dtStatusFailed(query->init(loaded.mesh, static_cast<int>(input.searchNodes)))) {
    dtFreeNavMeshQuery(query);
    throw HelperError("dtNavMeshQuery::init failed for " + std::to_string(input.searchNodes) + " nodes");
  }
  dtPolyRef safePolygon = 0;
  float safeNearest[3] = {0, 0, 0};
  if (input.hasSafePoint) {
    query->findNearestPoly(input.safePoint, input.nearestHalfExtents, &filter, &safePolygon, safeNearest);
  }
  int mainComponent = 0;
  if (safePolygon) {
    mainComponent = componentOf[static_cast<size_t>(globalIndex(loaded, safePolygon))];
  } else {
    for (size_t number = 1; number < components.size(); ++number) {
      if (components[number].area > components[static_cast<size_t>(mainComponent)].area) {
        mainComponent = static_cast<int>(number);
      }
    }
  }
  std::vector<int> order(components.size());
  std::iota(order.begin(), order.end(), 0);
  std::stable_sort(order.begin(), order.end(), [&](int first, int second) {
    if ((first == mainComponent) != (second == mainComponent)) {
      return first == mainComponent;
    }
    return components[static_cast<size_t>(first)].area > components[static_cast<size_t>(second)].area;
  });
  std::vector<int> renumbered(components.size());
  std::vector<Component> sorted;
  for (size_t position = 0; position < order.size(); ++position) {
    renumbered[static_cast<size_t>(order[position])] = static_cast<int>(position);
    sorted.push_back(components[static_cast<size_t>(order[position])]);
  }
  components.swap(sorted);
  for (int& number : componentOf) {
    if (number != notInComponent) {
      number = renumbered[static_cast<size_t>(number)];
    }
  }

  reportProgress("checking islands for snap risk", 0, 1);
  for (size_t tileIndex = 0; tileIndex < loaded.tiles.size(); ++tileIndex) {
    const dtMeshTile* tile = loaded.tiles[tileIndex];
    for (int polygon = 0; polygon < tile->header->polyCount; ++polygon) {
      const int number = componentOf[static_cast<size_t>(loaded.firstPolygon[tileIndex] + polygon)];
      if (number <= 0 || components[static_cast<size_t>(number)].snapRisk) {
        continue;
      }
      float low[3], high[3], center[3], halfExtents[3];
      polygonBounds(tile, tile->polys[polygon], low, high);
      dtVlerp(center, low, high, 0.5f);
      dtVsub(halfExtents, high, low);
      dtVscale(halfExtents, halfExtents, 0.5f);
      halfExtents[1] += input.snapHeight;
      MainPieceQuery nearMain(loaded, componentOf);
      query->queryPolygons(center, halfExtents, &filter, &nearMain);
      components[static_cast<size_t>(number)].snapRisk = nearMain.touchesMainPiece;
    }
  }

  std::vector<std::string> probes;
  const size_t targetCount = input.targets.size() / 3;
  if (input.hasSafePoint) {
    dtPolyRef start = 0;
    float startNearest[3];
    query->findNearestPoly(input.safePoint, input.pathHalfExtents, &filter, &start, startNearest);
    std::vector<dtPolyRef> path(input.pathPolygons);
    for (size_t target = 0; target < targetCount; ++target) {
      reportProgress("probing NPC paths", static_cast<long long>(target), static_cast<long long>(targetCount));
      const float* goalPoint = &input.targets[target * 3];
      dtPolyRef goal = 0;
      float goalNearest[3];
      query->findNearestPoly(goalPoint, input.pathHalfExtents, &filter, &goal, goalNearest);
      const int goalComponent = goal ? componentOf[static_cast<size_t>(globalIndex(loaded, goal))] : notInComponent;
      if (!start) {
        probes.push_back(probeJSON("noStartPolygon", 0, 0, goalComponent));
        continue;
      }
      if (!goal) {
        probes.push_back(probeJSON("noGoalPolygon", 0, 0, goalComponent));
        continue;
      }
      int pathCount = 0;
      const dtStatus status = query->findPath(start, goal, input.safePoint, goalPoint, &filter, path.data(), &pathCount, static_cast<int>(path.size()));
      const char* result = dtStatusFailed(status) || pathCount == 0 ? "failed" : (path[static_cast<size_t>(pathCount - 1)] == goal ? "complete" : "partial");
      probes.push_back(probeJSON(result, pathCount, status, goalComponent));
    }
  }
  dtFreeNavMeshQuery(query);

  std::ostringstream report;
  report << "{\"tiles\": " << loaded.tiles.size() << ", \"polygons\": " << loaded.polygonCount << ", \"excludedPolygons\": " << excludedPolygons;
  report << ", \"safePolygon\": " << safePolygon << ", \"safeNearest\": " << jsonVector(safeNearest);
  report << ", \"components\": [";
  for (size_t number = 0; number < components.size(); ++number) {
    const Component& component = components[number];
    report << (number ? ", " : "") << "{\"polygons\": " << component.polygons << ", \"area\": " << jsonFloat(component.area)
      << ", \"boundsMin\": " << jsonVector(component.low) << ", \"boundsMax\": " << jsonVector(component.high)
      << ", \"center\": " << jsonVector(component.center) << ", \"snapRisk\": " << (component.snapRisk ? "true" : "false") << "}";
  }
  report << "], \"polygonComponents\": [";
  for (size_t tileIndex = 0; tileIndex < loaded.tiles.size(); ++tileIndex) {
    report << (tileIndex ? ", " : "") << "[";
    for (int polygon = 0; polygon < loaded.tiles[tileIndex]->header->polyCount; ++polygon) {
      report << (polygon ? ", " : "") << componentOf[static_cast<size_t>(loaded.firstPolygon[tileIndex] + polygon)];
    }
    report << "]";
  }
  report << "], \"probes\": [";
  for (size_t index = 0; index < probes.size(); ++index) {
    report << (index ? ", " : "") << probes[index];
  }
  report << "]}";
  writeText(reportPath, report.str());
}
