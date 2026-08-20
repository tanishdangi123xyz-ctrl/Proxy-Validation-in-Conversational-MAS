"""Item loading, answer extraction and grading.

Runs on the Jetson: standard library only, Python 3.10 compatible. Reads the
plain JSON written by scripts/prepare_datasets.py, so the device never needs
the datasets library.

Extraction and grading are dataset-level, never topology-level. The same rule
is applied to a baseline answer and a debate synthesis, otherwise measured
accuracy would differ by topology for reasons unrelated to the topology.
"""

import json
import re
import string
import unicodedata

GSM8K = "gsm8k"
HOTPOTQA = "hotpotqa"

_ANSWER_MARKER = re.compile(r"answer\s*[:\-]\s*(.+)", re.IGNORECASE)
_NUMBER = re.compile(r"-?\$?\d[\d,]*\.?\d*")


def load_items(path):
    """Load a prepared item file and verify its recorded hash."""
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    recorded = payload.get("sha256")
    actual = items_hash(payload["items"])
    if recorded != actual:
        raise RuntimeError(
            "Item file hash mismatch for %s: recorded %s, computed %s"
            % (path, recorded, actual)
        )
    return payload


def items_hash(items):
    """Stable hash of an item list, for provenance in the methods section."""
    import hashlib
    blob = json.dumps(items, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _strip_number(text):
    text = text.replace(",", "").replace("$", "").strip()
    text = text.rstrip(".")
    return text


def _normalise(text):
    text = unicodedata.normalize("NFKD", text).lower()
    text = "".join(ch for ch in text if ch not in set(string.punctuation))
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    return " ".join(text.split())


def extract_answer(dataset, text):
    """Return the model's final answer, or None if nothing parseable is found.

    None is a parse failure: a first-class logged event that triggers a
    reprompt. Failures are expected to rise with temperature and are the
    mechanism the temperature-as-cause question depends on, so nothing here
    tries to rescue a malformed response.
    """
    if not text or not text.strip():
        return None

    marked = None
    for match in _ANSWER_MARKER.finditer(text):
        marked = match.group(1).strip()

    if dataset == GSM8K:
        candidate = marked if marked else text
        numbers = _NUMBER.findall(candidate)
        if not numbers and marked:
            numbers = _NUMBER.findall(text)
        if not numbers:
            return None
        return _strip_number(numbers[-1])

    if dataset == HOTPOTQA:
        if marked:
            return marked.split("\n")[0].strip()
        lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
        if not lines:
            return None
        return lines[-1]

    raise ValueError("Unknown dataset: %s" % dataset)


def _f1(predicted, gold):
    p_tokens = _normalise(predicted).split()
    g_tokens = _normalise(gold).split()
    if not p_tokens or not g_tokens:
        return float(p_tokens == g_tokens)
    common = {}
    for tok in p_tokens:
        common[tok] = common.get(tok, 0) + 1
    overlap = 0
    for tok in g_tokens:
        if common.get(tok, 0) > 0:
            common[tok] -= 1
            overlap += 1
    if overlap == 0:
        return 0.0
    precision = overlap / len(p_tokens)
    recall = overlap / len(g_tokens)
    return 2 * precision * recall / (precision + recall)


def grade(dataset, predicted, gold):
    """Score one answer. Returns parse_ok, correct, and f1 where defined."""
    if predicted is None:
        return {"parse_ok": False, "correct": False, "f1": 0.0}

    if dataset == GSM8K:
        try:
            ok = abs(float(_strip_number(predicted)) - float(_strip_number(gold))) < 1e-6
        except ValueError:
            ok = False
        return {"parse_ok": True, "correct": ok, "f1": float(ok)}

    if dataset == HOTPOTQA:
        exact = _normalise(predicted) == _normalise(gold)
        return {"parse_ok": True, "correct": exact, "f1": _f1(predicted, gold)}

    raise ValueError("Unknown dataset: %s" % dataset)


def build_task_text(dataset, item):
    """Render one item as the task string every topology receives."""
    if dataset == GSM8K:
        return item["question"]
    if dataset == HOTPOTQA:
        return "%s\n\nQuestion: %s" % (item["context"], item["question"])
    raise ValueError("Unknown dataset: %s" % dataset)
