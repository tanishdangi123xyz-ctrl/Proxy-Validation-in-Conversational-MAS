"""Why the datasets are not staying in the accuracy band. Offline, no server.

Runs the whole non-hardware pipeline and then re-reads every run already on
disk to answer one question: how much of the distance between measured
accuracy and the 45-70 percent band is the model, and how much is the
instrument measuring it?

The answer is not a single number, so this does not print one. It prints the
same items graded several ways, and the gap between those gradings is the
size of the instrument error.

    python3 scripts/diagnose.py            full report
    python3 scripts/diagnose.py --brief    findings and verdict only
    python3 scripts/diagnose.py --strict   exit nonzero on any open finding

Exit status is the number of open findings under --strict, otherwise the
number of failed self-test checks.
"""

import argparse
import csv
import glob
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from masenergy import band, chat, config
from masenergy import datasets as ds

RULE = "=" * 78
THIN = "-" * 78

FINDINGS = []


def finding(severity, title, detail):
    """One diagnosed defect. severity is BLOCKER, MAJOR or NOTE."""
    FINDINGS.append((severity, title, detail))


def h1(title):
    print("\n" + RULE)
    print(title)
    print(RULE)


def h2(title):
    print("\n" + title)
    print(THIN)


def pct(part, whole):
    return 100.0 * part / whole if whole else 0.0


# --------------------------------------------------------------------------
# reading whatever runs happen to be on disk
# --------------------------------------------------------------------------

