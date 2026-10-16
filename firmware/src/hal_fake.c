/*
 * Host-build HAL. Simulated time advances 1 ms per hal_now_ms() call, and the
 * button and sensor follow a fixed, deterministic script so program output is
 * reproducible run to run. Reads sample the signal at the time most recently
 * returned by hal_now_ms().
 */
#include "hal.h"

static uint32_t fake_now_ms;
static uint32_t fake_next_ms;

uint32_t hal_now_ms(void)
{
    fake_now_ms = fake_next_ms++;
    return fake_now_ms;
}

/* Contact bounce: the first 8 ms after each edge toggles every 1 ms. */
static bool bouncy_edge(uint32_t t, uint32_t edge_ms, bool before, bool after)
{
    if (t < edge_ms) {
        return before;
    }
    uint32_t dt = t - edge_ms;
    if (dt < 8u) {
        return (dt % 2u == 0u) ? after : before;
    }
    return after;
}

bool hal_read_button(void)
{
    /* Two presses: 100-400 ms and 700-900 ms, each with bouncy edges. */
    uint32_t t = fake_now_ms;
    if (t < 400u) {
        return bouncy_edge(t, 100u, false, true);
    }
    if (t < 700u) {
        return bouncy_edge(t, 400u, true, false);
    }
    if (t < 900u) {
        return bouncy_edge(t, 700u, false, true);
    }
    return bouncy_edge(t, 900u, true, false);
}

int32_t hal_read_sensor(void)
{
    /* Triangle wave 0..1000 with a 1000 ms period, plus +/-15 of ripple. */
    uint32_t phase = fake_now_ms % 1000u;
    int32_t tri = (phase < 500u) ? (int32_t)(phase * 2u) : (int32_t)((1000u - phase) * 2u);
    int32_t ripple = (int32_t)((fake_now_ms * 7u) % 31u) - 15;
    return tri + ripple;
}
