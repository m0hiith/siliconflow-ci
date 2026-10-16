#include <string.h>

#include "sfci_boot.h"
#include "unity.h"

void setUp(void) {}

void tearDown(void) {}

static void test_boot_line_is_exactly_the_contract(void)
{
    char buf[SFCI_BOOT_LINE_MAX];
    int n = sfci_boot_line(buf, sizeof buf, "v1.2.0-3-g1a2b3c4", "1a2b3c4");
    TEST_ASSERT_EQUAL_STRING("SFCI-BOOT version=v1.2.0-3-g1a2b3c4 sha=1a2b3c4\n", buf);
    TEST_ASSERT_EQUAL_INT((int)strlen(buf), n);
}

static void test_boot_line_is_one_line(void)
{
    char buf[SFCI_BOOT_LINE_MAX];
    (void)sfci_boot_line(buf, sizeof buf, "v1.2.0", "1a2b3c4");
    char *newline = strchr(buf, '\n');
    TEST_ASSERT_NOT_NULL(newline);
    TEST_ASSERT_EQUAL_CHAR('\0', newline[1]);
}

static void test_missing_version_or_sha_reads_unknown(void)
{
    char buf[SFCI_BOOT_LINE_MAX];
    (void)sfci_boot_line(buf, sizeof buf, NULL, NULL);
    TEST_ASSERT_EQUAL_STRING("SFCI-BOOT version=unknown sha=unknown\n", buf);
}

static void test_short_buffer_is_cut_and_terminated(void)
{
    char buf[12];
    int n = sfci_boot_line(buf, sizeof buf, "v1.2.0", "1a2b3c4");
    TEST_ASSERT_EQUAL_STRING("SFCI-BOOT v", buf);
    TEST_ASSERT_GREATER_OR_EQUAL_INT((int)sizeof buf, n);
    TEST_ASSERT_EQUAL_INT((int)strlen("SFCI-BOOT version=v1.2.0 sha=1a2b3c4\n"), n);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_boot_line_is_exactly_the_contract);
    RUN_TEST(test_boot_line_is_one_line);
    RUN_TEST(test_missing_version_or_sha_reads_unknown);
    RUN_TEST(test_short_buffer_is_cut_and_terminated);
    return UNITY_END();
}
