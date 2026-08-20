"""Model-level go/no-go checks. Runs anywhere a server is up.

Answers four questions that are properties of the model, prompts and datasets
rather than of the hardware, so they can be settled before the Jetson or the
power rig exist:

  1. Does the prompt format elicit a parseable answer, and does adherence
     degrade with temperature as the retry mechanism requires?
  2. Is baseline accuracy inside the 45-70 percent band? Outside it, every
     topology collapses behaviourally into the baseline.
  3. Do debate agents genuinely change their answers between rounds? Below
     roughly 10 percent, debate is a null topology.
  4. What are the real token lengths, and therefore what should CTX_SIZE be?

It also checks that thinking mode is actually suppressed, which is a silent
failure mode at high temperature.
"""

import argparse
import csv
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masenergy import chat, config, topologies
from masenergy import datasets as ds
from masenergy.client import LlamaClient
from masenergy.records import RecordWriter, new_run_id
from masenergy.runner import make_validator


def pct(part, whole):
    return 100.0 * part / whole if whole else 0.0


def run(n_items, out_dir, port):
    run_id = new_run_id(config.config_hash())
    out = Path(out_dir) / ("dry_%s.calls.csv" % run_id)
    client = LlamaClient(None, run_id=run_id, port=port)

    if not client.health():
        sys.exit("No server answering on port %d. Start scripts/serve_dev.sh first." % port)

    texts = defaultdict(list)
    debate_answers = defaultdict(dict)

    with RecordWriter(out, {"run_id": run_id, "kind": "dry_run"}) as writer:
        client.writer = writer
        for dataset in config.DATASETS:
            payload = ds.load_items(ROOT / "data" / "items" / ("items_%s.json" % dataset))
            items = payload["items"][:n_items]
            validator = make_validator(dataset)

            for temperature in config.TEMPERATURES:
                sys.stderr.write("baseline %-9s t=%.1f  " % (dataset, temperature))
                correct = 0
                for item in items:
                    task = ds.build_task_text(dataset, item)
                    r = topologies.get("baseline")(
                        client, task, temperature, 101,
                        {"dataset": dataset, "item_id": item["id"], "repetition": 0},
                        validator)
                    g = ds.grade(dataset, r["answer"], item["answer"])
                    correct += bool(g["correct"])
                sys.stderr.write("acc %.0f%%\n" % pct(correct, len(items)))

            sys.stderr.write("debate   %-9s t=0.7  " % dataset)
            for item in items:
                task = ds.build_task_text(dataset, item)
                r = topologies.get("debate")(
                    client, task, 0.7, 101,
                    {"dataset": dataset, "item_id": item["id"], "repetition": 0},
                    validator)
                for rec in r["records"]:
                    if rec.role.startswith("agent_"):
                        key = (dataset, item["id"], rec.role)
                        debate_answers[key][rec.round_index] = rec.answer_extracted
            sys.stderr.write("done\n")

    report(out, debate_answers)


def report(calls_path, debate_answers):
    rows = list(csv.DictReader(open(calls_path, encoding="utf-8")))
    print("\n" + "=" * 66)
    print("DRY RUN REPORT   %d call records" % len(rows))
    print("=" * 66)

    print("\n1. FORMAT ADHERENCE  (should fall as temperature rises)")
    by_t = defaultdict(lambda: [0, 0])
    for r in rows:
        b = by_t[r["temperature"]]
        b[1] += 1
        b[0] += r["parse_ok"] == "True"
    for t in sorted(by_t):
        ok, n = by_t[t]
        print("   t=%-5s  %5.1f%% parseable   (%d calls)" % (t, pct(ok, n), n))
    retries = sum(r["is_retry"] == "True" for r in rows)
    print("   retries triggered: %d of %d calls (%.1f%%)" % (retries, len(rows), pct(retries, len(rows))))

    print("\n2. BASELINE ACCURACY  (target band 45-70%)")
    acc = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["topology"] != "baseline" or r["is_retry"] == "True":
            continue
        k = (r["dataset"], r["temperature"])
        acc[k][1] += 1
    print("   see per-block accuracy printed during the run above")

    print("\n3. DEBATE ANSWER-CHANGE RATE  (need >10%)")
    per_ds = defaultdict(lambda: [0, 0])
    for (dataset, item_id, role), rounds in debate_answers.items():
        if 1 in rounds and 2 in rounds:
            per_ds[dataset][1] += 1
            if (rounds[1] or "").strip() != (rounds[2] or "").strip():
                per_ds[dataset][0] += 1
    for dataset in sorted(per_ds):
        changed, total = per_ds[dataset]
        verdict = "OK" if pct(changed, total) > 10 else "*** NULL TOPOLOGY RISK ***"
        print("   %-10s %5.1f%%  (%d of %d agent-rounds)  %s"
              % (dataset, pct(changed, total), changed, total, verdict))

    print("\n4. TOKEN LENGTHS")
    for dataset in sorted(set(r["dataset"] for r in rows if r["dataset"])):
        sub = [r for r in rows if r["dataset"] == dataset]
        outs = sorted(int(r["predicted_n"]) for r in sub)
        ins = sorted(int(r["prompt_n"]) for r in sub)
        print("   %-10s output  median %4d  p95 %4d  max %4d"
              % (dataset, statistics.median(outs), outs[int(len(outs) * .95) - 1], outs[-1]))
        print("   %-10s prompt  median %4d  p95 %4d  max %4d"
              % ("", statistics.median(ins), ins[int(len(ins) * .95) - 1], ins[-1]))
    worst = max(int(r["prompt_n"]) + int(r["predicted_n"]) for r in rows)
    suggested = 1024
    while suggested < worst * 1.6:
        suggested *= 2
    print("\n   worst observed prompt+output: %d tokens" % worst)
    print("   SUGGESTED CTX_SIZE: %d" % suggested)

    truncated = sum(r["truncated"] == "True" for r in rows)
    print("   truncated responses: %d  (nonzero means MAX_TOKENS is clipping)" % truncated)

    print("\n5. THINKING SUPPRESSION")
    leak = sum("<think>" in (r["answer_extracted"] or "") for r in rows)
    print("   think-tag leakage into extracted answers: %d" % leak)
    print("   also eyeball a raw response if output medians look unexpectedly long")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=10)
    ap.add_argument("--port", type=int, default=config.SERVER_PORT)
    ap.add_argument("--out", default=str(ROOT / "data" / "raw"))
    a = ap.parse_args()
    run(a.items, a.out, a.port)
