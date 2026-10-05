#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstring>
#include <map>
#include <mutex>
#include <thread>
#include <vector>

#include "ChunkyTriMesh.h"
#include "DetourCommon.h"
#include "DetourNavMesh.h"
#include "DetourNavMeshBuilder.h"
#include "Recast.h"
#include "helperIO.h"

namespace {

const char* navMagic = "ZWNAV001";
const int maximumTileBits = 14;
const int referenceBits = 22;
const int trianglesPerChunk = 512;
const float lowestVertexHeight = -15000.0f;

struct NavSettings {
  float cellSize, cellHeight, agentHeight, agentRadius, agentClimb, maxSlope;
  float regionMinSize, regionMergeSize, edgeMaxLength, edgeMaxError, detailSampleDistance, detailSampleMaxError;
  int verticesPerPolygon, tileSize, borderSize;
};

struct Volume {
  float corners[12];
  float low, high;
  unsigned char area;
};

struct TileResult {
  unsigned char* data = nullptr;
  int size = 0;
  int chunks = 0;
  std::string error;
};

class TileContext : public rcContext {
public:
  TileContext() : rcContext(true) {
    enableTimer(false);
  }

  std::string errors;

protected:
  void doLog(const rcLogCategory category, const char* message, const int length) override {
    if (category == RC_LOG_ERROR) {
      errors += (errors.empty() ? "" : "; ") + std::string(message, length);
    }
  }
};

struct NavInput {
  NavSettings settings;
  float boundsMin[3];
  float boundsMax[3];
  unsigned int threads;
  std::vector<float> vertices;
  std::vector<int> triangles;
  std::vector<Volume> volumes;
};

NavInput readNavInput(const std::string& path) {
  InputReader reader(path, navMagic);
  NavInput input;
  NavSettings& settings = input.settings;
  float* floats[] = {&settings.cellSize, &settings.cellHeight, &settings.agentHeight, &settings.agentRadius, &settings.agentClimb,
    &settings.maxSlope, &settings.regionMinSize, &settings.regionMergeSize, &settings.edgeMaxLength, &settings.edgeMaxError,
    &settings.detailSampleDistance, &settings.detailSampleMaxError};
  for (float* value : floats) {
    *value = reader.readFloat();
  }
  settings.verticesPerPolygon = reader.readInt32();
  settings.tileSize = reader.readInt32();
  settings.borderSize = reader.readInt32();
  reader.readFloats(input.boundsMin, 3);
  reader.readFloats(input.boundsMax, 3);
  input.threads = reader.readUInt32();
  if (input.threads == 0) {
    throw HelperError("the nav input asks for 0 threads");
  }
  const uint32_t triangleCount = reader.readUInt32();
  if (triangleCount == 0) {
    throw HelperError("the nav input holds no collidable triangles");
  }
  input.vertices.resize(static_cast<size_t>(triangleCount) * 9);
  reader.readFloats(input.vertices.data(), input.vertices.size());
  input.triangles.resize(static_cast<size_t>(triangleCount) * 3);
  for (size_t index = 0; index < input.triangles.size(); ++index) {
    input.triangles[index] = static_cast<int>(index);
  }
  const uint32_t volumeCount = reader.readUInt32();
  input.volumes.resize(volumeCount);
  for (uint32_t index = 0; index < volumeCount; ++index) {
    Volume& volume = input.volumes[index];
    reader.readFloats(volume.corners, 12);
    volume.low = reader.readFloat();
    volume.high = reader.readFloat();
    const uint32_t area = reader.readUInt32();
    if (area >= RC_WALKABLE_AREA) {
      throw HelperError("volume " + std::to_string(index) + " has area " + std::to_string(area) + ", past Recast's area ids");
    }
    volume.area = static_cast<unsigned char>(area);
  }
  reader.requireEnd();
  return input;
}

void refuseUnusableVertices(const NavInput& input) {
  const std::vector<float>& vertices = input.vertices;
  for (size_t corner = 0; corner < vertices.size() / 3; ++corner) {
    const float* vertex = &vertices[corner * 3];
    const std::string triangle = "collidable triangle " + std::to_string(corner / 3);
    if (!std::isfinite(vertex[0]) || !std::isfinite(vertex[1]) || !std::isfinite(vertex[2])) {
      throw HelperError(triangle + " has a vertex that is not a finite number");
    }
    if (vertex[1] <= lowestVertexHeight) {
      throw HelperError(triangle + " has a vertex at " + zoneAxesText(vertex) +
        ", at or below z -15000; map_edit leaves such vertices out of the nav bounds and drops their triangles");
    }
    for (int axis = 0; axis < 3; ++axis) {
      if (vertex[axis] < input.boundsMin[axis] || vertex[axis] > input.boundsMax[axis]) {
        throw HelperError(triangle + " has a vertex at " + zoneAxesText(vertex) + ", outside the nav bounds " + zoneAxesText(input.boundsMin) +
          " to " + zoneAxesText(input.boundsMax) + "; map_edit would drop the triangle");
      }
    }
  }
}

std::string tileName(int tileX, int tileY, const float* tileMin, const float* tileMax) {
  char text[160];
  std::snprintf(text, sizeof(text), "tile (%d, %d), zone x %.1f to %.1f, y %.1f to %.1f", tileX, tileY, tileMin[2], tileMax[2], tileMin[0], tileMax[0]);
  return text;
}

TileResult buildTile(const NavInput& input, const rcChunkyTriMesh& chunks, std::vector<int>& chunkIDs, int polygonBits, int tileX, int tileY,
    const float* tileMin, const float* tileMax) {
  const NavSettings& settings = input.settings;
  TileResult result;
  rcConfig config;
  std::memset(&config, 0, sizeof(config));
  config.cs = settings.cellSize;
  config.ch = settings.cellHeight;
  config.walkableSlopeAngle = settings.maxSlope;
  config.walkableHeight = static_cast<int>(std::ceil(settings.agentHeight / config.ch));
  config.walkableClimb = static_cast<int>(std::floor(settings.agentClimb / config.ch));
  config.walkableRadius = static_cast<int>(std::ceil(settings.agentRadius / config.cs));
  config.maxEdgeLen = static_cast<int>(settings.edgeMaxLength / settings.cellSize);
  config.maxSimplificationError = settings.edgeMaxError;
  config.minRegionArea = static_cast<int>(rcSqr(settings.regionMinSize));
  config.mergeRegionArea = static_cast<int>(rcSqr(settings.regionMergeSize));
  config.maxVertsPerPoly = settings.verticesPerPolygon;
  config.tileSize = settings.tileSize;
  config.borderSize = settings.borderSize;
  config.width = config.tileSize + config.borderSize * 2;
  config.height = config.tileSize + config.borderSize * 2;
  config.detailSampleDist = settings.detailSampleDistance < 0.9f ? 0 : settings.cellSize * settings.detailSampleDistance;
  config.detailSampleMaxError = settings.cellHeight * settings.detailSampleMaxError;
  rcVcopy(config.bmin, tileMin);
  rcVcopy(config.bmax, tileMax);
  config.bmin[0] -= config.borderSize * config.cs;
  config.bmin[2] -= config.borderSize * config.cs;
  config.bmax[0] += config.borderSize * config.cs;
  config.bmax[2] += config.borderSize * config.cs;

  float rectangleMin[2] = {config.bmin[0], config.bmin[2]};
  float rectangleMax[2] = {config.bmax[0], config.bmax[2]};
  result.chunks = rcGetChunksOverlappingRect(&chunks, rectangleMin, rectangleMax, chunkIDs.data(), static_cast<int>(chunkIDs.size()));
  if (result.chunks == 0) {
    return result;
  }

  TileContext context;
  const std::string name = tileName(tileX, tileY, tileMin, tileMax);
  auto fail = [&](const char* step) {
    result.error = name + ": " + step + " failed" + (context.errors.empty() ? "" : " (" + context.errors + ")");
    return result;
  };

  rcHeightfield* solid = rcAllocHeightfield();
  rcCompactHeightfield* compact = nullptr;
  rcContourSet* contours = nullptr;
  rcPolyMesh* polygonMesh = nullptr;
  rcPolyMeshDetail* detailMesh = nullptr;
  auto release = [&]() {
    rcFreeHeightField(solid);
    rcFreeCompactHeightfield(compact);
    rcFreeContourSet(contours);
    rcFreePolyMesh(polygonMesh);
    rcFreePolyMeshDetail(detailMesh);
  };

  if (!rcCreateHeightfield(&context, *solid, config.width, config.height, config.bmin, config.bmax, config.cs, config.ch)) {
    release();
    return fail("rcCreateHeightfield");
  }
  std::vector<unsigned char> triangleAreas(chunks.maxTrisPerChunk);
  const int vertexCount = static_cast<int>(input.vertices.size() / 3);
  for (int chunk = 0; chunk < result.chunks; ++chunk) {
    const rcChunkyTriMeshNode& node = chunks.nodes[chunkIDs[chunk]];
    const int* chunkTriangles = &chunks.tris[node.i * 3];
    std::memset(triangleAreas.data(), 0, node.n);
    rcMarkWalkableTriangles(&context, config.walkableSlopeAngle, input.vertices.data(), vertexCount, chunkTriangles, node.n, triangleAreas.data());
    if (!rcRasterizeTriangles(&context, input.vertices.data(), vertexCount, chunkTriangles, triangleAreas.data(), node.n, *solid, config.walkableClimb)) {
      release();
      return fail("rcRasterizeTriangles");
    }
  }
  rcFilterLowHangingWalkableObstacles(&context, config.walkableClimb, *solid);
  rcFilterLedgeSpans(&context, config.walkableHeight, config.walkableClimb, *solid);
  rcFilterWalkableLowHeightSpans(&context, config.walkableHeight, *solid);

  compact = rcAllocCompactHeightfield();
  if (!rcBuildCompactHeightfield(&context, config.walkableHeight, config.walkableClimb, *solid, *compact)) {
    release();
    return fail("rcBuildCompactHeightfield");
  }
  rcFreeHeightField(solid);
  solid = nullptr;
  if (!rcErodeWalkableArea(&context, config.walkableRadius, *compact)) {
    release();
    return fail("rcErodeWalkableArea");
  }
  for (const Volume& volume : input.volumes) {
    rcMarkConvexPolyArea(&context, volume.corners, 4, volume.low, volume.high, volume.area, *compact);
  }
  if (!rcBuildDistanceField(&context, *compact)) {
    release();
    return fail("rcBuildDistanceField");
  }
  if (!rcBuildRegions(&context, *compact, config.borderSize, config.minRegionArea, config.mergeRegionArea)) {
    release();
    return fail("rcBuildRegions");
  }
  contours = rcAllocContourSet();
  if (!rcBuildContours(&context, *compact, config.maxSimplificationError, config.maxEdgeLen, *contours)) {
    release();
    return fail("rcBuildContours");
  }
  if (contours->nconts == 0) {
    release();
    return result;
  }
  polygonMesh = rcAllocPolyMesh();
  if (!rcBuildPolyMesh(&context, *contours, config.maxVertsPerPoly, *polygonMesh)) {
    release();
    return fail("rcBuildPolyMesh");
  }
  detailMesh = rcAllocPolyMeshDetail();
  if (!rcBuildPolyMeshDetail(&context, *polygonMesh, *compact, config.detailSampleDist, config.detailSampleMaxError, *detailMesh)) {
    release();
    return fail("rcBuildPolyMeshDetail");
  }
  if (polygonMesh->npolys == 0) {
    release();
    return result;
  }
  if (polygonMesh->npolys > (1 << polygonBits)) {
    result.error = name + " has " + std::to_string(polygonMesh->npolys) + " polygons, more than the " + std::to_string(1 << polygonBits) +
      " its " + std::to_string(polygonBits) + " polygon bits address; their references would spill into the tile bits";
    release();
    return result;
  }
  for (int polygon = 0; polygon < polygonMesh->npolys; ++polygon) {
    if (polygonMesh->areas[polygon] == RC_WALKABLE_AREA) {
      polygonMesh->areas[polygon] = 0;
    }
    polygonMesh->flags[polygon] = static_cast<unsigned short>(1u << polygonMesh->areas[polygon]);
  }
  dtNavMeshCreateParams parameters;
  std::memset(&parameters, 0, sizeof(parameters));
  parameters.verts = polygonMesh->verts;
  parameters.vertCount = polygonMesh->nverts;
  parameters.polys = polygonMesh->polys;
  parameters.polyAreas = polygonMesh->areas;
  parameters.polyFlags = polygonMesh->flags;
  parameters.polyCount = polygonMesh->npolys;
  parameters.nvp = polygonMesh->nvp;
  parameters.detailMeshes = detailMesh->meshes;
  parameters.detailVerts = detailMesh->verts;
  parameters.detailVertsCount = detailMesh->nverts;
  parameters.detailTris = detailMesh->tris;
  parameters.detailTriCount = detailMesh->ntris;
  parameters.walkableHeight = settings.agentHeight;
  parameters.walkableRadius = settings.agentRadius;
  parameters.walkableClimb = settings.agentClimb;
  parameters.tileX = tileX;
  parameters.tileY = tileY;
  parameters.tileLayer = 0;
  rcVcopy(parameters.bmin, polygonMesh->bmin);
  rcVcopy(parameters.bmax, polygonMesh->bmax);
  parameters.cs = config.cs;
  parameters.ch = config.ch;
  parameters.buildBvTree = false;
  if (!dtCreateNavMeshData(&parameters, &result.data, &result.size)) {
    release();
    return fail("dtCreateNavMeshData");
  }
  release();
  return result;
}

}

