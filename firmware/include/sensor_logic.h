/*
 * sensor_logic: hardware-independent signal conditioning.
 *
 * - Button debouncer: a level must be stable for `stable_ms` before it is
 *   accepted; accepted changes are reported as PRESSED / RELEASED events.
 * - Moving average: fixed-size ring buffer, no dynamic allocation.
 * - Threshold detector: two-level hysteresis (rise at >= high, fall at <= low).
 *
 * All timestamps are uint32_t milliseconds; elapsed time is computed with
 * unsigned subtraction so a wrapping millisecond counter is handled correctly.
 */
#ifndef SENSOR_LOGIC_H
#define SENSOR_LOGIC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* ---------------------------------------------------------------- debounce */

typedef enum {
    SL_BUTTON_NONE = 0,
    SL_BUTTON_PRESSED,
    SL_BUTTON_RELEASED,
} sl_button_event_t;

typedef struct {
    uint32_t stable_ms;    /* time a new level must hold before it is accepted */
    bool stable_level;     /* last accepted level (true = pressed) */
    bool candidate_level;  /* most recent raw level */
    uint32_t candidate_since_ms;
} sl_debounce_t;

/* Start in `initial_level` at time `now_ms`; no event is emitted for it. */
void sl_debounce_init(sl_debounce_t *db, uint32_t stable_ms, bool initial_level,
                      uint32_t now_ms);

/*
 * Feed one raw sample. Returns PRESSED/RELEASED on the call where a new level
 * has been held for at least `stable_ms` (elapsed == stable_ms counts), else NONE.
 */
sl_button_event_t sl_debounce_update(sl_debounce_t *db, bool raw_level, uint32_t now_ms);

bool sl_debounce_level(const sl_debounce_t *db);

/* ---------------------------------------------------------- moving average */

#define SL_MAVG_MAX_WINDOW 32u

typedef struct {
    int32_t samples[SL_MAVG_MAX_WINDOW];
    int64_t sum;
    size_t window;  /* configured window, 1..SL_MAVG_MAX_WINDOW */
    size_t count;   /* samples held, saturates at window */
    size_t next;    /* ring index of the next write */
} sl_mavg_t;

/* Returns false (and leaves *f unusable) if window is 0 or > SL_MAVG_MAX_WINDOW. */
bool sl_mavg_init(sl_mavg_t *f, size_t window);

/*
 * Add a sample and return the mean of the samples held so far. During warm-up
 * (fewer than `window` samples) the mean is over the samples seen, not over a
 * zero-padded window. Division truncates toward zero.
 */
int32_t sl_mavg_update(sl_mavg_t *f, int32_t sample);

/* True once the window is full. */
bool sl_mavg_ready(const sl_mavg_t *f);

/* ------------------------------------------------------ threshold detector */

typedef enum {
    SL_THRESH_NONE = 0,
    SL_THRESH_RISING,   /* went from low to high state */
    SL_THRESH_FALLING,  /* went from high to low state */
} sl_thresh_event_t;

typedef struct {
    int32_t low;   /* value <= low  -> low state  */
    int32_t high;  /* value >= high -> high state */
    bool is_high;
} sl_thresh_t;

/* Starts in the low state. Returns false if low >= high. */
bool sl_thresh_init(sl_thresh_t *t, int32_t low, int32_t high);

sl_thresh_event_t sl_thresh_update(sl_thresh_t *t, int32_t value);

bool sl_thresh_is_high(const sl_thresh_t *t);

#endif /* SENSOR_LOGIC_H */
