/*
 * masenergy_sampler.ino
 *
 * The external rig's own instrument. Reads a 100 mOhm low-side shunt and a
 * resistor-divided bus voltage directly on the ESP32's own ADC1 pins,
 * watches the trigger line the Jetson drives, and streams one binary frame
 * per sample over USB serial to the logging laptop, which runs
 * host/capture.py.
 *
 * WHY THIS FILE STOPPED USING THE INA226 (2026-09-06)
 *
 * The original design (see CHANGES.md, 2026-09-02 entries) read an INA226
 * over I2C, which capped the sample rate at roughly 1.5 kHz: two I2C Fast
 * Mode register reads per sample plus transaction overhead dominate a
 * ~667us period. Reading the shunt and bus directly on the ESP32's own ADC
 * pins removes that I2C round trip entirely, and it also removes the
 * INA226 as a sourcing dependency, which had become a real blocker (see
 * CHANGES.md, 2026-09-03/04/05 entries on the shunt and INA226 sourcing
 * search in India: every hobbyist INA226 breakout found was either out of
 * stock or shipped with an undocumented fixed onboard shunt, not the
 * external, known-value shunt this project's calibration depends on).
 *
 * WHAT THIS TRADES AWAY, STATED PLAINLY
 *
 * The INA226 has its own dedicated 16-bit differential ADC built for
 * exactly this measurement. The ESP32's own ADC does not: it is a general-
 * purpose single-ended ADC with a documented, nontrivial error even after
 * factory calibration (Espressif's own developer blog, August 2026 posting
 * on ADC performance across their SoC lineup, states the original ESP32's
 * calibrated error is "generally less than 30 mV" and that it shows real
 * nonlinearity near the top and bottom of its range). At a 100 mOhm shunt,
 * 30 mV of uncorrected error is a 300 mA current error, which would be a
 * large fraction of this rig's expected 0.1-2A working range. Two design
 * choices below exist specifically to bring that error down to something
 * usable, and both are explained where they appear: LOW-SIDE SHUNT
 * PLACEMENT and STARTUP ZERO-OFFSET CALIBRATION. Averaging alone (see
 * OVERSAMPLING) only ever addresses the random component of that error; it
 * cannot remove a fixed offset, which is why the calibration step exists
 * as a separate mechanism rather than being treated as the same problem.
 *
 * This firmware has NOT yet been flashed or run against real hardware as
 * of this rewrite. The previous (INA226-based) version reached that point
 * (see CHANGES.md, 2026-09-02: real hardware bring-up, frame stream and
 * trigger_pulse_n both confirmed). This version starts over from "written,
 * not yet run," same as every other file in this rig did before it.
 *
 * LOW-SIDE SHUNT PLACEMENT (why this differs from the original circuit)
 *
 * The original build brief placed the shunt on the high side (in the
 * positive 19V supply line), which is where the INA226's own differential
 * inputs made sense: it measures the voltage difference between two points
 * regardless of what they sit on top of. A bare ESP32 ADC pin cannot do
 * that; it reads a single-ended voltage relative to the ESP32's own GND
 * pin. Reading a high-side shunt directly would mean reading a signal
 * riding on top of 19V, which a 0-3.3V ADC pin cannot do at all without
 * additional isolation or level-shifting circuitry this design does not
 * have.
 *
 * Moving the shunt to the LOW side (the ground return path, between the
 * Jetson's GND and the power supply's GND) fixes this: with the ESP32's
 * own GND tied to the supply-side ("far") node, current flowing through
 * the shunt raises the Jetson-side ("near") node above that reference by
 * I * R_SHUNT_OHMS, a small positive voltage the ADC can read directly.
 * PIN_ADC_CURRENT below must be wired to that near (Jetson-side) node, not
 * the far one; wiring it to the wrong side of the shunt reads a value at
 * or near the ESP32's own noise floor regardless of real current, which
 * would look like a stuck-near-zero reading rather than an obvious wiring
 * fault, so get this one connection right before trusting any data from
 * this firmware.
 *
 * WIRING
 *
 *   100 mOhm shunt   -> low side, in series with the return (GND) path
 *                       between the Jetson and the 19V supply's ground.
 *   PIN_ADC_CURRENT  -> the shunt's Jetson-side (near) terminal directly.
 *                       No amplification; see WHAT THIS TRADES AWAY above
 *                       for why this is workable at this shunt value.
 *   PIN_ADC_BUS      -> the midpoint of a resistor divider (R_DIVIDER_TOP
 *                       100k, R_DIVIDER_BOTTOM 18k) from the 19V bus down
 *                       to a range the ADC can read; see BUS_DIVIDER_RATIO.
 *   Jetson GPIO trigger -> 330 ohm series resistor -> ESP32 PIN_TRIGGER
 *                          (unchanged from the original design).
 *
 * ESP32 GND must be a solid, short connection to the shunt's supply-side
 * (far) terminal, the same node the 19V supply's own ground returns to.
 * Any resistance in that ground connection adds directly to the shunt's
 * own reading as an uncorrectable error, for the same reason a two-
 * terminal (non-Kelvin) shunt was rejected in the original design; a
 * single-ended ADO measurement cannot separate that wiring resistance from
 * the shunt's own the way a true 4-terminal Kelvin connection would, so
 * keep this wire short and use a dedicated connection, not a shared,
 * daisy-chained ground.
 *
 * STARTUP ZERO-OFFSET CALIBRATION (required procedure, read before use)
 *
 * The ESP32 is USB-powered, independent of the 19V rail the Jetson runs
 * from. This firmware uses that independence: at boot, before any real
 * current or bus voltage is assumed to be present, it takes many averaged
 * ADC readings on both channels and stores them as a zero-offset baseline,
 * which is subtracted from every later reading before it is transmitted.
 * This corrects the fixed component of the ESP32 ADC's error (see WHAT
 * THIS TRADES AWAY), which sample averaging alone cannot touch.
 *
 * This ONLY works if the operator follows the required power-up order:
 *
 *   1. Connect the ESP32 to the laptop over USB (it boots immediately).
 *   2. Wait for calibration to finish. CAL_SETTLE_MS after boot, the
 *      firmware averages CAL_SAMPLES readings per channel and locks in the
 *      baseline; there is no serial handshake to wait for for this step,
 *      the timing is fixed so a laptop script does not have to be running
 *      yet, but host/live_monitor.py or capture.py started shortly after
 *      will show FAULT_CAL_NOT_RUN clear once calibration has completed.
 *   3. Only THEN power on the Jetson's 19V supply.
 *
 * Powering the Jetson's supply before or during that window means real
 * current and real bus voltage get baked into the "zero" baseline, and
 * every reading for the rest of that session is wrong by that amount,
 * silently, with no fault bit to catch it (the firmware has no way to
 * know the baseline it captured was not actually zero). This is a real,
 * known limitation of this design, not a hidden one: a bare-ADC low-side
 * shunt has no independent way to verify its own zero point the way the
 * INA226 approach never needed to, since that approach never depended on
 * a baseline captured at a particular moment in time. Get the power-up
 * order right, every time, or the whole session's current and power
 * figures are wrong in a way nothing downstream will detect.
 *
 * A brownout or reset mid-session re-runs this calibration against
 * whatever the Jetson happens to be drawing at that instant, which is
 * silently wrong in the same way. FAULT_OVERRUN and the frame sequence
 * counter can reveal that a reset happened (seq restarting from 0 is the
 * tell, see host/capture.py's SeqTracker), but nothing in this firmware
 * detects a bad recalibration directly.
 *
 * OVERSAMPLING (addresses random noise only, see above for what it does
 * not address)
 *
 * Each reported sample is the SUM of OVERSAMPLE_K raw ADC reads taken back
 * to back on that channel, transmitted as a single offset-corrected signed
 * value rather than K separate values. Averaging (implemented here as
 * summing and letting the host divide by OVERSAMPLE_K, so no precision is
 * lost to integer division on this end) trades some of the raw ADC's speed
 * for resolution: the standard oversample-and-decimate rule of thumb is
 * roughly +2 effective bits of resolution per 16x oversampling, which is
 * why OVERSAMPLE_K defaults to 16 (12-bit raw -> roughly 14-bit effective
 * on the current channel, see host/capture.py's comments on the exact
 * figure). This is a deliberate choice, not a free win: it directly
 * divides the achievable sample rate by OVERSAMPLE_K, so raising it
 * further trades still more rate for still more resolution.
 *
 * SAMPLING IS CONTINUOUS, NOT GATED (unchanged from the original design)
 *
 * Every sample is sent, whether the trigger is high or low, for the same
 * reason the INA226-based version gave: gating sampling to only run while
 * the trigger is high would exclude the instrument's own idle draw from
 * the baseline every measurement gets compared against, which is exactly
 * the bias src/masenergy/ina3221.py's docstring describes for the onboard
 * sampler, and the reasoning applies identically here.
 *
 * WIRE FORMAT (see host/capture.py, which decodes exactly this layout)
 *
 * Still 20 bytes, still little-endian, still framed the same way; only the
 * meaning of the last two fields changed from INA226 register values to
 * offset-corrected ADC sums.
 *
 *   offset  size  field           meaning
 *   0       2     sync            0xA5 0x5A, fixed, checked by the reader
 *   2       4     seq             uint32, this board's own sample counter,
 *                                 increments every sample including faulted
 *                                 ones; a gap in seq is a dropped sample.
 *   6       4     micros          uint32, micros() at the time this sample
 *                                 was taken, wraps every ~71 minutes; used
 *                                 only to compute intervals within one
 *                                 attached session, never as a join key.
 *   10      1     trigger_level   0 or 1, the trigger pin read at sample
 *                                 time, debounced (see TRIGGER_DEBOUNCE_US).
 *   11      4     pulse_n         uint32, this board's own count of trigger
 *                                 low-to-high transitions seen so far,
 *                                 incremented on the same sample that
 *                                 reports the rising edge. Must line up 1:1
 *                                 with jetson.py Trigger.pulse_n for the
 *                                 join to mean anything; see that module's
 *                                 docstring.
 *   15      1     fault           bitfield, see FAULT_* below. 0 means the
 *                                 sample is trustworthy.
 *   16      2     current_adc_sum int16 (SIGNED), sum of OVERSAMPLE_K raw
 *                                 PIN_ADC_CURRENT reads, offset-corrected
 *                                 against the startup baseline. Signed
 *                                 because near-idle current sits close to
 *                                 the calibrated zero point and real noise
 *                                 legitimately dips the corrected value
 *                                 negative; never treat a small negative
 *                                 value here as an error by itself. Divide
 *                                 by OVERSAMPLE_K and the ADC's volts-per-
 *                                 count to get shunt voltage; see
 *                                 host/capture.py for the exact constants,
 *                                 which must match CURRENT_ATTEN and
 *                                 R_SHUNT_OHMS below exactly.
 *   18      2     bus_adc_sum     uint16 (UNSIGNED, unlike current_adc_sum
 *                                 above). The bus channel's corrected value
 *                                 is never small: a 19V bus divided down by
 *                                 BUS_DIVIDER_RATIO and summed over
 *                                 OVERSAMPLE_K reads lands around 57500 of
 *                                 a possible 65535, which does not fit a
 *                                 signed 16-bit field at all; this is why
 *                                 the two ADC sum fields use different
 *                                 signedness rather than matching for
 *                                 tidiness. Still needs BUS_DIVIDER_RATIO
 *                                 applied on the host side to undo the
 *                                 resistor divider.
 *
 * Baud rate is still 1,000,000 (1 Mbaud). The achievable sample rate with
 * this ADC-based design has not been measured against real hardware yet;
 * unlike the previous version, no specific target rate is asserted here.
 * host/live_monitor.py's own frame-rate readout is how that gets measured
 * once this is flashed, the same way the INA226 version's real ~1499 fps
 * figure was confirmed rather than assumed (see CHANGES.md, 2026-09-02).
 *
 * Standard Arduino-ESP32 core APIs only (analogRead, analogSetPinAttenuation,
 * Serial). No external libraries, no Wire.h; this firmware no longer talks
 * I2C to anything.
 *
 * BUILD (run a smoke test with firmware/esp32/smoke_test first; see that
 * sketch's own header for why)
 *
 *     arduino-cli compile --fqbn esp32:esp32:esp32 firmware/esp32/masenergy_sampler
 *     arduino-cli upload -p /dev/cu.usbserial-XXXX --fqbn esp32:esp32:esp32 firmware/esp32/masenergy_sampler
 *
 * arduino-cli requires a sketch's main .ino file to share its containing
 * folder's name, which is why this file lives in
 * firmware/esp32/masenergy_sampler/ rather than directly in firmware/esp32/.
 */

