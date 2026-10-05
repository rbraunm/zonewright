cmake_minimum_required(VERSION 3.20)
project(recastHelper CXX)

if(NOT DEFINED RECAST_ROOT)
  message(FATAL_ERROR "RECAST_ROOT must name the extracted recastnavigation tree")
endif()
cmake_path(CONVERT "${RECAST_ROOT}" TO_CMAKE_PATH_LIST recastRoot NORMALIZE)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_MSVC_RUNTIME_LIBRARY MultiThreaded)
# /fp:precise keeps every build's nav byte-identical; /fp:fast would let the optimizer reorder float math.
set(CMAKE_CXX_FLAGS "/EHsc /fp:precise /W3")
set(CMAKE_CXX_FLAGS_RELEASE "/O2 /DNDEBUG")

file(GLOB recastSources ${recastRoot}/Recast/Source/*.cpp)
file(GLOB detourSources ${recastRoot}/Detour/Source/*.cpp)

add_executable(recastHelper
  main.cpp
  helperIO.cpp
  navMode.cpp
  inspectMode.cpp
  ${recastSources}
  ${detourSources}
  ${recastRoot}/RecastDemo/Source/ChunkyTriMesh.cpp
)
target_include_directories(recastHelper PRIVATE
  ${recastRoot}/Recast/Include
  ${recastRoot}/Detour/Include
  ${recastRoot}/RecastDemo/Include
)
