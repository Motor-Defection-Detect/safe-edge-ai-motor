#include "r300_avr_runtime.h"

int main(void)
{
    r300_avr_runtime_t runtime;
    r300_avr_runtime_init(&runtime);
    for (;;) (void)r300_avr_runtime_step(&runtime, false);
}
