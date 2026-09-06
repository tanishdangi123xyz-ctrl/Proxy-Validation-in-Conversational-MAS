"""Real hardware readings for the Jetson Orin NX.

Everything the orchestrator knows about the device it is running on. The
interfaces in client.py are no-ops so the whole pipeline can be proven on a
laptop; this is the half that only works on the target, and it is written to
fail loudly off-target rather than to return plausible zeros.

That distinction is the point of the module. A Device that quietly reports
0.0 degrees on a machine with no thermal zones would pass every gate, satisfy
every check, and write a campaign in which temperature was never controlled
and nothing recorded says so.

Standard library only, Python 3.10 compatible.
"""

import sys
import threading
import time
from pathlib import Path

from . import config, gpio, ina3221
from .client import Device, EnergyMeter, Trigger

THERMAL_ROOT = Path("/sys/class/thermal")

DEVFREQ_ROOT = Path("/sys/class/devfreq")
CPUFREQ_GLOB = "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"
GPU_DEVFREQ_MARKERS = ("gpu", "ga10b", "gv11b", "gp10b")
EMC_DEVFREQ_MARKERS = ("emc",)
EMC_FALLBACKS = (Path("/sys/kernel/debug/bpmp/debug/clk/emc/rate"),)

SOC_ZONE_NAMES = ("tj-therm", "soc0-therm", "soc1-therm", "soc2-therm",
                  "ao-therm", "thermal-fan-est")
CPU_ZONE_NAMES = ("cpu-therm", "cpu-thermal")
GPU_ZONE_NAMES = ("gpu-therm", "gpu-thermal")

NVPMODEL_STATUS = Path("/var/lib/nvpmodel/status")
FAN_PWM_GLOB = "/sys/class/hwmon/hwmon*/pwm1"


class ThermalUnavailable(RuntimeError):
    """Raised when the thermal zones a gate depends on cannot be read."""


def discover_zones(root=THERMAL_ROOT):
    """Map lowercased thermal zone type to the file holding its temperature.

    Zone numbering is not stable across JetPack releases or across boots, so
    zones are found by the name they report rather than by index. A gate wired
    to thermal_zone0 is a gate that silently starts reading a different sensor
    after an upgrade.
    """
    zones = {}
    if not root.is_dir():
        return zones
    for zone in sorted(root.glob("thermal_zone*")):
        try:
            name = (zone / "type").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if name and (zone / "temp").exists():
            zones.setdefault(name, zone / "temp")
    return zones


def read_zone_c(path):
    """Temperature in degrees Celsius from a sysfs thermal zone.

    The kernel reports millidegrees. Reading the file as degrees is a factor of
    a thousand error that would put every reading far below any sane target and
    hold the gate open until it timed out on every call.
    """
    return int(Path(path).read_text(encoding="utf-8").strip()) / 1000.0


def _first_present(zones, names):
    for name in names:
        if name in zones:
            return zones[name]
    return None


def wait_until_in_band(read_temp, target, tolerance, timeout_s, poll_s,
                       monotonic=time.monotonic, sleep=time.sleep):
    """Block until a temperature is inside a band, in both directions.

    Waiting on a device that is too cold matters exactly as much as waiting on
    one that is too hot, and the reason is that both change the number being
    measured rather than merely the comfort of the hardware.

    Below the band the silicon leaks less, so static power is lower; the clock
    governor also sees thermal headroom and will hold a boost state it cannot
    sustain once the die warms. A cold call is therefore both faster and drawn
    at a different point on the voltage-frequency curve than the same call
    issued ten minutes later. Above the band the opposite happens and the
    governor throttles. Either way the joules attributed to a token depend on
    when in the block the call landed.

    That would be tolerable if it were noise. It is not: a device is coldest at
    the start of a block, so the bias is aligned with block boundaries, and
    block boundaries are where condition and temperature change. Randomising
    block order stops thermal drift correlating with condition across a
    ten-day campaign, but it does nothing about a within-block warm-up ramp
    that repeats identically in every block. Gating in both directions is what
    makes the first call of a block and the last one comparable.

    Returns {gate_wait_s, gate_timed_out}. Giving up is deliberate: a run that
    hangs because a fan failed on day six has lost six days, whereas a run that
    records gate_timed_out on the affected rows can be filtered at analysis
    time and keeps everything else.

    read_temp, monotonic and sleep are injected so the policy can be exercised
    without hardware and without waiting in real time.
    """
    started = monotonic()
    while True:
        temperature = read_temp()
        waited = monotonic() - started
        if abs(temperature - target) <= tolerance:
            return {"gate_wait_s": waited, "gate_timed_out": False}
        if waited >= timeout_s:
            return {"gate_wait_s": waited, "gate_timed_out": True}
        sleep(min(poll_s, max(0.0, timeout_s - waited)))


