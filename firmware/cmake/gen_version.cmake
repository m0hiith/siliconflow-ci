# Run in script mode at build time:
#   cmake -DSRC_DIR=... -DIN_FILE=... -DOUT_FILE=... -P gen_version.cmake
# configure_file only rewrites OUT_FILE when its content changes, so an
# unchanged commit does not trigger a rebuild.

find_package(Git QUIET)

set(FW_VERSION "unknown")
set(FW_COMMIT_SHORT "unknown")

if(GIT_FOUND)
  execute_process(
    COMMAND "${GIT_EXECUTABLE}" describe --always --dirty
    WORKING_DIRECTORY "${SRC_DIR}"
    OUTPUT_VARIABLE _describe
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE _rc
    ERROR_QUIET)
  if(_rc EQUAL 0 AND _describe)
    set(FW_VERSION "${_describe}")
  endif()

  execute_process(
    COMMAND "${GIT_EXECUTABLE}" rev-parse --short HEAD
    WORKING_DIRECTORY "${SRC_DIR}"
    OUTPUT_VARIABLE _sha
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE _rc
    ERROR_QUIET)
  if(_rc EQUAL 0 AND _sha)
    set(FW_COMMIT_SHORT "${_sha}")
  endif()
endif()

configure_file("${IN_FILE}" "${OUT_FILE}" @ONLY)
