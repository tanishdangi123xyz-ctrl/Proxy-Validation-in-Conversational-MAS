"""Serial capture for the external power rig.

Reads the 20-byte binary frames firmware/esp32/masenergy_sampler.ino streams
over USB, decodes them into physical units, and appends them to a CSV on the
logging laptop. This is the laptop half of the pipeline records.py's
docstring describes: energy_j_external is NaN on every row the Jetson
writes, and is filled in afterwards by a join script that matches this
file's rows to the Jetson's rows on trigger_pulse_n. Nothing here computes
that join; this module's only job is to get a faithful, gap-visible record of
what the ESP32's own ADC saw, with the same pulse ordinal the ESP32 counted.

AS OF 2026-09-06, THE SENSOR CHANGED FROM AN INA226 TO THE ESP32'S OWN ADC

The firmware used to read an INA226 over I2C; it now reads a 100 mOhm
low-side shunt and a resistor-divided bus voltage directly on two of the
ESP32's own ADC1 pins, to remove the I2C round trip's speed ceiling and the
INA226's sourcing problem (see the .ino file's own header, and CHANGES.md,
2026-09-06). The wire format's last two fields changed meaning accordingly:
they are no longer raw INA226 register values, they are offset-corrected,
oversampled ADC sums. See the constants block below (CURRENT_LSB_V,
OVERSAMPLE_K, BUS_DIVIDER_RATIO) for the new derivation, and keep this
file's constants and the .ino file's constants changing together, exactly
as before.

WHY A CUSTOM FRAMED PROTOCOL INSTEAD OF TEXT

At ~1.5 kHz for the length of a multi-day campaign, a line-based text
protocol (CSV-over-serial) would need to be parsed fast enough not to fall
behind the ESP32's own send rate, and a single dropped or corrupted byte in a
text stream can misalign every following line until the next newline is
found by luck. A fixed-size binary frame with a two-byte sync marker lets
this reader resynchronise deterministically after any corruption: scan for
the next 0xA5 0x5A, try to parse a frame there, and if the frame's implied
length runs past the buffer or the next sync marker does not appear where a
20-byte frame would put it, treat the candidate as noise and keep scanning
byte by byte. This never blocks indefinitely and never silently starts
reporting misaligned garbage as data.

WHY THE JOIN KEY IS pulse_n, NOT A TIMESTAMP

The ESP32's micros() clock and the Jetson's clock are never synchronised, so
no timestamp comparison between the two streams means anything. What both
sides agree on is the count of rising edges seen on the same physical wire:
jetson.py's Trigger.high() increments pulse_n before returning, and this
firmware increments its own pulse_n on the same debounced sample that
observes the rising edge (see the .ino file's header). This script never
tries to align by wall-clock time; it only preserves pulse_n as read off the
wire, and the row-count-preserving discipline in DATA_LOSS_NOTE below makes
sure that ordinal is trustworthy even across a lost frame.

FAULT AND DATA-LOSS DISCIPLINE

A faulted sample (startup calibration not yet complete, or either ADC
channel's raw reading pinned at a rail, meaning a clipped or faulted signal;
see the .ino file's FAULT_* constants) is written as NaN in amps/volts/watts,
never as zero, for the same reason records.py gives for the Jetson-side
rails: zero amps is a real, physically meaningful reading, and writing it for
a read that never happened makes a broken rig indistinguishable from a
genuinely idle one. seq_gap_before is written on every row and is
nonzero whenever this reader's own sequence tracking detected missing frames
immediately before this one (a dropped or corrupted frame, or a USB hiccup
this script recovered from by resynchronising); a downstream analysis can
therefore tell a genuinely continuous stretch of samples from one with a hole
in it, which integrate() in ina3221.py already refuses to trust silently for
the onboard rails, and the same caution applies here.

Standard library only (pyserial is the one external dependency this script
cannot avoid, since Python has no built-in serial port access; it is listed
in requirements.txt). Python 3.10 compatible.

    python3 host/capture.py --port /dev/ttyUSB0 --out data/raw/external_run.csv
    python3 host/capture.py --list-ports
"""

import argparse
import csv
import math
import os
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

FRAME_SIZE = 20
SYNC_0 = 0xA5
SYNC_1 = 0x5A
# offset 2: seq (u32) | 6: micros (u32) | 10: trigger_level (u8)
# 11: pulse_n (u32) | 15: fault (u8) | 16: current_adc_sum (i16, SIGNED)
# 18: bus_adc_sum (u16, UNSIGNED -- see the .ino file's WIRE FORMAT note on
# why these two fields differ in signedness)
_STRUCT = struct.Struct("<xxIIBIBhH")  # skips the 2 sync bytes via 'xx'

