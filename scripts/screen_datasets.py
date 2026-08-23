"""Stage-1 dataset screen.

Runs the baseline topology only, at one temperature, over a small sample of
every candidate dataset, and reports where each one lands relative to the
45-70 percent accuracy band. A candidate above the band makes every topology
collapse into the baseline; one below the band leaves the critic and the
debate agents arguing over noise.

Nothing here is frozen. Item files go to data/screen, which is gitignored,
and the two survivors are re-prepared afterwards through prepare_datasets.py
so the committed item sets all come from one path.

Two phases. --prep needs network and the datasets library. --run needs a
llama-server already up, launched with a context large enough for the longest
candidate prompt, which DROP and MuSiQue can push well past the 3072 the
campaign uses:

    scripts/serve_dev.sh models/Qwen3-1.7B-BF16.gguf 4096
    python scripts/screen_datasets.py --prep
    python scripts/screen_datasets.py --run
"""

import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masenergy import band, config, topologies
from masenergy import datasets as ds
from masenergy.client import LlamaClient
from masenergy.records import RecordWriter, new_run_id

SCREEN_DIR = ROOT / "data" / "screen"
BAND_LOW = band.BAND_LOW
BAND_HIGH = band.BAND_HIGH


def _num(value):
    """Delegates to the campaign gold formatter so both store the same string."""
    return ds.format_number_gold(value)


def _paragraph_block(pairs, question):
    """Delegates to the campaign renderer so the screen prompts what the run will."""
    return ds.render_context_task(ds.context_block(pairs), question.strip())


def _gold_pairs(row):
    ctx = row["context"]
    gold = set(row["supporting_facts"]["title"])
    pairs = []
    for title, sentences in zip(ctx["title"], ctx["sentences"]):
        if title in gold:
            pairs.append((title, "".join(sentences).strip()))
    return pairs


def _keep_all(row):
    return True


def _keep_math500(row):
    return int(row["level"]) <= 3


def _keep_drop(row):
    return any(s and s.strip() for s in row["answers_spans"]["spans"])


def _keep_wiki(row):
    return bool(_gold_pairs(row))


def _keep_musique(row):
    return bool(row["answerable"]) and any(p["is_supporting"] for p in row["paragraphs"])


def _build_gsm_hard(row, index):
    return {"id": "gsm_hard-%05d" % index, "task": row["input"].strip(),
            "answer": _num(row["target"]), "aliases": []}


def _build_math500(row, index):
    return {"id": str(row.get("unique_id") or "math500-%05d" % index),
            "task": row["problem"].strip(), "answer": row["answer"].strip(),
            "aliases": [], "level": int(row["level"])}


def _build_drop(row, index):
    spans = [s.strip() for s in row["answers_spans"]["spans"] if s and s.strip()]
    task = "Passage:\n%s\n\nQuestion: %s" % (row["passage"].strip(), row["question"].strip())
    return {"id": str(row.get("query_id") or "drop-%05d" % index),
            "task": task, "answer": spans[0], "aliases": spans[1:]}


def _build_hotpotqa(row, index):
    return {"id": str(row.get("id") or "hotpotqa-%05d" % index),
            "task": _paragraph_block(_gold_pairs(row), row["question"]),
            "answer": str(row["answer"]).strip(), "aliases": []}


def _build_wiki2hop(row, index):
    return {"id": str(row.get("id") or "wiki2hop-%05d" % index),
            "task": _paragraph_block(_gold_pairs(row), row["question"]),
            "answer": str(row["answer"]).strip(), "aliases": []}


def _build_musique(row, index):
    pairs = [(p["title"], p["paragraph_text"].strip())
             for p in row["paragraphs"] if p["is_supporting"]]
    return {"id": str(row.get("id") or "musique-%05d" % index),
            "task": _paragraph_block(pairs, row["question"]),
            "answer": str(row["answer"]).strip(),
            "aliases": [str(a).strip() for a in (row.get("answer_aliases") or [])]}


_BOXED = re.compile(r"\\boxed\{(.*)\}", re.S)
_TEXT_WRAP = re.compile(r"\\(?:text|mbox|mathrm)\{([^{}]*)\}")


def _norm_math(text):
    t = (text or "").strip()
    hit = _BOXED.search(t)
    if hit:
        t = hit.group(1)
    t = _TEXT_WRAP.sub(r"\1", t)
    for a, b in (("\\left", ""), ("\\right", ""), ("\\!", ""), ("\\,", ""),
                 ("\\;", ""), ("dfrac", "frac"), ("tfrac", "frac"),
                 ("^{\\circ}", ""), ("^\\circ", ""), ("$", ""), ("%", "")):
        t = t.replace(a, b)
    return t.replace(" ", "").rstrip(".")


