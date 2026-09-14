# The prism-live audio shim: libprism_live_shim.dll and its tests (prompt 2.5).
#
#   cmake -S native -B native/build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo \
#         -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++
#   cmake --build native/build
#   ctest --test-dir native/build --output-on-failure
#
# See native/README.md for the toolchain, the flags and why the DLL is committed.

cmake_minimum_required(VERSION 3.20)
project(prism_live_shim LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_C_STANDARD 99)

option(PLS_BUILD_TESTS "Build the native tests" ON)

# --- Source hash -----------------------------------------------------------------------------
# SHA-256 over what the DLL is built from: this file (the flags, the miniaudio definitions) and
# every file under include/, src/ and third_party/ (miniaudio itself). Sorted by path relative to
# native/ with forward slashes, each file contributes path + "\n" + contents with CRLF as LF + "\n",
# concatenated. The build type and the compiler are not in it; the README's pinned DLL SHA-256 ties
# the bytes to the toolchain. The DLL returns it from pls_source_hash(), and a Python test
# recomputes it from the checkout, so a committed DLL built from other sources fails. Every hashed
# file is a configure dependency, so editing one re-runs this and the define follows.
file(GLOB_RECURSE PLS_HASHED_FILES LIST_DIRECTORIES false CONFIGURE_DEPENDS
     RELATIVE "${CMAKE_CURRENT_SOURCE_DIR}"
     "${CMAKE_CURRENT_SOURCE_DIR}/include/*" "${CMAKE_CURRENT_SOURCE_DIR}/src/*"
     "${CMAKE_CURRENT_SOURCE_DIR}/third_party/*")
# Listed on its own: a recursive glob for it would also match any CMakeLists.txt below native/.
list(APPEND PLS_HASHED_FILES CMakeLists.txt)
list(SORT PLS_HASHED_FILES)
set(PLS_HASH_INPUT "")
foreach(relative IN LISTS PLS_HASHED_FILES)
  file(READ "${CMAKE_CURRENT_SOURCE_DIR}/${relative}" contents)
  string(REPLACE "\r\n" "\n" contents "${contents}")
  string(APPEND PLS_HASH_INPUT "${relative}\n${contents}\n")
  set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
               "${CMAKE_CURRENT_SOURCE_DIR}/${relative}")
endforeach()
string(SHA256 PLS_SOURCE_HASH "${PLS_HASH_INPUT}")
message(STATUS "pls_source_hash: ${PLS_SOURCE_HASH}")

# --- miniaudio ---------------------------------------------------------------------------------
# Playback through WASAPI only. The same definitions reach every file that includes the header,
# because they change the layout of ma_device.
set(PLS_MINIAUDIO_DEFINITIONS
    MA_NO_DECODING MA_NO_ENCODING MA_NO_GENERATION MA_NO_RESOURCE_MANAGER MA_NO_NODE_GRAPH
    MA_NO_ENGINE MA_ENABLE_ONLY_SPECIFIC_BACKENDS MA_ENABLE_WASAPI)

add_library(pls_miniaudio OBJECT third_party/miniaudio/miniaudio.c)
target_include_directories(pls_miniaudio PUBLIC third_party/miniaudio)
target_compile_definitions(pls_miniaudio PUBLIC ${PLS_MINIAUDIO_DEFINITIONS})
set_target_properties(pls_miniaudio PROPERTIES POSITION_INDEPENDENT_CODE ON)

# --- The shim ----------------------------------------------------------------------------------
# src/ is reached with -iquote, never -I: src/process.h would otherwise stand in for the C
# runtime's <process.h>, which <pthread.h> includes.
set(PLS_WARNINGS -Wall -Wextra -Wpedantic -Werror)
set(PLS_QUOTE_SRC "SHELL:-iquote ${CMAKE_CURRENT_SOURCE_DIR}/src")

add_library(pls_objects OBJECT
    src/heartbeat.cpp
    src/limiter.cpp
    src/process.cpp
    src/shim.cpp)
target_include_directories(pls_objects PUBLIC include)
target_link_libraries(pls_objects PUBLIC pls_miniaudio)
target_compile_definitions(pls_objects PRIVATE PLS_BUILD_SHARED
                           "PLS_SOURCE_HASH=\"${PLS_SOURCE_HASH}\"")
target_compile_options(pls_objects PRIVATE ${PLS_WARNINGS})
set_target_properties(pls_objects PROPERTIES
    POSITION_INDEPENDENT_CODE ON
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN ON)

add_library(prism_live_shim SHARED $<TARGET_OBJECTS:pls_objects> $<TARGET_OBJECTS:pls_miniaudio>)
set_target_properties(prism_live_shim PROPERTIES LINKER_LANGUAGE CXX)
# No MinGW runtime DLLs (-static), no symbols (-s), PE timestamp 0 so the build is reproducible.
target_link_options(prism_live_shim PRIVATE -static -s -Wl,--no-insert-timestamp)

# --- Tests -------------------------------------------------------------------------------------
if(PLS_BUILD_TESTS)
  enable_testing()

  # Each test links the shim's own objects, so it runs exactly the code in the DLL.
  function(pls_native_test name)
    add_executable(${name} tests/${name}.cpp
                   $<TARGET_OBJECTS:pls_objects> $<TARGET_OBJECTS:pls_miniaudio>)
    target_include_directories(${name} PRIVATE include)
    target_compile_definitions(${name} PRIVATE ${PLS_MINIAUDIO_DEFINITIONS})
    target_compile_options(${name} PRIVATE ${PLS_WARNINGS} ${PLS_QUOTE_SRC})
    target_link_options(${name} PRIVATE -static ${ARGN})
    add_test(NAME ${name} COMMAND ${name})
  endfunction()

  # The allocation wrappers see every malloc family call in those objects and in the static C++
  # runtime.
  pls_native_test(rt_tripwire_test
      -Wl,--wrap=malloc -Wl,--wrap=calloc -Wl,--wrap=realloc -Wl,--wrap=free
      -Wl,--wrap=_aligned_malloc -Wl,--wrap=_aligned_realloc -Wl,--wrap=_aligned_free)
  pls_native_test(shim_dsp_test)
endif()
