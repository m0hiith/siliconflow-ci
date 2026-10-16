/*
 * The boot line ci/hil/boot_check.py waits for on the UART after a reset:
 *
 *   SFCI-BOOT version=<FW_VERSION> sha=<FW_COMMIT_SHORT>
 *
 * Kept free of SDK headers so the host unit tests
 * (firmware/tests/test_sfci_boot.c) can include it.
 */
#ifndef SFCI_BOOT_H
#define SFCI_BOOT_H

#include <stddef.h>
#include <stdio.h>

/* Room for a long `git describe` and the short sha. */
#define SFCI_BOOT_LINE_MAX 160u

/*
 * Write the boot line, newline-terminated, into buf. Returns the length the
 * full line needs, as snprintf does: a result >= size means it was cut short.
 * A NULL version or sha is written as "unknown".
 */
static inline int sfci_boot_line(char *buf, size_t size, const char *version, const char *sha)
{
    return snprintf(buf, size, "SFCI-BOOT version=%s sha=%s\n", version ? version : "unknown",
                    sha ? sha : "unknown");
}

#endif /* SFCI_BOOT_H */
