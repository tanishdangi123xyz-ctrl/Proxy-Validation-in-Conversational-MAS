"""Download, sample and freeze the item sets. Laptop only.

Writes plain JSON to data/items/ so the Jetson never needs the datasets
library. Sampling uses a fixed seed and Python's own RNG rather than the
datasets library's shuffle, so the selection is reproducible independently
of library version.

The sample is drawn at random and frozen before any piloting. Do not later
filter items by observed model accuracy: selecting on performance stops this
being a random sample of the benchmark and makes the accuracy figure
meaningless. If baseline accuracy lands outside the target band, change the
dataset or the prompt, not the item list.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masenergy import config
from masenergy import datasets as ds

OUT_DIR = ROOT / "data" / "items"

GSM_HARD_REPOS = ["reasoning-machines/gsm-hard"]
GSM_HARD_SPLIT = "train"
HOTPOTQA_REPOS = ["hotpotqa/hotpot_qa", "hotpot_qa"]
HOTPOTQA_SPLIT = "validation"


def _load(names, subset, split):
    """Import the datasets library lazily so the row builders stay testable."""
    from datasets import load_dataset
    last = None
    for name in names:
        try:
            return load_dataset(name, subset, split=split), name
        except Exception as exc:
            last = exc
    raise RuntimeError("Could not load %s: %s" % (names, last))


def _sample_indices(n_total, n_want, seed):
    """Shared with the screen, so a screened item is one the campaign runs."""
    return ds.nested_sample(n_total, n_want, seed)


def build_gsm_hard(row, index, rank):
    """One gsm-hard row in the campaign item schema.

    gsm-hard, not gsm8k. The two are different benchmarks: gsm-hard replaces
    the operands with large awkward values, which is the reason it lands inside
    the accuracy band while plain gsm8k does not.
    """
    return {
        "id": "gsm_hard-%05d" % index,
        "rank": rank,
        "question": row["input"].strip(),
        "answer": ds.format_number_gold(row["target"]),
        "context": None,
        "level": None,
    }


def gold_pairs(row):
    """The supporting paragraphs of a hotpotqa row, distractors dropped."""
    gold_titles = set(row["supporting_facts"]["title"])
    pairs = []
    for title, sents in zip(row["context"]["title"], row["context"]["sentences"]):
        if title in gold_titles:
            pairs.append((title, "".join(sents).strip()))
    return pairs


def build_hotpotqa(row, index, rank):
    """One hotpotqa row in the campaign item schema."""
    return {
        "id": "hotpotqa-%05d" % index,
        "rank": rank,
        "question": row["question"].strip(),
        "answer": str(row["answer"]).strip(),
        "context": ds.context_block(gold_pairs(row)),
        "level": row.get("level"),
    }


def prepare_gsm_hard(n_items, seed):
    data, source = _load(GSM_HARD_REPOS, None, GSM_HARD_SPLIT)
    rows = list(data)
    idx = _sample_indices(len(rows), n_items, seed)
    items = [build_gsm_hard(rows[i], i, rank) for rank, i in enumerate(idx)]
    return items, source, len(rows)


def prepare_hotpotqa(n_items, seed):
    data, source = _load(HOTPOTQA_REPOS, "distractor", HOTPOTQA_SPLIT)
    rows = [r for r in data if gold_pairs(r)]
    idx = _sample_indices(len(rows), n_items, seed)
    items = [build_hotpotqa(rows[i], i, rank) for rank, i in enumerate(idx)]
    return items, source, len(rows)


def write(name, items, source, pool_size, split, seed, force=False):
    """Freeze one item set. Refuses to overwrite unless explicitly forced.

    Overwriting an item file mid-campaign silently redefines what the finished
    blocks measured, and the only trace is a hash in a metadata sidecar nobody
    reads until the analysis disagrees with itself. Regenerating both sets
    together and re-screening is a decision; doing it by re-running a script is
    an accident.
    """
    payload = {
        "dataset": name,
        "source": source,
        "source_split": split,
        "pool_size": pool_size,
        "sample_seed": seed,
        "n_items": len(items),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "sha256": ds.items_hash(items),
        "items": items,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / ("items_%s.json" % name)
    if path.exists() and not force:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("sha256") == payload["sha256"]:
            print("%-10s unchanged, kept  sha=%s" % (name, payload["sha256"][:16]))
            return path
        raise SystemExit(
            "%s already exists and would change: recorded sha %s, new sha %s.\n"
            "Blocks already run were measured on the recorded set. Re-run with "
            "--force only if you are regenerating every dataset and re-screening."
            % (path, existing.get("sha256", "")[:16], payload["sha256"][:16])
        )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print("%-10s %3d items from %-24s pool=%-6d sha=%s"
          % (name, len(items), source, pool_size, payload["sha256"][:16]))
    return path


def main(force=False):
    n = config.N_ITEMS
    seed = config.ORDER_SEED
    print("Preparing %d items per dataset, seed %d\n" % (n, seed))

    items, source, pool = prepare_gsm_hard(n, seed)
    write(ds.GSM_HARD, items, source, pool, GSM_HARD_SPLIT, seed, force)

    items, source, pool = prepare_hotpotqa(n, seed)
    write(ds.HOTPOTQA, items, source, pool, HOTPOTQA_SPLIT, seed, force)

    print("\nWritten to %s" % OUT_DIR)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite frozen item files that would change")
    main(ap.parse_args().force)