// ---- Board wiring: the only lines that should change per physical board ----
static const uint8_t PIN_TRIGGER = 4;         // unchanged from the original
                                               // design; through the 330 ohm
                                               // series resistor.
static const uint8_t PIN_ADC_CURRENT = 32;    // ADC1 channel 4; the shunt's
                                               // Jetson-side (near) terminal.
                                               // Must be an ADC1 pin (32-39),
                                               // never ADC2, which shares
                                               // hardware with WiFi even
                                               // though WiFi stays off here.
static const uint8_t PIN_ADC_BUS = 33;        // ADC1 channel 5; the
                                               // resistor divider midpoint.
static const uint32_t SERIAL_BAUD = 1000000;

// ---- Current channel: 0 dB attenuation, ~0-1100 mV range (Espressif's own
// figure; the real usable-linearity range is narrower at the extremes, see
// FAULT_CURRENT_ADC_RANGE below, which exists because of that). Chosen
// because the whole expected signal (0.1 to 2A across a 100 mOhm shunt is
// 10 to 200 mV) sits well inside this narrow, high-resolution range rather
// than being a sliver of the wider, noisier 11 dB range the bus channel
// needs. ----
static const adc_attenuation_t CURRENT_ATTEN = ADC_0db;
static const float R_SHUNT_OHMS = 0.100f;   // 100 mOhm, chosen 2026-09-05 to
                                             // trade shunt power dissipation
                                             // and a small bus voltage drop
                                             // for usable ADC resolution;
                                             // see CHANGES.md for the full
                                             // reasoning and the rejected
                                             // 20/50/200 mOhm alternatives.

