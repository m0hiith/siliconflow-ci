# Cross-compiling toolchain for the cortex-m4 profile (GNU Arm Embedded).
#
#   cmake -S firmware -B build-m4 -G Ninja \
#     --toolchain firmware/cmake/arm-none-eabi.cmake -DSFCI_BUILD_TESTS=OFF
#
# The profile proves the firmware cross-compiles and links for a generic
# Cortex-M4F; it does not target real silicon. Unit tests stay host-only.
set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_SYSTEM_PROCESSOR arm)

set(SFCI_ARM_PREFIX arm-none-eabi- CACHE STRING "GNU Arm toolchain prefix")
set(CMAKE_C_COMPILER ${SFCI_ARM_PREFIX}gcc)
set(CMAKE_ASM_COMPILER ${SFCI_ARM_PREFIX}gcc)
set(CMAKE_OBJCOPY ${SFCI_ARM_PREFIX}objcopy CACHE FILEPATH "")
set(CMAKE_SIZE ${SFCI_ARM_PREFIX}size CACHE FILEPATH "")

# There is no OS to run a test executable on, so try_compile builds a library.
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

set(_sfci_cpu "-mcpu=cortex-m4 -mthumb -mfloat-abi=hard -mfpu=fpv4-sp-d16")
set(CMAKE_C_FLAGS_INIT "${_sfci_cpu} -ffunction-sections -fdata-sections")
set(CMAKE_ASM_FLAGS_INIT "${_sfci_cpu}")
# newlib-nano; nosys stubs the system calls, so printf output is discarded.
set(CMAKE_EXE_LINKER_FLAGS_INIT
    "${_sfci_cpu} -Wl,--gc-sections --specs=nano.specs --specs=nosys.specs")

# Headers and libraries come from the toolchain's sysroot, programs from the host.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
