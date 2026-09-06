"""Joins the external rig's samples onto the Jetson's per-call records.

This is the script records.py's docstring refers to: "energy_j_external is
the one column no run ever fills... It is NaN on every row until the join
script matches rows to pulses by trigger_pulse_n." That script is this one.

WHAT IT DOES

Reads a Jetson-side CSV (written by records.RecordWriter, one row per model
call, trigger_pulse_n and trigger_edge_us already populated by client.py) and
a laptop-side CSV (written by host/capture.py, one row per sample from the
external rig's own ADC-based sensing, trigger_pulse_n populated by the ESP32
firmware's own edge count). For every Jetson row, it finds the contiguous run of external
samples whose trigger_pulse_n equals that row's trigger_pulse_n and whose
trigger_level is 1 (the ESP32 tags a sample with the pulse ordinal of the
most recent rising edge, so "pulse_n == N and level == 1" is exactly the
window between the Nth rising edge and the next falling edge), integrates
power over that window with the same trapezoidal method ina3221.py uses for
the onboard rails, and writes energy_j_external into a copy of the Jetson
CSV.

WHY THE JOIN KEY IS AN EXACT INTEGER MATCH, NOT A RANGE OR A TIMESTAMP

jetson.py's Trigger.status() docstring is explicit that pulse_n "counts
pulses attempted rather than pulses confirmed, so a failed edge leaves the
sequence intact and flagged rather than silently shifting every later row by
one." That guarantee is what makes an exact match safe: as long as both sides
count the same physical edges the same way, row N's trigger_pulse_n on the
Jetson always refers to the same edge as sample rows carrying pulse_n == N on
the laptop, even across a hardware fault on either side. This script trusts
that invariant rather than re-deriving it, and instead spends its effort
detecting the ways it can still fail in practice (see MISMATCH HANDLING).

MISMATCH HANDLING

A Jetson row can end up with no matching external samples (the laptop was not
capturing yet, or a USB dropout swallowed exactly this pulse's window, see
seq_gap_before in host/capture.py) or with samples whose count and duration
look implausible for the call's own wall_clock_ms_orchestrator. Both are
written as an explicit join_status value on the output row rather than left
to be inferred from an energy figure that happens to be NaN or zero:

    ok                   at least 2 samples found, none individually faulted,
                          sample span within EXPECTED_SPAN_TOLERANCE of the
                          call's own wall_clock_ms_orchestrator.
    no_samples            zero external samples carry this pulse_n at all.
    insufficient_samples   exactly 1 sample; integrate() cannot trapezoid a
                            single point.
    contains_faults        at least one matched sample had a nonzero fault
                            byte (see host/capture.py's FAULT_NAMES); the
                            energy figure is still computed from the
                            non-faulted samples where possible, because
                            discarding a whole window over one bad sample
                            throws away more than the fault itself cost, but
                            the status makes that visible.
    span_mismatch           the matched samples span a duration far from the
                            call's own recorded wall-clock time, which is the
                            signature of a join key collision (extremely
                            unlikely given the ordinal guarantee above, but
                            cheaper to detect here than to debate later).

energy_j_external is only ever a number for join_status == "ok" or
"contains_faults" (with the non-faulted subset integrated); every other
status leaves it NaN, following the same "NaN, never zero" rule records.py
states for the onboard rails, for the same reason: a zero here would be
indistinguishable from a call that genuinely drew no external energy.

Standard library only. Python 3.10 compatible.

    python3 host/join.py --calls data/raw/run.csv \\
        --external data/raw/external_run.csv \\
        --out data/raw/run_joined.csv
"""

import argparse
import csv
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masenergy.records import FIELDS as CALL_FIELDS  # noqa: E402

NAN = float("nan")

# How far a matched window's duration may differ from the call's own
# wall_clock_ms_orchestrator before join_status flags span_mismatch. Loose on
# purpose: the external window is measured from GPIO edges the ESP32 itself
# debounces (TRIGGER_DEBOUNCE_US in the firmware), while
# wall_clock_ms_orchestrator is measured in Python around the HTTP call, so a
# few sample periods of difference is expected on every row, not a fault.
EXPECTED_SPAN_TOLERANCE_MS = 50.0

OUTPUT_FIELDS = tuple(CALL_FIELDS) + ("join_status", "join_samples_n",
                                       "join_span_ms")