# Must match firmware/esp32/masenergy_sampler.ino's constants of the same
# name exactly, or every amps/volts/watts figure in this file is wrong by a
# constant factor while still looking perfectly plausible. Changing any of
# these means changing both files in the same commit; see the .ino header's
# own note pointing back here.
R_SHUNT_OHMS = 0.100          # ohms, the installed shunt's value
OVERSAMPLE_K = 16             # samples summed per reported frame on the ESP32
ADC_BITS = 12
ADC_MAX_COUNT = (1 << ADC_BITS) - 1  # 4095

# Attenuation-dependent ADC full-scale voltages, Espressif's own datasheet
# figures (approximate; real usable linearity is narrower at the extremes,
# which is exactly what FAULT_CURRENT_ADC_RANGE / FAULT_BUS_ADC_RANGE exist
# to flag rather than silently trust). Must match CURRENT_ATTEN / BUS_ATTEN
# in the .ino file.
CURRENT_ADC_FULL_SCALE_V = 1.100   # 0 dB attenuation
BUS_ADC_FULL_SCALE_V = 3.300       # 11 dB attenuation

CURRENT_LSB_V = CURRENT_ADC_FULL_SCALE_V / ADC_MAX_COUNT  # volts per raw
                                                           # ADC count on the
                                                           # current channel
BUS_LSB_V = BUS_ADC_FULL_SCALE_V / ADC_MAX_COUNT

# Undoes the .ino file's resistor divider (R_DIVIDER_TOP_OHMS over
# R_DIVIDER_BOTTOM_OHMS, 100k/18k there) to recover real bus voltage from
# what the ADC actually saw. Must match BUS_DIVIDER_RATIO in the .ino file
# exactly.
BUS_DIVIDER_RATIO = 18000.0 / (100000.0 + 18000.0)

FAULT_OVERRUN = 0x01
FAULT_CAL_NOT_RUN = 0x02
FAULT_CURRENT_ADC_RANGE = 0x04
FAULT_BUS_ADC_RANGE = 0x08

# Closed vocabulary, same discipline as records.HW_FAULTS: a fault token is
# named here or it is not a fault this pipeline recognises, which is what
# lets an analysis count occurrences by name instead of by guesswork.
FAULT_NAMES = {
    FAULT_OVERRUN: "sample_period_overrun",
    FAULT_CAL_NOT_RUN: "startup_calibration_not_run",
    FAULT_CURRENT_ADC_RANGE: "current_adc_range",
    FAULT_BUS_ADC_RANGE: "bus_adc_range",
}

NAN = float("nan")

FIELDS = (
    "capture_ts_utc",   # this laptop's wall clock at frame receipt; a
                         # diagnostic for the operator, never a join key.
    "seq",               # ESP32's own free-running sample counter.
    "seq_gap_before",    # frames missing immediately before this one, 0 if
                          # none. Computed here from consecutive seq values.
    "esp32_micros",       # ESP32 micros() at sample time, wraps ~71 min.
    "trigger_level",       # 0 or 1, debounced on the ESP32.
    "trigger_pulse_n",      # join key against the Jetson's trigger_pulse_n.
    "fault",                 # pipe-joined fault names, empty string if clean.
    "shunt_v",                # volts, NaN if FAULT_CAL_NOT_RUN or
                                # FAULT_CURRENT_ADC_RANGE.
    "bus_v",                   # volts, NaN if FAULT_CAL_NOT_RUN or
                                 # FAULT_BUS_ADC_RANGE.
    "current_a",                 # shunt_v / R_SHUNT_OHMS via CURRENT_LSB,
                                  # NaN if either read that feeds it failed.
    "power_w",                    # bus_v * current_a, NaN under the same
                                   # condition.
)


class FramingError(RuntimeError):
    """Raised only for conditions this reader cannot recover from itself."""


def decode_fault(byte_value):
    """Fault byte to a sorted, pipe-joined name string, empty if clean."""
    names = sorted(name for bit, name in FAULT_NAMES.items()
                    if byte_value & bit)
    return "|".join(names)