// ---- Bus channel: 11 dB attenuation, ~0-3300 mV range, needed because the
// divided-down 19V bus (see BUS_DIVIDER_RATIO) can approach 3V at the high
// end of the Jetson's supply tolerance. ----
static const adc_attenuation_t BUS_ATTEN = ADC_11db;
static const float R_DIVIDER_TOP_OHMS = 100000.0f;   // bus side
static const float R_DIVIDER_BOTTOM_OHMS = 18000.0f; // ADC side
static const float BUS_DIVIDER_RATIO =
    R_DIVIDER_BOTTOM_OHMS / (R_DIVIDER_TOP_OHMS + R_DIVIDER_BOTTOM_OHMS);
// host/capture.py must undo this ratio (divide the decoded voltage back up)
// to report real bus voltage; it must match this constant exactly.

static const uint8_t ADC_BITS = 12;
static const uint16_t ADC_MAX_COUNT = (1u << ADC_BITS) - 1;  // 4095

// ---- Oversampling: see the OVERSAMPLING header section for what this does
// and does not fix. Sum, not average, is transmitted, so the host can
// divide in floating point without losing precision to integer rounding
// twice. ----
static const uint16_t OVERSAMPLE_K = 16;

// ---- Startup zero-offset calibration ----
static const uint32_t CAL_SETTLE_MS = 500;   // time given for ADC readings
                                              // to stabilise after boot
                                              // before calibration samples
                                              // are taken; not a serial
                                              // handshake, just a fixed
                                              // delay, see the header note
                                              // on why no handshake is used.
