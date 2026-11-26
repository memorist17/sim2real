// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <cerrno>

namespace sesame {
// Reject, rather than execute, truncated commands and trailing tokens.
struct CommandLine {
  char text[160] = {};
  char* words[12] = {};
  int count = 0;
  bool parse(const char* input) {
    count = 0;
    if (strlen(input) >= sizeof(text)) return false;
    strcpy(text, input);
    auto blank = [](char ch) { return ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n'; };
    for (char* p = text;;) {
      while (blank(*p)) ++p;
      if (!*p) break;
      if (count == 12) return false;
      if (*p == '"') { // "a quoted word" may hold spaces (Wi-Fi names); no escapes
        char* end = strchr(++p, '"');
        if (!end || (end[1] && !blank(end[1]))) return false;
        *end = 0; words[count++] = p; p = end + 1;
      } else {
        words[count++] = p;
        while (*p && !blank(*p)) ++p;
        if (*p) *p++ = 0;
      }
    }
    return count != 0;
  }
  bool is(const char* name, int size) const { return count == size && !strcmp(words[0], name); }
  bool number(int index, float& value) const {
    if (index >= count) return false;
    char* end; errno = 0;
    value = strtof(words[index], &end);
    return end != words[index] && !*end && !errno && std::isfinite(value);
  }
  bool integer(int index, int& value) const {
    float f;
    if (!number(index, f) || f < -100000000 || f > 100000000 || truncf(f) != f) return false;
    value = int(f); return true;
  }
};
} // namespace sesame
