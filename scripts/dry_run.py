"""Model-level go/no-go checks. Runs anywhere a server is up.

Answers the questions that are properties of the model, prompts and datasets
rather than of the hardware, so they can be settled before the Jetson or the
power rig exist:

  1. Does the prompt format elicit a parseable answer, and does adherence
     degrade with temperature as the retry mechanism requires?
  2. Is baseline accuracy inside the 45-70 percent band? Outside it, every
     topology collapses behaviourally into the baseline.
  3. Do debate agents genuinely change their answers between rounds? Below
     roughly 10 percent, debate is a null topology.
  4. Does every topology actually do the thing it is named after, on real
     model output rather than on a stub?
  5. What are the real token lengths, and therefore what should CTX_SIZE be?

All four topologies are exercised, not just baseline and debate. A topology
that is never run before the campaign is a topology whose first real execution
happens with the power rig attached and ten days committed.

It also checks that thinking mode is actually suppressed, which is a silent
failure mode at high temperature.
"""

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masenergy import config, topologies
from masenergy import datasets as ds
from masenergy.client import LlamaClient
from masenergy.records import RecordWriter, new_run_id
from masenergy.runner import make_validator

TOPOLOGY_TEMPERATURE = 0.7


def pct(part, whole):
    return 100.0 * part / whole if whole else 0.0


def run(n_items, n_topology_items, out_dir, port):
    run_id = new_run_id(config.config_hash())
    out = Path(out_dir) / ("dry_%s.calls.csv" % run_id)
    client = LlamaClient(None, run_id=run_id, port=port)

    if not client.health():
        sys.exit("No server answering on port %d. Start scripts/serve_dev.sh first." % port)

    accuracy = {}
    topology_stats = defaultdict(lambda: {"items": 0, "correct": 0, "calls": 0,
                                          "prompt": 0, "predicted": 0})
    debate_answers = defaultdict(dict)
    critic_traces = defaultdict(list)

    with RecordWriter(out, {"run_id": run_id, "kind": "dry_run",
                            "n_items": n_items,
                            "n_topology_items": n_topology_items}) as writer:
        client.writer = writer
        for dataset in config.DATASETS:
            payload = ds.load_items(
                ROOT / "data" / "items" / ("items_%s.json" % dataset))
            sys.stderr.write("%s: %d items, sha %s\n"
                             % (dataset, len(payload["items"]),
                                payload["sha256"][:16]))
            items = payload["items"][:n_items]
            validator = make_validator(dataset)

            for temperature in config.TEMPERATURES:
                sys.stderr.write("  baseline sweep t=%.1f  " % temperature)
                correct = 0
                for item in items:
                    result = topologies.get("baseline")(
                        client, ds.build_task_text(dataset, item),
                        temperature, 101,
                        {"dataset": dataset, "item_id": item["id"], "repetition": 0},
                        validator)
                    hit = ds.grade(dataset, result["answer"], item["answer"])["correct"]
                    correct += bool(hit)
                    sys.stderr.write("+" if hit else ".")
                accuracy[(dataset, temperature)] = (correct, len(items))
                sys.stderr.write("  %.0f%%\n" % pct(correct, len(items)))

            for name in config.CONDITIONS:
                sys.stderr.write("  %-14s t=%.1f  " % (name, TOPOLOGY_TEMPERATURE))
                stats = topology_stats[(dataset, name)]
                for item in items[:n_topology_items]:
                    result = topologies.get(name)(
                        client, ds.build_task_text(dataset, item),
                        TOPOLOGY_TEMPERATURE, 101,
                        {"dataset": dataset, "item_id": item["id"], "repetition": 0},
                        validator)
                    hit = ds.grade(dataset, result["answer"], item["answer"])["correct"]
                    stats["items"] += 1
                    stats["correct"] += bool(hit)
                    stats["calls"] += result["n_calls"]
                    for rec in result["records"]:
                        stats["prompt"] += rec.prompt_n
                        stats["predicted"] += rec.predicted_n
                        if name == "debate" and rec.role.startswith("agent_"):
                            debate_answers[(dataset, item["id"], rec.role)][
                                rec.round_index] = rec.answer_extracted
                    if name == "solver_critic":
                        critic_traces[dataset].append(
                            [(r.role, r.round_index, r.prompt_n, r.predicted_n)
                             for r in result["records"]])
                    sys.stderr.write("+" if hit else ".")
                sys.stderr.write("  %d calls\n" % stats["calls"])

    report(out, accuracy, topology_stats, debate_answers, critic_traces)


