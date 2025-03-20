cmake_minimum_required(VERSION 3.16)
project(UpdateWaterPolygons LANGUAGES C CXX)

# Set the C++ standard to C++20
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED True)

set(BOOST_NO_WARN_NEW_VERSIONS 1)
find_package(Boost 1.79 REQUIRED filesystem)
include_directories(${Boost_INCLUDE_DIRS})
add_definitions(-DBOOST_FILESYSTEM_VERSION=4)

include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/third_party/shapelib)

file(GLOB_RECURSE SOURCES "src/*.cpp")
list(
  APPEND
  SOURCES
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/shapelib/shpopen.c
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/shapelib/dbfopen.c
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/shapelib/safileio.c
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/shapelib/shptree.c
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/shapelib/sbnsearch.c
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/shapelib/shapefil.h
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/shapelib/shapefil_private.h)
list(FILTER SOURCES EXCLUDE REGEX "main\.cpp")
add_library(core ${SOURCES})

add_executable(uwp src/main.cpp)
target_link_libraries(uwp PUBLIC core)
