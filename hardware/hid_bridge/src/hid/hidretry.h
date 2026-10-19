// Small pure-C helpers for the HID path and the board log, so they can be unit-tested on the host.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Calls send(ctx) until it returns true, waiting wait(gap_ms) between tries, at most `tries` times.
// Returns the number of retries (0 = first try succeeded), or -1 if every try failed.
int hid_send_confirmed(bool (*send)(void *), void *ctx, int tries, int gap_ms, void (*wait)(int ms));

// Pointer move pacing: step 1-127 units per report, 1-50 ms between reports.
bool pace_valid(int step, int step_ms);

// Token bucket: at most `max` events per `window_ms`; `dropped` counts refusals.
typedef struct {
  int max, count;
  uint32_t window_ms, window_start;
  uint32_t dropped;
} ratelimit;

void ratelimit_init(ratelimit *r, int max, uint32_t window_ms);
bool ratelimit_take(ratelimit *r, uint32_t now_ms);

#ifdef __cplusplus
}
#endif