def _read_hz(path):
    return int(Path(path).read_text(encoding="utf-8").strip())


def devfreq_path(markers, root=DEVFREQ_ROOT):
    """cur_freq for the first devfreq device whose name carries a marker.

    Matched by name rather than by position because the GPU's devfreq node is
    named after the SoC generation, so the same index means a different device
    on a different chip and the same chip can renumber across releases.
    """
    root = Path(root)
    try:
        if not root.is_dir():
            return None
        devices = sorted(root.iterdir())
    except OSError:
        return None
    for device in devices:
        name = device.name.lower()
        if not any(marker in name for marker in markers):
            continue
        candidate = device / "cur_freq"
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def read_cpu_hz(root=Path("/")):
    """Highest current core frequency, in hertz.

    The maximum across online cores rather than cpu0, because the governor
    parks idle cores and inference does not spread evenly: cpu0 at its floor
    while another core runs at its ceiling would record the floor and read as
    a machine doing nothing during a call.

    scaling_cur_freq is kilohertz and devfreq is hertz. Both are converted
    here so the three frequency columns share one unit; storing them in their
    native units would put a factor of a thousand between two columns that a
    reader would reasonably compare.
    """
    best = 0
    try:
        paths = sorted(Path(root).glob(CPUFREQ_GLOB.lstrip("/")))
    except OSError:
        return 0
    for path in paths:
        try:
            best = max(best, _read_hz(path) * 1000)
        except (OSError, ValueError):
            continue
    return best


def read_frequencies(faults=None, devfreq_root=DEVFREQ_ROOT, cpu_root=Path("/")):
    """GPU, CPU and EMC clocks in hertz, with 0 for anything unreadable.

    Candidates are read rather than tested for existence. Path.exists() has to
    stat the file, and stat raises PermissionError on a path whose parent this
    user cannot traverse: the EMC clock lives under debugfs, which is
    root-only, so probing for it politely is what would abort a campaign
    running unprivileged. Attempting the read and catching OSError covers
    absent, unreadable and forbidden in one branch, which are the same thing
    from here.
    """
    values = []
    for markers, fallbacks in ((GPU_DEVFREQ_MARKERS, ()),
                               (None, ()),
                               (EMC_DEVFREQ_MARKERS, EMC_FALLBACKS)):
        if markers is None:
            values.append(read_cpu_hz(cpu_root))
            continue
        candidates = [p for p in (devfreq_path(markers, devfreq_root),) if p]
        candidates.extend(fallbacks)
        value = 0
        for path in candidates:
            try:
                value = _read_hz(path)
                break
            except (OSError, ValueError):
                continue
        values.append(value)
    if faults is not None and not all(values):
        faults.append("freq_unreadable")
    return tuple(values)