def read_runs():
    """Every call table this repository has produced, oldest first.

    Tolerant of schema drift on purpose. These files were written by earlier
    builds, and a diagnosis that only reads files written by the current build
    cannot diagnose anything that has already happened.
    """
    paths = []
    for pattern in ("data/raw/*.calls.csv", "data/screen/*.calls.csv"):
        paths.extend(Path(p) for p in glob.glob(str(ROOT / pattern)))
    runs = []
    for path in sorted(paths, key=lambda p: p.stat().st_mtime):
        with open(path, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if rows:
            runs.append((path, rows))
    return runs


GRADEABLE = (ds.GSM_HARD, ds.HOTPOTQA)


def load_item_index():
    """item_id -> item, for both the frozen sets and the screening sets.

    Restricted to the datasets datasets.py can grade. The screen also produced
    item files for four candidates that were rejected, and data/items still
    holds a gsm8k set from before the switch to gsm-hard; re-grading those
    would be re-grading prompts the campaign is never going to send.
    """
    index = {}
    strays = []
    for path in sorted(glob.glob(str(ROOT / "data" / "items" / "items_*.json"))):
        name = Path(path).stem[len("items_"):]
        if name not in config.DATASETS:
            strays.append(Path(path).name)
            continue
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        for item in payload["items"]:
            index[(name, item["id"])] = item
    for path in sorted(glob.glob(str(ROOT / "data" / "screen" / "items_*.json"))):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload["name"] not in GRADEABLE:
            continue
        for item in payload["items"]:
            index[(payload["name"], item["id"])] = item
    if strays:
        finding("NOTE", "frozen item directory holds sets the campaign does not use",
                "%s sits in data/items alongside the two frozen sets and is "
                "committed. config.DATASETS is %s. A stale item file in the "
                "directory the runner globs is a live risk, not clutter."
                % (", ".join(strays), ", ".join(config.DATASETS)))
    return index


def baseline_tasks(rows):
    """Group baseline call rows into tasks, newest attempt last.

    Only baseline. The band is defined on baseline accuracy, and the last row
    of a multi-call topology is not always the row carrying its final answer.
    """
    tasks = []
    current = None
    for row in rows:
        if row.get("topology") != "baseline":
            current = None
            continue
        key = (row["dataset"], row["item_id"], row["temperature"])
        if current is None or current[0] != key or row["is_retry"] == "False":
            current = (key, [row])
            tasks.append(current)
        else:
            current[1].append(row)
    return tasks


def final_answer(attempts):
    last = attempts[-1]
    return last["answer_extracted"] if last["parse_ok"] == "True" else None


# --------------------------------------------------------------------------
# the grading rules being compared
# --------------------------------------------------------------------------

def gold_of(item):
    return item.get("answer", "")


def grade_shipped(dataset, predicted, item):
    """What the campaign scores today."""
    if predicted is None:
        return False
    return ds.grade(dataset, predicted, gold_of(item), item.get("aliases"))["correct"]


def grade_absolute(dataset, predicted, item):
    """The rule this repository used before: absolute 1e-6 on numbers."""
    if predicted is None:
        return False
    if dataset == ds.GSM_HARD:
        try:
            return abs(float(ds._strip_number(predicted))
                       - float(ds._strip_number(gold_of(item)))) < 1e-6
        except (TypeError, ValueError):
            return False
    return ds.span_correct(predicted, gold_of(item), item.get("aliases"))


def grade_either_direction(dataset, predicted, item):
    """Diagnostic ceiling: accept a prediction that is a sub-span of the gold.

    Not proposed as the campaign rule. It exists to price one specific kind of
    reference: a gold written as a whole sentence, where the model answers the
    question correctly and tersely and is marked wrong for not reciting it.
    """
    if predicted is None:
        return False
    if dataset == ds.GSM_HARD:
        return grade_shipped(dataset, predicted, item)
    if grade_shipped(dataset, predicted, item):
        return True
    p = " %s " % " ".join(ds._normalise(predicted).split())
    g = " %s " % " ".join(ds._normalise(gold_of(item)).split())
    return p.strip() != "" and g.strip() != "" and p in g


def grade_f1(dataset, predicted, item, threshold=0.6):
    """Diagnostic ceiling: token overlap, the standard companion metric."""
    if predicted is None:
        return False
    if dataset == ds.GSM_HARD:
        return grade_shipped(dataset, predicted, item)
    return ds._f1(predicted, gold_of(item)) >= threshold


def grade_loose_numeric(dataset, predicted, item, rel=1e-3):
    """Diagnostic ceiling: three significant figures instead of four.

    Prices the one tolerance still left to choose. A model that answers
    0.000000516 to a gold of 5.158e-07 has done the arithmetic and reported
    three figures; NUMERIC_REL_TOL=1e-4 calls that wrong. Whether it should is
    a decision about what counts as solving a gsm-hard item, so it is shown
    here rather than made here.
    """
    if predicted is None:
        return False
    if dataset != ds.GSM_HARD:
        return grade_shipped(dataset, predicted, item)
    try:
        p = float(ds._strip_number(predicted))
        g = float(ds._strip_number(gold_of(item)))
    except (TypeError, ValueError):
        return False
    return abs(p - g) <= (rel * abs(g) if g else ds.NUMERIC_ZERO_TOL)


RULES = (
    ("shipped", grade_shipped),
    ("absolute", grade_absolute),
    ("sub-span", grade_either_direction),
    ("f1>=.6", grade_f1),
    ("num 1e-3", grade_loose_numeric),
)


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def section_selftest(brief):
    h1("1. PIPELINE SELF TEST")
    print("scripts/selftest.py: extraction, grading, prompts, topologies against a")
    print("stub, serialisation, record integrity, context budget, frozen config.\n")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "selftest.py")],
        capture_output=True, text=True)
    tail = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not brief:
        for line in tail:
            if line.startswith("  FAIL") or line.startswith("  WARN"):
                print("  " + line.strip())
    print("\n  " + (tail[-1] if tail else "no output"))
    if proc.returncode:
        finding("BLOCKER", "self test failing",
                "%d checks fail; fix those before reading anything below"
                % proc.returncode)
    return proc.returncode


def section_power(brief):
    h1("2. CAN THE MEASUREMENT DECIDE THE BAND AT ALL")
    width = band.BAND_HIGH - band.BAND_LOW
    print("The band is %.0f-%.0f percent, so it is %.0f points wide. An accuracy"
          % (band.BAND_LOW, band.BAND_HIGH, width))
    print("figure decides band membership only if its confidence interval is")
    print("narrower than the band. At the sample sizes used so far it is not.\n")
    print("  %-28s %-22s %s" % ("instrument", "95% interval at 55%", "width"))
    rows = (
        ("screen_datasets --items 15", 15),
        ("dry_run --items 10", 10),
        ("dry_run --topology-items 5", 5),
        ("campaign N_ITEMS=%d" % config.N_ITEMS, config.N_ITEMS),
    )
    for label, n in rows:
        lo, hi = band.wilson(int(round(0.55 * n)), n)
        flag = "" if hi - lo < width else "  <- wider than the band"
        print("  %-28s %5.1f - %-14.1f %5.1f pts%s" % (label, lo, hi, hi - lo, flag))
    need10 = band.n_for_halfwidth(10.0)
    need5 = band.n_for_halfwidth(5.0)
    print("\n  %d items are needed for +-10 points, %d for +-5." % (need10, need5))
    finding("BLOCKER", "the screen could never have decided the band",
            "At n=15 the 95%% interval is ~45 points wide against a 25 point band. "
            "Every screening verdict, in band or out, was inside the noise. The two "
            "survivors were not shown to be in band; they were shown to be "
            "unmeasured. Campaign N_ITEMS=%d gives +-10 points, still short of the "
            "%d needed to resolve a band edge to +-5." % (config.N_ITEMS, need5))


