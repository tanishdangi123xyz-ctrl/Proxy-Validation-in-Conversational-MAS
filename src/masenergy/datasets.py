"""Item loading, answer extraction and grading.

Runs on the Jetson: standard library only, Python 3.10 compatible. Reads the
plain JSON written by scripts/prepare_datasets.py, so the device never needs
the datasets library.

Extraction and grading are dataset-level, never topology-level. The same rule
is applied to a baseline answer and a debate synthesis, otherwise measured
accuracy would differ by topology for reasons unrelated to the topology.
"""

import json
import math
import random
import re
import string
import unicodedata

GSM_HARD = "gsm_hard"
HOTPOTQA = "hotpotqa"

_ANSWER_MARKER = re.compile(r"answer\s*[:\-]\s*(.+)", re.IGNORECASE)
_NUMBER = re.compile(
    r"[-+]?\$?(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
)
_TEMPLATE_SLOT = re.compile(r"<[^<>]{0,80}>")
_BOXED = re.compile(r"\\boxed\s*\{(.*)\}", re.S)
_DELIMITERS_ONLY = re.compile(r"^[\s$\\()\[\]{}*`.,:;]*$")

_WRAPPERS = (("$$", "$$"), ("$", "$"), ("\\[", "\\]"), ("\\(", "\\)"),
             ("**", "**"), ("`", "`"))

CONTAINMENT_FACTOR = 2
CONTAINMENT_SLACK = 4

# Numeric agreement is relative, not absolute. gsm-hard golds are the output of
# a Python program, so a quarter of them are floats carrying ten to seventeen
# significant digits (2040087.3384615383) and three are in scientific notation
# (2.0107e-06). A fixed absolute threshold means two different things at those
# two magnitudes: on an eleven-digit integer it demands exactness, and on a gold
# of 2.0107e-06 it accepts a fifty percent error. One relative threshold means
# the same thing everywhere. The absolute floor only guards a gold of zero.
NUMERIC_REL_TOL = 1e-4
NUMERIC_ZERO_TOL = 1e-9


def nested_sample(pool_size, n_items, seed):
    """Indices of n_items drawn from a pool, nested across n_items.

    A prefix of one fixed permutation, so a draw of 80 always contains the
    draw of 15 taken with the same seed. That is the property the screen
    depends on: it admits a dataset on 15 items and the campaign then runs 80,
    and those two numbers only describe the same thing if the 15 are among the
    80.

    random.sample happens to nest for the sizes currently in use, but only
    because CPython picks its selection-set algorithm for both. It switches to
    a pool-based algorithm once k grows past a threshold that depends on n, and
    the nesting stops holding with nothing to show it stopped. This is the same
    guarantee written down instead of inherited.

    Returned sorted, so the item file stays in source-row order.
    """
    order = list(range(pool_size))
    random.Random(seed).shuffle(order)
    return sorted(order[:n_items])


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


def numbers_match(predicted, gold):
    """True if two numeric strings agree to NUMERIC_REL_TOL.

    Returns False rather than raising on anything unparseable, so a prose
    answer that reached the numeric branch is simply wrong, not a crash.
    """
    try:
        p = float(_strip_number(predicted))
        g = float(_strip_number(gold))
    except (TypeError, ValueError):
        return False
    if math.isnan(p) or math.isnan(g) or math.isinf(p) or math.isinf(g):
        return p == g
    tol = NUMERIC_ZERO_TOL if g == 0.0 else NUMERIC_REL_TOL * abs(g)
    return abs(p - g) <= tol


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
        ok = numbers_match(predicted, gold)
        return {"parse_ok": True, "correct": ok, "f1": float(ok)}

    if dataset == HOTPOTQA:
        ok = span_correct(predicted, gold, aliases)
        return {"parse_ok": True, "correct": ok, "f1": _f1(predicted, gold)}

    raise ValueError("Unknown dataset: %s" % dataset)


SPAN_GOLD_MAX_TOKENS = 4
NUMERIC_GOLD_MAX_DECIMALS = 4


def _decimal_places(text):
    """Digits after the decimal point in the mantissa of a numeric string."""
    mantissa = str(text).split("e")[0].split("E")[0]
    return len(mantissa.split(".")[1]) if "." in mantissa else 0


def gold_shape(dataset, gold):
    """Classify a gold answer by whether the grader can ever score it.

    Model-blind by construction: it looks only at the reference string, never
    at a prediction. That distinction matters. Dropping items because the model
    got them wrong destroys the sample; dropping items whose reference cannot be
    matched by any correct answer removes an instrument fault. Returns one of
    "ok", "long_span", "over_precise", or "unparseable".

    A span gold longer than SPAN_GOLD_MAX_TOKENS is a sentence, not an answer:
    contains_gold bounds the prediction at roughly twice the gold length, so a
    model that answers the question correctly and tersely is marked wrong for
    not reciting the sentence. A numeric gold carrying more than
    NUMERIC_GOLD_MAX_SIGFIGS significant digits cannot be reproduced by a model
    doing arithmetic in prose.
    """
    text = "" if gold is None else str(gold).strip()
    if not text:
        return "unparseable"
    if dataset == GSM_HARD:
        try:
            float(_strip_number(text))
        except (TypeError, ValueError):
            return "unparseable"
        if _decimal_places(text) > NUMERIC_GOLD_MAX_DECIMALS:
            return "over_precise"
        return "ok"
    if dataset == HOTPOTQA:
        if len(_normalise(text).split()) > SPAN_GOLD_MAX_TOKENS:
            return "long_span"
        return "ok"
    raise ValueError("Unknown dataset: %s" % dataset)


def build_task_text(dataset, item):
    """Render one item as the task string every topology receives."""
    if dataset == GSM_HARD:
        return item["question"]
    if dataset == HOTPOTQA:
        return render_context_task(item["context"], item["question"])
    raise ValueError("Unknown dataset: %s" % dataset)