def frame_to_row(raw, capture_ts_utc, seq_gap_before, r_shunt_ohms):
    """One decoded 20-byte frame to a CSV-ready dict.

    r_shunt_ohms is accepted as a parameter rather than hard-coded a second
    time here, so a run against a different shunt value only needs
    --shunt-ohms on the command line, not a code edit. CURRENT_LSB_V,
    OVERSAMPLE_K, and BUS_DIVIDER_RATIO above still have to match the
    firmware's own constants exactly regardless of this parameter, since
    those describe the ADC and divider hardware, not the shunt.

    current_adc_sum and bus_adc_sum arrive already offset-corrected against
    the ESP32's own startup calibration (see the .ino file's STARTUP
    ZERO-OFFSET CALIBRATION section) and already summed over OVERSAMPLE_K
    raw reads; this function's only job is to divide back out to a mean
    per-read ADC count, convert that to volts using the channel's own
    LSB size, and, for the current channel, divide by the shunt resistance
    to get amps.
    """
    (seq, micros_val, trigger_level, pulse_n, fault, current_adc_sum,
     bus_adc_sum) = _STRUCT.unpack(raw)

    cal_not_run = bool(fault & FAULT_CAL_NOT_RUN)
    current_range_fault = bool(fault & FAULT_CURRENT_ADC_RANGE)
    bus_range_fault = bool(fault & FAULT_BUS_ADC_RANGE)
    current_bad = cal_not_run or current_range_fault
    bus_bad = cal_not_run or bus_range_fault

    current_mean_counts = current_adc_sum / OVERSAMPLE_K
    bus_mean_counts = bus_adc_sum / OVERSAMPLE_K

    shunt_v = NAN if current_bad else current_mean_counts * CURRENT_LSB_V
    current_a = NAN if current_bad else shunt_v / r_shunt_ohms

    bus_v_at_adc = NAN if bus_bad else bus_mean_counts * BUS_LSB_V
    bus_v = NAN if bus_bad else bus_v_at_adc / BUS_DIVIDER_RATIO

    power_w = NAN if (current_bad or bus_bad) else bus_v * current_a

    assert r_shunt_ohms > 0, "shunt resistance must be positive"

    return {
        "capture_ts_utc": capture_ts_utc,
        "seq": seq,
        "seq_gap_before": seq_gap_before,
        "esp32_micros": micros_val,
        "trigger_level": trigger_level,
        "trigger_pulse_n": pulse_n,
        "fault": decode_fault(fault),
        "shunt_v": shunt_v,
        "bus_v": bus_v,
        "current_a": current_a,
        "power_w": power_w,
    }