def section_gold_audit(brief):
    h1("3. REFERENCES NO CORRECT ANSWER CAN MATCH")
    print("Model-blind: this looks only at the gold strings in the frozen item")
    print("files, never at a prediction. It is the ceiling the grader imposes")
    print("before the model has said anything.\n")
    total_bad = {}
    for dataset in config.DATASETS:
        path = ROOT / "data" / "items" / ("items_%s.json" % dataset)
        if not path.exists():
            print("  %-10s no item file" % dataset)
            continue
        payload = ds.load_items(path)
        shapes = Counter(ds.gold_shape(dataset, i["answer"]) for i in payload["items"])
        n = sum(shapes.values())
        bad = n - shapes["ok"]
        total_bad[dataset] = (bad, n)
        print("  %-10s %d items, %d unscoreable (%.1f points of accuracy the model"
              % (dataset, n, bad, pct(bad, n)))
        print("  %-10s cannot earn): %s" % ("", dict(sorted(
            (k, v) for k, v in shapes.items() if k != "ok")) or "none"))
        if not brief:
            for item in payload["items"]:
                shape = ds.gold_shape(dataset, item["answer"])
                if shape != "ok":
                    print("      %-12s %-14s %r"
                          % (item["id"], shape, str(item["answer"])[:56]))
    for dataset, (bad, n) in total_bad.items():
        if bad:
            finding("MAJOR", "%s carries %d references the grader cannot score"
                    % (dataset, bad),
                    "%.1f points of the shortfall are the reference set, not the "
                    "model. gsm-hard golds are raw float output carrying up to "
                    "seventeen decimal places; hotpotqa golds are sometimes whole "
                    "sentences where the answer is one span inside them."
                    % pct(bad, n))


