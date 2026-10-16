#include "sensor_logic.h"
#include "unity.h"

#define STABLE_MS 20u

static sl_debounce_t db;

void setUp(void)
{
    sl_debounce_init(&db, STABLE_MS, false, 0u);
}

void tearDown(void) {}

/* Hold `level` from t0 to t1 inclusive, 1 ms per sample; count events seen. */
static void hold(bool level, uint32_t t0, uint32_t t1, int *pressed, int *released)
{
    for (uint32_t t = t0; t <= t1; t++) {
        sl_button_event_t ev = sl_debounce_update(&db, level, t);
        if (ev == SL_BUTTON_PRESSED) {
            (*pressed)++;
        } else if (ev == SL_BUTTON_RELEASED) {
            (*released)++;
        }
    }
}

static void test_initial_level_emits_nothing(void)
{
    TEST_ASSERT_EQUAL(SL_BUTTON_NONE, sl_debounce_update(&db, false, 0u));
    TEST_ASSERT_EQUAL(SL_BUTTON_NONE, sl_debounce_update(&db, false, 1000u));
    TEST_ASSERT_FALSE(sl_debounce_level(&db));
}

static void test_event_fires_exactly_at_stable_time(void)
{
    TEST_ASSERT_EQUAL(SL_BUTTON_NONE, sl_debounce_update(&db, true, 100u));
    TEST_ASSERT_EQUAL(SL_BUTTON_NONE, sl_debounce_update(&db, true, 100u + STABLE_MS - 1u));
    TEST_ASSERT_EQUAL(SL_BUTTON_PRESSED, sl_debounce_update(&db, true, 100u + STABLE_MS));
    TEST_ASSERT_TRUE(sl_debounce_level(&db));
}

static void test_event_fires_only_once(void)
{
    int pressed = 0, released = 0;
    hold(true, 100u, 300u, &pressed, &released);
    TEST_ASSERT_EQUAL_INT(1, pressed);
    TEST_ASSERT_EQUAL_INT(0, released);
}

static void test_release_after_stable_time(void)
{
    (void)sl_debounce_update(&db, true, 0u);
    TEST_ASSERT_EQUAL(SL_BUTTON_PRESSED, sl_debounce_update(&db, true, STABLE_MS));

    TEST_ASSERT_EQUAL(SL_BUTTON_NONE, sl_debounce_update(&db, false, 500u));
    TEST_ASSERT_EQUAL(SL_BUTTON_NONE, sl_debounce_update(&db, false, 500u + STABLE_MS - 1u));
    TEST_ASSERT_EQUAL(SL_BUTTON_RELEASED, sl_debounce_update(&db, false, 500u + STABLE_MS));
    TEST_ASSERT_FALSE(sl_debounce_level(&db));
}

static void test_bounce_shorter_than_stable_time_is_ignored(void)
{
    /* Glitches of 1..(STABLE_MS-1) ms with returns to the stable level between. */
    uint32_t t = 0u;
    for (uint32_t width = 1u; width < STABLE_MS; width++) {
        int pressed = 0, released = 0;
        hold(true, t, t + width - 1u, &pressed, &released);
        t += width;
        hold(false, t, t + 5u, &pressed, &released);
        t += 6u;
        TEST_ASSERT_EQUAL_INT_MESSAGE(0, pressed + released, "glitch produced an event");
    }
    TEST_ASSERT_FALSE(sl_debounce_level(&db));
}

static void test_bouncy_press_yields_single_event(void)
{
    /* 1 ms toggles for 10 ms, then settle pressed; then bouncy release. */
    int pressed = 0, released = 0;
    for (uint32_t t = 0u; t < 10u; t++) {
        hold((t % 2u) == 0u, t, t, &pressed, &released);
    }
    hold(true, 10u, 200u, &pressed, &released);
    for (uint32_t t = 201u; t < 211u; t++) {
        hold((t % 2u) == 0u, t, t, &pressed, &released);
    }
    hold(false, 211u, 400u, &pressed, &released);

    TEST_ASSERT_EQUAL_INT(1, pressed);
    TEST_ASSERT_EQUAL_INT(1, released);
}

static void test_bounce_restarts_timer(void)
{
    /* Pressed for STABLE_MS-1, one-sample dropout, then pressed again: the
     * timer restarts at the second rising edge. */
    (void)sl_debounce_update(&db, true, 0u);
    (void)sl_debounce_update(&db, true, STABLE_MS - 1u);
    (void)sl_debounce_update(&db, false, STABLE_MS);
    TEST_ASSERT_EQUAL(SL_BUTTON_NONE, sl_debounce_update(&db, true, STABLE_MS + 1u));
    TEST_ASSERT_EQUAL(SL_BUTTON_NONE, sl_debounce_update(&db, true, 2u * STABLE_MS));
    TEST_ASSERT_EQUAL(SL_BUTTON_PRESSED, sl_debounce_update(&db, true, 2u * STABLE_MS + 1u));
}

static void test_counter_wraparound(void)
{
    uint32_t start = UINT32_MAX - 5u;
    sl_debounce_init(&db, STABLE_MS, false, start);
    TEST_ASSERT_EQUAL(SL_BUTTON_NONE, sl_debounce_update(&db, true, start));
    TEST_ASSERT_EQUAL(SL_BUTTON_NONE, sl_debounce_update(&db, true, start + STABLE_MS - 1u));
    TEST_ASSERT_EQUAL(SL_BUTTON_PRESSED, sl_debounce_update(&db, true, start + STABLE_MS));
}

static void test_zero_stable_time_accepts_immediately(void)
{
    sl_debounce_init(&db, 0u, false, 0u);
    TEST_ASSERT_EQUAL(SL_BUTTON_PRESSED, sl_debounce_update(&db, true, 5u));
    TEST_ASSERT_EQUAL(SL_BUTTON_RELEASED, sl_debounce_update(&db, false, 6u));
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_initial_level_emits_nothing);
    RUN_TEST(test_event_fires_exactly_at_stable_time);
    RUN_TEST(test_event_fires_only_once);
    RUN_TEST(test_release_after_stable_time);
    RUN_TEST(test_bounce_shorter_than_stable_time_is_ignored);
    RUN_TEST(test_bouncy_press_yields_single_event);
    RUN_TEST(test_bounce_restarts_timer);
    RUN_TEST(test_counter_wraparound);
    RUN_TEST(test_zero_stable_time_accepts_immediately);
    return UNITY_END();
}
