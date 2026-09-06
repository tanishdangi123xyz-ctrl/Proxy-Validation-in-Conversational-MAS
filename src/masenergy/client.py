"""The single choke point through which every model call passes.

Between trigger high and trigger low, nothing happens except one HTTP request
to llama.cpp. That is the whole basis of per-call energy attribution, and it
is enforced structurally here rather than left to caller discipline.

Concurrency is made impossible rather than merely avoided: a module-level lock
serialises every call, so two trigger windows can never overlap. Debate's
parallel first round is parallel in its conditioning, not in time.

Hardware sits behind three interfaces with no-op implementations, so the whole
orchestrator runs on a laptop against a local llama.cpp before the Jetson
exists. The real implementations live in jetson.py over the drivers in gpio.py
and ina3221.py, and none of them appear here: this file is what a reader has to
follow to be convinced the trigger brackets exactly one call, and it is worth
keeping short enough to read.

A hardware read that fails is recorded and the run continues. Every reading
returned to this module therefore carries a faults tuple alongside its values,
which is collected into the hw_status column, and every failed measurement is
NaN rather than zero. See the schema docstring in records.py for why.

Standard library only, Python 3.10 compatible.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import chat, config
from .records import CallRecord, hw_status as records_hw_status, utc_now

_CALL_LOCK = threading.Lock()


class Trigger:
    """Raises a line the sampler watches to mark one call's boundaries.

    status() is read once per call, after the trigger has gone low. It carries
    the pulse ordinal, which is what joins a row to the external rig's stream:
    the ESP32 keeps its own clock, so timestamps cannot align the two, but the
    nth pulse it counted is the nth call this process made. A missed edge then
    shows as a gap in the sequence rather than as a one-row offset that
    silently misattributes every call after it.
    """

    def high(self):
        return time.time()

    def low(self):
        return time.time()

    def status(self):
        return {"trigger_pulse_n": 0, "trigger_edge_us": 0.0, "faults": ()}

    def close(self):
        pass


class Device:
    """Thermal gate, device state, and the hardware safety watchdog.

    wait_for_gate blocks until the SoC is inside the target band, in both
    directions: too hot waits, too cold warms. A cold device is as
    unrepresentative as a hot one. That gate is about measurement
    comparability and its only failure mode is gate_timed_out, a flagged row,
    because faults never stop a run, per client.py's own module docstring.

    start_safety_watchdog and safety_tripped are a different, deliberately
    stricter mechanism layered on top: protecting the physical hardware
    rather than the data. Unlike every other fault in this codebase, a
    tripped safety watchdog is meant to stop the campaign outright, not get
    flagged and continued past, because the thing at risk is the board
    itself, not just this run's comparability. See ThermalEmergency and
    LlamaClient.call() below, and jetson.ThermalWatchdog for the real
    implementation; both are no-ops here so a laptop or --dry run is never
    affected by a check that only means something on the real device.
    """

    def wait_for_gate(self):
        return {"gate_wait_s": 0.0, "gate_timed_out": False}

    def start_safety_watchdog(self):
        """Begin background monitoring, for the campaign's whole lifetime.

        A no-op here. JetsonDevice overrides this to start
        jetson.ThermalWatchdog; nothing on a laptop or under --dry ever has
        anything to protect.
        """
        pass

    def safety_tripped(self):
        """None while safe, or a dict describing the trip once it fires.

        Checked once per call, before any hardware is touched (see
        LlamaClient.call()). Returning None unconditionally here is correct
        for the Null path: there is no real hardware to endanger, so there is
        nothing to trip.
        """
        return None

    def read_state(self):
        return {
            "temp_c_soc": 0.0,
            "temp_c_cpu": 0.0,
            "temp_c_gpu": 0.0,
            "ambient_c": 0.0,
            "freq_gpu": 0,
            "freq_cpu": 0,
            "freq_emc": 0,
            "nvpmodel_mode": str(config.NVPMODEL_MODE or ""),
            "fan_pwm": config.FAN_PWM,
            "faults": (),
        }

    def close(self):
        pass


class EnergyMeter:
    """The onboard rails, and the columns the external rig fills in later.

    energy_j_external is never produced here. The external INA226 is on the
    ESP32's bus and the ESP32 is hosted by the logging laptop, deliberately, so
    that the instrument's own draw stays outside the shunt. Nothing running on
    the Jetson can read it; the column is joined afterwards on trigger_pulse_n.
    """

    def start(self):
        pass

    def stop(self):
        return {
            "energy_j_external": 0.0,
            "energy_j_ina_vdd_in": 0.0,
            "energy_j_ina_cpu_gpu_cv": 0.0,
            "energy_j_ina_soc": 0.0,
            "idle_w_reference": 0.0,
            "meter_samples_n": 0,
            "meter_rate_hz": 0.0,
            "meter_window_s": 0.0,
            "faults": (),
        }

    def measure_idle(self, seconds):
        """Idle power with the server loaded and resident.

        Measured server-up, never server-down: resident weights draw refresh
        power and the process holds a CUDA context, so a server-down baseline
        would attribute static residency to every call.
        """
        return {"idle_w_external": 0.0, "idle_w_ina_vdd_in": 0.0,
                "duration_s": float(seconds), "meter_samples_n": 0,
                "meter_rate_hz": 0.0, "faults": ()}

    def close(self):
        pass


class NullTrigger(Trigger):
    pass


class NullDevice(Device):
    pass


class NullEnergyMeter(EnergyMeter):
    pass


class ServerError(RuntimeError):
    pass


class ThermalEmergency(RuntimeError):
    """Raised when the hardware safety watchdog has tripped.

    Deliberately not a fault that gets recorded and continued past.
    Everything else in this module's fault handling exists to keep a
    campaign running through a bad sensor or a dead thread, because losing
    ten days of data over a glitch is expensive and the row is already
    flagged. This is the one exception to that policy: the thing being
    protected here is the board itself, and continuing to issue calls to a
    device already over its safety ceiling is not a defensible trade against
    losing a run. Runner.run() and scripts/run_campaign.py let this
    propagate rather than catching and continuing, on purpose.
    """
    pass


def _as_cell(value):
    """Render a validator's extracted value for one CSV cell.

    Not every validator returns a string. The planner's returns a list of
    subtasks, and str() on a list writes a Python repr into a data column that
    every other row uses for an answer.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v) for v in value)
    return str(value)