class ThermalWatchdog:
    """Background thread that stops a campaign before heat damages the board.

    Structurally mirrors ina3221.RailSampler: a daemon thread polling on its
    own schedule, a threading.Event for a clean shutdown, and a design that
    survives being read from a different thread than the one writing to it.
    The two solve different problems, though. RailSampler exists to make
    power visible; this exists to make continuing dangerous, which is why it
    checks every named thermal zone this device has (SoC always, CPU/GPU
    where present) rather than only the one the comparability gate cares
    about, and why crossing the ceiling latches rather than resets: once
    tripped, this device is not to be trusted with another call this run,
    even if the very next reading happens to come back cooler.

    Runs for the whole lifetime of a campaign, started once in
    JetsonDevice.start_safety_watchdog() before the first call, not
    restarted per block or per call. A cooling failure does not wait
    politely for the next call to begin, and this project's own settle()
    waits and idle sampling are exactly the moments a temperature climbing
    with nothing to show for it in the call records would otherwise go
    unnoticed until the next call's before-reading caught it, which could be
    minutes later at BLOCK_SETTLE_S.
    """

    def __init__(self, zones, limit_c, poll_s, consecutive,
                 clock=time.monotonic, sleep=time.sleep):
        """zones is [(label, read_fn), ...]; read_fn() -> float Celsius.

        Every zone is checked on every poll and the watchdog trips on
        whichever one is worst, since the thing being protected is the one
        physical board underneath all of them, not a single sensor.
        """
        self.zones = list(zones)
        self.limit_c = float(limit_c)
        self.poll_s = float(poll_s)
        self.consecutive = max(1, int(consecutive))
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()
        self._tripped = threading.Event()
        self._trip_info = None
        self._over_streak = 0
        self._thread = None
        self.read_failures = 0

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="thermal-safety", daemon=True)
        self._thread.start()

    def _poll_once(self):
        """One round of every zone; returns the worst (label, temp) pair.

        A zone that fails to read is skipped for this poll rather than
        treated as either safe or unsafe by assumption; soc_temp_unreadable
        and friends already exist to surface a persistently failing sensor
        through the ordinary hw_status path on the next call record, and
        this watchdog would rather miss one noisy poll of a flaky zone than
        either trip on a read error or silently stop protecting the board
        because one of several zones went quiet.
        """
        worst = None
        for label, read_fn in self.zones:
            try:
                temp = float(read_fn())
            except (OSError, ValueError):
                self.read_failures += 1
                continue
            if worst is None or temp > worst[1]:
                worst = (label, temp)
        return worst

    def _step(self, worst, out=sys.stderr):
        """Update trip state from one poll's (label, temp) or None.

        Factored out of _loop so the policy (when does this trip, and does a
        lost reading help or hurt) can be exercised directly against a
        scripted sequence, the same way wait_until_in_band is tested without
        a real thread or real elapsed time. out is injectable so the alert
        message can be captured rather than actually printed in a test.
        """
        if worst is None:
            # Every zone failed to read this poll. Left as a no-op rather
            # than folded into either branch below: treating a lost reading
            # as "safe" (resetting the streak) could erase real progress
            # toward a trip during exactly the kind of sensor flakiness that
            # ought to make an operator more cautious, not less, and
            # treating it as "over limit" (incrementing the streak) would
            # trip the watchdog on read errors alone, with no actual
            # temperature behind it. A persistently failing zone still
            # surfaces through the ordinary soc_temp_unreadable/
            # cpu_temp_unreadable/gpu_temp_unreadable fault path on the next
            # call record.
            return
        if worst[1] < self.limit_c:
            self._over_streak = 0
            return
        self._over_streak += 1
        if self._over_streak < self.consecutive or self._tripped.is_set():
            return
        label, temp = worst
        self._trip_info = {
            "zone": label,
            "temp_c": temp,
            "limit_c": self.limit_c,
            "since_monotonic": self._clock(),
        }
        self._tripped.set()
        # Deliberately loud and immediate, not batched behind
        # HW_FAULT_ALERT_EVERY like an ordinary fault: this is the one
        # condition in the whole pipeline meant to stop an unattended
        # multi-day run, and an operator asleep or away from the terminal
        # needs this line to be the one that is impossible to miss when
        # they do look.
        out.write(
            "\n" + "!" * 72 +
            "\nTHERMAL SAFETY WATCHDOG TRIPPED\n"
            "  zone            %s\n"
            "  temperature     %.1f C\n"
            "  safety limit    %.1f C  (config.THERMAL_SAFETY_LIMIT_C)\n"
            "  consecutive     %d polls over limit, %.1fs apart\n"
            "No further calls will be issued. The campaign is stopping to "
            "protect\nthe hardware. See CHANGES.md for what to check before "
            "restarting.\n"
            % (label, temp, self.limit_c, self._over_streak, self.poll_s)
            + "!" * 72 + "\n\n")

    def _loop(self):
        while not self._stop.is_set():
            self._step(self._poll_once())
            self._sleep(self.poll_s)

    def tripped(self):
        """None while safe, or the trip detail dict once fired. Latches."""
        return dict(self._trip_info) if self._tripped.is_set() else None

    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.poll_s * 10))
            self._thread = None