static const uint16_t CAL_SAMPLES = 256;     // averaged per channel for the
                                              // baseline; deliberately much
                                              // larger than OVERSAMPLE_K
                                              // since this only runs once
                                              // and a noisy baseline biases
                                              // every sample for the rest of
                                              // the session.

// ---- Sampling and framing ----
// No specific SAMPLE_PERIOD_US target is asserted here, unlike the INA226
// version's 667us (~1.5 kHz) figure, because the achievable rate with
// OVERSAMPLE_K back-to-back ADC reads per channel has not been measured
// against real hardware. The loop below runs as fast as two
// OVERSAMPLE_K-read channel scans and one trigger check allow, and reports
// its own real interval via the micros field on every frame, exactly like
// the previous version's overrun detection did; host/live_monitor.py's
// frames/sec readout is how the real achieved rate gets measured once this
// is flashed.
static const uint32_t TRIGGER_DEBOUNCE_US = 50;  // unchanged from the
                                                  // original design; shorter
                                                  // than one sample period,
                                                  // only rejects sub-sample
                                                  // glitches, not real edges.

static const uint8_t SYNC_0 = 0xA5;
static const uint8_t SYNC_1 = 0x5A;
static const size_t FRAME_SIZE = 20;

// Closed fault vocabulary, bitfield so more than one can be set at once.
// Mirrors records.py's HW_FAULTS discipline: a fault is named, not implied
// by an out-of-range value, and a faulted sample never carries a value that
// looks like real data.
static const uint8_t FAULT_OVERRUN = 0x01;          // this sample's period
                                                     // ran long; sequence is
                                                     // still intact, but the
                                                     // interval between this
                                                     // row and the last does
                                                     // not reflect a steady
                                                     // rate.
