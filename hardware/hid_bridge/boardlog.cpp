#include "boardlog.h"
#include <stdarg.h>
#include "ble_link.h"
#include "src/hid/hidretry.h"

#ifndef DEBUG_SERIAL
#define DEBUG_SERIAL 0
#endif

static ratelimit limiter;
static bool inited;
static uint32_t reported;  // drops already mentioned in a line

static void emit(const char *msg) {
  if (!inited) {
    ratelimit_init(&limiter, LOG_MAX_PER_S, 1000);
    inited = true;
  }
  if (!ratelimit_take(&limiter, millis())) return;
  char line[200];
  uint32_t unreported = limiter.dropped - reported;
  if (unreported) {
    snprintf(line, sizeof line, "%s (+%lu dropped)", msg, (unsigned long)unreported);
    reported = limiter.dropped;
    msg = line;
  }
#if LOG_BLE
  ble_reply(String("log ") + msg);
#endif
#if DEBUG_SERIAL
  Serial.printf("[%lu] %s\n", millis(), msg);
#endif
}

void boardLog(const char *fmt, ...) {
  char buf[160];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof buf, fmt, ap);
  va_end(ap);
  emit(buf);
}

void boardLogLine(const char *msg) { emit(msg); }

uint32_t boardLogDropped() { return inited ? limiter.dropped : 0; }