class LlamaClient:
    """Issues calls to llama.cpp and writes one record per call."""

    def __init__(self, writer, trigger=None, device=None, meter=None,
                 host=None, port=None, run_id="", debug_truncated_dir=None):
        self.writer = writer
        self.trigger = trigger or NullTrigger()
        self.device = device or NullDevice()
        self.meter = meter or NullEnergyMeter()
        self.host = host or config.SERVER_HOST
        self.port = port or config.SERVER_PORT
        self.run_id = run_id
        self.consecutive_faults = 0
        self.config_hash = config.config_hash()
        self.prompts_hash = chat.prompts_hash()
        self._url = "http://%s:%d/completion" % (self.host, self.port)
        # None by default: zero cost, zero behaviour difference to any call.
        # Set to inspect what the model was doing when it hit MAX_TOKENS,
        # which the CSV cannot answer because raw completion text is not a
        # measurement and is not stored there. Written after the trigger has
        # already gone low, so it cannot perturb a measured window.
        self.debug_truncated_dir = (Path(debug_truncated_dir)
                                     if debug_truncated_dir else None)

    def _payload(self, prompt, temperature, seed):
        """Every sampler the server has a default for is named explicitly.

        A parameter left out of the payload is still applied, just by
        llama.cpp's default rather than by us, and it will not appear in
        config.snapshot(). min_p defaults to 0.05, which truncates the tail the
        temperature sweep exists to widen, so an unnamed min_p would quietly
        cap the effect this study is trying to measure.
        """
        return {
            "prompt": prompt,
            "temperature": float(temperature),
            "seed": int(seed),
            "n_predict": config.MAX_TOKENS,
            "top_p": config.TOP_P,
            "top_k": config.TOP_K,
            "min_p": config.MIN_P,
            "typical_p": config.TYPICAL_P,
            "repeat_penalty": config.REPEAT_PENALTY,
            "presence_penalty": config.PRESENCE_PENALTY,
            "frequency_penalty": config.FREQUENCY_PENALTY,
            "mirostat": config.MIROSTAT,
            "cache_prompt": config.CACHE_PROMPT,
            "stream": False,
        }

    def _post(self, payload):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=config.SERVER_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ServerError("llama.cpp unreachable at %s: %s" % (self._url, exc))

    def health(self):
        """True if a server is answering on the configured port."""
        try:
            url = "http://%s:%d/health" % (self.host, self.port)
            with urllib.request.urlopen(url, timeout=5.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _hw_status(self, *readings):
        """Every fault this call collected, as one cell, and shout if they persist.

        Faults never stop a run. A ten-day campaign that dies on day six
        because a fan controller went quiet has lost six days of accuracy data
        along with the energy data, whereas a run that flags the affected rows
        keeps everything and loses only the blocks that were actually spoiled.

        What faults do get is loud. A single flagged row is a glitch; a
        hundred consecutive flagged rows is an instrument that came unplugged
        during the night, and the difference has to be visible in the terminal
        rather than only in a column nobody reads until the campaign ends.
        """
        faults = set()
        for reading in readings:
            faults.update(reading.get("faults", ()))
        status = records_hw_status(faults)

        if not status:
            self.consecutive_faults = 0
            return status

        self.consecutive_faults += 1
        alert_every = max(1, int(config.HW_FAULT_ALERT_EVERY))
        if self.consecutive_faults % alert_every == 0:
            sys.stderr.write(
                "  hardware fault on %d consecutive calls: %s\n"
                % (self.consecutive_faults, status))
        return status

    def call(self, prompt, temperature, seed, context):
        """Issue exactly one model call and write exactly one record.

        context supplies the identity fields: dataset, item_id, topology,
        role, round_index, call_index_in_task, repetition, is_retry,
        retry_reason.

        Returns (text, record).
        """
        with _CALL_LOCK:
            trip = self.device.safety_tripped()
            if trip is not None:
                raise ThermalEmergency(
                    "Hardware safety watchdog tripped, refusing to start a "
                    "new call: %s. This call was never issued; nothing after "
                    "the trip was measured. See jetson.ThermalWatchdog and "
                    "config.THERMAL_SAFETY_LIMIT_C." % trip)

            gate = self.device.wait_for_gate()
            before = self.device.read_state()

            self.meter.start()
            t_high = self.trigger.high()
            t0 = time.monotonic()
            try:
                resp = self._post(self._payload(prompt, temperature, seed))
            finally:
                t1 = time.monotonic()
                t_low = self.trigger.low()
                energy = self.meter.stop()

            after = self.device.read_state()
            pulse = self.trigger.status()

        # Response validation lives here rather than in _post so it covers any
        # transport, and so it runs after the trigger has gone low. llama.cpp
        # can answer 200 with an error body; treating that as an empty
        # completion would spend three retries on a server that is not going to
        # recover, and record them as the model failing to follow the format.
        if not isinstance(resp, dict):
            raise ServerError("llama.cpp returned %s, not an object" % type(resp).__name__)
        if resp.get("error"):
            raise ServerError("llama.cpp returned an error: %s" % (resp["error"],))

        timings = resp.get("timings", {}) or {}
        text = resp.get("content", "") or ""

        prompt_n = int(timings.get("prompt_n", resp.get("tokens_evaluated", 0)) or 0)
        prompt_n_total = int(resp.get("tokens_evaluated", prompt_n) or 0)
        if not config.CACHE_PROMPT and prompt_n_total and prompt_n != prompt_n_total:
            raise ServerError(
                "prompt cache is live despite cache_prompt=false: the server "
                "prefilled %d of %d prompt tokens. Energy would be attributed "
                "to tokens it never processed. Check --cache-reuse and "
                "--slot-save-path on the llama-server command line."
                % (prompt_n, prompt_n_total)
            )

        record = CallRecord(
            run_id=self.run_id,
            config_hash=self.config_hash,
            prompts_hash=self.prompts_hash,
            timestamp_utc=utc_now(),

            dataset=context.get("dataset", ""),
            item_id=context.get("item_id", ""),
            topology=context.get("topology", ""),
            temperature=float(temperature),
            seed=int(seed),
            repetition=int(context.get("repetition", 0)),

            role=context.get("role", ""),
            round_index=int(context.get("round_index", 0)),
            call_index_in_task=int(context.get("call_index_in_task", 0)),
            is_retry=bool(context.get("is_retry", False)),
            retry_reason=context.get("retry_reason", ""),

            prompt_n=prompt_n,
            prompt_n_total=prompt_n_total,
            predicted_n=int(timings.get("predicted_n", resp.get("tokens_predicted", 0)) or 0),
            slot_cache_n=int(resp.get("tokens_cached", 0) or 0),

            wall_clock_ms_orchestrator=(t1 - t0) * 1000.0,
            server_prefill_ms=float(timings.get("prompt_ms", 0.0) or 0.0),
            server_decode_ms=float(timings.get("predicted_ms", 0.0) or 0.0),

            trigger_high_ts=t_high,
            trigger_low_ts=t_low,
            trigger_pulse_n=int(pulse["trigger_pulse_n"]),
            trigger_edge_us=float(pulse["trigger_edge_us"]),

            energy_j_external=energy["energy_j_external"],
            energy_j_ina_vdd_in=energy["energy_j_ina_vdd_in"],
            energy_j_ina_cpu_gpu_cv=energy["energy_j_ina_cpu_gpu_cv"],
            energy_j_ina_soc=energy["energy_j_ina_soc"],
            idle_w_reference=energy["idle_w_reference"],

            meter_samples_n=int(energy["meter_samples_n"]),
            meter_rate_hz=float(energy["meter_rate_hz"]),
            meter_window_s=float(energy["meter_window_s"]),
            hw_status=self._hw_status(pulse, energy, before, after),

            temp_c_soc_before=before["temp_c_soc"],
            temp_c_soc_after=after["temp_c_soc"],
            temp_c_cpu=after["temp_c_cpu"],
            temp_c_gpu=after["temp_c_gpu"],
            ambient_c=after["ambient_c"],

            gate_wait_s=gate["gate_wait_s"],
            gate_timed_out=gate["gate_timed_out"],

            freq_gpu=after["freq_gpu"],
            freq_cpu=after["freq_cpu"],
            freq_emc=after["freq_emc"],
            nvpmodel_mode=after["nvpmodel_mode"],
            fan_pwm=after["fan_pwm"],

            finish_reason=resp.get("stop_type", "") or "",
            truncated=bool(resp.get("truncated", False)),
            thinking_leak=("<think>" in text or "</think>" in text),
        )

        if self.debug_truncated_dir and record.finish_reason == "limit":
            self.debug_truncated_dir.mkdir(parents=True, exist_ok=True)
            name = ("%s-%s-t%s-round%d-%s.txt"
                    % (record.dataset or "?", record.item_id or "?",
                       temperature, record.round_index,
                       record.timestamp_utc.replace(":", "")))
            (self.debug_truncated_dir / name).write_text(
                "PROMPT\n======\n%s\n\nCOMPLETION (cut off at MAX_TOKENS)\n"
                "===================================\n%s\n" % (prompt, text),
                encoding="utf-8")

        return text, record

    def call_with_retries(self, prompt, temperature, seed, context, validator):
        """Call until validator accepts, up to MAX_REPROMPTS extra attempts.

        Each attempt is a separate call, a separate trigger window and a
        separate record. Retries are the mechanism the temperature-as-cause
        question depends on, so they are measured, never hidden.

        validator(text) returns (ok, extracted). Returns
        (text, extracted, ok, [records]).
        """
        records = []
        attempt = 0
        while True:
            ctx = dict(context)
            ctx["is_retry"] = attempt > 0
            ctx["retry_reason"] = "parse_failure" if attempt > 0 else ""
            ctx["call_index_in_task"] = context.get("call_index_in_task", 0)

            text, record = self.call(prompt, temperature, seed + attempt, ctx)
            ok, extracted = validator(text)
            record.parse_ok = bool(ok)
            record.answer_extracted = _as_cell(extracted)
            records.append(record)
            self.writer.write(record)

            if ok or attempt >= config.MAX_REPROMPTS:
                return text, extracted, bool(ok), records
            attempt += 1
