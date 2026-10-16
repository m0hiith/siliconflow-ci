#include <string.h>

#include "sfci_cli.h"
#include "unity.h"

static sfci_cli_t cli;

void setUp(void)
{
    sfci_cli_init(&cli);
}

void tearDown(void) {}

/* Feed a whole string; return the last non-NONE command seen (or NONE). */
static sfci_cli_cmd_t feed(const char *s)
{
    sfci_cli_cmd_t last = SFCI_CLI_NONE;
    for (; *s; s++) {
        sfci_cli_cmd_t cmd = sfci_cli_feed(&cli, *s);
        if (cmd != SFCI_CLI_NONE) {
            last = cmd;
        }
    }
    return last;
}

static void test_command_is_recognised_only_at_end_of_line(void)
{
    const char *s = "inject-bounce";
    for (; *s; s++) {
        TEST_ASSERT_EQUAL(SFCI_CLI_NONE, sfci_cli_feed(&cli, *s));
    }
    TEST_ASSERT_EQUAL(SFCI_CLI_INJECT_BOUNCE, sfci_cli_feed(&cli, '\n'));
}

static void test_any_line_ending_works(void)
{
    TEST_ASSERT_EQUAL(SFCI_CLI_INJECT_BOUNCE, feed("inject-bounce\n"));
    TEST_ASSERT_EQUAL(SFCI_CLI_INJECT_BOUNCE, feed("inject-bounce\r"));
    TEST_ASSERT_EQUAL(SFCI_CLI_INJECT_BOUNCE, feed("inject-bounce\r\n"));
}

static void test_crlf_gives_one_command(void)
{
    int n = 0;
    for (const char *s = "inject-bounce\r\n"; *s; s++) {
        n += sfci_cli_feed(&cli, *s) != SFCI_CLI_NONE;
    }
    TEST_ASSERT_EQUAL_INT(1, n);
}

static void test_empty_lines_are_ignored(void)
{
    TEST_ASSERT_EQUAL(SFCI_CLI_NONE, feed("\n\r\n\r"));
}

static void test_other_lines_are_unknown(void)
{
    TEST_ASSERT_EQUAL(SFCI_CLI_UNKNOWN, feed("help\n"));
    TEST_ASSERT_EQUAL(SFCI_CLI_UNKNOWN, feed("inject-bounce \n")); /* exact match only */
    TEST_ASSERT_EQUAL(SFCI_CLI_UNKNOWN, feed("INJECT-BOUNCE\n"));
    TEST_ASSERT_EQUAL(SFCI_CLI_UNKNOWN, feed("inject\n"));
}

static void test_state_resets_between_lines(void)
{
    TEST_ASSERT_EQUAL(SFCI_CLI_UNKNOWN, feed("garbage\n"));
    TEST_ASSERT_EQUAL(SFCI_CLI_INJECT_BOUNCE, feed("inject-bounce\n"));
}

static void test_longest_line_fits(void)
{
    char line[SFCI_CLI_LINE_MAX + 2u];
    memset(line, 'x', SFCI_CLI_LINE_MAX);
    line[SFCI_CLI_LINE_MAX] = '\n';
    line[SFCI_CLI_LINE_MAX + 1u] = '\0';
    TEST_ASSERT_EQUAL(SFCI_CLI_UNKNOWN, feed(line));
}

static void test_overlong_line_is_discarded_whole(void)
{
    char line[SFCI_CLI_LINE_MAX + 16u];
    memset(line, 'x', sizeof line - 2u);
    /* A command hidden at the end must not run. */
    memcpy(line + sizeof line - 2u - strlen("inject-bounce"), "inject-bounce", strlen("inject-bounce"));
    line[sizeof line - 2u] = '\n';
    line[sizeof line - 1u] = '\0';
    TEST_ASSERT_EQUAL(SFCI_CLI_TOO_LONG, feed(line));
    TEST_ASSERT_EQUAL(SFCI_CLI_INJECT_BOUNCE, feed("inject-bounce\n"));
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_command_is_recognised_only_at_end_of_line);
    RUN_TEST(test_any_line_ending_works);
    RUN_TEST(test_crlf_gives_one_command);
    RUN_TEST(test_empty_lines_are_ignored);
    RUN_TEST(test_other_lines_are_unknown);
    RUN_TEST(test_state_resets_between_lines);
    RUN_TEST(test_longest_line_fits);
    RUN_TEST(test_overlong_line_is_discarded_whole);
    return UNITY_END();
}