void runNav(const std::string& inputPath, const std::string& payloadPath, const std::string& reportPath) {
  const auto start = std::chrono::steady_clock::now();
  NavInput input = readNavInput(inputPath);
  const NavSettings& settings = input.settings;
  refuseUnusableVertices(input);
  const int triangleCount = static_cast<int>(input.triangles.size() / 3);

  const float* boundsMin = input.boundsMin;
  const float* boundsMax = input.boundsMax;
  const int gridWidth = static_cast<int>((boundsMax[0] - boundsMin[0]) / settings.cellSize + 0.5f);
  const int gridHeight = static_cast<int>((boundsMax[2] - boundsMin[2]) / settings.cellSize + 0.5f);
  const int tilesWide = (gridWidth + settings.tileSize - 1) / settings.tileSize;
  const int tilesHigh = (gridHeight + settings.tileSize - 1) / settings.tileSize;
  const long long tileCount = static_cast<long long>(tilesWide) * tilesHigh;
  if (tileCount > (1LL << maximumTileBits)) {
    throw HelperError("the nav grid needs " + std::to_string(tilesWide) + " x " + std::to_string(tilesHigh) + " = " + std::to_string(tileCount) +
      " tiles, more than the 16,384 that 14 tile bits address; map_edit would stop at 16,384 and leave the rest of the zone without nav");
  }
  const int tileBits = static_cast<int>(dtIlog2(dtNextPow2(static_cast<unsigned int>(tileCount))));
  const int polygonBits = referenceBits - tileBits;

  reportProgress("partitioning collidable triangles", 0, 1);
  rcChunkyTriMesh chunks;
  if (!rcCreateChunkyTriMesh(input.vertices.data(), input.triangles.data(), triangleCount, trianglesPerChunk, &chunks)) {
    throw HelperError("rcCreateChunkyTriMesh failed");
  }

  const float tileWorldSize = settings.tileSize * settings.cellSize;
  std::vector<TileResult> results(static_cast<size_t>(tileCount));
  std::atomic<long long> nextTile(0);
  std::atomic<long long> builtCount(0);
  const unsigned int threadCount = static_cast<unsigned int>(std::min<long long>(input.threads, tileCount));
  long long reportedPercent = -1;
  std::mutex progressLock;
  auto work = [&]() {
    std::vector<int> chunkIDs(chunks.nnodes);
    while (true) {
      const long long tile = nextTile++;
      if (tile >= tileCount) {
        return;
      }
      const int tileX = static_cast<int>(tile % tilesWide);
      const int tileY = static_cast<int>(tile / tilesWide);
      const float tileMin[3] = {boundsMin[0] + tileX * tileWorldSize, boundsMin[1], boundsMin[2] + tileY * tileWorldSize};
      const float tileMax[3] = {boundsMin[0] + (tileX + 1) * tileWorldSize, boundsMax[1], boundsMin[2] + (tileY + 1) * tileWorldSize};
      results[static_cast<size_t>(tile)] = buildTile(input, chunks, chunkIDs, polygonBits, tileX, tileY, tileMin, tileMax);
      const long long done = ++builtCount;
      std::lock_guard<std::mutex> guard(progressLock);
      const long long percent = done * 100 / tileCount;
      if (percent > reportedPercent) {
        reportedPercent = percent;
        reportProgress("building nav tiles", done, tileCount);
      }
    }
  };
  std::vector<std::thread> workers;
  for (unsigned int index = 0; index < threadCount; ++index) {
    workers.emplace_back(work);
  }
  for (std::thread& worker : workers) {
    worker.join();
  }

  auto freeResults = [&]() {
    for (TileResult& result : results) {
      dtFree(result.data);
      result.data = nullptr;
    }
  };
  for (const TileResult& result : results) {
    if (!result.error.empty()) {
      const std::string error = result.error;
      freeResults();
      throw HelperError(error);
    }
  }

  dtNavMeshParams parameters;
  rcVcopy(parameters.orig, boundsMin);
  parameters.tileWidth = tileWorldSize;
  parameters.tileHeight = tileWorldSize;
  parameters.maxTiles = 1 << tileBits;
  parameters.maxPolys = 1 << polygonBits;
  dtNavMesh* mesh = dtAllocNavMesh();
  if (dtStatusFailed(mesh->init(&parameters))) {
    dtFreeNavMesh(mesh);
    freeResults();
    throw HelperError("dtNavMesh::init failed for " + std::to_string(parameters.maxTiles) + " tiles of " + std::to_string(parameters.maxPolys) + " polygons");
  }
  int mostChunks = 0;
  for (long long tile = 0; tile < tileCount; ++tile) {
    TileResult& result = results[static_cast<size_t>(tile)];
    mostChunks = std::max(mostChunks, result.chunks);
    if (!result.data) {
      continue;
    }
    const dtStatus status = mesh->addTile(result.data, result.size, DT_TILE_FREE_DATA, 0, nullptr);
    if (dtStatusFailed(status)) {
      const int tileX = static_cast<int>(tile % tilesWide);
      const int tileY = static_cast<int>(tile / tilesWide);
      dtFreeNavMesh(mesh);
      freeResults();
      throw HelperError("dtNavMesh::addTile failed for tile (" + std::to_string(tileX) + ", " + std::to_string(tileY) + ") with status " + std::to_string(status));
    }
    result.data = nullptr;
  }

  std::vector<unsigned char> payload;
  uint32_t storedTiles = 0;
  const dtNavMesh* readMesh = mesh;
  for (int index = 0; index < mesh->getMaxTiles(); ++index) {
    const dtMeshTile* tile = readMesh->getTile(index);
    if (tile && tile->header && tile->dataSize) {
      ++storedTiles;
    }
  }
  appendBytes(payload, &storedTiles, 4);
  appendBytes(payload, mesh->getParams(), sizeof(dtNavMeshParams));
  int polygonCount = 0;
  std::map<int, int> areaCounts;
  for (int index = 0; index < mesh->getMaxTiles(); ++index) {
    const dtMeshTile* tile = readMesh->getTile(index);
    if (!tile || !tile->header || !tile->dataSize) {
      continue;
    }
    const uint32_t reference = mesh->getTileRef(tile);
    const int32_t size = tile->dataSize;
    if (reference == 0 || size == 0) {
      dtFreeNavMesh(mesh);
      throw HelperError("tile (" + std::to_string(tile->header->x) + ", " + std::to_string(tile->header->y) + ") has tile reference " +
        std::to_string(reference) + " and size " + std::to_string(size) + "; the server drops the whole mesh on a zero");
    }
    appendBytes(payload, &reference, 4);
    appendBytes(payload, &size, 4);
    appendBytes(payload, tile->data, static_cast<size_t>(size));
    for (int polygon = 0; polygon < tile->header->polyCount; ++polygon) {
      ++polygonCount;
      ++areaCounts[tile->polys[polygon].getArea()];
    }
  }
  dtFreeNavMesh(mesh);
  writeBytes(payloadPath, payload);

  const double seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
  std::ostringstream report;
  report << "{\"inputTriangles\": " << triangleCount
    << ", \"boundsMin\": " << jsonVector(boundsMin) << ", \"boundsMax\": " << jsonVector(boundsMax)
    << ", \"grid\": [" << gridWidth << ", " << gridHeight << "], \"tilesWide\": " << tilesWide << ", \"tilesHigh\": " << tilesHigh
    << ", \"tileBits\": " << tileBits << ", \"polygonBits\": " << polygonBits
    << ", \"chunks\": " << chunks.nnodes << ", \"mostChunksPerTile\": " << mostChunks
    << ", \"builtTiles\": " << storedTiles << ", \"polygons\": " << polygonCount << ", \"areas\": {";
  bool first = true;
  for (const auto& area : areaCounts) {
    report << (first ? "" : ", ") << "\"" << area.first << "\": " << area.second;
    first = false;
  }
  report << "}, \"threads\": " << threadCount << ", \"seconds\": " << seconds << "}";
  writeText(reportPath, report.str());
}