class JetsonTrigger(Trigger):
    """The GPIO line the ESP32 watches, held open for the whole run.

    high() and low() are one ioctl each on a descriptor claimed at
    construction. Nothing is opened, allocated or formatted between the edge
    and the HTTP request, because every microsecond spent there is attributed
    to the model call by an instrument that has no way of knowing otherwise.

    A failed edge is recorded and the call proceeds. The alternative is
    aborting a ten-day campaign over one ioctl, and the row is already marked
    unusable by the fault, so nothing is silently salvaged by continuing.
    """

    def __init__(self, chip=None, line=None, consumer=None):
        chip = config.TRIGGER_CHIP if chip is None else chip
        line = config.TRIGGER_LINE if line is None else line
        if chip is None or line is None:
            raise gpio.GpioError(
                "TRIGGER_CHIP and TRIGGER_LINE are unset. Run "
                "scripts/check_device.py --gpio on the Jetson and set them "
                "from what it lists; a guessed line either fails to claim or "
                "drives the wrong pin.")
        self.line = gpio.OutputLine(
            chip, line, consumer or config.TRIGGER_CONSUMER, initial=0)
        self.pulse_n = 0
        self._edge_us = 0.0
        self._faults = ()

    def _edge(self, value):
        started = time.perf_counter()
        try:
            self.line.set(value)
            failed = False
        except (OSError, gpio.GpioError):
            failed = True
        elapsed = (time.perf_counter() - started) * 1e6
        return elapsed, failed

    def high(self):
        self.pulse_n += 1
        self._edge_us, failed = self._edge(1)
        self._faults = ("trigger_edge_failed",) if failed else ()
        return time.time()

    def low(self):
        elapsed, failed = self._edge(0)
        self._edge_us = max(self._edge_us, elapsed)
        if failed:
            self._faults = ("trigger_edge_failed",)
        return time.time()

    def status(self):
        """Pulse ordinal and the worse of the two edge costs, in microseconds.

        The ordinal is the join key against the ESP32's stream. It counts
        pulses attempted rather than pulses confirmed, so a failed edge leaves
        the sequence intact and flagged rather than silently shifting every
        later row by one.
        """
        return {"trigger_pulse_n": self.pulse_n,
                "trigger_edge_us": self._edge_us,
                "faults": self._faults}

    def close(self):
        self.line.close()