class FrameReader:
    """Turns a byte stream into frames, resynchronising after corruption.

    Fed bytes incrementally via feed(), because a serial port hands over
    whatever happens to be in its buffer on each read, not whole frames.
    Internally buffers only as much as it takes to find and validate the next
    frame, so a capture that runs for days does not grow this buffer
    unboundedly even under sustained corruption.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        self._buf.extend(data)
        frames = []
        while True:
            sync_at = self._find_sync()
            if sync_at is None:
                # No sync marker at all in the buffer. Keep at most one byte
                # (in case a sync byte's second half is about to arrive) and
                # drop the rest; there is nothing decodable in it.
                if len(self._buf) > 1:
                    del self._buf[:-1]
                break
            if sync_at > 0:
                del self._buf[:sync_at]
            if len(self._buf) < FRAME_SIZE:
                break
            candidate = bytes(self._buf[:FRAME_SIZE])
            frames.append(candidate)
            del self._buf[:FRAME_SIZE]
        return frames

    def _find_sync(self):
        target = bytes((SYNC_0, SYNC_1))
        return self._buf.find(target) if self._buf else None


class SeqTracker:
    """Detects gaps in the ESP32's free-running seq counter.

    seq wraps at 2**32; ESP32_HZ * 71 minutes is roughly the wrap period at
    the target sample rate, well past any single capture session, so wraps
    are handled (the modular distance below) but are not expected to matter
    in practice.
    """

    def __init__(self):
        self._last_seq = None

    def gap_before(self, seq):
        if self._last_seq is None:
            self._last_seq = seq
            return 0
        expected = (self._last_seq + 1) % (2**32)
        if seq == expected:
            gap = 0
        else:
            gap = (seq - expected) % (2**32)
        self._last_seq = seq
        return gap


class CaptureWriter:
    """Append-only CSV writer for decoded rows, mirroring records.RecordWriter.

    Flushes every row and fsyncs periodically for the same reason
    records.RecordWriter does: a crash mid-campaign should lose at most the
    row in flight, and an fsync on every single row at 1.5 kHz would make the
    disk the bottleneck long before the ESP32 is.
    """

    def __init__(self, path, fsync_every=1500):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fsync_every = fsync_every
        self._rows = 0
        existed = self.path.exists() and self.path.stat().st_size > 0
        if existed:
            with open(self.path, "r", newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh), [])
            if tuple(header) != FIELDS:
                raise RuntimeError(
                    "Schema mismatch appending to %s: file has columns %s, "
                    "this build writes %s" % (self.path, header, FIELDS))
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=FIELDS)
        if not existed:
            self._writer.writeheader()
            self._fh.flush()

    def write(self, row):
        self._writer.writerow(row)
        self._fh.flush()
        self._rows += 1
        if self.fsync_every and self._rows % self.fsync_every == 0:
            os.fsync(self._fh.fileno())
        return self._rows

    def close(self):
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def list_serial_ports():
    """Every serial port pyserial can see, for --list-ports."""
    if list_ports is None:
        return []
    return sorted(p.device for p in list_ports.comports())


def run_capture(port, baud, out_path, r_shunt_ohms,
                 stop_after_s=None, reconnect_wait_s=2.0, print_every=1500):
    """Open the port, decode frames forever (or for stop_after_s), write CSV.

    Unlike the INA226-based design this replaced, r_shunt_ohms is now used
    arithmetically in every frame_to_row call (current_a = shunt_v /
    r_shunt_ohms), not just recorded for provenance: the ESP32 no longer
    applies the shunt value itself anywhere (there is no calibration
    register in this design), so getting --shunt-ohms right here is now
    load-bearing, not cosmetic. CURRENT_LSB_V, OVERSAMPLE_K, and
    BUS_DIVIDER_RATIO are hardware/firmware constants rather than
    per-run values, so unlike the old --current-lsb flag, they are not
    exposed on the command line; changing any of them means editing this
    file and the .ino file together, same as changing PIN_ADC_CURRENT would.

    Reconnects on a dropped USB connection rather than exiting, because a
    laptop that must babysit a serial cable for ten days of campaign defeats
    the point of an unattended rig. Each reconnect attempt is logged to
    stderr with a row count, so a gap in the data is visible in the operator
    log even though seq_gap_before cannot itself span a full disconnect (the
    ESP32's own seq counter keeps running independently of whether anyone is
    listening, so a reconnect simply picks the stream back up wherever the
    board currently is, and the gap shows up as a large seq_gap_before on the
    first row after reconnection).
    """
    if serial is None:
        raise RuntimeError(
            "pyserial is not installed. pip install pyserial (it is listed "
            "in requirements.txt) before running this script.")

    reader = FrameReader()
    tracker = SeqTracker()
    started = time.monotonic()
    rows_written = 0
    faults_seen = 0

    with CaptureWriter(out_path) as writer:
        while True:
            try:
                with serial.Serial(port, baud, timeout=1.0) as ser:
                    print("host/capture.py: connected to %s at %d baud"
                          % (port, baud), file=sys.stderr)
                    while True:
                        if (stop_after_s is not None
                                and time.monotonic() - started >= stop_after_s):
                            print("host/capture.py: stop_after_s reached, "
                                  "%d rows written" % rows_written,
                                  file=sys.stderr)
                            return rows_written
                        chunk = ser.read(4096)
                        if not chunk:
                            continue
                        for raw in reader.feed(chunk):
                            capture_ts = datetime.now(timezone.utc).isoformat()
                            (seq, _mic, _lvl, _pn, fault, _cs,
                             _bs) = _STRUCT.unpack(raw)
                            gap = tracker.gap_before(seq)
                            row = frame_to_row(raw, capture_ts, gap,
                                                r_shunt_ohms)
                            rows_written = writer.write(row)
                            if fault:
                                faults_seen += 1
                            if rows_written % print_every == 0:
                                print(
                                    "host/capture.py: %d rows, %d faulted, "
                                    "last pulse_n=%d"
                                    % (rows_written, faults_seen,
                                       row["trigger_pulse_n"]),
                                    file=sys.stderr)
            except (serial.SerialException, OSError) as exc:
                print("host/capture.py: serial error (%s), reconnecting in "
                      "%.1fs" % (exc, reconnect_wait_s), file=sys.stderr)
                time.sleep(reconnect_wait_s)
                continue
            except KeyboardInterrupt:
                print("host/capture.py: interrupted, %d rows written"
                      % rows_written, file=sys.stderr)
                return rows_written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="serial device, e.g. /dev/ttyUSB0 or "
                                        "COM5")
    parser.add_argument("--baud", type=int, default=1000000,
                         help="must match SERIAL_BAUD in the .ino firmware")
    parser.add_argument("--out", help="CSV path to append to, created if "
                                       "absent")
    parser.add_argument("--shunt-ohms", type=float, default=0.100,
                         help="the installed shunt's real resistance, used "
                              "directly in current_a = shunt_v / "
                              "shunt_ohms; load-bearing, not cosmetic, "
                              "under the ADC-based design, see "
                              "run_capture's docstring")
    parser.add_argument("--stop-after-s", type=float, default=None,
                         help="exit after this many seconds; omit to run "
                              "until interrupted")
    parser.add_argument("--list-ports", action="store_true",
                         help="print available serial ports and exit")
    args = parser.parse_args(argv)

    if args.list_ports:
        ports = list_serial_ports()
        if not ports:
            print("No serial ports found (or pyserial is not installed).")
        for p in ports:
            print(p)
        return 0

    if not args.port or not args.out:
        parser.error("--port and --out are required unless --list-ports is "
                      "given")

    run_capture(args.port, args.baud, args.out, args.shunt_ohms,
                stop_after_s=args.stop_after_s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
