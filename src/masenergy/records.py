"""Per-call record schema and append-only writer.

One row per model call. The call is the unit of analysis, so a retry is its
own row with its own energy, not an amendment to the row it retried.

Two hashes are stamped, not one. config_hash covers the frozen parameters and
prompts_hash covers the prompt files, which live outside config and would
otherwise change the experiment without changing its recorded identity.

Input and output tokens are stored separately and never summed. The output
token asymmetry question cannot be answered from a total, so no total_tokens
field exists anywhere in the pipeline.

Three token fields, not one, because llama.cpp reports three different things.
prompt_n is what the server actually ran through prefill, prompt_n_total is how
long the prompt was, and slot_cache_n is how much KV the slot held afterwards.
With prompt caching off the first two must be equal; a gap between them means
the server served part of the prompt from cache and the energy of a call no
longer matches the tokens recorded against it. slot_cache_n is roughly
prompt_n + predicted_n and is a diagnostic, not a cache-hit count, which is why
it is no longer called cached_n.

Grading is not stored here. It is a property of a task, computed from the final
answer of a multi-call topology, so it lives in the task table. Carrying an
ungraded correct column on every call row would read as a campaign in which
nothing was ever right.

Nothing derived is stored. Imputed cost is a function of the token counts and
the price schedule, so it is computed during analysis; storing it would let a
price revision silently invalidate old rows.

A failed hardware reading is NaN, never zero, and never absent. Zero joules and
zero degrees are both physically meaningful values, so writing one for a read
that did not happen makes a broken instrument indistinguishable from a quiet
device, and any mean taken over the column afterwards is biased with nothing to
show for it. NaN propagates instead: an analysis that ignores it breaks loudly
rather than reporting a smaller number. hw_status names the fault alongside it,
and the meter_ columns carry the evidence, because the worst failure in this
chain raises no exception at all. A twelve-second call that collected four
samples throws nothing, flags nothing, and produces an energy figure that looks
exactly like data; only the sample count says otherwise.

energy_j_external is the one column no run ever fills. The external rig's
INA226 sits on the ESP32's bus, hosted by the logging laptop so the instrument
stays outside the measured domain, and the Jetson has no path to it. It is NaN
on every row until the join script matches rows to pulses by trigger_pulse_n.

Standard library only, Python 3.10 compatible.
"""

import csv
import json
import os
from dataclasses import dataclass, fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class CallRecord:
    """One model call."""

    run_id: str = ""
    config_hash: str = ""
    prompts_hash: str = ""
    timestamp_utc: str = ""

    dataset: str = ""
    item_id: str = ""
    topology: str = ""
    temperature: float = 0.0
    seed: int = 0
    repetition: int = 0

    role: str = ""
    round_index: int = 0
    call_index_in_task: int = 0
    is_retry: bool = False
    retry_reason: str = ""

    prompt_n: int = 0
    prompt_n_total: int = 0
    predicted_n: int = 0
    slot_cache_n: int = 0

    wall_clock_ms_orchestrator: float = 0.0
    server_prefill_ms: float = 0.0
    server_decode_ms: float = 0.0

    trigger_high_ts: float = 0.0
    trigger_low_ts: float = 0.0
    trigger_pulse_n: int = 0
    trigger_edge_us: float = 0.0

    energy_j_external: float = 0.0
    energy_j_ina_vdd_in: float = 0.0
    energy_j_ina_cpu_gpu_cv: float = 0.0
    energy_j_ina_soc: float = 0.0
    idle_w_reference: float = 0.0

    meter_samples_n: int = 0
    meter_rate_hz: float = 0.0
    meter_window_s: float = 0.0
    hw_status: str = ""

    temp_c_soc_before: float = 0.0
    temp_c_soc_after: float = 0.0
    temp_c_cpu: float = 0.0
    temp_c_gpu: float = 0.0
    ambient_c: float = 0.0

    gate_wait_s: float = 0.0
    gate_timed_out: bool = False

    freq_gpu: int = 0
    freq_cpu: int = 0
    freq_emc: int = 0
    nvpmodel_mode: str = ""
    fan_pwm: int = 0

    finish_reason: str = ""
    truncated: bool = False
    thinking_leak: bool = False
    parse_ok: bool = False
    answer_extracted: str = ""


