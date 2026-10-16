/*
 * Minimal startup for a generic Cortex-M4F (cortex-m4 profile only).
 *
 * The vector table holds the initial stack pointer and the core exception
 * handlers; there are no device interrupts because no real part is targeted.
 * Reset_Handler initialises .data and .bss, enables the FPU (the profile
 * builds with -mfloat-abi=hard, so FP instructions may run before main),
 * runs static constructors and calls main.
 */
#include <stdint.h>

extern uint32_t _estack;
extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss;

extern void __libc_init_array(void);
extern int main(void);

void Reset_Handler(void);
void Default_Handler(void);

/* Each handler can be overridden by defining a function of the same name. */
void NMI_Handler(void) __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void MemManage_Handler(void) __attribute__((weak, alias("Default_Handler")));
void BusFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void UsageFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void) __attribute__((weak, alias("Default_Handler")));
void DebugMon_Handler(void) __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void) __attribute__((weak, alias("Default_Handler")));

typedef void (*vector_t)(void);

__attribute__((section(".isr_vector"), used))
static const vector_t vector_table[16] = {
    (vector_t)(uintptr_t)&_estack,
    Reset_Handler,
    NMI_Handler,
    HardFault_Handler,
    MemManage_Handler,
    BusFault_Handler,
    UsageFault_Handler,
    0,
    0,
    0,
    0,
    SVC_Handler,
    DebugMon_Handler,
    0,
    PendSV_Handler,
    SysTick_Handler,
};

/* Coprocessor Access Control Register (ARMv7-M architecture reference manual). */
#define SCB_CPACR (*(volatile uint32_t *)0xE000ED88u)
#define CPACR_CP10_CP11_FULL (0xFu << 20)

void Reset_Handler(void)
{
    SCB_CPACR |= CPACR_CP10_CP11_FULL;
    __asm volatile("dsb\n\tisb" ::: "memory");

    const uint32_t *src = &_sidata;
    for (uint32_t *dst = &_sdata; dst < &_edata;) {
        *dst++ = *src++;
    }
    for (uint32_t *dst = &_sbss; dst < &_ebss;) {
        *dst++ = 0u;
    }

    __libc_init_array();
    (void)main();
    for (;;) {
    }
}

void Default_Handler(void)
{
    for (;;) {
    }
}
