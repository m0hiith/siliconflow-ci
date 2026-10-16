cmake_minimum_required(VERSION 3.24)
project(siliconflow_firmware LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(SFCI_SANITIZE "Build with AddressSanitizer and UBSan" OFF)
option(SFCI_COVERAGE "Build with gcov coverage instrumentation" OFF)
option(SFCI_BUILD_TESTS "Build Unity unit tests" ON)

# Cross builds (cmake/arm-none-eabi.cmake) only prove the firmware compiles and
# links for a Cortex-M4F; tests, sanitizers and coverage are host-only.
if(CMAKE_CROSSCOMPILING)
  if(SFCI_SANITIZE OR SFCI_COVERAGE)
    message(FATAL_ERROR "SFCI_SANITIZE and SFCI_COVERAGE are host-only options")
  endif()
  set(SFCI_BUILD_TESTS OFF CACHE BOOL "Build Unity unit tests" FORCE)
endif()

# Simulated run length for the host demo binary (see src/main.c).
set(SFCI_HOST_RUN_MS 2000 CACHE STRING "Simulated milliseconds the host demo runs")

# ------------------------------------------------------------ build options
# Applied only to our own targets, so third-party code (Unity) is not held to -Werror.
add_library(sfci_options INTERFACE)
target_compile_options(sfci_options INTERFACE -Wall -Wextra -Werror)

if(SFCI_SANITIZE)
  set(_san -fsanitize=address,undefined -fno-sanitize-recover=undefined -fno-omit-frame-pointer)
  target_compile_options(sfci_options INTERFACE ${_san})
  target_link_options(sfci_options INTERFACE ${_san})
endif()

if(SFCI_COVERAGE)
  target_compile_options(sfci_options INTERFACE --coverage -O0 -g)
  target_link_options(sfci_options INTERFACE --coverage)
endif()

# -------------------------------------------------------------- version.h
# Regenerated on every build so the version tracks the current commit.
set(SFCI_GEN_INCLUDE "${CMAKE_CURRENT_BINARY_DIR}/generated/include")
add_custom_target(sfci_version
  COMMAND ${CMAKE_COMMAND}
    -DSRC_DIR=${CMAKE_CURRENT_SOURCE_DIR}
    -DIN_FILE=${CMAKE_CURRENT_SOURCE_DIR}/include/version.h.in
    -DOUT_FILE=${SFCI_GEN_INCLUDE}/version.h
    -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/gen_version.cmake
  BYPRODUCTS ${SFCI_GEN_INCLUDE}/version.h
  COMMENT "Generating version.h from git describe"
  VERBATIM)

# ---------------------------------------------------------------- targets
add_library(sensor_logic STATIC src/sensor_logic.c)
target_include_directories(sensor_logic PUBLIC include)
target_link_libraries(sensor_logic PRIVATE sfci_options)

# Hardware-in-the-loop test hooks (bounce script, UART command parser). Pure
# C, unit-tested on the host; only the siwx917-soc build wires them to
# hardware, and only with SFCI_HIL_HOOKS (see firmware/siwx917/app.c).
add_library(sfci_hil STATIC src/sfci_bounce.c src/sfci_cli.c)
target_include_directories(sfci_hil PUBLIC include)
target_link_libraries(sfci_hil PRIVATE sfci_options)

add_library(hal_fake STATIC src/hal_fake.c)
target_include_directories(hal_fake PUBLIC include)
target_link_libraries(hal_fake PRIVATE sfci_options)

add_executable(firmware src/main.c)
add_dependencies(firmware sfci_version)
target_include_directories(firmware PRIVATE ${SFCI_GEN_INCLUDE})
# The simulated HAL is linked on the cross build too: no real part is targeted.
target_link_libraries(firmware PRIVATE sensor_logic hal_fake sfci_options)

if(CMAKE_CROSSCOMPILING)
  set(SFCI_LINKER_SCRIPT ${CMAKE_CURRENT_SOURCE_DIR}/target/cortex_m4.ld)
  target_sources(firmware PRIVATE target/startup_cortex_m4.c)
  target_link_options(firmware PRIVATE
    -T${SFCI_LINKER_SCRIPT}
    -Wl,-Map=$<TARGET_FILE_DIR:firmware>/firmware.map)
  set_target_properties(firmware PROPERTIES SUFFIX .elf LINK_DEPENDS ${SFCI_LINKER_SCRIPT})
else()
  target_compile_definitions(firmware PRIVATE SFCI_HOST_RUN_MS=${SFCI_HOST_RUN_MS}u)
endif()

# ------------------------------------------------------------------ tests
if(SFCI_BUILD_TESTS)
  enable_testing()
  add_subdirectory(tests)
endif()

# --------------------------------------------------------------- coverage
find_program(GCOVR_EXECUTABLE gcovr)
set(_gcovr_args)
if(CMAKE_C_COMPILER_ID MATCHES "Clang")
  # Clang writes LLVM-format .gcda files; gcovr needs llvm-cov's gcov mode.
  find_program(LLVM_COV_EXECUTABLE llvm-cov)
  if(NOT LLVM_COV_EXECUTABLE AND APPLE)
    execute_process(COMMAND xcrun --find llvm-cov
      OUTPUT_VARIABLE LLVM_COV_EXECUTABLE OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
  endif()
  if(LLVM_COV_EXECUTABLE)
    list(APPEND _gcovr_args --gcov-executable "${LLVM_COV_EXECUTABLE} gcov")
  endif()
endif()

if(GCOVR_EXECUTABLE)
  add_custom_target(coverage
    COMMAND ${CMAKE_CTEST_COMMAND} --test-dir ${CMAKE_BINARY_DIR} --output-on-failure
    COMMAND ${GCOVR_EXECUTABLE}
      --root ${CMAKE_CURRENT_SOURCE_DIR}
      --filter ${CMAKE_CURRENT_SOURCE_DIR}/src/
      # Host-only entry point and simulated HAL are not unit-tested product code.
      --exclude ${CMAKE_CURRENT_SOURCE_DIR}/src/main.c
      --exclude ${CMAKE_CURRENT_SOURCE_DIR}/src/hal_fake.c
      ${_gcovr_args}
      --cobertura ${CMAKE_BINARY_DIR}/coverage.xml
      --print-summary
      ${CMAKE_BINARY_DIR}
    WORKING_DIRECTORY ${CMAKE_BINARY_DIR}
    COMMENT "Running tests and writing Cobertura coverage to coverage.xml"
    VERBATIM)
else()
  add_custom_target(coverage
    COMMAND ${CMAKE_COMMAND} -E echo "gcovr not found; install it (pipx install gcovr) and reconfigure"
    COMMAND ${CMAKE_COMMAND} -E false
    VERBATIM)
endif()

if(NOT SFCI_COVERAGE)
  message(STATUS "coverage target: configure with -DSFCI_COVERAGE=ON for meaningful results")
endif()
