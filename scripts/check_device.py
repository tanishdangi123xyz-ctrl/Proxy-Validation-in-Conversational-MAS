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

Also confirms, by default and unconditionally (see report_trip_points()),
whether this board exposes a kernel-enforced "critical" thermal trip point,
the one hardware/kernel backstop that survives a dead Python process, an
unstarted jetson.ThermalWatchdog, or a bug in this codebase's own software
safety mechanism. config.THERMAL_SAFETY_LIMIT_C protects the board only as
long as this process is alive and correctly wired in; this check is how
that assumption gets replaced with an actual confirmed fact about this
specific unit rather than an assumption about Jetsons in general.

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


def read_trip_points(zone_temp_path):
    """Kernel-defined trip points for one zone, as [(type, celsius), ...].

    Independent of anything this codebase configures. The Linux thermal
    sysfs ABI (Documentation/ABI/testing/sysfs-class-thermal) exposes each
    zone's own device-tree-defined trip points as trip_point_N_type ("
    critical", "hot", "passive", "active", ...) and trip_point_N_temp
    (millidegrees) files alongside the zone's own temp file. "critical" is
    the one that matters most here: crossing it makes the kernel itself
    shut the board down, with no dependency on this process, Python, or
    jetson.ThermalWatchdog being alive to react. That is a backstop this
    codebase cannot take credit for and does not implement, it already
    exists in the kernel/device-tree on a real Jetson, and this function
    exists only to make it visible and confirmed rather than assumed.
    """
    zone_dir = Path(zone_temp_path).parent
    points = []
    try:
        type_paths = sorted(zone_dir.glob("trip_point_*_type"))
    except OSError:
        return points
    for type_path in type_paths:
        temp_path = zone_dir / type_path.name.replace("_type", "_temp")
        try:
            kind = type_path.read_text(encoding="utf-8").strip()
            milli = int(temp_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        points.append((kind, milli / 1000.0))
    return points


def report_trip_points(zones):
    """The kernel's own thermal trip points, an independent backstop.

    jetson.ThermalWatchdog (config.THERMAL_SAFETY_LIMIT_C, 90 C by default)
    is a software mechanism: it depends on this Python process being alive
    and the campaign's own code path being the one running. A "critical"
    trip point here is enforced by the kernel itself regardless of what
    userspace is doing, and is the backstop that exists even if the
    watchdog thread died, the script crashed, or a future run forgot to
    call start_safety_watchdog() at all. This has never been confirmed on
    a real Jetson from this repository; it is reported here rather than
    assumed, the same discipline every other bring-up check in this file
    applies.
    """
    print("\nKERNEL THERMAL TRIP POINTS (independent of THERMAL_SAFETY_LIMIT_C)")
    any_critical = False
    for name, path in sorted(zones.items()):
        points = read_trip_points(path)
        if not points:
            print("  %-24s no trip points exposed under %s"
                  % (name, Path(path).parent))
            continue
        for kind, celsius in points:
            flag = "  <-- kernel-enforced shutdown" if kind == "critical" else ""
            print("  %-24s %-10s %6.1f C%s" % (name, kind, celsius, flag))
            if kind == "critical":
                any_critical = True
    print()
    if any_critical:
        print("  At least one 'critical' trip point is exposed. NVIDIA's own")
        print("  Thermal Design Guide documents 105 C as the Orin SoC hardware")
        print("  shutdown temperature; confirm the number(s) above are in that")
        print("  neighbourhood, not something unexpectedly low or high, and")
        print("  keep THERMAL_SAFETY_LIMIT_C (90 C) well under whatever this")
        print("  reports on this specific unit, not just under NVIDIA's spec.")
    else:
        print("  No 'critical' trip point found on any zone. Do not assume one")
        print("  exists but is merely unreadable: confirm with the vendor/L4T")
        print("  release notes for this specific carrier board and JetPack")
        print("  version before treating THERMAL_SAFETY_LIMIT_C as the only")
        print("  thing standing between a runaway load and hardware damage.")
    return any_critical


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
    has_critical_trip = report_trip_points(zones)
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
    if not has_critical_trip:
        print("\n  WARNING: no kernel 'critical' thermal trip point was found")
        print("  above. jetson.ThermalWatchdog is a software mechanism only;")
        print("  do not treat it as sufficient on its own for a multi-day")
        print("  unattended campaign without understanding why this board")
        print("  has no visible kernel-level backstop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