def _read_csv(path):
    with open(path, "r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _to_float(value, default=NAN):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if result != result else result  # keep NaN as NaN


def index_external_by_pulse(external_rows):
    """trigger_pulse_n -> list of (esp32_micros, power_w, fault) tuples.

    Ordered by esp32_micros within each pulse group, which is the order
    host/capture.py wrote them in anyway (one serial stream, read in order),
    but sorting here makes that an explicit guarantee rather than an
    accident of file order that a future reordering of capture.py could
    silently break.
    """
    by_pulse = {}
    for row in external_rows:
        try:
            pulse_n = int(row["trigger_pulse_n"])
        except (KeyError, ValueError):
            continue
        if int(row.get("trigger_level", "0") or "0") != 1:
            continue
        by_pulse.setdefault(pulse_n, []).append((
            int(row["esp32_micros"]),
            _to_float(row.get("power_w")),
            bool(row.get("fault")),
        ))
    for pulse_n in by_pulse:
        by_pulse[pulse_n].sort(key=lambda t: t[0])
    return by_pulse


def integrate_window(samples):
    """Trapezoidal energy in joules over (micros, watts, faulted) tuples.

    Mirrors ina3221.integrate()'s contract: fewer than two usable points
    returns NaN rather than a partial figure, for the same reason that
    module gives, a window that lost samples should not produce a number
    that looks exactly like a clean one.

    esp32_micros wraps at 2**32 (~71 minutes); a wrap mid-call would need
    modular arithmetic here, but no single call in this study's frozen
    parameters (CTX_SIZE, the topologies) runs anywhere near that long, so
    this is intentionally not handled and the assertion below fires loudly
    if that assumption is ever wrong instead of silently integrating a
    negative interval.
    """
    usable = [(t, w) for t, w, faulted in samples
              if not faulted and w == w]
    if len(usable) < 2:
        return NAN, 0
    total = 0.0
    prev_t, prev_w = usable[0]
    for t, w in usable[1:]:
        dt_s = (t - prev_t) / 1e6
        assert dt_s >= 0, (
            "esp32_micros went backwards within one pulse window; this "
            "only happens across a 71-minute micros() wrap, which no call "
            "in this study should approach")
        total += dt_s * (w + prev_w) / 2.0
        prev_t, prev_w = t, w
    return total, len(usable)


def join_row(call_row, external_by_pulse):
    """One Jetson row, decorated with energy_j_external and join_status."""
    out = dict(call_row)
    try:
        pulse_n = int(call_row.get("trigger_pulse_n", "0") or "0")
    except ValueError:
        pulse_n = 0

    samples = external_by_pulse.get(pulse_n, [])
    n_total = len(samples)
    any_fault = any(faulted for _, _, faulted in samples)

    if n_total == 0:
        out["join_status"] = "no_samples"
        out["join_samples_n"] = 0
        out["join_span_ms"] = NAN
        out["energy_j_external"] = NAN
        return out

    if n_total == 1:
        out["join_status"] = "insufficient_samples"
        out["join_samples_n"] = n_total
        out["join_span_ms"] = 0.0
        out["energy_j_external"] = NAN
        return out

    span_ms = (samples[-1][0] - samples[0][0]) / 1000.0
    energy_j, usable_n = integrate_window(samples)

    call_ms = _to_float(call_row.get("wall_clock_ms_orchestrator"))
    span_ok = (call_ms != call_ms  # call_ms itself unusable, do not flag
               or abs(span_ms - call_ms) <= EXPECTED_SPAN_TOLERANCE_MS)

    if not span_ok:
        status = "span_mismatch"
    elif any_fault:
        status = "contains_faults"
    elif usable_n < 2:
        status = "insufficient_samples"
        energy_j = NAN
    else:
        status = "ok"

    out["join_status"] = status
    out["join_samples_n"] = n_total
    out["join_span_ms"] = span_ms
    out["energy_j_external"] = energy_j if status in ("ok", "contains_faults") else NAN
    return out


def run_join(calls_path, external_path, out_path):
    call_rows = _read_csv(calls_path)
    external_rows = _read_csv(external_path)
    by_pulse = index_external_by_pulse(external_rows)

    joined = [join_row(row, by_pulse) for row in call_rows]

    counts = {}
    for row in joined:
        counts[row["join_status"]] = counts.get(row["join_status"], 0) + 1

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in joined:
            writer.writerow({k: row.get(k, "") for k in OUTPUT_FIELDS})

    print("host/join.py: %d rows joined -> %s" % (len(joined), out_path))
    for status in ("ok", "contains_faults", "span_mismatch",
                    "insufficient_samples", "no_samples"):
        if status in counts:
            print("  %-22s %d" % (status, counts[status]))
    unexpected = set(counts) - {"ok", "contains_faults", "span_mismatch",
                                 "insufficient_samples", "no_samples"}
    if unexpected:
        print("  unexpected join_status values: %s" % sorted(unexpected),
              file=sys.stderr)
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calls", required=True,
                         help="Jetson-side CSV from records.RecordWriter")
    parser.add_argument("--external", required=True,
                         help="laptop-side CSV from host/capture.py")
    parser.add_argument("--out", required=True,
                         help="output CSV path; input files are never "
                              "modified in place")
    args = parser.parse_args(argv)

    counts = run_join(args.calls, args.external, args.out)
    ok = counts.get("ok", 0) + counts.get("contains_faults", 0)
    total = sum(counts.values())
    if total and ok < total:
        print("host/join.py: %d/%d rows did not join cleanly, see the "
              "join_status column" % (total - ok, total), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
