#include "sensor_logic.h"

/* ---------------------------------------------------------------- debounce */

void sl_debounce_init(sl_debounce_t *db, uint32_t stable_ms, bool initial_level,
                      uint32_t now_ms)
{
    db->stable_ms = stable_ms;
    db->stable_level = initial_level;
    db->candidate_level = initial_level;
    db->candidate_since_ms = now_ms;
}

sl_button_event_t sl_debounce_update(sl_debounce_t *db, bool raw_level, uint32_t now_ms)
{
    if (raw_level != db->candidate_level) {
        /* Any change restarts the stability timer. */
        db->candidate_level = raw_level;
        db->candidate_since_ms = now_ms;
    }

    if (db->candidate_level == db->stable_level) {
        return SL_BUTTON_NONE;
    }

    /* Unsigned subtraction is correct across a counter wrap. */
    if ((uint32_t)(now_ms - db->candidate_since_ms) < db->stable_ms) {
        return SL_BUTTON_NONE;
    }

    db->stable_level = db->candidate_level;
    return db->stable_level ? SL_BUTTON_PRESSED : SL_BUTTON_RELEASED;
}

bool sl_debounce_level(const sl_debounce_t *db)
{
    return db->stable_level;
}

/* ---------------------------------------------------------- moving average */

bool sl_mavg_init(sl_mavg_t *f, size_t window)
{
    if (window == 0u || window > SL_MAVG_MAX_WINDOW) {
        return false;
    }
    for (size_t i = 0; i < SL_MAVG_MAX_WINDOW; i++) {
        f->samples[i] = 0;
    }
    f->sum = 0;
    f->window = window;
    f->count = 0;
    f->next = 0;
    return true;
}

int32_t sl_mavg_update(sl_mavg_t *f, int32_t sample)
{
    if (f->count == f->window) {
        f->sum -= f->samples[f->next];
    } else {
        f->count++;
    }
    f->samples[f->next] = sample;
    f->sum += sample;
    f->next = (f->next + 1u) % f->window;

    /* |mean| <= max |sample|, so the result always fits in int32_t. */
    return (int32_t)(f->sum / (int64_t)f->count);
}

bool sl_mavg_ready(const sl_mavg_t *f)
{
    return f->count == f->window;
}

/* ------------------------------------------------------ threshold detector */

bool sl_thresh_init(sl_thresh_t *t, int32_t low, int32_t high)
{
    if (low >= high) {
        return false;
    }
    t->low = low;
    t->high = high;
    t->is_high = false;
    return true;
}

sl_thresh_event_t sl_thresh_update(sl_thresh_t *t, int32_t value)
{
    if (!t->is_high && value >= t->high) {
        t->is_high = true;
        return SL_THRESH_RISING;
    }
    if (t->is_high && value <= t->low) {
        t->is_high = false;
        return SL_THRESH_FALLING;
    }
    return SL_THRESH_NONE;
}

bool sl_thresh_is_high(const sl_thresh_t *t)
{
    return t->is_high;
}