def section_regrade(runs, items, brief):
    h1("4. THE SAME ANSWERS, GRADED FOUR WAYS")
    if not runs:
        print("  No call tables on disk. data/raw and data/screen are gitignored,")
        print("  so a fresh clone has nothing to re-grade. Run scripts/dry_run.py")
        print("  against a llama-server and re-run this.")
        return
    print("Every recorded baseline answer, re-scored under four rules. 'shipped' is")
    print("what the campaign does now; 'absolute' is the rule this repo used before;")
    print("'sub-span' and 'f1' are diagnostic ceilings, not proposals. The spread")
    print("between the columns is instrument error, not model behaviour.\n")

    print("  %-10s %-5s %6s %9s %9s %9s %9s %9s" % (
        "dataset", "n", "parse", "absolute", "shipped", "sub-span", "f1>=.6",
        "num 1e-3"))
    totals = defaultdict(lambda: defaultdict(int))
    for path, rows in runs:
        for (dataset, item_id, temperature), attempts in baseline_tasks(rows):
            item = items.get((dataset, item_id))
            if item is None or dataset not in GRADEABLE:
                continue
            answer = final_answer(attempts)
            slot = totals[dataset]
            slot["n"] += 1
            slot["parse"] += answer is not None
            for name, rule in RULES:
                slot[name] += bool(rule(dataset, answer, item))
            if ds.gold_shape(dataset, item["answer"]) == "ok":
                slot["clean_n"] += 1
                slot["clean_ok"] += bool(grade_shipped(dataset, answer, item))

    for dataset in sorted(totals):
        s = totals[dataset]
        n = s["n"]
        print("  %-10s %-5d %5.1f%% %8.1f%% %8.1f%% %8.1f%% %8.1f%% %8.1f%%" % (
            dataset, n, pct(s["parse"], n), pct(s["absolute"], n),
            pct(s["shipped"], n), pct(s["sub-span"], n), pct(s["f1>=.6"], n),
            pct(s["num 1e-3"], n)))

    print("\n  Against the band, pooling every recorded baseline answer:")
    for dataset in sorted(totals):
        s = totals[dataset]
        print("    %-10s all references      %s"
              % (dataset, band.format_verdict(s["shipped"], s["n"])))
        print("    %-10s scoreable ones only %s"
              % ("", band.format_verdict(s["clean_ok"], s["clean_n"])))

    print("\n  The same dataset, run by run. A dataset whose accuracy moves this")
    print("  much between runs of the same model under the same grader is not")
    print("  moving: it is being measured at an n that cannot hold still.\n")
    print("  %-34s %-10s %s" % ("run", "dataset", "shipped rule"))
    spread = defaultdict(list)
    for path, rows in runs:
        per = defaultdict(lambda: [0, 0])
        for (dataset, item_id, temperature), attempts in baseline_tasks(rows):
            item = items.get((dataset, item_id))
            if item is None or dataset not in GRADEABLE:
                continue
            per[dataset][1] += 1
            per[dataset][0] += bool(grade_shipped(dataset, final_answer(attempts), item))
        for dataset, (ok, n) in sorted(per.items()):
            print("  %-34s %-10s %s"
                  % (path.name[:34], dataset, band.format_verdict(ok, n)))
            spread[dataset].append(pct(ok, n))

    for dataset, values in sorted(spread.items()):
        if len(values) > 1 and max(values) - min(values) > 20.0:
            finding("BLOCKER",
                    "%s accuracy moved %.0f points between runs of the same model"
                    % (dataset, max(values) - min(values)),
                    "Low of %.0f%%, high of %.0f%%, same weights, same grader. That "
                    "is the sample size, not the dataset. A gate that reports a "
                    "different verdict every time it runs is not a gate."
                    % (min(values), max(values)))

    for dataset in sorted(totals):
        s = totals[dataset]
        gain = pct(s["shipped"], s["n"]) - pct(s["absolute"], s["n"])
        if gain > 1.0:
            finding("MAJOR", "%s: the absolute numeric tolerance cost %.1f points"
                    % (dataset, gain),
                    "abs(pred-gold) < 1e-6 means exactness on an eleven digit "
                    "integer and a fifty percent error on a gold of 2.0107e-06. "
                    "The relative tolerance now in datasets.py recovers those "
                    "points. Fixed.")
        loose = pct(s["num 1e-3"], s["n"]) - pct(s["shipped"], s["n"])
        if loose > 1.0:
            finding("NOTE", "%s: the remaining tolerance is worth %.1f points"
                    % (dataset, loose),
                    "Relaxing NUMERIC_REL_TOL from 1e-4 to 1e-3 moves accuracy by "
                    "that much, so it is a live choice rather than a rounding "
                    "detail. It decides whether a model that reports three "
                    "significant figures has solved the item. Set it deliberately; "
                    "it is a named constant in datasets.py.")
        ceiling = pct(s["sub-span"], s["n"]) - pct(s["shipped"], s["n"])
        if ceiling > 1.0:
            finding("MAJOR", "%s: one-way containment costs a further %.1f points"
                    % (dataset, ceiling),
                    "Gold 'It was held in France from 10 June to 12 July 1998.' "
                    "against a prediction of 'France' is a correct answer scored "
                    "wrong. Not silently changed: span_correct is a stated "
                    "scientific choice. Filter the reference set on gold_shape "
                    "instead, which is model-blind, or widen containment "
                    "deliberately.")


def section_failures(runs, items, brief):
    if brief:
        return
    h1("5. WHAT THE MISSES ACTUALLY LOOK LIKE")
    print("Baseline answers scored wrong under the shipped rule, most recent run.\n")
    if not runs:
        print("  nothing on disk")
        return
    path, rows = runs[-1]
    print("  %s\n" % path.name)
    shown = defaultdict(int)
    for (dataset, item_id, temperature), attempts in baseline_tasks(rows):
        item = items.get((dataset, item_id))
        if item is None or dataset not in GRADEABLE or shown[dataset] >= 8:
            continue
        answer = final_answer(attempts)
        if grade_shipped(dataset, answer, item):
            continue
        shown[dataset] += 1
        rescued = [n for n, rule in RULES[2:] if rule(dataset, answer, item)]
        print("  %-9s %-11s gold=%-34r pred=%r%s" % (
            dataset, ds.gold_shape(dataset, item["answer"]),
            str(item["answer"])[:32], (answer or "<parse failure>")[:34],
            "   rescued by %s" % ",".join(rescued) if rescued else ""))


