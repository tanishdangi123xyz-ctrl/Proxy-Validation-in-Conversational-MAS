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
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masenergy import config
from masenergy.datasets import items_hash

from datasets import load_dataset

OUT_DIR = ROOT / "data" / "items"


def _load(names, subset, split):
    last = None
    for name in names:
        try:
            return load_dataset(name, subset, split=split), name
        except Exception as exc:
            last = exc
    raise RuntimeError("Could not load %s: %s" % (names, last))


def _sample_indices(n_total, n_want, seed):
    rng = random.Random(seed)
    return sorted(rng.sample(range(n_total), n_want))


def prepare_gsm8k(n_items, seed):
    ds, source = _load(["openai/gsm8k", "gsm8k"], "main", "test")
    idx = _sample_indices(len(ds), n_items, seed)
    items = []
    for rank, i in enumerate(idx):
        row = ds[i]
        gold = row["answer"].split("####")[-1].strip()
        items.append({
            "id": "gsm8k-%05d" % i,
            "rank": rank,
            "question": row["question"].strip(),
            "answer": gold,
            "context": None,
            "level": None,
        })
    return items, source, len(ds)


def prepare_hotpotqa(n_items, seed):
    ds, source = _load(
        ["hotpotqa/hotpot_qa", "hotpot_qa"], "distractor", "validation"
    )
    idx = _sample_indices(len(ds), n_items, seed)
    items = []
    for rank, i in enumerate(idx):
        row = ds[i]
        gold_titles = set(row["supporting_facts"]["title"])
        paragraphs = []
        titles = row["context"]["title"]
        sentences = row["context"]["sentences"]
        for title, sents in zip(titles, sentences):
            if title in gold_titles:
                paragraphs.append("%s\n%s" % (title, " ".join(s.strip() for s in sents)))
        items.append({
            "id": "hotpotqa-%05d" % i,
            "rank": rank,
            "question": row["question"].strip(),
            "answer": row["answer"].strip(),
            "context": "\n\n".join(paragraphs),
            "level": row.get("level"),
        })
    return items, source, len(ds)


def write(name, items, source, pool_size, split, seed):
    payload = {
        "dataset": name,
        "source": source,
        "source_split": split,
        "pool_size": pool_size,
        "sample_seed": seed,
        "n_items": len(items),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "sha256": items_hash(items),
        "items": items,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / ("items_%s.json" % name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print("%-10s %3d items from %-24s pool=%-6d sha=%s"
          % (name, len(items), source, pool_size, payload["sha256"][:16]))
    return path


def main():
    n = config.N_ITEMS
    seed = config.ORDER_SEED
    print("Preparing %d items per dataset, seed %d\n" % (n, seed))

    items, source, pool = prepare_gsm8k(n, seed)
    write("gsm8k", items, source, pool, "test", seed)

    items, source, pool = prepare_hotpotqa(n, seed)
    write("hotpotqa", items, source, pool, "validation", seed)

    print("\nWritten to %s" % OUT_DIR)


if __name__ == "__main__":
    main()
