#include <stdint.h>

#include "sensor_logic.h"
#include "sfci_bounce.h"
#include "unity.h"

/* firmware/siwx917/app.c's debounce time. */
#define DEBOUNCE_MS 20u

static sfci_bounce_t b;

void setUp(void)
{
    sfci_bounce_init(&b);
}

void tearDown(void) {}

typedef struct {
    int pressed;
    int released;
    uint32_t pressed_at;  /* ms after start */
    uint32_t released_at;
    uint32_t ended_at;    /* first sample at which the script had ended */
} run_t;

/* Run the script from `start`, sampling every `step` ms through a real debouncer. */
static run_t run_through_debouncer(uint32_t start, uint32_t step)
{
    run_t r = {0};
    sl_debounce_t db;
    sl_debounce_init(&db, DEBOUNCE_MS, false, start);
    TEST_ASSERT_TRUE(sfci_bounce_start(&b, start));

    for (uint32_t dt = 0;; dt += step) {
        bool level;
        if (!sfci_bounce_level(&b, start + dt, &level)) {
            r.ended_at = dt;
            break;
        }
        switch (sl_debounce_update(&db, level, start + dt)) {
        case SL_BUTTON_PRESSED:
            r.pressed++;
            r.pressed_at = dt;
            break;
        case SL_BUTTON_RELEASED:
            r.released++;
            r.released_at = dt;
            break;
        case SL_BUTTON_NONE:
            break;
        }
        TEST_ASSERT_LESS_THAN_UINT32(10000u, dt); /* the script must end */
    }
    TEST_ASSERT_FALSE(sl_debounce_level(&db));
    return r;
}

static void test_not_started_drives_nothing(void)
{
    bool level = true;
    TEST_ASSERT_FALSE(sfci_bounce_level(&b, 0u, &level));
    TEST_ASSERT_TRUE(level); /* untouched */
}

static void test_script_length(void)
{
    /* 10 ms press bounce + hold + 24 ms release bounce + settle */
    TEST_ASSERT_EQUAL_UINT32(10u + SFCI_BOUNCE_HOLD_MS + 24u + SFCI_BOUNCE_SETTLE_MS, sfci_bounce_length_ms());
}

static void test_levels_at_the_edges(void)
{
    bool level;
    TEST_ASSERT_TRUE(sfci_bounce_start(&b, 1000u));
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 1000u, &level));
    TEST_ASSERT_TRUE(level); /* first contact */
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 1001u, &level));
    TEST_ASSERT_FALSE(level); /* first bounce */
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 1010u, &level));
    TEST_ASSERT_TRUE(level); /* hold starts */
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 1209u, &level));
    TEST_ASSERT_TRUE(level); /* last ms of hold */
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 1210u, &level));
    TEST_ASSERT_FALSE(level); /* release bounce starts */
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 1219u, &level));
    TEST_ASSERT_TRUE(level); /* the long re-contact */
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 1233u, &level));
    TEST_ASSERT_TRUE(level);
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 1234u, &level));
    TEST_ASSERT_FALSE(level); /* settled released */
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 1000u + sfci_bounce_length_ms() - 1u, &level));
    TEST_ASSERT_FALSE(level);
    TEST_ASSERT_FALSE(sfci_bounce_level(&b, 1000u + sfci_bounce_length_ms(), &level));
}

static void test_ends_and_stays_ended(void)
{
    bool level;
    TEST_ASSERT_TRUE(sfci_bounce_start(&b, 0u));
    TEST_ASSERT_FALSE(sfci_bounce_level(&b, sfci_bounce_length_ms(), &level));
    TEST_ASSERT_FALSE(b.active);
    /* Even when the counter later comes back round to the script's window. */
    TEST_ASSERT_FALSE(sfci_bounce_level(&b, 5u, &level));
}

static void test_start_while_running_is_refused(void)
{
    bool level;
    TEST_ASSERT_TRUE(sfci_bounce_start(&b, 100u));
    TEST_ASSERT_FALSE(sfci_bounce_start(&b, 150u));
    /* Still timed from the first start: 240 ms in is settled (released);
     * timed from the refused one it would be 190 ms in, the hold (pressed). */
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 340u, &level));
    TEST_ASSERT_FALSE(level);
}

static void test_can_run_again_after_it_ends(void)
{
    bool level;
    TEST_ASSERT_TRUE(sfci_bounce_start(&b, 0u));
    TEST_ASSERT_FALSE(sfci_bounce_level(&b, sfci_bounce_length_ms(), &level));
    TEST_ASSERT_TRUE(sfci_bounce_start(&b, 5000u));
    TEST_ASSERT_TRUE(sfci_bounce_level(&b, 5000u, &level));
}

static void test_glitches_are_shorter_than_a_debounce(void)
{
    TEST_ASSERT_LESS_THAN_UINT32(DEBOUNCE_MS, SFCI_BOUNCE_MAX_GLITCH_MS);
    TEST_ASSERT_GREATER_OR_EQUAL_UINT32(DEBOUNCE_MS, SFCI_BOUNCE_SETTLE_MS);
}

static void test_debouncer_sees_one_press_and_one_release(void)
{
    run_t r = run_through_debouncer(0u, 1u);
    TEST_ASSERT_EQUAL_INT(1, r.pressed);
    TEST_ASSERT_EQUAL_INT(1, r.released);
    /* Hold starts at 10 ms; the last re-contact ends at 234 ms. */
    TEST_ASSERT_EQUAL_UINT32(10u + DEBOUNCE_MS, r.pressed_at);
    TEST_ASSERT_EQUAL_UINT32(234u + DEBOUNCE_MS, r.released_at);
    TEST_ASSERT_LESS_THAN_UINT32(r.ended_at, r.released_at);
}

static void test_one_pair_at_any_loop_rate(void)
{
    /* The target's main loop may run slower than 1 kHz while it prints. */
    for (uint32_t step = 1u; step <= 9u; step++) {
        sfci_bounce_init(&b);
        run_t r = run_through_debouncer(0u, step);
        TEST_ASSERT_EQUAL_INT_MESSAGE(1, r.pressed, "pressed");
        TEST_ASSERT_EQUAL_INT_MESSAGE(1, r.released, "released");
        TEST_ASSERT_LESS_THAN_UINT32(r.released_at, r.pressed_at);
    }
}

static void test_one_pair_across_a_counter_wrap(void)
{
    run_t r = run_through_debouncer(UINT32_MAX - 100u, 1u);
    TEST_ASSERT_EQUAL_INT(1, r.pressed);
    TEST_ASSERT_EQUAL_INT(1, r.released);
    TEST_ASSERT_EQUAL_UINT32(234u + DEBOUNCE_MS, r.released_at);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_not_started_drives_nothing);
    RUN_TEST(test_script_length);
    RUN_TEST(test_levels_at_the_edges);
    RUN_TEST(test_ends_and_stays_ended);
    RUN_TEST(test_start_while_running_is_refused);
    RUN_TEST(test_can_run_again_after_it_ends);
    RUN_TEST(test_glitches_are_shorter_than_a_debounce);
    RUN_TEST(test_debouncer_sees_one_press_and_one_release);
    RUN_TEST(test_one_pair_at_any_loop_rate);
    RUN_TEST(test_one_pair_across_a_counter_wrap);
    return UNITY_END();
}
