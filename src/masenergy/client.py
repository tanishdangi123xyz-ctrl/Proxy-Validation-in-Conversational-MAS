"""The single choke point through which every model call passes.

Between trigger high and trigger low, nothing happens except one HTTP request
to llama.cpp. That is the whole basis of per-call energy attribution, and it
is enforced structurally here rather than left to caller discipline.

Concurrency is made impossible rather than merely avoided: a module-level lock
serialises every call, so two trigger windows can never overlap. Debate's
parallel first round is parallel in its conditioning, not in time.

Hardware sits behind three interfaces with no-op implementations, so the whole
orchestrator runs on a laptop against a local llama.cpp before the Jetson
exists. Only the three real implementations remain untested at deploy time.

Standard library only, Python 3.10 compatible.
"""

import json
import threading
import time
import urllib.error
import urllib.request

from . import chat, config
from .records import CallRecord, utc_now

_CALL_LOCK = threading.Lock()


class Trigger:
    """Raises a line the sampler watches to mark one call's boundaries."""

    def high(self):
        return time.time()

    def low(self):
        return time.time()

    def close(self):
        pass


class Device:
    """Thermal gate and device state.

    wait_for_gate blocks until the SoC is inside the target band, in both
    directions: too hot waits, too cold warms. A cold device is as
    unrepresentative as a hot one.
    """

    def wait_for_gate(self):
        return {"gate_wait_s": 0.0, "gate_timed_out": False}

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
        }


class EnergyMeter:
    """External INA226 rig plus the onboard rails."""

    def start(self):
        pass

    def stop(self):
        return {
            "energy_j_external": 0.0,
            "energy_j_ina_vdd_in": 0.0,
            "energy_j_ina_cpu_gpu_cv": 0.0,
            "energy_j_ina_soc": 0.0,
            "idle_w_reference": 0.0,
        }

    def measure_idle(self, seconds):
        """Idle power with the server loaded and resident.

        Measured server-up, never server-down: resident weights draw refresh
        power and the process holds a CUDA context, so a server-down baseline
        would attribute static residency to every call.
        """
        return {"idle_w_external": 0.0, "idle_w_ina_vdd_in": 0.0,
                "duration_s": float(seconds)}


class NullTrigger(Trigger):
    pass


class NullDevice(Device):
    pass


class NullEnergyMeter(EnergyMeter):
    pass


class ServerError(RuntimeError):
    pass


class LlamaClient:
    """Issues calls to llama.cpp and writes one record per call."""

    def __init__(self, writer, trigger=None, device=None, meter=None,
                 host=None, port=None, run_id=""):
        self.writer = writer
        self.trigger = trigger or NullTrigger()
        self.device = device or NullDevice()
        self.meter = meter or NullEnergyMeter()
        self.host = host or config.SERVER_HOST
        self.port = port or config.SERVER_PORT
        self.run_id = run_id
        self.config_hash = config.config_hash()
        self.prompts_hash = chat.prompts_hash()
        self._url = "http://%s:%d/completion" % (self.host, self.port)

    def _payload(self, prompt, temperature, seed):
        return {
            "prompt": prompt,
            "temperature": float(temperature),
            "seed": int(seed),
            "n_predict": config.MAX_TOKENS,
            "top_p": config.TOP_P,
            "top_k": config.TOP_K,
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

    def call(self, prompt, temperature, seed, context):
        """Issue exactly one model call and write exactly one record.

        context supplies the identity fields: dataset, item_id, topology,
        role, round_index, call_index_in_task, repetition, is_retry,
        retry_reason.

        Returns (text, record).
        """
        with _CALL_LOCK:
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

        timings = resp.get("timings", {}) or {}
        text = resp.get("content", "") or ""

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

            prompt_n=int(timings.get("prompt_n", resp.get("tokens_evaluated", 0)) or 0),
            predicted_n=int(timings.get("predicted_n", resp.get("tokens_predicted", 0)) or 0),
            cached_n=int(resp.get("tokens_cached", 0) or 0),

            wall_clock_ms_orchestrator=(t1 - t0) * 1000.0,
            server_prefill_ms=float(timings.get("prompt_ms", 0.0) or 0.0),
            server_decode_ms=float(timings.get("predicted_ms", 0.0) or 0.0),

            trigger_high_ts=t_high,
            trigger_low_ts=t_low,

            energy_j_external=energy["energy_j_external"],
            energy_j_ina_vdd_in=energy["energy_j_ina_vdd_in"],
            energy_j_ina_cpu_gpu_cv=energy["energy_j_ina_cpu_gpu_cv"],
            energy_j_ina_soc=energy["energy_j_ina_soc"],
            idle_w_reference=energy["idle_w_reference"],

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
        )
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
            record.answer_extracted = "" if extracted is None else str(extracted)
            records.append(record)
            self.writer.write(record)

            if ok or attempt >= config.MAX_REPROMPTS:
                return text, extracted, bool(ok), records
            attempt += 1