static const uint8_t FAULT_CAL_NOT_RUN = 0x02;      // startup calibration
                                                     // has not completed
                                                     // yet; every sample
                                                     // this early is
                                                     // uncorrected and
                                                     // should be discarded.
static const uint8_t FAULT_CURRENT_ADC_RANGE = 0x04;  // the current
                                                       // channel's raw
                                                       // reading (before
                                                       // offset correction)
                                                       // pinned at 0 or
                                                       // ADC_MAX_COUNT on at
                                                       // least one of the
                                                       // OVERSAMPLE_K reads
                                                       // this sample,
                                                       // meaning the true
                                                       // signal may be
                                                       // clipped or the
                                                       // wiring may be
                                                       // faulted; see the
                                                       // LOW-SIDE SHUNT
                                                       // PLACEMENT note on
                                                       // what a
                                                       // near-permanent
                                                       // zero usually means.
static const uint8_t FAULT_BUS_ADC_RANGE = 0x08;      // same idea, for the
                                                       // bus channel.

uint32_t g_seq = 0;
uint32_t g_pulse_n = 0;
uint8_t g_last_trigger_level = 0;
uint32_t g_last_edge_us = 0;
bool g_cal_done = false;
int32_t g_current_baseline_sum = 0;  // OVERSAMPLE_K-scale baseline, captured
                                     // once at boot; see STARTUP ZERO-OFFSET
                                     // CALIBRATION.
int32_t g_bus_baseline_sum = 0;

// Sums OVERSAMPLE_K back-to-back raw ADC reads on one pin. Also reports
// whether any individual read hit either rail, for FAULT_*_ADC_RANGE.
uint32_t oversample_sum(uint8_t pin, bool *hit_rail) {
  uint32_t sum = 0;
  *hit_rail = false;
  for (uint16_t i = 0; i < OVERSAMPLE_K; i++) {
    int raw = analogRead(pin);
    if (raw <= 0 || raw >= (int)ADC_MAX_COUNT) {
      *hit_rail = true;
    }
    sum += (uint32_t)raw;
  }
  return sum;
}

// Same idea, but averaging CAL_SAMPLES single reads rather than
// OVERSAMPLE_K, for the one-time startup baseline; returned already scaled
// to OVERSAMPLE_K's sum range so it subtracts directly from later frames
// without a further scaling step at 1.5+ kHz.
int32_t calibrate_baseline_sum(uint8_t pin) {
  uint64_t total = 0;
  for (uint16_t i = 0; i < CAL_SAMPLES; i++) {
    total += (uint32_t)analogRead(pin);
  }
  double mean = (double)total / (double)CAL_SAMPLES;
  return (int32_t)(mean * (double)OVERSAMPLE_K + 0.5);
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  // Wifi and Bluetooth are never started, unchanged from the original
  // design: this board does nothing except ADC sampling and serial while a
  // capture is running.

  pinMode(PIN_TRIGGER, INPUT);

  analogReadResolution(ADC_BITS);
  analogSetPinAttenuation(PIN_ADC_CURRENT, CURRENT_ATTEN);
  analogSetPinAttenuation(PIN_ADC_BUS, BUS_ATTEN);

  g_last_trigger_level = (uint8_t)digitalRead(PIN_TRIGGER);
  g_last_edge_us = micros();

  // STARTUP ZERO-OFFSET CALIBRATION. See the header section of the same
  // name: this is only correct if the Jetson's 19V supply is still off
  // when this runs. FAULT_CAL_NOT_RUN covers every frame sent before this
  // completes; nothing here can detect the supply being on too early.
  delay(CAL_SETTLE_MS);
  g_current_baseline_sum = calibrate_baseline_sum(PIN_ADC_CURRENT);
  g_bus_baseline_sum = calibrate_baseline_sum(PIN_ADC_BUS);
  g_cal_done = true;
}

