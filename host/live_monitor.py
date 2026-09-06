"""Human-readable live view of the ESP32's serial stream, for bring-up only.

host/capture.py is what a real campaign uses: it writes every frame to CSV
and is silent otherwise, by design, so it can run unattended for ten days.
That makes it useless as a first look at a freshly flashed board, since raw
20-byte binary frames printed to a terminal are unreadable noise, there is
no CSV to open yet, and nothing on screen confirms the board is even alive.

This script exists only to answer, by eye, in real time: is the board
sending frames at all, at roughly the right rate, with sane fault bits, and
does trigger_pulse_n visibly increment when the trigger pin is toggled by
hand. It reuses capture.py's own FrameReader and frame_to_row rather than
reimplementing frame decoding a second time, so what this script shows is
guaranteed to match what a real campaign's CSV would contain for the same
bytes, not a second, potentially drifted, interpretation of the wire
format.

Nothing here is written to disk. This is a bring-up tool, not a capture
tool; use host/capture.py once you are ready to actually record a run.

    python3 host/live_monitor.py --port /dev/cu.usbserial-0001
    python3 host/live_monitor.py --port /dev/cu.usbserial-0001 --baud 1000000
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture import FrameReader, frame_to_row, _STRUCT  # noqa: E402

try:
    import serial
except ImportError:
    serial = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True,
                         help="e.g. /dev/cu.usbserial-0001")
    parser.add_argument("--baud", type=int, default=1000000,
                         help="must match SERIAL_BAUD in the .ino firmware")
    parser.add_argument("--every", type=int, default=1,
                         help="print every Nth frame, default 1 (every "
                              "frame); the achievable rate under the "
                              "ADC-based design has not been measured yet "
                              "(see the .ino file's header), so pick a "
                              "value here after watching the raw rate for "
                              "a moment with --every 1")
    parser.add_argument("--shunt-ohms", type=float, default=0.100,
                         help="must match R_SHUNT_OHMS in the .ino "
                              "firmware; only affects the printed "
                              "current/power figures here, not the "
                              "trigger/fault/rate readout this tool exists "
                              "for")
    args = parser.parse_args(argv)

    if serial is None:
        print("pyserial is not installed. Run: pip3 install pyserial",
              file=sys.stderr)
        return 1

    reader = FrameReader()
    count = 0
    last_pulse_n = None
    last_report = time.monotonic()
    frames_since_report = 0

    print("host/live_monitor.py: connecting to %s at %d baud"
          % (args.port, args.baud))
    print("Ctrl+C to stop. Watching for: a steady frame rate (no specific "
          "target yet, see the .ino file's header on why), "
          "startup_calibration_not_run clearing a moment after boot, "
          "current_adc_range/bus_adc_range staying clear once the rig is "
          "wired up, and trigger_pulse_n incrementing when you toggle the "
          "trigger pin.")
    print("-" * 72)

    try:
        with serial.Serial(args.port, args.baud, timeout=1.0) as ser:
            while True:
                chunk = ser.read(4096)
                if not chunk:
                    continue
                for raw in reader.feed(chunk):
                    count += 1
                    frames_since_report += 1
                    if count % args.every != 0:
                        continue
                    row = frame_to_row(raw, "", 0, args.shunt_ohms)
                    pulse_n = row["trigger_pulse_n"]
                    edge_marker = ""
                    if last_pulse_n is not None and pulse_n != last_pulse_n:
                        edge_marker = "  <-- pulse_n changed (%d -> %d)" % (
                            last_pulse_n, pulse_n)
                    last_pulse_n = pulse_n
                    fault = row["fault"] or "clean"
                    print("frame %8d  trig=%d  pulse_n=%-6d fault=%-55s%s"
                          % (count, row["trigger_level"], pulse_n, fault,
                             edge_marker))

                now = time.monotonic()
                if now - last_report >= 2.0:
                    rate = frames_since_report / (now - last_report)
                    print("-- rate: %.1f frames/sec (no target yet under "
                          "the ADC-based design; this line is how the "
                          "real achieved rate gets measured) --" % rate)
                    frames_since_report = 0
                    last_report = now
    except KeyboardInterrupt:
        print("\nstopped, %d frames seen" % count)
        return 0
    except serial.SerialException as exc:
        print("serial error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
