#include "helperIO.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <mutex>

namespace {

std::mutex stderrLock;

void writeLine(const std::string& line) {
  std::lock_guard<std::mutex> guard(stderrLock);
  std::fputs(line.c_str(), stderr);
  std::fputc('\n', stderr);
  std::fflush(stderr);
}

}

InputReader::InputReader(const std::string& path, const char* magic) : path(path), offset(0) {
  std::ifstream source(path, std::ios::binary);
  if (!source) {
    throw HelperError("cannot open input " + path);
  }
  bytes.assign(std::istreambuf_iterator<char>(source), std::istreambuf_iterator<char>());
  const size_t magicLength = std::strlen(magic);
  if (bytes.size() < magicLength || std::memcmp(bytes.data(), magic, magicLength) != 0) {
    throw HelperError(path + " does not start with " + magic);
  }
  offset = magicLength;
}

void InputReader::require(size_t count, const char* what) const {
  if (bytes.size() - offset < count) {
    std::ostringstream message;
    message << path << ": " << what << " at offset " << offset << " needs " << count << " bytes, " << bytes.size() - offset << " remain";
    throw HelperError(message.str());
  }
}

uint32_t InputReader::readUInt32() {
  require(4, "uint32");
  uint32_t value;
  std::memcpy(&value, bytes.data() + offset, 4);
  offset += 4;
  return value;
}

int32_t InputReader::readInt32() {
  require(4, "int32");
  int32_t value;
  std::memcpy(&value, bytes.data() + offset, 4);
  offset += 4;
  return value;
}

float InputReader::readFloat() {
  require(4, "float32");
  float value;
  std::memcpy(&value, bytes.data() + offset, 4);
  offset += 4;
  return value;
}

void InputReader::readFloats(float* destination, size_t count) {
  require(count * 4, "float32 array");
  std::memcpy(destination, bytes.data() + offset, count * 4);
  offset += count * 4;
}

void InputReader::readBytes(unsigned char* destination, size_t count) {
  require(count, "byte block");
  std::memcpy(destination, bytes.data() + offset, count);
  offset += count;
}

size_t InputReader::remaining() const {
  return bytes.size() - offset;
}

void InputReader::requireEnd() const {
  if (offset != bytes.size()) {
    std::ostringstream message;
    message << path << ": input ends at offset " << offset << " but holds " << bytes.size() << " bytes";
    throw HelperError(message.str());
  }
}

void reportProgress(const std::string& step, long long done, long long of) {
  std::ostringstream line;
  line << "{\"step\": " << jsonString(step) << ", \"done\": " << done << ", \"of\": " << of << "}";
  writeLine(line.str());
}

void reportError(const std::string& message) {
  writeLine("{\"error\": " + jsonString(message) + "}");
}

void writeBytes(const std::string& path, const std::vector<unsigned char>& bytes) {
  std::ofstream destination(path, std::ios::binary);
  destination.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
  if (!destination) {
    throw HelperError("cannot write " + path);
  }
}

void writeText(const std::string& path, const std::string& text) {
  std::ofstream destination(path, std::ios::binary);
  destination << text;
  if (!destination) {
    throw HelperError("cannot write " + path);
  }
}

void appendBytes(std::vector<unsigned char>& output, const void* source, size_t count) {
  const unsigned char* start = static_cast<const unsigned char*>(source);
  output.insert(output.end(), start, start + count);
}

std::string jsonString(const std::string& text) {
  std::string quoted = "\"";
  for (const char character : text) {
    if (character == '"' || character == '\\') {
      quoted += '\\';
      quoted += character;
    } else if (static_cast<unsigned char>(character) < 0x20) {
      char escaped[8];
      std::snprintf(escaped, sizeof(escaped), "\\u%04x", static_cast<unsigned char>(character));
      quoted += escaped;
    } else {
      quoted += character;
    }
  }
  return quoted + "\"";
}

std::string jsonFloat(float value) {
  if (!std::isfinite(value)) {
    throw HelperError("a reported number is not finite");
  }
  char text[32];
  std::snprintf(text, sizeof(text), "%.9g", static_cast<double>(value));
  return text;
}

std::string jsonVector(const float* values) {
  return "[" + jsonFloat(values[0]) + ", " + jsonFloat(values[1]) + ", " + jsonFloat(values[2]) + "]";
}

std::string zoneAxesText(const float* recast) {
  char text[96];
  std::snprintf(text, sizeof(text), "zone (%.2f, %.2f, %.2f)", recast[2], recast[0], recast[1]);
  return text;
}
