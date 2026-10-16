#include "sensor_logic.h"
#include "unity.h"

#define LOW 300
#define HIGH 700

static sl_thresh_t t;

void setUp(void)
{
    TEST_ASSERT_TRUE(sl_thresh_init(&t, LOW, HIGH));
}

void tearDown(void) {}

static void test_init_rejects_inverted_or_equal_thresholds(void)
{
    sl_thresh_t bad;
    TEST_ASSERT_FALSE(sl_thresh_init(&bad, 500, 500));
    TEST_ASSERT_FALSE(sl_thresh_init(&bad, 700, 300));
}

static void test_starts_low(void)
{
    TEST_ASSERT_FALSE(sl_thresh_is_high(&t));
    TEST_ASSERT_EQUAL(SL_THRESH_NONE, sl_thresh_update(&t, 0));
}

static void test_rising_exactly_at_high(void)
{
    TEST_ASSERT_EQUAL(SL_THRESH_NONE, sl_thresh_update(&t, HIGH - 1));
    TEST_ASSERT_FALSE(sl_thresh_is_high(&t));
    TEST_ASSERT_EQUAL(SL_THRESH_RISING, sl_thresh_update(&t, HIGH));
    TEST_ASSERT_TRUE(sl_thresh_is_high(&t));
}

static void test_falling_exactly_at_low(void)
{
    TEST_ASSERT_EQUAL(SL_THRESH_RISING, sl_thresh_update(&t, HIGH));
    TEST_ASSERT_EQUAL(SL_THRESH_NONE, sl_thresh_update(&t, LOW + 1));
    TEST_ASSERT_TRUE(sl_thresh_is_high(&t));
    TEST_ASSERT_EQUAL(SL_THRESH_FALLING, sl_thresh_update(&t, LOW));
    TEST_ASSERT_FALSE(sl_thresh_is_high(&t));
}

static void test_no_chatter_inside_band_while_low(void)
{
    /* Coming from low: values in (LOW, HIGH) never trigger. */
    for (int32_t v = LOW - 50; v < HIGH; v += 7) {
        TEST_ASSERT_EQUAL(SL_THRESH_NONE, sl_thresh_update(&t, v));
    }
    TEST_ASSERT_FALSE(sl_thresh_is_high(&t));
}

static void test_no_chatter_inside_band_while_high(void)
{
    /* Coming from high: values in (LOW, HIGH) never release. */
    TEST_ASSERT_EQUAL(SL_THRESH_RISING, sl_thresh_update(&t, HIGH + 100));
    for (int32_t v = HIGH + 50; v > LOW; v -= 7) {
        TEST_ASSERT_EQUAL(SL_THRESH_NONE, sl_thresh_update(&t, v));
    }
    TEST_ASSERT_TRUE(sl_thresh_is_high(&t));
}

static void test_noisy_signal_around_one_threshold(void)
{
    /* Oscillating +/-20 around HIGH: one RISING, never FALLING. */
    int rising = 0, falling = 0;
    for (int i = 0; i < 100; i++) {
        int32_t v = HIGH + ((i % 2 == 0) ? 20 : -20);
        sl_thresh_event_t ev = sl_thresh_update(&t, v);
        rising += (ev == SL_THRESH_RISING);
        falling += (ev == SL_THRESH_FALLING);
    }
    TEST_ASSERT_EQUAL_INT(1, rising);
    TEST_ASSERT_EQUAL_INT(0, falling);
}

static void test_full_cycle_events_alternate(void)
{
    TEST_ASSERT_EQUAL(SL_THRESH_RISING, sl_thresh_update(&t, 1000));
    TEST_ASSERT_EQUAL(SL_THRESH_NONE, sl_thresh_update(&t, 1000));
    TEST_ASSERT_EQUAL(SL_THRESH_FALLING, sl_thresh_update(&t, 0));
    TEST_ASSERT_EQUAL(SL_THRESH_NONE, sl_thresh_update(&t, 0));
    TEST_ASSERT_EQUAL(SL_THRESH_RISING, sl_thresh_update(&t, 1000));
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_init_rejects_inverted_or_equal_thresholds);
    RUN_TEST(test_starts_low);
    RUN_TEST(test_rising_exactly_at_high);
    RUN_TEST(test_falling_exactly_at_low);
    RUN_TEST(test_no_chatter_inside_band_while_low);
    RUN_TEST(test_no_chatter_inside_band_while_high);
    RUN_TEST(test_noisy_signal_around_one_threshold);
    RUN_TEST(test_full_cycle_events_alternate);
    return UNITY_END();
}