def _grade_numeric(predicted, gold, aliases):
    """Delegates to the campaign grader.

    The screen previously used a relative tolerance, which on a gold answer of
    4219328.6 accepted anything within about four units. The campaign compares
    absolutely, so the looser rule scored answers the campaign would reject.
    """
    if predicted is None:
        return False
    return ds.grade(ds.GSM_HARD, predicted, gold)["correct"]


def _grade_math(predicted, gold, aliases):
    if _norm_math(predicted) and _norm_math(predicted) == _norm_math(gold):
        return True
    return _grade_numeric(predicted, gold, aliases)


def _grade_span(predicted, gold, aliases):
    """Delegates to the campaign grader so the screen measures what the run will.

    A screening gate graded by a different rule than the campaign predicts the
    wrong accuracy, which is the whole purpose of the gate.
    """
    if predicted is None:
        return False
    return ds.span_correct(predicted, gold, aliases)


GRADERS = {"numeric": _grade_numeric, "math": _grade_math, "span": _grade_span}

CANDIDATES = (
    {"name": "gsm_hard", "slot": "math", "grader": "numeric", "extract_as": "gsm_hard",
     "repos": ("reasoning-machines/gsm-hard",), "subset": None, "split": "train",
     "keep": _keep_all, "build": _build_gsm_hard},
    {"name": "math500", "slot": "math", "grader": "math", "extract_as": "hotpotqa",
     "repos": ("HuggingFaceH4/MATH-500",), "subset": None, "split": "test",
     "keep": _keep_math500, "build": _build_math500},
    {"name": "drop", "slot": "math", "grader": "span", "extract_as": "hotpotqa",
     "repos": ("ucinlp/drop", "drop"), "subset": None, "split": "validation",
     "keep": _keep_drop, "build": _build_drop},
    {"name": "hotpotqa", "slot": "qa", "grader": "span", "extract_as": "hotpotqa",
     "repos": ("hotpotqa/hotpot_qa", "hotpot_qa"), "subset": "distractor",
     "split": "validation", "keep": _keep_wiki, "build": _build_hotpotqa},
    {"name": "wiki2hop", "slot": "qa", "grader": "span", "extract_as": "hotpotqa",
     "repos": ("framolfese/2WikiMultihopQA", "xanhho/2WikiMultihopQA"),
     "subset": None, "split": "validation", "keep": _keep_wiki, "build": _build_wiki2hop},
    {"name": "musique", "slot": "qa", "grader": "span", "extract_as": "hotpotqa",
     "repos": ("dgslibisey/MuSiQue",), "subset": None, "split": "validation",
     "keep": _keep_musique, "build": _build_musique},
)


def _load(repos, subset, split):
    from datasets import load_dataset
    last = None
    for name in repos:
        try:
            return load_dataset(name, subset, split=split), name
        except Exception as exc:
            last = exc
    raise RuntimeError("none of [%s] loaded: %s" % (", ".join(repos), last))


def _validator(kind):
    def check(text):
        answer = ds.extract_answer(kind, text)
        return answer is not None, answer
    return check


def _selected(only):
    return [s for s in CANDIDATES if not only or s["name"] in only]


def prep(n_items, only):
    """Sample n_items from every selected candidate into data/screen."""
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    for spec in _selected(only):
        try:
            data, source = _load(spec["repos"], spec["subset"], spec["split"])
        except RuntimeError as exc:
            print("%-10s FAILED   %s" % (spec["name"], exc))
            continue
        rows = [r for r in data if spec["keep"](r)]
        if len(rows) < n_items:
            print("%-10s FAILED   pool has only %d usable rows" % (spec["name"], len(rows)))
            continue
        # Same draw the campaign preparer makes, so the items screened are the
        # items the campaign will run, and a screen of 30 contains a screen of
        # 15. Previously both called random.sample independently and nested
        # only because CPython happens to use one algorithm at both sizes.
        picked = ds.nested_sample(len(rows), n_items, config.ORDER_SEED)
        items = [spec["build"](rows[i], i) for i in picked]
        payload = {"name": spec["name"], "source": source, "split": spec["split"],
                   "pool_size": len(rows), "sample_seed": config.ORDER_SEED,
                   "sha256": ds.items_hash(items), "items": items}
        path = SCREEN_DIR / ("items_%s.json" % spec["name"])
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print("%-10s ok       %d items  %s  pool=%d  sha=%s"
              % (spec["name"], len(items), source, len(rows), payload["sha256"][:16]))


