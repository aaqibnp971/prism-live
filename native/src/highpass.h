// highpass.h: the 62 Hz high-pass on the engine buffer (chain step 2).
//
// 36 to 62 Hz belongs to the heartbeat layer alone (CLAUDE.md hard rule 2). The engine does not
// keep that band clear, so the shim removes it from the engine buffer before the heartbeat layer
// is added. Five biquads in transposed direct form II with double state, coefficients for 48 kHz
// from highpass_coefficients.h, which tools/design_highpass.py generates: at least 30 dB down at
// and below 62 Hz, within 1 dB from 69.35 Hz.

#ifndef PLS_HIGHPASS_H
#define PLS_HIGHPASS_H

#include <cstdint>

#include "highpass_coefficients.h"

namespace pls {

class Highpass {
 public:
  void reset() {
    for (int s = 0; s < kHighpassSections; ++s) {
      z1_[s] = 0.0;
      z2_[s] = 0.0;
    }
  }

  // In place. The float buffer is widened to double for the cascade and narrowed on the way out.
  void process(float* buffer, uint32_t frames) {
    for (uint32_t i = 0; i < frames; ++i) {
      double x = buffer[i];
      for (int s = 0; s < kHighpassSections; ++s) {
        const double* c = kHighpassCoefficients[s];
        const double y = c[0] * x + z1_[s];
        z1_[s] = c[1] * x - c[3] * y + z2_[s];
        z2_[s] = c[2] * x - c[4] * y;
        x = y;
      }
      buffer[i] = static_cast<float>(x);
    }
  }

 private:
  double z1_[kHighpassSections] = {};
  double z2_[kHighpassSections] = {};
};

}  // namespace pls

#endif  // PLS_HIGHPASS_H
