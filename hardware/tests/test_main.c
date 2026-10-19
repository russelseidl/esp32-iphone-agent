// Host unit tests for the board's pure-C parts: bplist codec and the GOP buffer / video chunker.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../hid_bridge/src/bplist/bplist.h"
#include "../hid_bridge/src/video/gop.h"

static int fails;
#define CHECK(c) do { if (!(c)) { fails++; printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

static void test_bplist_roundtrip(void) {
  bp_writer w;
  bp_writer_init(&w);
  int root = bp_w_dict(&w);
  bp_w_set(&w, root, "features", bp_w_uint(&w, 0x5A7FFEE6ull));
  bp_w_set(&w, root, "big", bp_w_uint(&w, 0x123456789ull));
  bp_w_set(&w, root, "name", bp_w_string(&w, "ESP32 Agent with a long name"));
  bp_w_set(&w, root, "ok", bp_w_bool(&w, true));
  bp_w_set(&w, root, "rate", bp_w_real(&w, 1.0 / 60));
  uint8_t pk[32];
  for (int i = 0; i < 32; i++) pk[i] = (uint8_t)i;
  bp_w_set(&w, root, "pk", bp_w_data(&w, pk, 32));
  int arr = bp_w_array(&w);
  bp_w_set(&w, root, "displays", arr);
  int d0 = bp_w_dict(&w);
  bp_w_append(&w, arr, d0);
  bp_w_set(&w, d0, "width", bp_w_uint(&w, 480));
  size_t len = 0;
  uint8_t *buf = bp_w_finish(&w, &len);
  bp_writer_free(&w);
  CHECK(buf != NULL);

  bp_reader r;
  int top = bp_open(&r, buf, len);
  CHECK(top >= 0 && bp_type(&r, top) == BP_DICT && bp_count(&r, top) == 7);
  uint64_t u = 0;
  CHECK(bp_get_uint(&r, bp_dict_get(&r, top, "features"), &u) && u == 0x5A7FFEE6ull);
  CHECK(bp_get_uint(&r, bp_dict_get(&r, top, "big"), &u) && u == 0x123456789ull);
  char s[64];
  CHECK(bp_get_string(&r, bp_dict_get(&r, top, "name"), s, sizeof s) && !strcmp(s, "ESP32 Agent with a long name"));
  bool b = false;
  CHECK(bp_get_bool(&r, bp_dict_get(&r, top, "ok"), &b) && b);
  double d = 0;
  CHECK(bp_get_real(&r, bp_dict_get(&r, top, "rate"), &d) && d == 1.0 / 60);
  const uint8_t *p;
  size_t n;
  CHECK(bp_get_data(&r, bp_dict_get(&r, top, "pk"), &p, &n) && n == 32 && !memcmp(p, pk, 32));
  int a = bp_dict_get(&r, top, "displays");
  CHECK(bp_type(&r, a) == BP_ARRAY && bp_count(&r, a) == 1);
  CHECK(bp_get_uint(&r, bp_dict_get(&r, bp_array_get(&r, a, 0), "width"), &u) && u == 480);
  CHECK(bp_dict_get(&r, top, "missing") < 0);
  CHECK(bp_array_get(&r, a, 5) < 0);

  // truncation and garbage never crash
  for (size_t cut = 0; cut < len; cut++) {
    bp_reader t;
    int tt = bp_open(&t, buf, cut);
    if (tt >= 0) bp_dict_get(&t, tt, "pk");
  }
  uint8_t junk[200];
  memcpy(junk, buf, len < 200 ? len : 200);
  for (int i = 8; i < 200 && (size_t)i < len - 32; i += 3) junk[i] ^= 0xA5;
  bp_reader t;
  int tt = bp_open(&t, junk, len < 200 ? len : 200);
  if (tt >= 0) { bp_dict_get(&t, tt, "pk"); bp_count(&t, tt); }
  free(buf);
}

// Hand-made plist as Apple writes it: UTF-16 string value and 1-byte refs.
static void test_bplist_utf16(void) {
  static const uint8_t pl[] = {
    'b','p','l','i','s','t','0','0',
    0xD1, 0x01, 0x02,                // dict {1: 2}         @8
    0x54, 'n','a','m','e',           // "name"              @11
    0x63, 0x00,'H', 0x20,0x19, 0x00,'s',  // u"H’s"        @16
    8, 11, 16,                       // offsets            @23
    0,0,0,0,0,0, 1, 1,
    0,0,0,0,0,0,0,3, 0,0,0,0,0,0,0,0, 0,0,0,0,0,0,0,23};
  bp_reader r;
  int top = bp_open(&r, pl, sizeof pl);
  char s[32];
  CHECK(top == 0);
  CHECK(bp_get_string(&r, bp_dict_get(&r, top, "name"), s, sizeof s) && !strcmp(s, "H\xE2\x80\x99s"));
}

