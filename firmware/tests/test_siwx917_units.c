#include <math.h>
#include <stdint.h>

#include "hal_siwx917_units.h"
#include "unity.h"

void setUp(void) {}

void tearDown(void) {}

static void test_celsius_becomes_tenths_rounded_half_away_from_zero(void)
{
    TEST_ASSERT_EQUAL_INT32(235, hal_siwx917_deci_celsius(23.46));
    TEST_ASSERT_EQUAL_INT32(234, hal_siwx917_deci_celsius(23.44));
    TEST_ASSERT_EQUAL_INT32(0, hal_siwx917_deci_celsius(0.0));
    TEST_ASSERT_EQUAL_INT32(-235, hal_siwx917_deci_celsius(-23.46));
    TEST_ASSERT_EQUAL_INT32(-234, hal_siwx917_deci_celsius(-23.44));
    TEST_ASSERT_EQUAL_INT32(3, hal_siwx917_deci_celsius(0.25));
    TEST_ASSERT_EQUAL_INT32(-3, hal_siwx917_deci_celsius(-0.25));
}

static void test_thresholds_in_app_are_whole_degrees(void)
{
    TEST_ASSERT_EQUAL_INT32(300, hal_siwx917_deci_celsius(30.0));
    TEST_ASSERT_EQUAL_INT32(700, hal_siwx917_deci_celsius(70.0));
}

static void test_out_of_range_saturates_and_nan_is_zero(void)
{
    TEST_ASSERT_EQUAL_INT32(INT32_MAX, hal_siwx917_deci_celsius(1e12));
    TEST_ASSERT_EQUAL_INT32(INT32_MIN, hal_siwx917_deci_celsius(-1e12));
    TEST_ASSERT_EQUAL_INT32(INT32_MAX, hal_siwx917_deci_celsius(INFINITY));
    TEST_ASSERT_EQUAL_INT32(INT32_MIN, hal_siwx917_deci_celsius(-INFINITY));
    TEST_ASSERT_EQUAL_INT32(0, hal_siwx917_deci_celsius(NAN));
    /* Just inside the range still rounds instead of saturating. */
    TEST_ASSERT_EQUAL_INT32(INT32_MAX, hal_siwx917_deci_celsius(214748364.69));
    TEST_ASSERT_EQUAL_INT32(INT32_MIN, hal_siwx917_deci_celsius(-214748364.79));
}

static void test_ms_wraps_at_uint32_max(void)
{
    TEST_ASSERT_EQUAL_UINT32(0u, hal_siwx917_ms32(0u));
    TEST_ASSERT_EQUAL_UINT32(UINT32_MAX, hal_siwx917_ms32(UINT32_MAX));
    TEST_ASSERT_EQUAL_UINT32(0u, hal_siwx917_ms32((uint64_t)UINT32_MAX + 1u));
    TEST_ASSERT_EQUAL_UINT32(41u, hal_siwx917_ms32(((uint64_t)3 << 32) + 41u));
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_celsius_becomes_tenths_rounded_half_away_from_zero);
    RUN_TEST(test_thresholds_in_app_are_whole_degrees);
    RUN_TEST(test_out_of_range_saturates_and_nan_is_zero);
    RUN_TEST(test_ms_wraps_at_uint32_max);
    return UNITY_END();
}
