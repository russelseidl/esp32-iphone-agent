// Board log: one line per event. With -DLOG_BLE=1 lines go to every BLE peer as `log <text>` on the
// reply characteristic (the Mac server writes them to its telemetry); with -DDEBUG_SERIAL=1 they also go
// to USB serial. Rate-limited to LOG_MAX_PER_S lines per second; the number dropped is appended to the next
// line that gets through, as " (+N dropped)".
#pragma once
#include <Arduino.h>

#ifndef LOG_BLE
#define LOG_BLE 0
#endif
const int LOG_MAX_PER_S = 20;

void boardLog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void boardLogLine(const char *msg);   // the airplay_cfg.log callback shape
uint32_t boardLogDropped();