static void test_gop(void) {
  static uint8_t arena[100];
  static gop_au aus[4];
  gop_buf g;
  gop_init(&g, arena, sizeof arena, aus, 4);
  uint8_t au[40] = {0, 0, 0, 1, 0x41};
  CHECK(gop_push(&g, au, 10, false, 1) == -1);  // waits for the first IDR
  CHECK(gop_push(&g, au, 30, true, 2) == 0);
  CHECK(gop_push(&g, au, 30, false, 3) == 1);
  CHECK(gop_push(&g, au, 30, false, 4) == 2 && g.used == 90);
  CHECK(gop_push(&g, au, 30, false, 5) == -1 && g.n == 0 && g.wait_idr && g.overflows == 1);
  CHECK(gop_push(&g, au, 10, false, 6) == -1);
  CHECK(gop_push(&g, au, 10, true, 7) == 0 && !g.wait_idr);

  // overflow while a streaming client is caught up: keep the AUs it has not received yet
  gop_init(&g, arena, sizeof arena, aus, 4);
  uint8_t a0[30] = {0}, a1[30] = {1}, a2[30] = {2}, a3[30] = {3};
  CHECK(gop_push(&g, a0, 30, true, 1) == 0 && gop_push(&g, a1, 30, false, 2) == 1 && gop_push(&g, a2, 30, false, 3) == 2);
  uint32_t gen = g.gen;
  gop_mark_sent(&g, gen + 1, 2);  // another generation: ignored
  CHECK(g.keep_from == 0);
  gop_mark_sent(&g, gen, 0);
  gop_mark_sent(&g, gen, 1);
  CHECK(g.keep_from == 2);
  CHECK(gop_push(&g, a3, 30, false, 4) == 1);  // a0, a1 dropped; a2 kept at 0, a3 at 1
  CHECK(g.gen == gen + 1 && g.cont && !g.wait_idr && g.n == 2 && g.used == 60 && g.keep_from == 0);
  CHECK(g.continuations == 1 && g.overflows == 0);  // a compaction is not a drop
  CHECK(g.aus[0].off == 0 && g.aus[0].ts == 3 && g.arena[0] == 2 && g.aus[1].off == 30 && g.aus[1].ts == 4 && g.arena[30] == 3);
  gop_mark_sent(&g, g.gen, 0);
  CHECK(gop_push(&g, a0, 30, false, 5) == 2 && g.n == 3 && g.used == 90);
  CHECK(gop_push(&g, a1, 30, false, 6) == 2 && g.n == 3 && g.gen == gen + 2 && g.cont);  // compacted again
  gop_mark_sent(&g, g.gen, -1);  // client gone: the next overflow drops everything
  CHECK(g.keep_from == 0);
  CHECK(gop_push(&g, a2, 30, false, 7) == -1 && g.n == 0 && g.wait_idr && !g.cont && g.overflows == 1);
  CHECK(gop_push(&g, a0, 30, true, 8) == 0 && !g.cont && !g.wait_idr);  // an IDR clears the continuation
  // a tail that is itself too big for the arena is dropped, not compacted into garbage
  gop_init(&g, arena, sizeof arena, aus, 4);
  uint8_t big[70] = {9};
  CHECK(gop_push(&g, a0, 30, true, 1) == 0 && gop_push(&g, big, 70, false, 2) == 1);
  gop_mark_sent(&g, g.gen, 0);
  CHECK(gop_push(&g, big, 70, false, 3) == -1 && g.wait_idr && g.n == 0);

  uint8_t idr[] = {0, 0, 0, 1, 0x67, 1, 0, 0, 0, 1, 0x65, 9};
  CHECK(annexb_has_idr(idr, sizeof idr));
  CHECK(!annexb_has_idr(idr, 6));

  uint8_t avcc[] = {0, 0, 0, 2, 0x65, 1, 0, 0, 0, 1, 0x41};
  uint8_t want[] = {0, 0, 0, 1, 0x65, 1, 0, 0, 0, 1, 0x41};
  CHECK(avcc_to_annexb(avcc, sizeof avcc) && !memcmp(avcc, want, sizeof want));
  uint8_t bad[] = {0, 0, 0, 9, 0x65};
  CHECK(!avcc_to_annexb(bad, sizeof bad));
}

