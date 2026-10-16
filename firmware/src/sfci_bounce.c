#include "sfci_bounce.h"

#include <stddef.h>

typedef struct {
    bool level;
    uint32_t ms;
} segment_t;

static const segment_t script[] = {
    /* press bounce */
    {true, 1u}, {false, 2u}, {true, 1u}, {false, 3u}, {true, 2u}, {false, 1u},
    {true, SFCI_BOUNCE_HOLD_MS},
    /* release bounce, ending in a re-contact just shorter than a debounce */
    {false, 1u}, {true, 2u}, {false, 1u}, {true, 3u}, {false, 2u}, {true, SFCI_BOUNCE_MAX_GLITCH_MS},
    {false, SFCI_BOUNCE_SETTLE_MS},
};

#define SCRIPT_LEN (sizeof script / sizeof script[0])

void sfci_bounce_init(sfci_bounce_t *b)
{
    b->active = false;
    b->start_ms = 0u;
}

bool sfci_bounce_start(sfci_bounce_t *b, uint32_t now_ms)
{
    if (b->active) {
        return false;
    }
    b->active = true;
    b->start_ms = now_ms;
    return true;
}

bool sfci_bounce_level(sfci_bounce_t *b, uint32_t now_ms, bool *level)
{
    if (!b->active) {
        return false;
    }
    /* Unsigned subtraction keeps dt correct across a counter wrap. */
    uint32_t dt = now_ms - b->start_ms;
    for (size_t i = 0; i < SCRIPT_LEN; i++) {
        if (dt < script[i].ms) {
            *level = script[i].level;
            return true;
        }
        dt -= script[i].ms;
    }
    b->active = false;
    return false;
}

uint32_t sfci_bounce_length_ms(void)
{
    uint32_t total = 0u;
    for (size_t i = 0; i < SCRIPT_LEN; i++) {
        total += script[i].ms;
    }
    return total;
}
