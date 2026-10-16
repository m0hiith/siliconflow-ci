# Stage log samples

Real tool output, captured 2026-10-15, used by `tests/test_classify.py`. Nothing
here is hand-written or edited. Each `*.log` is a stage log as `ci/stage.sh`
writes it, or plain tool output where noted.

Unless noted, each sample comes from the stages in `firmware-ci.yml`
(`ci/stage.sh <stage> -- …`, `-DSFCI_SANITIZE=ON`) run in the
`sfci-toolchain` image (gcc 13.3.0, cmake 3.28, cppcheck 2.13.0), against a
copy of `firmware/` with one defect injected:

| File | Defect / condition |
| --- | --- |
| `clean.{configure,compile,test,analyze}.log` | none (all stages pass) |
| `gcc-undeclared.compile.log` | `src/sensor_logic.c` uses an undeclared identifier |
| `gcc-werror.compile.log` | unused local variable (`-Werror=unused-variable`) |
| `ld-undefined-reference.compile.log` | `main.c` calls a declared but undefined function |
| `ld-multiple-definition.compile.log` | `main.c` redefines `hal_now_ms` |
| `cmake-error.configure.log` | `CMakeLists.txt` links a target that does not exist |
| `ctest-unity-fail.test.log` | a Unity assertion in `test_threshold.c` expects the wrong value |
| `ctest-ubsan.test.log` | `test_threshold.c` reads past an array (UBSan aborts the test) |
| `cppcheck-null-pointer.analyze.log` | `src/sensor_logic.c` dereferences a null pointer |
| `cmake-no-network.configure.log` | container run with `--network none` (Unity cannot be fetched) |
| `gcc-no-space.compile.log` | container run with `--tmpfs /tmp:size=12k` |
| `gcc-undeclared-color.log` | the `gcc-undeclared` defect, plain `gcc … -fdiagnostics-color=always -c src/sensor_logic.c` (ANSI colors) |
| `gcc-oom-killed.compile.log` | `ci/stage.sh compile -- gcc -O2 -c` of a 60k-line generated file, container limited to 96 MiB (the kernel OOM killer ends `cc1`) |

Captured on macOS outside the image:

| File | Command |
| --- | --- |
| `ld-section-will-not-fit.link.log` | `arm-none-eabi-gcc` 16.1.0 `-mcpu=cortex-m33 -nostdlib -T tiny.ld`, 8 KiB `const` table into a 4 KiB FLASH region |
| `ld-region-overflowed.link.log` | same, 4 KiB `.bss` buffer into a 1 KiB RAM region |
| `openocd-no-cmsis-dap.flash.log` | OpenOCD 0.12.0 `-f interface/cmsis-dap.cfg -f target/efm32.cfg -c "program … verify reset exit"`, no probe attached |
| `openocd-no-jlink.flash.log` | same with `-f interface/jlink.cfg -c "transport select swd"` |
