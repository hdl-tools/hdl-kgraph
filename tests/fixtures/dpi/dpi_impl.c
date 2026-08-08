/* M8 DPI-C boundary fixture: C implementations of dpi_top.sv's imports. */
#include "svdpi.h"

int my_add(int a, int b) {
    return a + b;
}

/* Linkage name for the aliased import `sv_mult`. */
int c_mult(int a, int b) {
    return a * b;
}

/* Imported as an SV task. */
void my_task(int x) {
    (void)x;
}

/* A prototype-only declaration (no body) — still a resolvable FUNCTION node. */
int helper_proto(int x);
