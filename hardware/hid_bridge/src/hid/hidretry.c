#include "hidretry.h"

int hid_send_confirmed(bool (*send)(void *), void *ctx, int tries, int gap_ms, void (*wait)(int ms)) {
  for (int i = 0; i < tries; i++) {
    if (send(ctx)) return i;
    if (i + 1 < tries) wait(gap_ms);
  }
  return -1;
}

bool pace_valid(int step, int step_ms) {
  return step >= 1 && step <= 127 && step_ms >= 1 && step_ms <= 50;
}

void ratelimit_init(ratelimit *r, int max, uint32_t window_ms) {
  r->max = max;
  r->count = 0;
  r->window_ms = window_ms;
  r->window_start = 0;
  r->dropped = 0;
}

bool ratelimit_take(ratelimit *r, uint32_t now_ms) {
  if (now_ms - r->window_start >= r->window_ms) {
    r->window_start = now_ms;
    r->count = 0;
  }
  if (r->count < r->max) {
    r->count++;
    return true;
  }
  r->dropped++;
  return false;
}
