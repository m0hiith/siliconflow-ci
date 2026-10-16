#include "sfci_cli.h"

#include <string.h>

void sfci_cli_init(sfci_cli_t *cli)
{
    cli->len = 0u;
    cli->overflow = false;
    cli->line[0] = '\0';
}

static sfci_cli_cmd_t parse(const char *line)
{
    if (strcmp(line, "inject-bounce") == 0) {
        return SFCI_CLI_INJECT_BOUNCE;
    }
    return SFCI_CLI_UNKNOWN;
}

sfci_cli_cmd_t sfci_cli_feed(sfci_cli_t *cli, char c)
{
    if (c != '\r' && c != '\n') {
        if (cli->len < SFCI_CLI_LINE_MAX) {
            cli->line[cli->len++] = c;
        } else {
            cli->overflow = true;
        }
        return SFCI_CLI_NONE;
    }

    /* End of line. The LF of a CRLF ends an empty line, which is ignored. */
    sfci_cli_cmd_t cmd;
    if (cli->overflow) {
        cmd = SFCI_CLI_TOO_LONG;
    } else if (cli->len == 0u) {
        cmd = SFCI_CLI_NONE;
    } else {
        cli->line[cli->len] = '\0';
        cmd = parse(cli->line);
    }
    sfci_cli_init(cli);
    return cmd;
}