class JetsonEnergyMeter(EnergyMeter):
    """Energy on the onboard rails, integrated between start() and stop().

    The sampler runs from construction to close() rather than starting and
    stopping around each call, so the thread's own CPU cost is present during
    measure_idle as well as during calls and cancels in the subtraction. A
    sampler that ran only during calls would add its own draw to every call
    and to no baseline.

    The window integrated here is a few hundred microseconds wider than the
    trigger window, because start() precedes the rising edge. On a ten-second
    call that is under a hundredth of a percent, far below the calibration
    error of the rig itself, and the edge-exact figure is what the external
    join produces.
    """

    def __init__(self, rails=None, poll_s=None, span_s=None):
        rails = ina3221.discover_rails() if rails is None else rails
        missing = [key for key in ina3221.RAIL_KEYS if key not in rails]
        if len(missing) == len(ina3221.RAIL_KEYS):
            raise ina3221.RailsUnavailable(
                "No INA3221 rails found under %s. Looked for %s. Run "
                "scripts/check_device.py --rails, which lists every hwmon "
                "label present, and add the spelling this board uses to "
                "ina3221.RAIL_ALIASES."
                % (ina3221.HWMON_ROOT,
                   ", ".join(sorted(
                       a for names in ina3221.RAIL_ALIASES.values()
                       for a in names))))
        self.rails = rails
        self.missing = tuple(missing)
        self.sampler = ina3221.RailSampler(
            rails,
            config.METER_POLL_S if poll_s is None else poll_s,
            config.SERVER_TIMEOUT_S if span_s is None else span_s,
        )
        self.sampler.start()
        self._t0 = None

    def start(self):
        self._t0 = self.sampler.clock()

    def _window(self):
        t1 = self.sampler.clock()
        t0 = self._t0 if self._t0 is not None else t1
        return self.sampler.window(t0, t1)

    def _measure(self, samples):
        """Per-rail energy, plus the evidence needed to judge it.

        A rail this board does not expose is missing, not zero: it reports NaN
        and meter_rail_missing, so the column cannot be mistaken for a rail
        that drew nothing.
        """
        faults = []
        if not self.sampler.alive():
            faults.append("meter_thread_dead")
        if len(samples) < 2:
            faults.append("meter_no_samples")
        rate = ina3221.observed_rate_hz(samples)
        if samples and rate < config.METER_RATE_FLOOR_HZ:
            faults.append("meter_rate_low")
        if self.missing:
            faults.append("meter_rail_missing")

        energies = {}
        for key in ina3221.RAIL_KEYS:
            if key not in self.sampler.keys:
                energies[key] = ina3221.NAN
                continue
            value = ina3221.integrate(samples, self.sampler.keys.index(key) + 1)
            if value != value and samples:
                faults.append("meter_rail_unreadable")
            energies[key] = value

        span = (samples[-1][0] - samples[0][0]) if len(samples) >= 2 else 0.0
        return energies, faults, rate, span

    def stop(self):
        """Energy on each rail across the window, with the evidence to judge it.

        idle_w_reference is NaN here for the same reason energy_j_external is,
        though for a different cause: the baseline is measured every
        IDLE_EVERY_N_CALLS and lives in the block's idle table, so copying the
        last one onto each call row would store a derived value and invite an
        analysis that subtracts a baseline sampled in a different thermal
        state from the call it is subtracted from.
        """
        samples = self._window()
        energies, faults, rate, span = self._measure(samples)
        return {
            "energy_j_external": ina3221.NAN,
            "energy_j_ina_vdd_in": energies["vdd_in"],
            "energy_j_ina_cpu_gpu_cv": energies["cpu_gpu_cv"],
            "energy_j_ina_soc": energies["soc"],
            "idle_w_reference": ina3221.NAN,
            "meter_samples_n": len(samples),
            "meter_rate_hz": rate,
            "meter_window_s": span,
            "faults": tuple(sorted(set(faults))),
        }

    def measure_idle(self, seconds):
        """Mean idle power across a window, with the server up and resident.

        idle_w_external is NaN for the same reason energy_j_external is: the
        external rig is not reachable from this machine, and the joined table
        is where that column acquires a value.
        """
        started = self.sampler.clock()
        time.sleep(float(seconds))
        samples = self.sampler.window(started, self.sampler.clock())
        _, faults, rate, _ = self._measure(samples)
        index = (self.sampler.keys.index("vdd_in") + 1
                 if "vdd_in" in self.sampler.keys else None)
        return {
            "idle_w_external": ina3221.NAN,
            "idle_w_ina_vdd_in": (ina3221.mean_watts(samples, index)
                                  if index else ina3221.NAN),
            "duration_s": float(seconds),
            "meter_samples_n": len(samples),
            "meter_rate_hz": rate,
            "faults": tuple(sorted(set(faults))),
        }

    def close(self):
        self.sampler.close()


