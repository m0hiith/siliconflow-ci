/*
 * sensor_logic on the SiWx917 SoC: the same pipeline as firmware/src/main.c,
 * driven from the SDK's main loop instead of a for loop.
 *
 * - On boot it prints one line, "SFCI-BOOT version=... sha=...", that the
 *   hardware-in-the-loop test (ci/hil/boot_check.py) checks.
 * - BTN0 is debounced on every pass.
 * - The temperature sensor is sampled every SAMPLE_PERIOD_MS, averaged over
 *   FILTER_WINDOW samples and checked against THRESH_LOW/THRESH_HIGH (0.1 degC).
 * - With SFCI_HIL_HOOKS (ci/siwx917.sh, never for a release image) it also
 *   reads commands on the UART (sfci_cli.h) for tests/hil, and answers each
 *   with one "SFCI-CLI ..." line.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "app.h"
#include "hal.h"
#include "hal_siwx917.h"
#include "sensor_logic.h"
#include "sfci_boot.h"
#include "version.h" /* written to autogen/ by ci/siwx917.sh generate */

#if SFCI_HIL_HOOKS
#include "sfci_bounce.h"
#include "sfci_cli.h"
#endif

#define DEBOUNCE_MS 20u
#define SAMPLE_PERIOD_MS 100u
#define FILTER_WINDOW 8u
#define THRESH_LOW 300  /* 30.0 degC */
#define THRESH_HIGH 700 /* 70.0 degC */

#if SFCI_HIL_HOOKS
/* What makes the injected press debounce to exactly one PRESSED/RELEASED. */
_Static_assert(SFCI_BOUNCE_MAX_GLITCH_MS < DEBOUNCE_MS && DEBOUNCE_MS <= SFCI_BOUNCE_SETTLE_MS,
               "the bounce script's glitches must be shorter than DEBOUNCE_MS");

static sfci_cli_t cli;

static void reply(const char *line)
{
    printf("SFCI-CLI %s\n", line);
    fflush(stdout);
}

static void poll_cli(void)
{
    char c;
    while (hal_siwx917_uart_getc(&c)) {
        switch (sfci_cli_feed(&cli, c)) {
        case SFCI_CLI_INJECT_BOUNCE:
            reply(hal_siwx917_inject_bounce() ? "ok inject-bounce" : "error busy");
            break;
        case SFCI_CLI_UNKNOWN:
            reply("error unknown-command"); /* the line is not echoed */
            break;
        case SFCI_CLI_TOO_LONG:
            reply("error line-too-long");
            break;
        case SFCI_CLI_NONE:
            break;
        }
    }
}
#endif

static sl_debounce_t button;
static sl_mavg_t filter;
static sl_thresh_t level;
static bool sensor_ok;
static uint32_t last_sample_ms;

void app_init(void)
{
    /* First, so nothing else can print before it. */
    char boot[SFCI_BOOT_LINE_MAX];
    (void)sfci_boot_line(boot, sizeof boot, FW_VERSION, FW_COMMIT_SHORT);
    fputs(boot, stdout);
    fflush(stdout);

    sl_status_t status = hal_siwx917_init();
    sensor_ok = status == SL_STATUS_OK;
    if (!sensor_ok) {
        printf("temperature sensor init failed: 0x%lx\n", (unsigned long)status);
    }

    uint32_t now = hal_now_ms();
    sl_debounce_init(&button, DEBOUNCE_MS, hal_read_button(), now);
    (void)sl_mavg_init(&filter, FILTER_WINDOW);
    (void)sl_thresh_init(&level, THRESH_LOW, THRESH_HIGH);
    last_sample_ms = now;
#if SFCI_HIL_HOOKS
    sfci_cli_init(&cli);
    printf("SFCI-HIL hooks enabled\n");
#endif
    printf("sensor_logic on SiWx917 started\n");
}

void app_process_action(void)
{
#if SFCI_HIL_HOOKS
    poll_cli();
#endif
    uint32_t now = hal_now_ms();

    switch (sl_debounce_update(&button, hal_read_button(), now)) {
    case SL_BUTTON_PRESSED:
        printf("%10lu ms  button  PRESSED\n", (unsigned long)now);
        break;
    case SL_BUTTON_RELEASED:
        printf("%10lu ms  button  RELEASED\n", (unsigned long)now);
        break;
    case SL_BUTTON_NONE:
        break;
    }

    /* Unsigned subtraction keeps the period correct across a counter wrap. */
    if (!sensor_ok || (uint32_t)(now - last_sample_ms) < SAMPLE_PERIOD_MS) {
        return;
    }
    last_sample_ms = now;

    int32_t avg = sl_mavg_update(&filter, hal_read_sensor());
    if (!sl_mavg_ready(&filter)) {
        return;
    }
    switch (sl_thresh_update(&level, avg)) {
    case SL_THRESH_RISING:
        printf("%10lu ms  sensor  HIGH  avg=%ld\n", (unsigned long)now, (long)avg);
        break;
    case SL_THRESH_FALLING:
        printf("%10lu ms  sensor  LOW   avg=%ld\n", (unsigned long)now, (long)avg);
        break;
    case SL_THRESH_NONE:
        break;
    }
}