static void test_gop_resume(void) {
  static uint8_t arena[100];
  static gop_au aus[4];
  gop_buf g;
  gop_init(&g, arena, sizeof arena, aus, 4);
  uint8_t au[30] = {0, 0, 0, 1, 0x41};
  CHECK(gop_resume_index(&g, 5) == 0);  // nothing buffered: whatever comes is a fresh start
  CHECK(gop_push(&g, au, 30, true, 10) == 0 && gop_push(&g, au, 30, false, 20) == 1 && gop_push(&g, au, 30, false, 30) == 2);
  CHECK(gop_resume_index(&g, 5) == 0);   // the client's last AU predates this GOP: full replay
  CHECK(gop_resume_index(&g, 10) == 1);  // has the IDR: continue with the P-frames
  CHECK(gop_resume_index(&g, 25) == 2);
  CHECK(gop_resume_index(&g, 30) == 3);  // has everything
  CHECK(gop_resume_index(&g, 99) == 3);
  gop_mark_sent(&g, g.gen, 1);
  CHECK(gop_push(&g, au, 30, false, 40) == 1 && g.cont && g.n == 2);  // compacted: AUs 10 and 20 are gone
  CHECK(gop_resume_index(&g, 20) == -1);  // never got AU 30, which is now the start: wait for an IDR
  CHECK(gop_resume_index(&g, 30) == 1);   // has AU 30: continue at 40
  CHECK(gop_resume_index(&g, 40) == 2);
}

static void test_chunker(void) {
  uint8_t data[100], out[64], got[100];
  for (int i = 0; i < 100; i++) data[i] = (uint8_t)i;
  vchunk c;
  vchunk_begin(&c, data, sizeof data, VF_IDR | VF_SNAP, 0xAABBCCDD);
  size_t n, got_len = 0;
  int chunks = 0;
  uint16_t seq = 65534;
  while ((n = vchunk_next(&c, out, 20, seq)) > 0) {
    CHECK(n <= VCHUNK_HDR + 20);
    CHECK(out[0] == (uint8_t)seq && out[1] == (uint8_t)(seq >> 8));
    CHECK(out[2] & VF_IDR && out[2] & VF_SNAP);
    size_t off = VCHUNK_HDR;
    if (chunks == 0) {
      CHECK(out[2] & VF_START);
      CHECK(out[3] == 0xDD && out[6] == 0xAA);
      off += 4;
    } else {
      CHECK(!(out[2] & VF_START));
    }
    memcpy(got + got_len, out + off, n - off);
    got_len += n - off;
    chunks++;
    seq++;
    if (got_len < 100) CHECK(!(out[2] & VF_END));
  }
  CHECK(out[2] & VF_END);
  CHECK(got_len == 100 && !memcmp(got, data, 100));
  CHECK(chunks == 6);  // 16 + 5 * 20 >= 100
  CHECK(seq == 4);     // wrapped

  vchunk_begin(&c, data, 3, VF_CONFIG, 1);  // tiny AU: one chunk is START and END
  n = vchunk_next(&c, out, 20, 0);
  CHECK(n == VCHUNK_HDR + 4 + 3 && (out[2] & (VF_START | VF_END | VF_CONFIG)) == (VF_START | VF_END | VF_CONFIG));
  CHECK(vchunk_next(&c, out, 20, 1) == 0);
}

#include "../hid_bridge/src/hid/hidretry.h"

static int fake_calls, fake_ok_at, fake_waits;
static bool fake_send(void *ctx) { (void)ctx; fake_calls++; return fake_calls >= fake_ok_at; }
static void fake_wait(int ms) { (void)ms; fake_waits++; }

static void test_hidretry(void) {
  fake_calls = 0; fake_ok_at = 1; fake_waits = 0;
  CHECK(hid_send_confirmed(fake_send, NULL, 5, 20, fake_wait) == 0);  // first try ok: 0 retries, no wait
  CHECK(fake_calls == 1 && fake_waits == 0);
  fake_calls = 0; fake_ok_at = 3; fake_waits = 0;
  CHECK(hid_send_confirmed(fake_send, NULL, 5, 20, fake_wait) == 2);  // ok on the 3rd try: 2 retries
  CHECK(fake_calls == 3 && fake_waits == 2);
  fake_calls = 0; fake_ok_at = 99; fake_waits = 0;
  CHECK(hid_send_confirmed(fake_send, NULL, 5, 20, fake_wait) == -1);  // never: gives up after 5
  CHECK(fake_calls == 5 && fake_waits == 4);

  CHECK(pace_valid(10, 8) && pace_valid(1, 1) && pace_valid(127, 50));
  CHECK(!pace_valid(0, 8) && !pace_valid(128, 8) && !pace_valid(10, 0) && !pace_valid(10, 51));

  ratelimit rl;
  ratelimit_init(&rl, 3, 1000);          // 3 per second
  CHECK(ratelimit_take(&rl, 0) && ratelimit_take(&rl, 10) && ratelimit_take(&rl, 20));
  CHECK(!ratelimit_take(&rl, 30) && rl.dropped == 1);
  CHECK(!ratelimit_take(&rl, 999) && rl.dropped == 2);
  CHECK(ratelimit_take(&rl, 1000) && rl.dropped == 2);  // window rolled: allowed again
}

int main(void) {
  test_bplist_roundtrip();
  test_bplist_utf16();
  test_gop();
  test_gop_resume();
  test_chunker();
  test_hidretry();
  printf(fails ? "%d FAILED\n" : "all tests passed\n", fails);
  return fails != 0;
}
