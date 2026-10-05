#pragma once

#include <cstddef>
#include <cstdint>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

class HelperError : public std::runtime_error {
public:
  using std::runtime_error::runtime_error;
};

class InputReader {
public:
  InputReader(const std::string& path, const char* magic);
  uint32_t readUInt32();
  int32_t readInt32();
  float readFloat();
  void readFloats(float* destination, size_t count);
  void readBytes(unsigned char* destination, size_t count);
  size_t remaining() const;
  void requireEnd() const;

private:
  void require(size_t count, const char* what) const;
  std::string path;
  std::vector<unsigned char> bytes;
  size_t offset;
};

void reportProgress(const std::string& step, long long done, long long of);
void reportError(const std::string& message);
void writeBytes(const std::string& path, const std::vector<unsigned char>& bytes);
void writeText(const std::string& path, const std::string& text);
void appendBytes(std::vector<unsigned char>& output, const void* source, size_t count);

std::string jsonString(const std::string& text);
std::string jsonFloat(float value);
std::string jsonVector(const float* values);
std::string zoneAxesText(const float* recast);