def report(calls_path, accuracy, topology_stats, debate_answers, critic_traces):
    rows = list(csv.DictReader(open(calls_path, encoding="utf-8")))
    print("\n" + "=" * 72)
    print("DRY RUN REPORT   %d call records   config %s"
          % (len(rows), config.config_hash()))
    print("=" * 72)

    print("\n1. FORMAT ADHERENCE  (parse rate should fall as temperature rises)")
    by_t = defaultdict(lambda: [0, 0])
    for r in rows:
        bucket = by_t[float(r["temperature"])]
        bucket[1] += 1
        bucket[0] += r["parse_ok"] == "True"
    for t in sorted(by_t):
        ok, n = by_t[t]
        print("   t=%-5s %6.1f%% parseable   (%d calls)" % (t, pct(ok, n), n))
    retries = sum(r["is_retry"] == "True" for r in rows)
    print("   retries triggered: %d of %d calls (%.1f%%)"
          % (retries, len(rows), pct(retries, len(rows))))

    print("\n2. BASELINE ACCURACY  (target band 45-70%)")
    for (dataset, temperature), (correct, total) in sorted(accuracy.items()):
        value = pct(correct, total)
        if value > 70:
            verdict = "too easy, ceiling"
        elif value < 45:
            verdict = "too hard, floor"
        else:
            verdict = "IN BAND"
        print("   %-10s t=%.1f  %5.1f%%  (%d of %d)   %s"
              % (dataset, temperature, value, correct, total, verdict))

    print("\n3. DEBATE ANSWER-CHANGE RATE  (need >10%)")
    per_dataset = defaultdict(lambda: [0, 0])
    for (dataset, _, _), rounds in debate_answers.items():
        if 1 in rounds and 2 in rounds:
            per_dataset[dataset][1] += 1
            if (rounds[1] or "").strip() != (rounds[2] or "").strip():
                per_dataset[dataset][0] += 1
    if not per_dataset:
        print("   no paired agent rounds recorded")
    for dataset in sorted(per_dataset):
        changed, total = per_dataset[dataset]
        verdict = "OK" if pct(changed, total) > 10 else "*** NULL TOPOLOGY RISK ***"
        print("   %-10s %5.1f%%  (%d of %d agent-rounds)  %s"
              % (dataset, pct(changed, total), changed, total, verdict))

    print("\n4. TOPOLOGY BEHAVIOUR  (t=%.1f)" % TOPOLOGY_TEMPERATURE)
    print("   %-10s %-14s %6s %7s %8s %9s %9s"
          % ("dataset", "topology", "acc", "calls", "calls/it", "prompt/it", "output/it"))
    for (dataset, name), s in sorted(topology_stats.items()):
        if not s["items"]:
            continue
        print("   %-10s %-14s %5.0f%% %7d %8.1f %9.0f %9.0f"
              % (dataset, name, pct(s["correct"], s["items"]), s["calls"],
                 s["calls"] / s["items"], s["prompt"] / s["items"],
                 s["predicted"] / s["items"]))

    print("\n   a solver revision carries the task, the draft and the critique, so")
    print("   its prompt must grow by more than the draft alone. Growing by only")
    print("   the draft means the solver is being handed the bare verdict:")
    for dataset in sorted(critic_traces):
        observed, expected = [], []
        for trace in critic_traces[dataset]:
            first = next((r for r in trace if r[0] == "solver"), None)
            critic = next((r for r in trace if r[0] == "critic"), None)
            revision = next((r for r in trace
                             if r[0] == "solver" and r[1] > 0), None)
            if not (first and critic and revision):
                continue
            observed.append(revision[2] - first[2])
            expected.append(first[3] + critic[3])
        if not observed:
            print("   %-10s no revisions occurred, every critique returned ACCEPT"
                  % dataset)
            continue
        got, want = statistics.mean(observed), statistics.mean(expected)
        ok = got > want * 0.6
        print("   %-10s prompt grew %4.0f tokens, draft+critique is %4.0f   %s"
              % (dataset, got, want,
                 "OK" if ok else "*** CRITIQUE NOT REACHING SOLVER ***"))

    print("\n5. TOKEN LENGTHS")
    for dataset in sorted(set(r["dataset"] for r in rows if r["dataset"])):
        sub = [r for r in rows if r["dataset"] == dataset]
        outs = sorted(int(r["predicted_n"]) for r in sub)
        ins = sorted(int(r["prompt_n"]) for r in sub)
        print("   %-10s output  median %4d  p95 %4d  max %4d"
              % (dataset, statistics.median(outs), outs[int(len(outs) * .95) - 1], outs[-1]))
        print("   %-10s prompt  median %4d  p95 %4d  max %4d"
              % ("", statistics.median(ins), ins[int(len(ins) * .95) - 1], ins[-1]))
    worst = max(int(r["prompt_n"]) + int(r["predicted_n"]) for r in rows)
    headroom = config.CTX_SIZE - worst
    print("\n   worst observed prompt+output: %d tokens" % worst)
    print("   CTX_SIZE %d leaves %d tokens of headroom   %s"
          % (config.CTX_SIZE, headroom,
             "OK" if headroom > 0 else "*** CTX_SIZE TOO SMALL ***"))
    truncated = sum(r["truncated"] == "True" for r in rows)
    print("   truncated responses: %d  (nonzero means MAX_TOKENS is clipping)"
          % truncated)

    print("\n6. THINKING SUPPRESSION")
    leak = sum("<think>" in (r["answer_extracted"] or "") for r in rows)
    print("   think-tag leakage into extracted answers: %d" % leak)
    print("   eyeball a raw response too if output medians look unexpectedly long")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=10,
                    help="items per dataset for the baseline temperature sweep")
    ap.add_argument("--topology-items", type=int, default=5,
                    help="items per dataset for the four-topology sweep")
    ap.add_argument("--port", type=int, default=config.SERVER_PORT)
    ap.add_argument("--out", default=str(ROOT / "data" / "raw"))
    a = ap.parse_args()
    run(a.items, a.topology_items, a.out, a.port)