def run(n_items, port, only, temperature):
    """Baseline topology over every prepared candidate, then one table."""
    run_id = new_run_id(config.config_hash())
    out = SCREEN_DIR / ("screen_%s.calls.csv" % run_id)
    client = LlamaClient(None, run_id=run_id, port=port)
    if not client.health():
        sys.exit("No server answering on port %d. Start scripts/serve_dev.sh first." % port)

    summary = []
    with RecordWriter(out, {"run_id": run_id, "kind": "dataset_screen",
                            "temperature": temperature, "n_items": n_items}) as writer:
        client.writer = writer
        for spec in _selected(only):
            path = SCREEN_DIR / ("items_%s.json" % spec["name"])
            if not path.exists():
                print("%-10s skipped, no item file, run --prep first" % spec["name"])
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            items = payload["items"][:n_items]
            grader = GRADERS[spec["grader"]]
            validator = _validator(spec["extract_as"])
            correct = 0
            sys.stderr.write("%-10s " % spec["name"])
            for item in items:
                result = topologies.get("baseline")(
                    client, item["task"], temperature, 101,
                    {"dataset": spec["name"], "item_id": item["id"], "repetition": 0},
                    validator)
                if grader(result["answer"], item["answer"], item.get("aliases")):
                    correct += 1
                    sys.stderr.write("+")
                else:
                    sys.stderr.write(".")
            sys.stderr.write("  %d/%d\n" % (correct, len(items)))
            summary.append((spec, correct, len(items)))

    report(out, summary, temperature)


def report(calls_path, summary, temperature):
    rows = list(csv.DictReader(open(calls_path, encoding="utf-8")))
    by_dataset = defaultdict(list)
    for r in rows:
        by_dataset[r["dataset"]].append(r)

    print("\n" + "=" * 78)
    print("STAGE-1 DATASET SCREEN   t=%.1f   %d call records" % (temperature, len(rows)))
    print("=" * 78)
    print("\n%-10s %-5s %8s %8s %8s %8s %8s   %s"
          % ("dataset", "slot", "acc", "parse", "prompt", "output", "worst", "verdict"))

    for spec, correct, total in summary:
        sub = by_dataset.get(spec["name"], [])
        if not sub:
            continue
        acc = 100.0 * correct / total if total else 0.0
        parse = 100.0 * sum(r["parse_ok"] == "True" for r in sub) / len(sub)
        ins = sorted(int(r["prompt_n"]) for r in sub)
        outs = sorted(int(r["predicted_n"]) for r in sub)
        worst = max(int(r["prompt_n"]) + int(r["predicted_n"]) for r in sub)
        name, _, lo, hi = band.verdict(correct, total)
        print("%-10s %-5s %7.1f%% %7.1f%% %8d %8d %8d   %s"
              % (spec["name"], spec["slot"], acc, parse,
                 statistics.median(ins), statistics.median(outs), worst,
                 "%s  95%% CI %.0f-%.0f" % (name, lo, hi)))

    retries = sum(r["is_retry"] == "True" for r in rows)
    # stop_type "limit", not the truncated flag: truncated means the prompt
    # overran the context, which is a different failure and is false on every
    # row this pipeline has ever written.
    clipped = sum(r["finish_reason"] == "limit" for r in rows)
    truncated = sum(r["truncated"] == "True" for r in rows)
    leaked = sum(r.get("thinking_leak") == "True" for r in rows)
    n_per = max((t for _, _, t in summary), default=0)
    print("\n  acc     baseline accuracy, target band %.0f-%.0f%%" % (BAND_LOW, BAND_HIGH))
    print("  prompt and output are medians in tokens, worst is the largest prompt+output")
    print("  retries triggered: %d of %d calls" % (retries, len(rows)))
    print("  hit MAX_TOKENS (stop_type=limit): %d, nonzero means output length is censored"
          % clipped)
    print("  prompt truncated by the server: %d" % truncated)
    print("  responses carrying a think tag: %d" % leaked)
    if rows:
        print("  largest prompt+output across all candidates: %d tokens"
              % max(int(r["prompt_n"]) + int(r["predicted_n"]) for r in rows))
    if n_per:
        lo, hi = band.wilson(int(round(0.55 * n_per)), n_per)
        print("  at n=%d the 95%% interval is %.0f points wide and the band is only %.0f,"
              % (n_per, hi - lo, BAND_HIGH - BAND_LOW))
        print("  so every verdict above that reads UNRESOLVED genuinely is. %d items"
              % band.n_for_halfwidth(10.0))
        print("  would give +-10 points, %d would give +-5.\n" % band.n_for_halfwidth(5.0))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prep", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--items", type=int, default=15)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--port", type=int, default=config.SERVER_PORT)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    picked = set(x.strip() for x in a.only.split(",") if x.strip())
    if not a.prep and not a.run:
        sys.exit("choose --prep, --run, or both")
    if a.prep:
        prep(a.items, picked)
    if a.run:
        run(a.items, a.port, picked, a.temperature)