FIELDS = tuple(f.name for f in dataclass_fields(CallRecord))

# The closed vocabulary of hw_status tokens. Closed on purpose: a status column
# whose values are formed ad hoc at the call site cannot be counted, and
# "how many rows are clean" is the first question anyone asks of ten days of
# data. Adding a fault means adding it here, which is where the analysis
# looks for the list.
HW_FAULTS = frozenset((
    "trigger_edge_failed",
    "meter_thread_dead",
    "meter_no_samples",
    "meter_rate_low",
    "meter_rail_unreadable",
    "meter_rail_missing",
    "soc_temp_unreadable",
    "cpu_temp_unreadable",
    "gpu_temp_unreadable",
    "freq_unreadable",
    "nvpmodel_unreadable",
    "fan_unreadable",
))


def hw_status(tokens):
    """Render fault tokens into one cell: sorted, deduplicated, pipe joined.

    Sorted so the same set of faults always produces the same string and can be
    grouped on without parsing. An unknown token raises rather than being
    written, because a typo in a fault name is a fault that never appears in
    any count of itself.
    """
    unique = set(tokens or ())
    unknown = sorted(unique - HW_FAULTS)
    if unknown:
        raise ValueError(
            "Unknown hw_status token(s) %s. Add them to HW_FAULTS or the "
            "analysis will never count them." % ", ".join(unknown))
    return "|".join(sorted(unique))

_FORBIDDEN = ("total_tokens", "tokens", "n_tokens")
for _name in _FORBIDDEN:
    if _name in FIELDS:
        raise RuntimeError(
            "Field '%s' would merge input and output tokens; remove it" % _name
        )


def _check_header(path):
    """Refuse to append rows that will not line up with the ones already there.

    A schema change between two sessions of a resumable run would otherwise
    write correctly formed CSV whose columns mean something different below the
    join, and nothing downstream could tell.
    """
    with open(path, "r", newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh), [])
    if tuple(header) != FIELDS:
        raise RuntimeError(
            "Schema mismatch appending to %s: file has %d columns, this build "
            "writes %d. Differences: %s"
            % (path, len(header), len(FIELDS),
               sorted(set(header) ^ set(FIELDS)) or "column order")
        )


def new_run_id(config_hash):
    """Timestamped identifier tying a data file to its configuration."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "%s-%s" % (stamp, config_hash)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class RecordWriter:
    """Append-only CSV writer with a companion metadata file.

    Flushes after every row so a crash loses at most the row in flight, and
    fsyncs periodically so a power loss during a multi-day run costs seconds
    rather than the block. Both happen after the trigger has gone low, so
    neither appears inside a measured window.
    """

    def __init__(self, path, meta=None, fsync_every=50):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.path.with_suffix(".meta.json")
        self.fsync_every = fsync_every
        self._rows = 0

        existed = self.path.exists() and self.path.stat().st_size > 0
        if existed:
            _check_header(self.path)
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=FIELDS)
        if not existed:
            self._writer.writeheader()
            self._fh.flush()

        self._meta = dict(meta or {})
        self._meta.setdefault("started_utc", utc_now())
        self._meta["schema_fields"] = list(FIELDS)
        self._write_meta()

    def write(self, record):
        """Append one call record."""
        if not isinstance(record, CallRecord):
            raise TypeError("write() expects a CallRecord")
        self._writer.writerow({f: getattr(record, f) for f in FIELDS})
        self._fh.flush()
        self._rows += 1
        if self.fsync_every and self._rows % self.fsync_every == 0:
            os.fsync(self._fh.fileno())
        return self._rows

    def _write_meta(self):
        self._meta["rows_written"] = self._rows
        with open(self.meta_path, "w", encoding="utf-8") as fh:
            json.dump(self._meta, fh, indent=2, default=str)

    def close(self):
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._meta["finished_utc"] = utc_now()
        self._write_meta()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