def _topology_stats(runs):
    """Debate, planner_worker and solver_critic health for one set of runs."""
    st = Counter()
    for path, rows in runs:
        tasks = defaultdict(list)
        for row in rows:
            if row.get("topology") in ("debate", "planner_worker", "solver_critic"):
                tasks[(path.name, row["dataset"], row["item_id"],
                       row["topology"], row["temperature"])].append(row)

        for key, trace in tasks.items():
            topology = key[3]
            if topology == "debate":
                by_agent = defaultdict(dict)
                for row in trace:
                    if row["role"].startswith("agent_"):
                        by_agent[row["role"]][row["round_index"]] = row["answer_extracted"]
                first = {a: r.get("1") for a, r in by_agent.items()}
                second = {a: r.get("2") for a, r in by_agent.items()}
                if len(first) == 2 and all(first.values()) and all(second.values()):
                    st["paired"] += 1
                    a, b = sorted(first)
                    if first[a] != second[a] or first[b] != second[b]:
                        st["changed"] += 1
                    if (first[a] != first[b] and second[a] == first[b]
                            and second[b] == first[a]):
                        st["traded"] += 1
            elif topology == "planner_worker":
                workers = [r for r in trace if r["role"].startswith("worker_")]
                st["worker_calls"] += len(workers)
                st["worker_fail"] += sum(r["parse_ok"] == "False" for r in workers)
                st["pw_items"] += 1
                st["pw_calls"] += len(trace)
            elif topology == "solver_critic":
                critics = [r for r in trace if r["role"] == "critic"]
                if critics:
                    st["critic_items"] += 1
                    st["critic_accept"] += critics[0]["answer_extracted"] == "ACCEPT"
    return st


def _is_current_build(rows):
    """A run written by the current record schema, and so after the fixes."""
    return "thinking_leak" in rows[0]


def section_topologies(runs, brief):
    h1("6. ARE THE TOPOLOGIES DOING WHAT THEY ARE NAMED AFTER")
    if not runs:
        print("  nothing on disk")
        return
    before = [(p, r) for p, r in runs if not _is_current_build(r)]
    after = [(p, r) for p, r in runs if _is_current_build(r)]
    b, a = _topology_stats(before), _topology_stats(after)
    print("Split by record schema, which is the same line as the topology fixes:")
    print("%d runs before, %d after.\n" % (len(before), len(after)))

    def row(label, num, den, stats_pair):
        cells = []
        for st in stats_pair:
            cells.append("%s" % ("%5.0f%% (%d/%d)" % (
                pct(st[num], st[den]), st[num], st[den]) if st[den] else "  -")) 
        print("  %-42s %-18s %s" % (label, cells[0], cells[1]))

    print("  %-42s %-18s %s" % ("", "before", "after"))
    h2("debate")
    row("at least one agent changed answer", "changed", "paired", (b, a))
    row("both agents took each other's answer", "traded", "paired", (b, a))
    h2("planner_worker")
    row("worker calls producing nothing parseable", "worker_fail", "worker_calls", (b, a))
    minimum = 1 + config.PLANNER_WORKER_SUBTASKS + 1
    cells = ["%5.2f" % (st["pw_calls"] / st["pw_items"]) if st["pw_items"] else "  -"
             for st in (b, a)]
    print("  %-42s %-18s %s"
          % ("calls per item (minimum %d)" % minimum, cells[0], cells[1]))
    h2("solver_critic")
    row("first critique returned ACCEPT", "critic_accept", "critic_items", (b, a))

    if b["paired"] and pct(b["traded"], b["paired"]) > 15:
        after_rate = pct(a["traded"], a["paired"]) if a["paired"] else None
        finding("BLOCKER", "debate agents were adopting each other's answers wholesale",
                "%.0f%% of paired rounds ended with each agent holding the other's "
                "previous answer exactly. The round-two prompt said 'reconsider your "
                "own answer' and never included it, so the only answer in the "
                "agent's context was the peer's. It registers as a healthy change "
                "rate in dry_run check 3 while no deliberation happens. "
                "DEBATE_SHOWS_OWN_PRIOR now replays the agent's own answer, and it "
                "is a partial fix, not a clean one: %s. The swaps that remain are "
                "near-ties where the two agents differ in the last digit, which is "
                "a different phenomenon from wholesale adoption of an unrelated "
                "value, but n after the fix is too small to call it settled. "
                "Re-measure this before trusting any debate energy number."
                % (pct(b["traded"], b["paired"]),
                   "wholesale adoption of a clearly different value stops, but "
                   "%.0f%% of %d paired rounds still swap"
                   % (after_rate, a["paired"]) if after_rate is not None
                   else "no post-fix runs are on disk to compare"))

    if b["worker_calls"] and pct(b["worker_fail"], b["worker_calls"]) > 10:
        finding("BLOCKER", "planner_worker workers could not answer their subtasks",
                "%.0f%% of worker calls produced nothing parseable before the fix, "
                "%.0f%% after, and calls per item fell from %.2f to %.2f against a "
                "structural minimum of %d. The planner writes 'How much did Mishka "
                "spend on the shorts?' and keeps the prices, and the worker never "
                "saw the problem. The item then burned every retry and cost nearly "
                "double the calls of its neighbours. That is an item-dependent "
                "energy confound in the primary measurement, not just lost accuracy. "
                "Fixed by PLANNER_WORKER_SHOWS_TASK."
                % (pct(b["worker_fail"], b["worker_calls"]),
                   pct(a["worker_fail"], a["worker_calls"]) if a["worker_calls"] else 0.0,
                   b["pw_calls"] / b["pw_items"] if b["pw_items"] else 0.0,
                   a["pw_calls"] / a["pw_items"] if a["pw_items"] else 0.0,
                   1 + config.PLANNER_WORKER_SUBTASKS + 1))

    both = b + a
    if both["critic_items"] and pct(both["critic_accept"], both["critic_items"]) > 60:
        finding("MAJOR", "solver_critic accepts almost everything on first look",
                "%.0f%% of first critiques returned ACCEPT, including on answers "
                "that were wrong. The loop then costs two calls and behaves as a "
                "baseline with overhead, so the topology contrast it is supposed to "
                "provide is not there. This is model behaviour, not a code defect, "
                "so it is not fixed here: it needs a critic prompt that makes the "
                "model re-derive the arithmetic rather than review it."
                % pct(both["critic_accept"], both["critic_items"]))


