#include <exception>
#include <new>
#include <string>

#include "helperIO.h"

void runNav(const std::string& inputPath, const std::string& payloadPath, const std::string& reportPath);
void runInspect(const std::string& inputPath, const std::string& reportPath);

int main(int argumentCount, char** arguments) {
  try {
    if (argumentCount != 5) {
      throw HelperError("usage: recastHelper nav|inspect <input.bin> <output|-> <report.json>");
    }
    const std::string mode = arguments[1];
    if (mode == "nav") {
      runNav(arguments[2], arguments[3], arguments[4]);
    } else if (mode == "inspect") {
      if (std::string(arguments[3]) != "-") {
        throw HelperError("inspect writes no output file; pass - in its place");
      }
      runInspect(arguments[2], arguments[4]);
    } else {
      throw HelperError("unknown mode '" + mode + "'; the modes are nav and inspect");
    }
  } catch (const std::bad_alloc&) {
    reportError("out of memory");
    return 1;
  } catch (const std::exception& error) {
    reportError(error.what());
    return 1;
  }
  return 0;
}
