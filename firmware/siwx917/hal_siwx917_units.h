/*
 * Pure conversions used by hal_siwx917.c. Kept free of SDK headers so the
 * host unit tests (firmware/tests/test_siwx917_units.c) can include them.
 */
#ifndef HAL_SIWX917_UNITS_H
#define HAL_SIWX917_UNITS_H

#include <stdint.h>

/*
 * Temperature in degrees Celsius to the HAL's sensor units, 0.1 degC
 * (so 23.46 -> 235). Rounds half away from zero. NaN reads as 0, and values
 * outside int32_t saturate.
 */
static inline int32_t hal_siwx917_deci_celsius(double celsius)
{
    double deci = celsius * 10.0;
    if (deci != deci) {
        return 0;
    }
    if (deci >= (double)INT32_MAX) {
        return INT32_MAX;
    }
    if (deci <= (double)INT32_MIN) {
        return INT32_MIN;
    }
    return (int32_t)(deci < 0.0 ? deci - 0.5 : deci + 0.5);
}

/* A 64-bit millisecond count as hal_now_ms() reports it: wrapping at UINT32_MAX. */
static inline uint32_t hal_siwx917_ms32(uint64_t ms)
{
    return (uint32_t)(ms & UINT32_MAX);
}

#endif /* HAL_SIWX917_UNITS_H */
