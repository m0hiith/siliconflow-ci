/*
 * Hardware abstraction layer. Application code reaches hardware only through
 * these functions. Host builds link src/hal_fake.c; target builds provide
 * their own implementation (the SiWx917 SoC: siwx917/hal_siwx917.c).
 */
#ifndef HAL_H
#define HAL_H

#include <stdbool.h>
#include <stdint.h>

/* Monotonic millisecond counter; may wrap at UINT32_MAX. */
uint32_t hal_now_ms(void);

/* Raw (undebounced) button level; true = pressed. */
bool hal_read_button(void);

/* Raw sensor reading in sensor units. */
int32_t hal_read_sensor(void);

#endif /* HAL_H */
