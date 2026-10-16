include(FetchContent)

# Unity v2.6.1, pinned by commit so the fetched source cannot change under us.
FetchContent_Declare(unity
  GIT_REPOSITORY https://github.com/ThrowTheSwitch/Unity.git
  GIT_TAG cbcd08fa7de711053a3deec6339ee89cad5d2697 # v2.6.1
  GIT_SHALLOW FALSE)
FetchContent_MakeAvailable(unity)

function(sfci_add_unity_test name)
  add_executable(${name} ${name}.c)
  target_link_libraries(${name} PRIVATE sensor_logic unity sfci_options)
  add_test(NAME ${name} COMMAND ${name})
endfunction()

sfci_add_unity_test(test_debounce)
sfci_add_unity_test(test_moving_average)
sfci_add_unity_test(test_threshold)

# The SiWx917 HAL's pure conversions (firmware/siwx917/hal_siwx917_units.h);
# the HAL itself needs the SDK and is built only by the siwx917-soc profile.
sfci_add_unity_test(test_siwx917_units)
target_include_directories(test_siwx917_units PRIVATE ${PROJECT_SOURCE_DIR}/siwx917)

# The boot line the hardware-in-the-loop test reads (firmware/siwx917/sfci_boot.h).
sfci_add_unity_test(test_sfci_boot)
target_include_directories(test_sfci_boot PRIVATE ${PROJECT_SOURCE_DIR}/siwx917)

# The hardware-in-the-loop test hooks (firmware/src/sfci_bounce.c, sfci_cli.c).
sfci_add_unity_test(test_sfci_bounce)
target_link_libraries(test_sfci_bounce PRIVATE sfci_hil)
sfci_add_unity_test(test_sfci_cli)
target_link_libraries(test_sfci_cli PRIVATE sfci_hil)
