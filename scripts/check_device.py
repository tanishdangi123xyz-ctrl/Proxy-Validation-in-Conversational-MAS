"""First contact with the Jetson. Run this before committing ten days to it.

Nothing in jetson.py has ever read a real thermal zone. Its policy is tested
against injected clocks and its refusal to construct off-target is tested, but
the sysfs half is unverified until it runs here.

It also answers the question that currently blocks the campaign. THERMAL_TARGET_C
has to be chosen, and it cannot be chosen blind: too low and the gate waits
forever for a device that never cools that far, too high and it waits forever
for one that never reaches it. This samples the device and reports the range it
actually occupies, so the target is picked from evidence.

It is also where bring-up gets the three values that cannot be chosen from a
laptop: the GPIO chip and line for the trigger, and the label this board spells
its INA3221 rails with. Those are device-tree properties, so config ships with
them unset and validate() refuses a run until this script has been read.

    python3 scripts/check_device.py                 what the device reports
    python3 scripts/check_device.py --sample 120    watch it drift under load
    python3 scripts/check_device.py --gate 50       exercise the real gate
    python3 scripts/check_device.py --gpio          chips and free lines
    python3 scripts/check_device.py --rails         INA3221 channels and labels
    python3 scripts/check_device.py --freq          which clock paths resolved

Exit status is nonzero if the device cannot support a campaign.
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masenergy import config, gpio, ina3221, jetson


def sample(device, seconds, interval):
    """SoC temperature over a window, as (elapsed, celsius) pairs."""
    readings = []
    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        readings.append((elapsed, device.read_soc_temp()))
        if elapsed >= seconds:
            return readings
        time.sleep(interval)


def report_zones(root):
    """Every thermal zone the kernel exposes, and which ones jetson.py picks."""
    zones = jetson.discover_zones(root)
    print("THERMAL ZONES under %s" % root)
    if not zones:
        print("  none. This is not a Jetson, or thermal sysfs is not mounted.")
        return zones
    for name, path in sorted(zones.items()):
        try:
            print("  %-24s %6.1f C   %s" % (name, jetson.read_zone_c(path), path))
        except (OSError, ValueError) as exc:
            print("  %-24s unreadable: %s" % (name, exc))
    return zones


def report_selection(device):
    """Which zone each role resolved to, so a wrong sensor is visible."""
    print("\nSELECTED SENSORS")
    for label, path in (("soc (gates on this)", device.soc),
                        ("cpu", device.cpu), ("gpu", device.gpu)):
        print("  %-20s %s" % (label, path if path else "absent, will record NaN"))


def report_state(device):
    """The row-level state every call will carry."""
    print("\nDEVICE STATE")
    state = device.read_state()
    for key in sorted(state):
        print("  %-16s %s" % (key, state[key]))
    if not all(state[k] for k in ("freq_gpu", "freq_cpu", "freq_emc")):
        print("\n  A frequency of 0 means the path was not found, not that the")
        print("  clock is stopped. hw_status carries freq_unreadable on every")
        print("  row this happens to; --freq below says which one is missing.")


def report_stability(readings):
    """What the observed range implies for the target and the tolerance."""
    values = [t for _, t in readings]
    low, high = min(values), max(values)
    spread = high - low
    print("\nSOC TEMPERATURE over %.0fs, %d samples" % (readings[-1][0], len(values)))
    print("  min %.1f   median %.1f   max %.1f   spread %.1f C"
          % (low, statistics.median(values), high, spread))
    if len(values) > 1:
        print("  sample-to-sample sd %.2f C" % statistics.stdev(values))

    print("\nWHAT THIS IMPLIES")
    print("  THERMAL_TARGET_C must be reachable from both directions. A target")
    print("  below the idle floor is never reached by cooling; one above the")
    print("  loaded ceiling is never reached by warming. Either gates every call")
    print("  into a %.0fs timeout." % config.THERMAL_TIMEOUT_S)
    print("  Idle here is around %.0f C. Re-run with --sample during a dry run"
          % low)
    print("  to see the loaded ceiling, then choose a target between the two.")
    if spread > config.THERMAL_TOLERANCE_C:
        print("\n  Idle drift of %.1f C already exceeds THERMAL_TOLERANCE_C of %.1f."
              % (spread, config.THERMAL_TOLERANCE_C))
        print("  A band narrower than the sensor's own noise cannot be held, and")
        print("  every call would wait out the timeout. Widen the tolerance or")
        print("  confirm this drift is real rather than quantisation.")
    return low, high


def report_gate(device, target):
    """Run the real gate against the live device, briefly."""
    print("\nGATE against target %.1f +/- %.1f C, timeout %.0fs"
          % (target, config.THERMAL_TOLERANCE_C, config.THERMAL_TIMEOUT_S))
    saved = config.THERMAL_TARGET_C
    config.THERMAL_TARGET_C = target
    try:
        started = time.monotonic()
        result = device.wait_for_gate()
    finally:
        config.THERMAL_TARGET_C = saved
    print("  gate_wait_s     %.2f  (wall %.2f)"
          % (result["gate_wait_s"], time.monotonic() - started))
    print("  gate_timed_out  %s" % result["gate_timed_out"])
    if result["gate_timed_out"]:
        print("  The device never entered the band. At %.1f C it is %s the target."
              % (device.read_soc_temp(),
                 "above" if device.read_soc_temp() > target else "below"))
    return result


def report_gpio():
    """Every GPIO chip and the lines free to be claimed as the trigger.

    TRIGGER_CHIP and TRIGGER_LINE are unset in config on purpose. The line
    numbering is a device-tree property of this carrier board, so it cannot be
    chosen anywhere but here, and a guessed number either fails to claim or
    drives a pin belonging to something else. Both values get pasted into
    config from this listing.
    """
    print("\nGPIO CHIPS")
    chips = gpio.chip_paths()
    if not chips:
        print("  none under %s. Not a Linux machine with GPIO, or no access."
              % gpio.DEV_ROOT)
        return
    for path in chips:
        try:
            info = gpio.chip_info(path)
        except OSError as exc:
            print("  %-20s unreadable: %s" % (path, exc))
            continue
        print("  %s   %s, %d lines" % (path, info["label"], info["lines"]))
        for offset in range(info["lines"]):
            try:
                line = gpio.line_info(path, offset)
            except OSError:
                continue
            if line["consumer"]:
                continue
            print("      line %-4d %-24s %s"
                  % (offset, line["name"] or "(unnamed)",
                     "output" if line["is_output"] else "free"))
    print("\n  Lines already held by a driver are omitted. Claiming one would")
    print("  fail at the first call rather than at startup.")


def report_rails():
    """The INA3221 channels, and any label the alias table does not know."""
    print("\nONBOARD RAILS under %s" % ina3221.HWMON_ROOT)
    rails = ina3221.discover_rails()
    for key in ina3221.RAIL_KEYS:
        rail = rails.get(key)
        if rail is None:
            print("  %-12s absent, will record NaN and flag meter_rail_missing"
                  % key)
            continue
        try:
            watts = "%.3f W" % rail.watts()
        except (OSError, ValueError) as exc:
            watts = "unreadable: %s" % exc
        print("  %-12s %-34s %s" % (key, rail.describe(), watts))

    strays = ina3221.unmatched_labels()
    if strays:
        print("\n  Labels no alias claims:")
        for path, label in strays:
            print("    %-28s %s" % (label, path))
        print("\n  A rail missing above but listed here is a spelling this board")
        print("  uses that ina3221.RAIL_ALIASES does not. That is a one-line fix.")


def report_frequencies():
    """Which clock paths resolved, so a zero can be told from an absence."""
    print("\nCLOCKS")
    for label, markers in (("gpu", jetson.GPU_DEVFREQ_MARKERS),
                           ("emc", jetson.EMC_DEVFREQ_MARKERS)):
        path = jetson.devfreq_path(markers)
        print("  %-6s %s" % (label, path or "no devfreq node matched %s"
                             % ", ".join(markers)))
    for fallback in jetson.EMC_FALLBACKS:
        try:
            fallback.read_text(encoding="utf-8")
            state = "readable"
        except OSError as exc:
            state = "%s" % exc.strerror
        print("  %-6s %s   %s" % ("emc", fallback, state))
    try:
        cores = len(list(Path("/").glob(jetson.CPUFREQ_GLOB.lstrip("/"))))
    except OSError:
        cores = 0
    print("  %-6s %d online cores reporting" % ("cpu", cores))
    faults = []
    print("  values %s   faults %s"
          % (jetson.read_frequencies(faults), faults or "none"))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Verify the Jetson device readings jetson.py depends on.")
    ap.add_argument("--gpio", action="store_true",
                    help="list GPIO chips and free lines, to choose the trigger pin")
    ap.add_argument("--rails", action="store_true",
                    help="list the INA3221 rails and any unrecognised label")
    ap.add_argument("--freq", action="store_true",
                    help="show which clock paths resolved on this board")
    ap.add_argument("--sample", type=float, default=10.0,
                    help="seconds of SoC temperature to sample")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between samples")
    ap.add_argument("--gate", type=float, default=None,
                    help="exercise the real gate against this target, in C")
    ap.add_argument("--root", default=str(jetson.THERMAL_ROOT))
    a = ap.parse_args(argv)

    print("=" * 68)
    print("DEVICE CHECK   config %s" % config.config_hash())
    print("=" * 68)

    if a.gpio:
        report_gpio()
    if a.rails:
        report_rails()
    if a.freq:
        report_frequencies()

    root = Path(a.root)
    zones = report_zones(root)
    try:
        device = jetson.JetsonDevice(root)
    except jetson.ThermalUnavailable as exc:
        print("\nREFUSED")
        print("  %s" % exc)
        print("\n  This is the correct result off the Jetson. jetson.py fails here")
        print("  rather than reporting zeros that would pass every gate.")
        return 1

    report_selection(device)
    report_state(device)
    if a.sample > 0:
        report_stability(sample(device, a.sample, a.interval))
    if a.gate is not None:
        report_gate(device, a.gate)

    print("\nREADY")
    print("  %d zones, SoC gating on %s." % (len(zones), device.soc))
    print("  Run --gpio --rails --freq to fill in TRIGGER_CHIP, TRIGGER_LINE")
    print("  and any rail alias this board spells differently.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
