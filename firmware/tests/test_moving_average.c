#include "sensor_logic.h"
#include "unity.h"

static sl_mavg_t f;

void setUp(void) {}

void tearDown(void) {}

static void test_init_rejects_invalid_window(void)
{
    TEST_ASSERT_FALSE(sl_mavg_init(&f, 0u));
    TEST_ASSERT_FALSE(sl_mavg_init(&f, SL_MAVG_MAX_WINDOW + 1u));
    TEST_ASSERT_TRUE(sl_mavg_init(&f, 1u));
    TEST_ASSERT_TRUE(sl_mavg_init(&f, SL_MAVG_MAX_WINDOW));
}

static void test_warmup_averages_samples_seen_so_far(void)
{
    TEST_ASSERT_TRUE(sl_mavg_init(&f, 4u));
    TEST_ASSERT_FALSE(sl_mavg_ready(&f));
    TEST_ASSERT_EQUAL_INT32(100, sl_mavg_update(&f, 100)); /* 100/1 */
    TEST_ASSERT_FALSE(sl_mavg_ready(&f));
    TEST_ASSERT_EQUAL_INT32(150, sl_mavg_update(&f, 200)); /* 300/2 */
    TEST_ASSERT_FALSE(sl_mavg_ready(&f));
    TEST_ASSERT_EQUAL_INT32(200, sl_mavg_update(&f, 300)); /* 600/3 */
    TEST_ASSERT_FALSE(sl_mavg_ready(&f));
    TEST_ASSERT_EQUAL_INT32(250, sl_mavg_update(&f, 400)); /* 1000/4 */
    TEST_ASSERT_TRUE(sl_mavg_ready(&f));
}

static void test_window_slides_after_warmup(void)
{
    TEST_ASSERT_TRUE(sl_mavg_init(&f, 4u));
    for (int32_t v = 1; v <= 4; v++) {
        (void)sl_mavg_update(&f, v * 100);
    }
    TEST_ASSERT_EQUAL_INT32(350, sl_mavg_update(&f, 500)); /* 200..500 */
    TEST_ASSERT_EQUAL_INT32(450, sl_mavg_update(&f, 600)); /* 300..600 */
    TEST_ASSERT_TRUE(sl_mavg_ready(&f));
}

static void test_constant_input_is_unchanged(void)
{
    TEST_ASSERT_TRUE(sl_mavg_init(&f, 8u));
    for (int i = 0; i < 50; i++) {
        TEST_ASSERT_EQUAL_INT32(-42, sl_mavg_update(&f, -42));
    }
}

static void test_window_of_one_is_passthrough(void)
{
    TEST_ASSERT_TRUE(sl_mavg_init(&f, 1u));
    TEST_ASSERT_EQUAL_INT32(7, sl_mavg_update(&f, 7));
    TEST_ASSERT_TRUE(sl_mavg_ready(&f));
    TEST_ASSERT_EQUAL_INT32(-3, sl_mavg_update(&f, -3));
}

static void test_division_truncates_toward_zero(void)
{
    TEST_ASSERT_TRUE(sl_mavg_init(&f, 2u));
    (void)sl_mavg_update(&f, 1);
    TEST_ASSERT_EQUAL_INT32(0, sl_mavg_update(&f, 0));   /* 1/2 */
    TEST_ASSERT_TRUE(sl_mavg_init(&f, 2u));
    (void)sl_mavg_update(&f, -1);
    TEST_ASSERT_EQUAL_INT32(0, sl_mavg_update(&f, 0));   /* -1/2 */
}

static void test_extreme_values_do_not_overflow(void)
{
    TEST_ASSERT_TRUE(sl_mavg_init(&f, SL_MAVG_MAX_WINDOW));
    for (unsigned i = 0; i < 3u * SL_MAVG_MAX_WINDOW; i++) {
        TEST_ASSERT_EQUAL_INT32(INT32_MAX, sl_mavg_update(&f, INT32_MAX));
    }
    for (unsigned i = 0; i < SL_MAVG_MAX_WINDOW; i++) {
        (void)sl_mavg_update(&f, INT32_MIN);
    }
    TEST_ASSERT_EQUAL_INT32(INT32_MIN, sl_mavg_update(&f, INT32_MIN));
}

static void test_reinit_clears_history(void)
{
    TEST_ASSERT_TRUE(sl_mavg_init(&f, 4u));
    for (int i = 0; i < 4; i++) {
        (void)sl_mavg_update(&f, 1000);
    }
    TEST_ASSERT_TRUE(sl_mavg_init(&f, 4u));
    TEST_ASSERT_FALSE(sl_mavg_ready(&f));
    TEST_ASSERT_EQUAL_INT32(10, sl_mavg_update(&f, 10));
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_init_rejects_invalid_window);
    RUN_TEST(test_warmup_averages_samples_seen_so_far);
    RUN_TEST(test_window_slides_after_warmup);
    RUN_TEST(test_constant_input_is_unchanged);
    RUN_TEST(test_window_of_one_is_passthrough);
    RUN_TEST(test_division_truncates_toward_zero);
    RUN_TEST(test_extreme_values_do_not_overflow);
    RUN_TEST(test_reinit_clears_history);
    return UNITY_END();
}
