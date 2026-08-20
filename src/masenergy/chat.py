"""ChatML templating and frozen prompt loading.

The template is applied here rather than by the server so that the exact
token sequence reaching the model is authored by us. Reporting prompt_n for
a prompt the server assembled would mean reporting a number for text we did
not fully control.

Qwen3 is a hybrid thinking model. Suppression is not a flag but a template
detail: an empty think block opens the assistant turn. Getting this wrong
silently re-enables reasoning traces and destroys the output length
distribution, so verify it at pilot at temperature 1.0.

Standard library only, Python 3.10 compatible.
"""

import hashlib
from pathlib import Path

IM_START = "<|im_start|>"
IM_END = "<|im_end|>"
EMPTY_THINK = "<think>\n\n</think>\n\n"

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

_CACHE = {}


def load(name):
    """Load a frozen role prompt by name."""
    if name not in _CACHE:
        path = PROMPT_DIR / ("%s.txt" % name)
        _CACHE[name] = path.read_text(encoding="utf-8").strip()
    return _CACHE[name]


def prompts_hash():
    """Hash of every prompt file, for provenance in the methods section."""
    h = hashlib.sha256()
    for path in sorted(PROMPT_DIR.glob("*.txt")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def build(system, user, suppress_thinking=True):
    """Render a single-turn exchange as raw ChatML text."""
    parts = []
    if system:
        parts.append("%ssystem\n%s%s\n" % (IM_START, system, IM_END))
    parts.append("%suser\n%s%s\n" % (IM_START, user, IM_END))
    parts.append("%sassistant\n" % IM_START)
    if suppress_thinking:
        parts.append(EMPTY_THINK)
    return "".join(parts)


def call_seed(base_seed, call_index):
    """Distinct seed per call within a task.

    Two debate agents given the same prompt and seed would emit identical
    text, collapsing the topology. Spacing is wider than MAX_REPROMPTS so
    retry offsets cannot collide with the next call's seed.
    """
    return int(base_seed) * 1000 + int(call_index) * 10
