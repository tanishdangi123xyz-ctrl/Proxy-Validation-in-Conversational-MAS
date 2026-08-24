"""Onboard INA3221 rails, read from hwmon sysfs.

This is the software measurement path, the one every other paper in this space
relies on and the one this study exists to check against an external rig. It
is not the external measurement: the INA226 on the shunt is on the ESP32's I2C
bus, and the ESP32 is hosted by the logging laptop precisely so the instrument
stays outside the measured domain. The Jetson cannot see that chip at all, and
energy_j_external is joined to these rows afterwards by pulse ordinal.

Sampling is continuous rather than started and stopped around each call. A
sampler that only ran during calls would not be running during measure_idle,
so its own CPU cost would be present in every call and absent from the
baseline subtracted from them, biasing every energy figure upward by the cost
of the instrument. Running always makes that cost a constant that cancels.

Channels are found by the label the kernel reports, never by index. Channel
order is a property of the device tree and moves between JetPack releases; a
reader wired to curr1_input is a reader that silently starts reporting a
different rail after an upgrade.

No project imports. Standard library only, Python 3.10 compatible.
"""

import threading
import time
from collections import deque
from pathlib import Path

HWMON_ROOT = Path("/sys/class/hwmon")

# Record fields are energy_j_ina_vdd_in, _cpu_gpu_cv and _soc, so these keys
# are the field suffixes. The aliases cover the label spellings seen across
# Orin carrier revisions and JetPack releases; an unmatched label is reported
# by the probe rather than guessed at here.
RAIL_ALIASES = {
    "vdd_in": ("vdd_in", "vdd_in_sys", "vdd_sys_in", "pom_5v_in"),
    "cpu_gpu_cv": ("vdd_cpu_gpu_cv", "vdd_cpu_gpu", "vdd_sys_cpu",
                   "pom_5v_gpu"),
    "soc": ("vdd_soc", "vdd_sys_soc", "pom_5v_cpu"),
}

RAIL_KEYS = tuple(RAIL_ALIASES)

NAN = float("nan")


class RailsUnavailable(RuntimeError):
    """Raised when no hwmon device exposes the rails the study records."""


def _read_int(path):
    return int(Path(path).read_text(encoding="utf-8").strip())


def _canonical(label):
    """Map a kernel label onto a record field suffix, or None."""
    lowered = label.strip().lower()
    for key, aliases in RAIL_ALIASES.items():
        if lowered in aliases:
            return key
    return None


class Rail:
    """One INA3221 channel, and how to get watts out of it.

    Voltage and current are preferred over the power node because their hwmon
    units are unambiguous: millivolts and milliamps, defined by the ABI. The
    power node is documented as microwatts but has been reported in milliwatts
    by vendor drivers, and a factor of a thousand in an energy figure looks
    entirely plausible on a plot. Two reads are cheap; a silent unit error is
    not.
    """

    def __init__(self, key, label, volt_path=None, curr_path=None,
                 power_path=None):
        self.key = key
        self.label = label
        self.volt_path = volt_path
        self.curr_path = curr_path
        self.power_path = power_path
        self.source = "volt_x_current" if (volt_path and curr_path) else "power"

    def watts(self):
        """Instantaneous power on this rail. Raises if sysfs refuses."""
        if self.source == "volt_x_current":
            return (_read_int(self.volt_path) * _read_int(self.curr_path)) / 1e6
        return _read_int(self.power_path) / 1e6

    def describe(self):
        return "%s -> %s via %s" % (self.label, self.key, self.source)