void send_frame(uint32_t micros_now, uint8_t trigger_level, uint8_t fault,
                int16_t current_adc_sum, uint16_t bus_adc_sum) {
  uint8_t frame[FRAME_SIZE];
  frame[0] = SYNC_0;
  frame[1] = SYNC_1;
  memcpy(frame + 2, &g_seq, 4);
  memcpy(frame + 6, &micros_now, 4);
  frame[10] = trigger_level;
  memcpy(frame + 11, &g_pulse_n, 4);
  frame[15] = fault;
  memcpy(frame + 16, &current_adc_sum, 2);
  memcpy(frame + 18, &bus_adc_sum, 2);
  Serial.write(frame, FRAME_SIZE);
}

void loop() {
  static uint32_t last_loop_us = 0;
  uint32_t loop_start_us = micros();

  uint8_t fault = 0;
  if (last_loop_us != 0) {
    // No fixed SAMPLE_PERIOD_US target exists to compare against (see the
    // header note); FAULT_OVERRUN here instead flags a loop iteration that
    // ran unusually long relative to this session's own running average,
    // computed the same cheap way host/capture.py's SeqTracker looks for
    // gaps: by comparison with the immediately preceding interval, not a
    // hardcoded constant.
    static uint32_t prev_interval_us = 0;
    uint32_t interval_us = loop_start_us - last_loop_us;
    if (prev_interval_us != 0 && interval_us > prev_interval_us * 4) {
      fault |= FAULT_OVERRUN;
    }
    prev_interval_us = interval_us;
  }
  last_loop_us = loop_start_us;

  uint8_t level = (uint8_t)digitalRead(PIN_TRIGGER);
  uint32_t sample_us = micros();
  if (level != g_last_trigger_level &&
      (sample_us - g_last_edge_us) >= TRIGGER_DEBOUNCE_US) {
    if (level == 1) {
      g_pulse_n++;  // rising edge: same convention as jetson.py's
                     // Trigger.high(), which increments pulse_n before
                     // returning, so the nth rising edge on both sides
                     // carries the same ordinal.
    }
    g_last_trigger_level = level;
    g_last_edge_us = sample_us;
  }

  if (!g_cal_done) {
    fault |= FAULT_CAL_NOT_RUN;
  }

  bool current_hit_rail = false;
  bool bus_hit_rail = false;
  uint32_t current_sum = oversample_sum(PIN_ADC_CURRENT, &current_hit_rail);
  uint32_t bus_sum = oversample_sum(PIN_ADC_BUS, &bus_hit_rail);

  if (current_hit_rail) fault |= FAULT_CURRENT_ADC_RANGE;
  if (bus_hit_rail) fault |= FAULT_BUS_ADC_RANGE;

  // Offset-correct against the startup baseline. current_corrected is
  // clamped to SIGNED int16 range (see the WIRE FORMAT section's note on
  // current_adc_sum for the one known edge case, a hard overcurrent fault
  // past this shunt's design range, where this clamp and
  // FAULT_CURRENT_ADC_RANGE fire together). bus_corrected is clamped to
  // UNSIGNED uint16 range instead: the bus channel's baseline is captured
  // near 0V (see STARTUP ZERO-OFFSET CALIBRATION, the Jetson's supply is
  // off during that step), so a correctly working bus channel never
  // produces a negative corrected value in practice; clamping its floor to
  // 0 rather than allowing negative values simply discards the small
  // amount of below-baseline noise the same way the current channel's
  // negative headroom absorbs it, without needing a second signed field.
  int32_t current_corrected = (int32_t)current_sum - g_current_baseline_sum;
  int32_t bus_corrected = (int32_t)bus_sum - g_bus_baseline_sum;
  if (current_corrected > 32767) current_corrected = 32767;
  if (current_corrected < -32768) current_corrected = -32768;
  if (bus_corrected > 65535) bus_corrected = 65535;
  if (bus_corrected < 0) bus_corrected = 0;

  send_frame(sample_us, level, fault,
             (int16_t)current_corrected, (uint16_t)bus_corrected);
  g_seq++;
}