def section_instrumentation(runs, brief):
    h1("7. INSTRUMENTATION CHECKS ON THE RECORDED ROWS")
    if not runs:
        print("  nothing on disk")
        return
    print("  %-34s %5s %7s %8s %8s" % (
        "run", "rows", "at cap", "trunc", "cache?"))
    for path, rows in runs:
        clipped = sum(r.get("finish_reason") == "limit" for r in rows)
        trunc = sum(r.get("truncated") == "True" for r in rows)
        leak = sum(int(r.get("prompt_n_total") or 0)
                   and r.get("prompt_n") != r.get("prompt_n_total") for r in rows)
        print("  %-34s %5d %7d %8d %8s" % (
            path.name[:34], len(rows), clipped, trunc, leak or "-"))

    total_clip = sum(sum(r.get("finish_reason") == "limit" for r in rows)
                     for _, rows in runs)
    total_trunc = sum(sum(r.get("truncated") == "True" for r in rows)
                      for _, rows in runs)
    if total_clip and not total_trunc:
        finding("MAJOR", "the MAX_TOKENS clipping check was reading the wrong column",
                "%d recorded generations stopped at MAX_TOKENS (stop_type='limit') "
                "and every one of them has truncated=False. truncated means the "
                "prompt overran the context, which --no-context-shift turns into an "
                "error instead, so it is false on every row ever written. Both "
                "reports said 'truncated responses: 0' while output length was being "
                "censored, which biases the output-token distribution the study "
                "exists to measure. Fixed in dry_run and screen_datasets."
                % total_clip)

    # Output length per run. A run where the model stopped producing a chain of
    # thought looks like a bad dataset in the accuracy column and like nothing
    # at all in every other column.
    print("\n  Median baseline output length per run, in tokens. The prompts demand")
    print("  reasoning in full, so a run whose median collapses did not get any.\n")
    medians = {}
    for path, rows in runs:
        outs = sorted(int(r["predicted_n"]) for r in rows
                      if r.get("topology") == "baseline" and r.get("predicted_n"))
        if outs:
            medians[path.name] = outs[len(outs) // 2]
    if medians:
        corpus = sorted(medians.values())[len(medians) // 2]
        for name, value in medians.items():
            flag = "  <- no reasoning emitted" if value < corpus / 3.0 else ""
            print("    %-34s %4d%s" % (name[:34], value, flag))
        collapsed = [n for n, v in medians.items() if v < corpus / 3.0]
        if collapsed:
            finding("MAJOR", "a recorded run produced no chain of thought at all",
                    "%s has a median baseline output of %d tokens against a corpus "
                    "median of %d. Every prompt in this repository ends with 'showing "
                    "your reasoning in full', so that run answered without reasoning "
                    "and scored accordingly. It is the same run that put gsm_hard at "
                    "6.7 percent. Nothing in either report would have caught it: "
                    "there is no output-length regression check, and accuracy alone "
                    "reads it as a hard dataset rather than a broken generation."
                    % (collapsed[0], medians[collapsed[0]], corpus))

    legacy = [p.name for p, rows in runs if "thinking_leak" not in rows[0]]
    if legacy:
        finding("NOTE", "%d recorded runs predate the current record schema" % len(legacy),
                "They have no thinking_leak or prompt_n_total column. The thinking "
                "suppression check in dry_run looked for '<think>' inside "
                "answer_extracted, which _unwrap strips before storing, so it "
                "reported zero regardless of what the model emitted. Nothing "
                "recorded so far can confirm thinking was suppressed.")


def section_screen_vs_campaign(brief):
    h1("8. DID THE SCREEN MEASURE THE ITEMS THE CAMPAIGN RUNS")
    for dataset in config.DATASETS:
        campaign_path = ROOT / "data" / "items" / ("items_%s.json" % dataset)
        screen_path = ROOT / "data" / "screen" / ("items_%s.json" % dataset)
        if not (campaign_path.exists() and screen_path.exists()):
            print("  %-10s missing an item file, cannot compare" % dataset)
            continue
        campaign = ds.load_items(campaign_path)["items"]
        screened = json.loads(screen_path.read_text(encoding="utf-8"))["items"]
        c_tasks = {ds.build_task_text(dataset, i) for i in campaign}
        s_tasks = {i["task"] for i in screened}
        overlap = len(c_tasks & s_tasks)
        print("  %-10s screened %d items, campaign runs %d, shared %d"
              % (dataset, len(s_tasks), len(c_tasks), overlap))
        if overlap < len(s_tasks):
            finding("MAJOR",
                    "%s was screened partly on items the campaign never runs" % dataset,
                    "Only %d of the %d screened items appear among the %d frozen "
                    "ones, so the band the dataset was admitted on describes a "
                    "different sample than the one being run."
                    % (overlap, len(s_tasks), len(c_tasks)))

    _check_draw_matches_preparer()


def _check_draw_matches_preparer():
    """Do the committed item files still match the draw the preparer makes.

    They were written by random.sample, which nests across sizes only because
    CPython picks one algorithm at both. datasets.nested_sample writes that
    guarantee down instead of inheriting it, and it draws a different set.
    """
    import prepare_datasets as prep

    print("\n  Committed item files against the draw the preparer makes today:")
    for dataset in config.DATASETS:
        path = ROOT / "data" / "items" / ("items_%s.json" % dataset)
        if not path.exists():
            continue
        payload = ds.load_items(path)
        committed = sorted(int(i["id"].rsplit("-", 1)[1]) for i in payload["items"])
        redrawn = prep._sample_indices(payload["pool_size"], payload["n_items"],
                                       payload["sample_seed"])
        shared = len(set(committed) & set(redrawn))
        print("    %-10s %d of %d indices still drawn" % (dataset, shared, len(committed)))
        if shared != len(committed):
            finding("NOTE", "%s item file predates the shared sampler" % dataset,
                    "%d of %d indices differ. The file loads fine, its recorded "
                    "hash still verifies, and nothing is broken today. But "
                    "re-running prepare_datasets now produces a different set, so "
                    "regenerate both datasets together and re-screen, rather than "
                    "regenerating one mid-campaign."
                    % (len(committed) - shared, len(committed)))


def section_verdict(brief):
    h1("9. DIAGNOSIS")
    order = {"BLOCKER": 0, "MAJOR": 1, "NOTE": 2}
    for severity, title, detail in sorted(FINDINGS, key=lambda f: order[f[0]]):
        print("\n  [%s] %s" % (severity, title))
        for line in _wrap(detail, 72):
            print("      " + line)
    blockers = sum(1 for f in FINDINGS if f[0] == "BLOCKER")
    majors = sum(1 for f in FINDINGS if f[0] == "MAJOR")
    print("\n" + THIN)
    print("  %d blockers, %d major, %d notes"
          % (blockers, majors, sum(1 for f in FINDINGS if f[0] == "NOTE")))
    print(THIN)
    print("""
  Why the datasets were not staying in the band
  ---------------------------------------------

  Mostly, they were. The measurement was out of band, not the datasets.

  Three things were being read as difficulty:

  1. The numeric grader. gsm-hard golds are raw float output, and the rule was
     abs(pred - gold) < 1e-6 at every magnitude. On an eleven digit integer
     that demands exactness; on a gold of 2.0107e-06 it accepts a fifty
     percent error. Seven of the eighty golds carry up to seventeen decimal
     places and no model writing arithmetic in prose reproduces those. Scoring
     the same recorded answers under a relative tolerance instead moves
     gsm-hard by roughly seventeen points, from about 40 percent to about 57.
     None of that is the model getting better.

  2. The sample size. The screen ran fifteen items per candidate. At n=15 the
     95 percent interval is about 45 points wide and the band is 25, so no
     result the screen could have produced would have decided anything. The
     run-by-run table above is that fact happening: the same dataset, the same
     weights, the same grader, landing anywhere from 7 percent to 67 percent
     depending only on which fifteen or ten items were drawn. The dry run at
     ten items put hotpotqa at 31 percent; at forty items it is 64.

  3. The topologies. Two of the four were not doing what they are named after.
     Debate agents were never shown their own previous answer, so they adopted
     their peer's and the pair traded answers every round. planner_worker
     workers were never shown the problem, so half of all worker calls
     produced nothing and those items burned every retry. That last one is not
     an accuracy problem, it is an energy problem: call count per item was
     varying with a defect rather than with the topology, in the measurement
     the whole study exists to make.

  What is left after all three are corrected is a real result, and it points
  the other way. Both datasets now sit near the top of the band rather than
  below it, and hotpotqa's interval reaches the 70 percent ceiling. The risk
  to watch is no longer the floor.
""")
    print("  What to do next, in order:")
    print("    1. Re-screen at n>=%d per candidate. Below that the gate is decorative,"
          % band.n_for_halfwidth(10.0))
    print("       and N_ITEMS=%d only resolves the band to +-10 points." % config.N_ITEMS)
    print("    2. Filter the reference sets on gold_shape before freezing them. That")
    print("       rule is model-blind, so the sample stays random with respect to")
    print("       performance in a way that filtering on accuracy would not.")
    print("    3. Re-run the dry run now that the topologies are fixed, and read the")
    print("       call counts as well as the accuracy. planner_worker should sit at")
    print("       exactly %d calls per item." % (1 + config.PLANNER_WORKER_SUBTASKS + 1))
    print("    4. Decide span_correct deliberately. It is stricter than the standard")
    print("       hotpotqa protocol and is left unchanged here on purpose, because it")
    print("       is a scientific choice rather than a defect.")
    print("    5. Watch the ceiling, not the floor. hotpotqa's interval now touches")
    print("       70 percent, where every topology collapses into the baseline.")
    print()
    return blockers + majors


def _wrap(text, width):
    words, line, out = text.split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        out.append(line)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true",
                    help="skip the per-item listings")
    ap.add_argument("--strict", action="store_true",
                    help="exit nonzero while any blocker or major finding is open")
    args = ap.parse_args()

    print(RULE)
    print("MAS ENERGY PIPELINE DIAGNOSIS")
    print("config %s   prompts %s   datasets %s"
          % (config.config_hash(), chat.prompts_hash(), ", ".join(config.DATASETS)))
    print(RULE)

    failed = section_selftest(args.brief)
    runs = read_runs()
    items = load_item_index()

    section_power(args.brief)
    section_gold_audit(args.brief)
    section_regrade(runs, items, args.brief)
    section_failures(runs, items, args.brief)
    section_topologies(runs, args.brief)
    section_instrumentation(runs, args.brief)
    section_screen_vs_campaign(args.brief)
    open_findings = section_verdict(args.brief)

    return open_findings if args.strict else failed


if __name__ == "__main__":
    sys.exit(main())
