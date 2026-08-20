"""Per-call record schema and append-only writer.

One row per model call. The call is the unit of analysis, so a retry is its
own row with its own energy, not an amendment to the row it retried.

Input and output tokens are stored separately and never summed. The output
token asymmetry question cannot be answered from a total, so no total_tokens
field exists anywhere in the pipeline.

Nothing derived is stored. Imputed cost is a function of the token counts and
the price schedule, so it is computed during analysis; storing it would let a
price revision silently invalidate old rows.

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
    predicted_n: int = 0
    cached_n: int = 0

    wall_clock_ms_orchestrator: float = 0.0
    server_prefill_ms: float = 0.0
    server_decode_ms: float = 0.0

    trigger_high_ts: float = 0.0
    trigger_low_ts: float = 0.0

    energy_j_external: float = 0.0
    energy_j_ina_vdd_in: float = 0.0
    energy_j_ina_cpu_gpu_cv: float = 0.0
    energy_j_ina_soc: float = 0.0
    idle_w_reference: float = 0.0

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
    parse_ok: bool = False
    answer_extracted: str = ""
    correct: bool = False
    f1: float = 0.0


FIELDS = tuple(f.name for f in dataclass_fields(CallRecord))

_FORBIDDEN = ("total_tokens", "tokens", "n_tokens")
for _name in _FORBIDDEN:
    if _name in FIELDS:
        raise RuntimeError(
            "Field '%s' would merge input and output tokens; remove it" % _name
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
