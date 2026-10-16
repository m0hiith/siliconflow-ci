#include <stdio.h>

#include "hal.h"
#include "sensor_logic.h"
#include "version.h"

#define DEBOUNCE_MS 20u
#define FILTER_WINDOW 8u
#define THRESH_LOW 300
#define THRESH_HIGH 700

int main(void)
{
    sl_debounce_t button;
    sl_mavg_t filter;
    sl_thresh_t level;

    printf("sensor_logic version=%s sha=%s\n", FW_VERSION, FW_COMMIT_SHORT);

    sl_debounce_init(&button, DEBOUNCE_MS, false, hal_now_ms());
    (void)sl_mavg_init(&filter, FILTER_WINDOW);
    (void)sl_thresh_init(&level, THRESH_LOW, THRESH_HIGH);

#ifdef SFCI_HOST_RUN_MS
    /* Host builds run a fixed simulated time so the program terminates. */
    for (uint32_t now = hal_now_ms(); now < SFCI_HOST_RUN_MS; now = hal_now_ms()) {
#else
    for (;;) {
        uint32_t now = hal_now_ms();
#endif
        switch (sl_debounce_update(&button, hal_read_button(), now)) {
        case SL_BUTTON_PRESSED:
            printf("%6lu ms  button  PRESSED\n", (unsigned long)now);
            break;
        case SL_BUTTON_RELEASED:
            printf("%6lu ms  button  RELEASED\n", (unsigned long)now);
            break;
        case SL_BUTTON_NONE:
            break;
        }

        int32_t avg = sl_mavg_update(&filter, hal_read_sensor());
        if (!sl_mavg_ready(&filter)) {
            continue;
        }
        switch (sl_thresh_update(&level, avg)) {
        case SL_THRESH_RISING:
            printf("%6lu ms  sensor  HIGH  avg=%ld\n", (unsigned long)now, (long)avg);
            break;
        case SL_THRESH_FALLING:
            printf("%6lu ms  sensor  LOW   avg=%ld\n", (unsigned long)now, (long)avg);
            break;
        case SL_THRESH_NONE:
            break;
        }
    }
    return 0;
}
