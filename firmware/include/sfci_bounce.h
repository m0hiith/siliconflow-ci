/*
 * sfci_bounce: a scripted, bouncy button press for the hardware-in-the-loop
 * test hook (SFCI_HIL_HOOKS). While a script runs it replaces the raw button
 * level the HAL reports, so the real debouncer on the target sees contact
 * bounce without anyone touching the board.
 *
 * The script, from start:
 *   press bounce     glitches of 1-3 ms
 *   held pressed     SFCI_BOUNCE_HOLD_MS
 *   release bounce   glitches of 1-3 ms, then one re-contact of
 *                    SFCI_BOUNCE_MAX_GLITCH_MS
 *   held released    SFCI_BOUNCE_SETTLE_MS, then the script ends
 *
 * A debouncer whose stable time is longer than SFCI_BOUNCE_MAX_GLITCH_MS and
 * at most SFCI_BOUNCE_SETTLE_MS reports exactly one PRESSED and one RELEASED,
 * and has reported both before the script ends.
 *
 * Kept free of SDK headers so the host unit tests can use it.
 */
#ifndef SFCI_BOUNCE_H
#define SFCI_BOUNCE_H

#include <stdbool.h>
#include <stdint.h>

#define SFCI_BOUNCE_HOLD_MS 200u
#define SFCI_BOUNCE_SETTLE_MS 100u
/* The longest time any glitch holds one level. */
#define SFCI_BOUNCE_MAX_GLITCH_MS 15u

typedef struct {
    bool active;
    uint32_t start_ms;
} sfci_bounce_t;

/* Not running. */
void sfci_bounce_init(sfci_bounce_t *b);

/* Start the script at now_ms. Returns false, and changes nothing, if one is already running. */
bool sfci_bounce_start(sfci_bounce_t *b, uint32_t now_ms);

/*
 * While the script runs, writes its level at now_ms (true = pressed) to
 * *level and returns true. Returns false once it has ended (and marks it
 * ended) or if it never started. now_ms may wrap past UINT32_MAX.
 */
bool sfci_bounce_level(sfci_bounce_t *b, uint32_t now_ms, bool *level);

/* Total script length in ms. */
uint32_t sfci_bounce_length_ms(void);

#endif /* SFCI_BOUNCE_H */