def discover_rails(root=HWMON_ROOT):
    """Map record field suffix to Rail, for every rail this study records."""
    found = {}
    root = Path(root)
    try:
        hwmons = sorted(root.glob("hwmon*")) if root.is_dir() else []
    except OSError:
        return found
    for hwmon in hwmons:
        try:
            label_paths = sorted(hwmon.glob("in*_label"))
        except OSError:
            continue
        for label_path in label_paths:
            try:
                label = label_path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            key = _canonical(label)
            if key is None or key in found:
                continue
            channel = label_path.name[len("in"):-len("_label")]
            volt = hwmon / ("in%s_input" % channel)
            curr = hwmon / ("curr%s_input" % channel)
            power = hwmon / ("power%s_input" % channel)
            if volt.exists() and curr.exists():
                found[key] = Rail(key, label, volt_path=volt, curr_path=curr)
            elif power.exists():
                found[key] = Rail(key, label, power_path=power)
    return found


def unmatched_labels(root=HWMON_ROOT):
    """Every hwmon channel label that no alias claims.

    Bring-up reads this. A rail that exists under a spelling not in
    RAIL_ALIASES is a rail that would otherwise be silently absent, and the
    fix is one line in the alias table rather than a debugging session.
    """
    strays = []
    root = Path(root)
    try:
        hwmons = sorted(root.glob("hwmon*")) if root.is_dir() else []
    except OSError:
        return strays
    for hwmon in hwmons:
        try:
            label_paths = sorted(hwmon.glob("*_label"))
        except OSError:
            continue
        for label_path in label_paths:
            try:
                label = label_path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if _canonical(label) is None:
                strays.append((str(label_path), label))
    return strays


def integrate(samples, index):
    """Trapezoidal energy in joules over timestamped power samples.

    samples are (t_monotonic, w_0, w_1, ...) tuples and index selects the rail.

    Returns NaN rather than a partial figure when the series cannot support an
    integral: fewer than two points, or any unreadable sample inside the
    window. A window that lost half its samples produces a number that is too
    small by an unknown amount and looks exactly like a quiet call, which is
    the failure this whole module is arranged to make visible.
    """
    if len(samples) < 2:
        return NAN
    total = 0.0
    previous_t, previous_w = samples[0][0], samples[0][index]
    if previous_w != previous_w:
        return NAN
    for sample in samples[1:]:
        t, w = sample[0], sample[index]
        if w != w:
            return NAN
        total += (t - previous_t) * (w + previous_w) / 2.0
        previous_t, previous_w = t, w
    return total


def mean_watts(samples, index):
    """Time-weighted mean power over a window, for the idle reference."""
    if len(samples) < 2:
        return NAN
    span = samples[-1][0] - samples[0][0]
    if span <= 0:
        return NAN
    energy = integrate(samples, index)
    return energy / span if energy == energy else NAN


def observed_rate_hz(samples):
    """Samples per second actually achieved across a window."""
    if len(samples) < 2:
        return 0.0
    span = samples[-1][0] - samples[0][0]
    return (len(samples) - 1) / span if span > 0 else 0.0


class RailSampler:
    """Background thread appending timestamped power samples to a ring.

    The ring is sized from the longest call the server is allowed to take, so
    a window can always be reconstructed even if a call runs to the server
    timeout. Reconstruction happens in stop(), which the client calls after the
    trigger has gone low, so no scan of this buffer ever falls inside a
    measured window.
    """

    def __init__(self, rails, poll_s, span_s, clock=time.monotonic,
                 sleep=time.sleep):
        self.rails = [rails[key] for key in RAIL_KEYS if key in rails]
        self.keys = [rail.key for rail in self.rails]
        self.poll_s = float(poll_s)
        self.clock = clock
        self._sleep = sleep
        capacity = max(64, int(span_s / max(poll_s, 1e-4)) + 64)
        self.samples = deque(maxlen=capacity)
        self.read_failures = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="ina3221",
                                        daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            row = [self.clock()]
            for rail in self.rails:
                try:
                    row.append(rail.watts())
                except (OSError, ValueError):
                    self.read_failures += 1
                    row.append(NAN)
            self.samples.append(tuple(row))
            self._sleep(self.poll_s)

    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def window(self, t0, t1):
        """Samples between two monotonic marks, inclusive."""
        return [s for s in tuple(self.samples) if t0 <= s[0] <= t1]

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.poll_s * 10))
            self._thread = None
