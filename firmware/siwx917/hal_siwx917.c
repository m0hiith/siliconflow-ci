/*
 * hal.h on the SiWx917 SoC (BRD4338A):
 * - hal_now_ms:      sleeptimer 64-bit tick count, in ms, truncated to 32 bits
 * - hal_read_button: raw GPIO level of BTN0 (active low on the board)
 * - hal_read_sensor: on-chip BJT temperature sensor, in 0.1 degC
 *
 * hal_siwx917_init() must run before the other functions (app_init does it).
 *
 * With SFCI_HIL_HOOKS, a running bounce script (hal_siwx917_inject_bounce)
 * replaces BTN0's level, and hal_siwx917_uart_getc reads the VCOM UART.
 */
#include <stdbool.h>
#include <stdint.h>

#include "hal.h"
#include "hal_siwx917.h"
#include "hal_siwx917_units.h"
#include "sl_si91x_bjt_temperature_sensor.h"
#include "sl_si91x_button_instances.h"
#include "sl_si91x_driver_gpio.h"
#include "sl_si91x_gpio.h"
#include "sl_sleeptimer.h"
#include "rsi_debug.h"

#if SFCI_HIL_HOOKS
#include "sfci_bounce.h"
#include "sl_si91x_usart.h"

/* The VCOM iostream's USART (WiSeConnect sl_iostream_usart_si91x.c, which
 * no header declares). If an SDK update renames it, the link fails. */
extern sl_usart_handle_t usart_handle;

static sfci_bounce_t bounce;
static uint8_t rx_byte;
static bool rx_armed;
#endif

/* Fixed sensor configuration (ADC channel 0, OPAMP1 unity gain) defined by the driver. */
extern adc_config_t sl_bjt_config;
extern adc_ch_config_t sl_bjt_channel_config;

/* Last good temperature; returned again if a conversion fails. */
static int32_t last_deci_celsius;

sl_status_t hal_siwx917_init(void)
{
    sl_status_t status = sl_si91x_bjt_temperature_sensor_init(sl_bjt_channel_config, sl_bjt_config);
    /* The sensor init retunes M4SS_REF_CLK while it reads eFuse calibration;
     * re-derive the debug UART's prescaler so later output is not garbled. */
    DEBUGINIT();
#if SFCI_HIL_HOOKS
    sfci_bounce_init(&bounce);
#endif
    return status;
}

uint32_t hal_now_ms(void)
{
    uint64_t ms = 0;
    (void)sl_sleeptimer_tick64_to_ms(sl_sleeptimer_get_tick_count64(), &ms);
    return hal_siwx917_ms32(ms);
}

bool hal_read_button(void)
{
#if SFCI_HIL_HOOKS
    bool injected;
    if (sfci_bounce_level(&bounce, hal_now_ms(), &injected)) {
        return injected;
    }
#endif
    uint8_t level;
    if (button_btn0.port == HP || button_btn0.port == ULP) {
        level = sl_gpio_get_pin_input(button_btn0.port, button_btn0.pin);
    } else {
        level = sl_si91x_gpio_get_uulp_npss_pin(button_btn0.pin);
    }
    return level == 0u;
}

int32_t hal_read_sensor(void)
{
    double celsius = 0.0;
    if (sl_si91x_bjt_temperature_sensor_read_data(&celsius) == SL_STATUS_OK) {
        last_deci_celsius = hal_siwx917_deci_celsius(celsius);
    }
    return last_deci_celsius;
}

#if SFCI_HIL_HOOKS
bool hal_siwx917_inject_bounce(void)
{
    return sfci_bounce_start(&bounce, hal_now_ms());
}

bool hal_siwx917_uart_getc(char *c)
{
    /* The iostream's read busy-waits for each byte, which would stall the
     * main loop; instead keep one interrupt-driven 1-byte receive armed and
     * check whether it has finished. */
    bool got = false;
    if (rx_armed) {
        if (sl_si91x_usart_get_status(usart_handle).rx_busy) {
            return false;
        }
        rx_armed = false;
        if (sl_si91x_usart_get_rx_data_count(usart_handle) == 1u) {
            *c = (char)rx_byte;
            got = true;
        }
    }
    rx_armed = sl_si91x_usart_receive_data(usart_handle, &rx_byte, 1u) == SL_STATUS_OK;
    return got;
}
#endif
