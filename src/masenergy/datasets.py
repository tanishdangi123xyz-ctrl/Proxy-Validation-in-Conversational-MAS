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

GSM_HARD = "gsm_hard"
HOTPOTQA = "hotpotqa"

_ANSWER_MARKER = re.compile(r"answer\s*[:\-]\s*(.+)", re.IGNORECASE)
_NUMBER = re.compile(r"-?\$?\d[\d,]*\.?\d*")
_TEMPLATE_SLOT = re.compile(r"<[^<>]{0,80}>")
_BOXED = re.compile(r"\\boxed\s*\{(.*)\}", re.S)
_DELIMITERS_ONLY = re.compile(r"^[\s$\\()\[\]{}*`.,:;]*$")

_WRAPPERS = (("$$", "$$"), ("$", "$"), ("\\[", "\\]"), ("\\(", "\\)"),
             ("**", "**"), ("`", "`"))

CONTAINMENT_FACTOR = 2
CONTAINMENT_SLACK = 4


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


def format_number_gold(value):
    """Render a numeric gold answer without a spurious trailing .0.

    Shared by the screen and the campaign preparer so a gold answer of 8.0 is
    stored as the same string in both, rather than as 8 in one and 8.0 in the
    other.
    """
    f = float(value)
    return str(int(f)) if f == int(f) else repr(f)


def context_block(pairs):
    """Join gold (title, text) paragraphs into the context stored on an item."""
    return "\n\n".join("%s: %s" % (title, text) for title, text in pairs)


def render_context_task(context, question):
    """The exact task string a context-bearing item becomes.

    The screen and the campaign must render an item identically. If they do
    not, screened accuracy is measured on a prompt the campaign never sends,
    and the band the dataset was selected on describes nothing.
    """
    return "Context:\n%s\n\nQuestion: %s" % (context, question.strip())


def _strip_number(text):
    text = text.replace(",", "").replace("$", "").strip()
    text = text.rstrip(".")
    return text


def _unwrap(text):
    """Strip echoed template slots and display wrappers from a candidate answer.

    A template slot reaching this function means the prompt leaked its own
    placeholder into the response, which is an instrument fault rather than a
    malformed answer, so removing it recovers a measurement that was never the
    model's to get wrong. Display wrappers are stripped for the same reason:
    the delimiters around a LaTeX answer are formatting, not content.
    """
    t = _TEMPLATE_SLOT.sub(" ", text or "").strip()
    hit = _BOXED.search(t)
    if hit:
        t = hit.group(1).strip()
    for opening, closing in _WRAPPERS:
        span = len(opening) + len(closing)
        while t.startswith(opening) and t.endswith(closing) and len(t) > span:
            t = t[len(opening):-len(closing)].strip()
    return t.rstrip(".,;:").strip()


def _first_usable(lines):
    """First line in the given order that unwraps to something other than punctuation."""
    for line in lines:
        candidate = _unwrap(line)
        if candidate and not _DELIMITERS_ONLY.match(candidate):
            return candidate
    return None


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
    tries to rescue a malformed response. Unwrapping is not a rescue: it
    removes formatting and prompt leakage, never guesses at missing content.
    A response with no usable final line stays a failure.

    When an answer line unwraps to nothing, only text after that line is
    considered. The model declared where its answer began, so anything above is
    reasoning; reading it would be inventing an answer that was never given.
    With nothing after it, the response is a failure.
    """
    if not text or not text.strip():
        return None

    marked = None
    marker_end = None
    for match in _ANSWER_MARKER.finditer(text):
        marker_end = match.end()
        candidate = _unwrap(match.group(1))
        if candidate:
            marked = candidate

    if dataset == GSM_HARD:
        candidate = marked if marked else text
        numbers = _NUMBER.findall(candidate)
        if not numbers and marked:
            numbers = _NUMBER.findall(text)
        if not numbers:
            return None
        return _strip_number(numbers[-1])

    if dataset == HOTPOTQA:
        if marked:
            return marked
        if marker_end is not None:
            return _first_usable(text[marker_end:].split("\n"))
        return _first_usable(reversed(text.strip().split("\n")))

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


def contains_gold(predicted, gold):
    """True if the gold phrase appears verbatim inside a prediction that stays short.

    Unbounded containment is exploitable: a long enough response contains the
    gold string by accident, so leniency would grow with output length and
    reward the verbose topologies for being verbose. The length bound keeps a
    terse answer carrying a few extra words and rejects a paragraph that merely
    spans the gold phrase.
    """
    p_tokens = _normalise(predicted).split()
    g_tokens = _normalise(gold).split()
    if not p_tokens or not g_tokens:
        return False
    limit = max(CONTAINMENT_FACTOR * len(g_tokens), len(g_tokens) + CONTAINMENT_SLACK)
    if len(p_tokens) > limit:
        return False
    return " %s " % " ".join(g_tokens) in " %s " % " ".join(p_tokens)


def span_correct(predicted, gold, aliases=None):
    """Exact match on any accepted surface form, else bounded containment."""
    accepted = [gold] + list(aliases or [])
    for candidate in accepted:
        if _normalise(predicted) == _normalise(candidate):
            return True
    for candidate in accepted:
        if contains_gold(predicted, candidate):
            return True
    return False


def grade(dataset, predicted, gold, aliases=None):
    """Score one answer. Returns parse_ok, correct, and f1 where defined."""
    if predicted is None:
        return {"parse_ok": False, "correct": False, "f1": 0.0}

    if dataset == GSM_HARD:
        try:
            ok = abs(float(_strip_number(predicted)) - float(_strip_number(gold))) < 1e-6
        except ValueError:
            ok = False
        return {"parse_ok": True, "correct": ok, "f1": float(ok)}

    if dataset == HOTPOTQA:
        ok = span_correct(predicted, gold, aliases)
        return {"parse_ok": True, "correct": ok, "f1": _f1(predicted, gold)}

    raise ValueError("Unknown dataset: %s" % dataset)


def build_task_text(dataset, item):
    """Render one item as the task string every topology receives."""
    if dataset == GSM_HARD:
        return item["question"]
    if dataset == HOTPOTQA:
        return render_context_task(item["context"], item["question"])
    raise ValueError("Unknown dataset: %s" % dataset)
