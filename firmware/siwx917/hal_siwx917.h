/*
 * SiWx917-specific additions to hal.h.
 */
#ifndef HAL_SIWX917_H
#define HAL_SIWX917_H

#include <stdbool.h>

#include "sfci_build.h" /* SFCI_HIL_HOOKS; written to autogen/ by ci/siwx917.sh generate */
#include "sl_status.h"

/* Initialise the BJT temperature sensor. Call once, before hal_read_sensor(). */
sl_status_t hal_siwx917_init(void);

#if SFCI_HIL_HOOKS
/*
 * Hardware-in-the-loop test hooks. Never in a release image.
 */

/* Run the scripted bouncy press (sfci_bounce.h) in place of BTN0, from now.
 * Returns false if one is already running. */
bool hal_siwx917_inject_bounce(void);

/* Take one byte received on the VCOM UART, if there is one. Never waits. */
bool hal_siwx917_uart_getc(char *c);
#endif

#endif /* HAL_SIWX917_H */