class JetsonDevice(Device):
    """Thermal gate and device state read from the running Jetson.

    Construction fails if the SoC zone the gate depends on is absent, so this
    class cannot be instantiated on a machine where it would report zeros. That
    is what separates it from the stub it replaces, and what run_campaign.py's
    stub check is looking for.
    """

    def __init__(self, root=THERMAL_ROOT):
        self.zones = discover_zones(root)
        self.soc = _first_present(self.zones, SOC_ZONE_NAMES)
        self.cpu = _first_present(self.zones, CPU_ZONE_NAMES)
        self.gpu = _first_present(self.zones, GPU_ZONE_NAMES)
        if self.soc is None:
            raise ThermalUnavailable(
                "No SoC thermal zone under %s. Looked for %s and found %s. "
                "The thermal gate cannot run without it, and a campaign "
                "without the gate is a campaign with an uncontrolled "
                "confound."
                % (root, ", ".join(SOC_ZONE_NAMES),
                   ", ".join(sorted(self.zones)) or "no zones at all")
            )
        self._watchdog = None

    def read_soc_temp(self):
        """Current SoC temperature in Celsius. The value the gate acts on."""
        return read_zone_c(self.soc)

    def wait_for_gate(self):
        """Hold until the SoC is inside the target band, or until the timeout."""
        return wait_until_in_band(
            self.read_soc_temp,
            float(config.THERMAL_TARGET_C),
            float(config.THERMAL_TOLERANCE_C),
            float(config.THERMAL_TIMEOUT_S),
            float(config.THERMAL_POLL_S),
        )

    def start_safety_watchdog(self):
        """Start the background thermal safety watchdog, once per device.

        Every zone this board actually exposes is watched, not only the SoC
        zone the comparability gate uses: a GPU or CPU zone running away
        while the SoC zone lags behind it is exactly the asymmetric failure
        a single-zone check would miss. Safe to call more than once; only
        the first call starts a thread.
        """
        if self._watchdog is not None:
            return
        zones = [("soc", self.read_soc_temp)]
        if self.cpu is not None:
            zones.append(("cpu", lambda: read_zone_c(self.cpu)))
        if self.gpu is not None:
            zones.append(("gpu", lambda: read_zone_c(self.gpu)))
        self._watchdog = ThermalWatchdog(
            zones,
            config.THERMAL_SAFETY_LIMIT_C,
            config.THERMAL_SAFETY_POLL_S,
            config.THERMAL_SAFETY_CONSECUTIVE,
        )
        self._watchdog.start()

    def safety_tripped(self):
        """None while safe, or the trip detail once the watchdog has fired.

        None (not False) before start_safety_watchdog() has ever been
        called, deliberately indistinguishable from "safe": a device that is
        not being watched is not thereby unsafe, it is just not this
        method's job to notice a watchdog nobody started. Runner.run()
        starts it unconditionally before the first call for exactly this
        reason, so this branch is not expected to matter in practice.
        """
        if self._watchdog is None:
            return None
        return self._watchdog.tripped()

    def close(self):
        """Stop the watchdog thread alongside everything else this run closes."""
        if self._watchdog is not None:
            self._watchdog.close()

    def _zone_or_nan(self, path, token, faults):
        """Zone temperature, or NaN where a sensor is genuinely absent.

        NaN rather than 0.0 because a missing sensor and a sensor reading zero
        degrees have to be distinguishable in the recorded data, and 0.0 is a
        temperature. The fault token separates the two remaining cases: a
        sensor this board does not have, and a sensor that stopped answering
        partway through a campaign.
        """
        if path is None:
            return float("nan")
        try:
            return read_zone_c(path)
        except (OSError, ValueError):
            faults.append(token)
            return float("nan")

    def read_state(self):
        """Device state recorded either side of every call.

        Frequencies are integers, so they cannot carry NaN the way the
        temperatures do, and 0 Hz is not a value any of these clocks takes.
        An unreadable frequency is therefore reported as 0 with
        freq_unreadable in hw_status, and the flag is the only thing that
        separates it from a clock genuinely at rest, which is why it is a
        flag and not an inference.
        """
        faults = []
        gpu, cpu, emc = read_frequencies(faults)
        temps = {
            "temp_c_soc": self.read_soc_temp(),
            "temp_c_cpu": self._zone_or_nan(self.cpu, "cpu_temp_unreadable",
                                            faults),
            "temp_c_gpu": self._zone_or_nan(self.gpu, "gpu_temp_unreadable",
                                            faults),
        }
        return dict(
            temps,
            ambient_c=float("nan"),
            freq_gpu=gpu,
            freq_cpu=cpu,
            freq_emc=emc,
            nvpmodel_mode=self.read_nvpmodel(faults),
            fan_pwm=self.read_fan_pwm(faults),
            faults=tuple(faults),
        )

    def read_nvpmodel(self, faults=None):
        """Active power mode, from the file nvpmodel writes on every change.

        Falling back to the configured value is not a way of filling the gap:
        the mode a reboot actually left the device in is the thing worth
        recording, and config only says what it was supposed to be. The
        fallback keeps the column populated; the fault says not to trust it.
        """
        try:
            status = NVPMODEL_STATUS.read_text(encoding="utf-8").strip()
        except OSError:
            if faults is not None:
                faults.append("nvpmodel_unreadable")
            return str(config.NVPMODEL_MODE or "")
        return status.split(":")[-1].strip() or str(config.NVPMODEL_MODE or "")

    def read_fan_pwm(self, faults=None):
        """Fan duty cycle, or the configured value when no hwmon exposes one."""
        try:
            paths = sorted(Path("/").glob(FAN_PWM_GLOB.lstrip("/")))
        except OSError:
            paths = []
        for path in paths:
            try:
                return int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
        if faults is not None:
            faults.append("fan_unreadable")
        return config.FAN_PWM
