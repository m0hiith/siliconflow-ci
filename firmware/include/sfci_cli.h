/*
 * sfci_cli: the line-based UART command parser for the hardware-in-the-loop
 * test hooks (SFCI_HIL_HOOKS). Bytes are fed one at a time; a command is
 * recognised when its line ends (CR, LF or CRLF). Nothing is echoed.
 *
 * Commands:
 *   inject-bounce   run the scripted bouncy press (sfci_bounce.h)
 *
 * Kept free of SDK headers so the host unit tests can use it.
 */
#ifndef SFCI_CLI_H
#define SFCI_CLI_H

#include <stdbool.h>
#include <stddef.h>

/* Longest accepted line, without its terminator. */
#define SFCI_CLI_LINE_MAX 63u

typedef enum {
    SFCI_CLI_NONE = 0,      /* no complete line yet, or an empty one */
    SFCI_CLI_INJECT_BOUNCE,
    SFCI_CLI_UNKNOWN,       /* a line that is not a command */
    SFCI_CLI_TOO_LONG,      /* a line longer than SFCI_CLI_LINE_MAX; discarded */
} sfci_cli_cmd_t;

typedef struct {
    char line[SFCI_CLI_LINE_MAX + 1u];
    size_t len;
    bool overflow;
} sfci_cli_t;

void sfci_cli_init(sfci_cli_t *cli);

/* Feed one received byte. Returns the command its line held when c ends a line, else NONE. */
sfci_cli_cmd_t sfci_cli_feed(sfci_cli_t *cli, char c);

#endif /* SFCI_CLI_H */
