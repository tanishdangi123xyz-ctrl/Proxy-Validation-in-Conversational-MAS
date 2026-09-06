"""Whole-pipeline self test. No server, no network, no hardware.

Everything the campaign depends on that can be checked without a GPU is
checked here: extraction, grading, prompt surface, the agreement between the
screen and the campaign, topology orchestration against a stubbed model,
record integrity, serialisation, and the frozen configuration.

The stub returns canned text, so the topologies are exercised for structure
rather than for answer quality: call counts, seed spacing, role labelling,
retry accounting and what each role is actually shown.

Exit status is the number of failed checks.
"""

import contextlib
import csv
import ctypes
import io
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from masenergy import band, chat, config, gpio, ina3221, jetson, records, runner, topologies
from masenergy import datasets as ds
from masenergy import client as client_module
from masenergy.client import LlamaClient, ServerError, _CALL_LOCK
from masenergy.records import CallRecord, FIELDS, RecordWriter, new_run_id

import check_device
import prepare_datasets as prep
import run_campaign
import screen_datasets as screen

GSM_HARD, HOTPOTQA = ds.GSM_HARD, ds.HOTPOTQA

RESULTS = []


def check(section, label, ok, detail=""):
    RESULTS.append((section, label, "ok" if ok else "FAIL", detail))
    return bool(ok)


def warn(section, label, ok, detail=""):
    """A hygiene check that should not block a run it cannot invalidate."""
    RESULTS.append((section, label, "ok" if ok else "WARN", detail))
    return bool(ok)


class StubWriter:
    """Collects records instead of writing them."""

    def __init__(self):
        self.rows = []

    def write(self, record):
        self.rows.append(record)
        return len(self.rows)


class StubClient(LlamaClient):
    """A LlamaClient whose HTTP call is replaced by canned text.

    Subclassing rather than reimplementing keeps the record construction, the
    lock and the retry loop under test instead of mocked away.
    """

    def __init__(self, script=None, writer=None):
        super().__init__(writer or StubWriter(), run_id="selftest")
        self.script = list(script or [])
        self.prompts = []
        self.seeds = []
        self.concurrent = 0
        self.max_concurrent = 0

    def _post(self, payload):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.prompts.append(payload["prompt"])
        self.seeds.append(payload["seed"])
        time.sleep(0.001)
        text = self.script.pop(0) if self.script else "reasoning\nAnswer: 42"
        self.concurrent -= 1
        return {"content": text, "timings": {"prompt_n": 11, "predicted_n": 7},
                "stop_type": "eos", "truncated": False}


def answered(value):
    return "working through it\nAnswer: %s" % value


def verdict(value):
    return "the arithmetic is fine\nVerdict: %s" % value


EXTRACTION = [
    ("echoed slot, answer on same line",
     HOTPOTQA, "reasoning\nAnswer: <your final answer> 49.6", "49.6"),
    ("echoed slot, answer on next line",
     HOTPOTQA, "reasoning\nAnswer: <your final answer>\n49.6", "49.6"),
    ("echoed slot, numeric branch",
     GSM_HARD, "reasoning\nAnswer: <your final answer> 49.6", "49.6"),
    ("display math unwrapped across delimiters",
     HOTPOTQA, "so we get\n$$\n\\frac{1}{2}\n$$", "\\frac{1}{2}"),
    ("delimiters with no content are a failure", HOTPOTQA, "$$\n$$", None),
    ("walk stops at first content line",
     HOTPOTQA, "first line\nsecond line\n$$", "second line"),
    ("boxed latex", HOTPOTQA, "work\nAnswer: $\\boxed{\\frac{1}{2}}$", "\\frac{1}{2}"),
    ("inline math wrapper", HOTPOTQA, "work\nAnswer: $42$", "42"),
    ("bare answer untouched", HOTPOTQA, "work\nAnswer: Treaty of Amiens", "Treaty of Amiens"),
    ("dollar amount is not a wrapper", GSM_HARD, "work\nAnswer: $5", "5"),
    ("trailing period stripped", HOTPOTQA, "work\nAnswer: Marie Curie.", "Marie Curie"),
    ("last marker wins", HOTPOTQA, "Answer: wrong\nmore\nAnswer: right", "right"),
    ("no marker, plain last line", HOTPOTQA, "reasoning\nTreaty of Amiens", "Treaty of Amiens"),
    ("empty response is a failure", HOTPOTQA, "   ", None),
    ("empty slot is a failure", HOTPOTQA, "Answer: <your final answer>", None),
    ("negative number", GSM_HARD, "work\nAnswer: -342927260", "-342927260"),
    ("thousands separators stripped", GSM_HARD, "work\nAnswer: 1,335,907", "1335907"),
    ("scientific notation survives as one number",
     GSM_HARD, "work\nAnswer: 2.0107e-06", "2.0107e-06"),
    ("negative exponent is not split into a trailing 06",
     GSM_HARD, "work\nAnswer: 5.158e-07", "5.158e-07"),
    ("leading-dot decimal", GSM_HARD, "work\nAnswer: .5", ".5"),
]

GRADING_SPAN = [
    ("exact match", "Treaty of Amiens", "Treaty of Amiens", (), True),
    ("case and article insensitive", "the treaty of amiens", "Treaty of Amiens", (), True),
    ("short prose wrapper accepted",
     "The Treaty of Amiens happened first", "Treaty of Amiens", (), True),
    ("alias accepted", "NYC", "New York City", ("NYC",), True),
    ("long paragraph spanning gold refused",
     "There were many candidates under discussion that year and after weighing "
     "them all the Treaty of Amiens is the one that came first", "Treaty of Amiens",
     (), False),
    ("wrong answer refused", "Treaty of Paris", "Treaty of Amiens", (), False),
    ("single token gold gets slack", "the year was 1996", "1996", (), True),
    ("gold absent refused", "something else", "Treaty of Amiens", (), False),
]

GRADING_NUMERIC = [
    ("exact numeric", "49.6", "49.6", True),
    ("integer gold as float prediction", "8.0", "8", True),
    ("separators tolerated", "1,335,907", "1335907", True),
    ("large magnitude exact", "75455064000", "75455064000", True),
    ("four significant figures refused", "0.01456", "0.0145623999", False),
    ("wrong number refused", "49.7", "49.6", False),
    ("unparseable prediction refused", "not a number", "49.6", False),
    ("scientific notation matches itself", "2.0107e-06", "2.0107e-06", True),
    ("relative tolerance holds at large magnitude",
     "2040087.34", "2040087.3384615383", True),
    ("decimal form of a scientific-notation gold",
     "0.0000020107", "2.0107e-06", True),
    ("three significant figures refused at small magnitude",
     "0.00000201", "2.0107e-06", False),
    ("a tolerance that is absolute at every scale would accept this",
     "0.000001", "2.0107e-06", False),
    ("half a percent off is still wrong", "2050000", "2040087.3384615383", False),
]

HOTPOT_ROW = {
    "question": "  Which magazine was started first, Arthur's or First for Women?  ",
    "answer": "Arthur's Magazine",
    "level": "medium",
    "supporting_facts": {"title": ["Arthur's Magazine", "First for Women"]},
    "context": {
        "title": ["Arthur's Magazine", "Radio City", "First for Women"],
        "sentences": [
            ["Arthur's Magazine was an American periodical. ", "Published in 1844."],
            ["Radio City is India's first private FM radio station."],
            ["First for Women is a womans magazine. ", "Started in 1989."],
        ],
    },
}

GSM_HARD_ROW = {"input": "  Janet has 4219328.6 ducks.  ", "target": 8.0}

_NUMBERED = re.compile(r"^\s*(\d)[\.\)]\s*(.+)$", re.MULTILINE)
PLAN_SAMPLE = "1. Which treaty ended the war in 1802?\n2. When was Luneville signed?"

ANSWER_PROMPTS = ("baseline_solver", "debate_agent", "debate_synthesiser",
                  "planner_synthesiser", "solver", "worker")


def test_extraction():
    for label, dataset, text, expected in EXTRACTION:
        got = ds.extract_answer(dataset, text)
        check("extraction", label, got == expected,
              "" if got == expected else "got %r want %r" % (got, expected))


def test_grading():
    for label, pred, gold, aliases, expected in GRADING_SPAN:
        check("grading span", label, ds.span_correct(pred, gold, aliases) == expected)
    for label, pred, gold, expected in GRADING_NUMERIC:
        got = ds.grade(GSM_HARD, pred, gold)["correct"]
        check("grading numeric", label, got == expected,
              "" if got == expected else "got %s" % got)
    check("grading numeric", "parse failure is not correct",
          ds.grade(GSM_HARD, None, "5") == {"parse_ok": False, "correct": False, "f1": 0.0})
    check("grading span", "unknown dataset raises", _raises(ds.grade, "nope", "a", "a"))


def _raises(fn, *args):
    try:
        fn(*args)
    except (ValueError, KeyError, RuntimeError):
        return True
    return False


def test_prompt_surface():
    blocks = set()
    for name in ANSWER_PROMPTS:
        text = chat.load(name)
        if "Work through the problem" not in text:
            check("prompts", "%s carries the answer block" % name, False)
            continue
        blocks.add(text[text.index("Work through the problem"):])
    check("prompts", "answer block byte-identical across %d prompts" % len(ANSWER_PROMPTS),
          len(blocks) == 1)

    leaked = [p.name for p in sorted(chat.PROMPT_DIR.glob("*.txt"))
              if "<" in p.read_text(encoding="utf-8")]
    check("prompts", "no template slots remain", not leaked, str(leaked))

    check("prompts", "reasoning still demanded",
          all("showing your reasoning in full" in chat.load(n) for n in ANSWER_PROMPTS))

    found = [b.strip() for _, b in _NUMBERED.findall(PLAN_SAMPLE)]
    check("prompts", "planner format parses to %d subtasks" % config.PLANNER_WORKER_SUBTASKS,
          len(found) == config.PLANNER_WORKER_SUBTASKS)

    rendered = chat.build("SYSTEM", "USER")
    check("chat", "chatml has system, user and assistant turns",
          rendered.count(chat.IM_START) == 3 and rendered.count(chat.IM_END) == 2)
    check("chat", "thinking suppressed by empty think block",
          rendered.endswith(chat.EMPTY_THINK))
    check("chat", "thinking block omitted when not requested",
          chat.EMPTY_THINK not in chat.build("S", "U", suppress_thinking=False))


def test_seed_spacing():
    seeds = [chat.call_seed(101, i) for i in range(20)]
    check("seeds", "distinct per call index", len(set(seeds)) == len(seeds))
    widened = set()
    for i in range(20):
        for attempt in range(config.MAX_REPROMPTS + 1):
            widened.add(chat.call_seed(101, i) + attempt)
    check("seeds", "retry offsets cannot collide with the next call",
          len(widened) == 20 * (config.MAX_REPROMPTS + 1))
    check("seeds", "different base seeds stay disjoint",
          not (set(chat.call_seed(101, i) for i in range(20))
               & set(chat.call_seed(202, i) for i in range(20))))


def test_screen_campaign_agreement():
    screened = screen._build_hotpotqa(HOTPOT_ROW, 0)["task"]
    campaign = ds.build_task_text(HOTPOTQA, prep.build_hotpotqa(HOTPOT_ROW, 0, 0))
    check("agreement", "hotpotqa renders identically in both paths",
          screened == campaign, "" if screened == campaign else repr(screened[:70]))

    s_item = screen._build_gsm_hard(GSM_HARD_ROW, 0)
    c_item = prep.build_gsm_hard(GSM_HARD_ROW, 0, 0)
    check("agreement", "gsm_hard renders identically in both paths",
          s_item["task"] == ds.build_task_text(GSM_HARD, c_item))
    check("agreement", "gsm_hard gold formats identically as %r" % c_item["answer"],
          s_item["answer"] == c_item["answer"] == "8")

    check("agreement", "campaign preparer targets gsm-hard, not gsm8k",
          "gsm-hard" in prep.GSM_HARD_REPOS[0] and prep.GSM_HARD_SPLIT == "train")
    check("agreement", "every campaign dataset was screened",
          set(config.DATASETS) <= {c["name"] for c in screen.CANDIDATES})
    check("agreement", "screen grades survivors under their own mode",
          {c["extract_as"] for c in screen.CANDIDATES
           if c["name"] in config.DATASETS} == set(config.DATASETS))
    check("agreement", "screen numeric grader delegates to the campaign",
          screen._grade_numeric("0.01456", "0.0145623999", ()) is False)
    check("agreement", "a screen of 15 is contained in a campaign draw of 80",
          set(ds.nested_sample(1319, 15, config.ORDER_SEED))
          <= set(ds.nested_sample(1319, 80, config.ORDER_SEED)))
    check("agreement", "screen and campaign draw through the same sampler",
          prep._sample_indices(1319, 15, config.ORDER_SEED)
          == ds.nested_sample(1319, 15, config.ORDER_SEED))
    check("agreement", "the draw is deterministic across processes",
          ds.nested_sample(500, 20, 7) == ds.nested_sample(500, 20, 7))
    check("agreement", "runner validator matches the screen validator",
          runner.make_validator(HOTPOTQA)("Answer: Paris")
          == screen._validator(HOTPOTQA)("Answer: Paris"))


def run_topology(name, script, seed=101):
    client = StubClient(script)
    result = topologies.get(name)(
        client, "TASK", 0.7, seed,
        {"dataset": GSM_HARD, "item_id": "x", "repetition": 0},
        runner.make_validator(GSM_HARD))
    return client, result


def test_topologies():
    client, result = run_topology("baseline", [answered("42")])
    check("topology", "baseline issues exactly one call", result["n_calls"] == 1)
    check("topology", "baseline extracts the answer", result["answer"] == "42")
    check("topology", "baseline labels its role",
          client.writer.rows[0].role == "solver"
          and client.writer.rows[0].topology == "baseline")

    expected = config.DEBATE_AGENTS * config.DEBATE_ROUNDS + 1
    client, result = run_topology("debate", [answered(i) for i in range(expected)])
    check("topology", "debate issues %d calls" % expected, result["n_calls"] == expected)
    roles = [r.role for r in client.writer.rows]
    check("topology", "debate ends with a synthesiser", roles[-1] == "synthesiser")
    check("topology", "debate agents are labelled distinctly",
          len({r for r in roles if r.startswith("agent_")}) == config.DEBATE_AGENTS)
    check("topology", "debate round one hides peer answers",
          "Solver 1 answered" not in client.prompts[0])
    check("topology", "debate round two shows peer answers",
          "answered" in client.prompts[config.DEBATE_AGENTS])
    check("topology", "debate seeds are all distinct",
          len(set(client.seeds)) == len(client.seeds))
    # Round two must replay the agent's own answer. Without it the only answer
    # in context is the peer's, the agent adopts it, and two agents trade
    # answers every round while the change-rate check reports healthy churn.
    round_two = client.prompts[config.DEBATE_AGENTS]
    check("topology", "debate round two replays the agent's own prior answer",
          ("Your own previous answer" in round_two)
          == config.DEBATE_SHOWS_OWN_PRIOR)
    check("topology", "debate agent sees its own answer, not only the peer's",
          "Answer: 0" in round_two and "Answer: 1" in round_two,
          "round two prompt: %r" % round_two[-160:])

    client, result = run_topology(
        "planner_worker",
        [PLAN_SAMPLE] + [answered(i) for i in range(config.PLANNER_WORKER_SUBTASKS)]
        + [answered(99)])
    expected = 1 + config.PLANNER_WORKER_SUBTASKS + 1
    check("topology", "planner_worker issues %d calls" % expected,
          result["n_calls"] == expected)
    check("topology", "planner_worker reports a usable plan", result["plan_ok"] is True)
    check("topology", "workers receive their own subtask",
          "Which treaty" in client.prompts[1] and "Luneville" in client.prompts[2])
    check("topology", "workers never see each other",
          "Luneville" not in client.prompts[1])
    # A subtask alone is not answerable: the planner keeps the numbers. Without
    # the problem as background the worker fails, exhausts its retries, and the
    # item costs double the calls of its neighbours for no output.
    check("topology", "worker carries the problem as background",
          ("TASK" in client.prompts[1]) == config.PLANNER_WORKER_SHOWS_TASK)
    check("topology", "planner subtasks reach the record as text, not a list repr",
          "[" not in client.writer.rows[0].answer_extracted
          and "Which treaty" in client.writer.rows[0].answer_extracted,
          "planner row: %r" % client.writer.rows[0].answer_extracted)

    bad_plan = ["no numbered list here"] * (config.MAX_REPROMPTS + 1)
    client, result = run_topology(
        "planner_worker",
        bad_plan + [answered(i) for i in range(config.PLANNER_WORKER_SUBTASKS)]
        + [answered(99)])
    check("topology", "planner failure keeps the call structure intact",
          result["plan_ok"] is False
          and result["n_calls"] == (config.MAX_REPROMPTS + 1)
          + config.PLANNER_WORKER_SUBTASKS + 1)

    client, result = run_topology(
        "solver_critic", [answered("42"), verdict("ACCEPT")])
    check("topology", "solver_critic stops on ACCEPT", result["n_calls"] == 2)

    script = [answered("42")]
    for _ in range(config.SOLVER_CRITIC_MAX_ITERS - 1):
        script += [verdict("REJECT"), answered("43")]
    script += [verdict("REJECT")]
    client, result = run_topology("solver_critic", script)
    ceiling = 2 * config.SOLVER_CRITIC_MAX_ITERS
    check("topology", "solver_critic caps at %d calls" % ceiling,
          result["n_calls"] == ceiling, "got %d" % result["n_calls"])
    revise = [p for p in client.prompts if "Critic feedback" in p]
    check("topology", "solver is shown the critique, not just the verdict",
          bool(revise) and "the arithmetic is fine" in revise[0],
          "solver saw: %r" % (revise[0].split("Critic feedback:")[-1].strip()[:40]
                              if revise else "nothing"))

    client, result = run_topology("baseline", ["no answer marker at all"] * 3)
    check("topology", "parse failure retries up to MAX_REPROMPTS",
          result["n_calls"] == config.MAX_REPROMPTS + 1)
    check("topology", "retry rows are flagged",
          [r.is_retry for r in client.writer.rows] == [False] + [True] * config.MAX_REPROMPTS)
    check("topology", "retry reason recorded",
          all(r.retry_reason == "parse_failure" for r in client.writer.rows[1:]))
    check("topology", "exhausted retries report parse failure", result["parse_ok"] is False)

    predicted = config.calls_per_item()
    actual = 1 + (config.DEBATE_AGENTS * config.DEBATE_ROUNDS + 1) \
        + (1 + config.PLANNER_WORKER_SUBTASKS + 1)
    lo = actual + 2
    hi = actual + 2 * config.SOLVER_CRITIC_MAX_ITERS
    check("budget", "calls_per_item %.1f lies inside the achievable range %d-%d"
          % (predicted, lo, hi), lo <= predicted <= hi)


SERVER_DEFAULTED_SAMPLERS = ("top_p", "top_k", "min_p", "typical_p",
                             "repeat_penalty", "presence_penalty",
                             "frequency_penalty", "mirostat")


def test_payload_and_cache():
    client = StubClient()
    payload = client._payload("p", 0.7, 5)
    missing = [k for k in SERVER_DEFAULTED_SAMPLERS if k not in payload]
    # A sampler left out of the payload still runs, just on llama.cpp's default
    # and outside config_hash(). min_p defaults to 0.05, which clips the tail
    # the temperature sweep exists to widen.
    check("payload", "every server-defaulted sampler is pinned explicitly",
          not missing, str(missing))
    check("payload", "prompt caching is off on the wire",
          payload["cache_prompt"] is False)
    check("payload", "pinned samplers are the recorded ones",
          payload["min_p"] == config.MIN_P and payload["top_p"] == config.TOP_P
          and payload["repeat_penalty"] == config.REPEAT_PENALTY)

    snapshot = config.snapshot()[0]
    unrecorded = [k.upper() for k in SERVER_DEFAULTED_SAMPLERS
                  if k.upper() not in snapshot]
    check("payload", "every pinned sampler appears in the config snapshot",
          not unrecorded, str(unrecorded))

    class Leaky(StubClient):
        def _post(self, payload):
            out = super()._post(payload)
            out["tokens_evaluated"] = 400   # prompt was 400 long
            out["timings"]["prompt_n"] = 40  # only 40 were prefilled
            return out

    # A server that serves 360 of 400 prompt tokens from cache spends a
    # fraction of the energy while the row still claims the whole prompt. That
    # has to stop the run, not survive into the regression.
    leaky = Leaky()
    check("payload", "a live prompt cache aborts the call",
          _raises_type(ServerError,
                       lambda: leaky.call("p", 0.7, 1, {"dataset": GSM_HARD})))

    class Failing(StubClient):
        def _post(self, payload):
            return {"error": {"message": "context overflow"}}

    check("payload", "a 200 carrying an error body is not a successful call",
          _raises_type(ServerError,
                       lambda: Failing().call("p", 0.7, 1, {"dataset": GSM_HARD})))

    class Thinking(StubClient):
        def _post(self, payload):
            out = super()._post(payload)
            out["content"] = "<think>hmm</think>\nAnswer: 42"
            return out

    _, record = Thinking().call("p", 0.7, 1, {"dataset": GSM_HARD})
    check("payload", "a think tag in the raw response is flagged",
          record.thinking_leak is True)
    check("payload", "the flag survives extraction stripping the tag",
          ds.extract_answer(GSM_HARD, "<think>hmm</think>\nAnswer: 42") == "42")


def test_debug_truncated_capture():
    """A call cut off at MAX_TOKENS can be inspected after the fact.

    The CSV cannot answer "what was the model doing" for a truncated call:
    raw completion text is not a measurement and is not a column. This is the
    opt-in side door dry_run.py's --debug-truncated wires up. Off by default,
    so the three cases that matter are: nothing written when unset, nothing
    written for an ordinary call even when set, something written only when
    both are true and it lands outside the CSV's own schema.
    """
    class Limited(StubClient):
        def _post(self, payload):
            out = super()._post(payload)
            out["stop_type"] = "limit"
            return out

    tmp = Path(tempfile.mkdtemp())

    client = Limited()
    client.call("p", 0.7, 1, {"dataset": GSM_HARD, "item_id": "i"})
    check("client", "unset debug_truncated_dir writes nothing",
          not any(tmp.iterdir()), "debug_truncated_dir defaults to None")

    client = Limited()
    client.debug_truncated_dir = tmp
    ordinary = StubClient()
    ordinary.debug_truncated_dir = tmp
    ordinary.call("p", 0.7, 1, {"dataset": GSM_HARD, "item_id": "ok"})
    check("client", "an eos call writes nothing even with the dir set",
          not any(tmp.iterdir()),
          "only stop_type=='limit' should ever produce a file")

    _, record = client.call("prompt text", 0.7, 1,
                            {"dataset": GSM_HARD, "item_id": "trunc-1"})
    written = list(tmp.iterdir())
    check("client", "a limited call writes exactly one file",
          len(written) == 1, str(written))
    if written:
        body = written[0].read_text(encoding="utf-8")
        check("client", "the file carries both the prompt and the completion",
              "prompt text" in body and "reasoning" in body, body[:120])
    check("client", "the CSV record itself is untouched by the debug write",
          record.finish_reason == "limit" and not hasattr(record, "debug_path"),
          "the side file must never become a schema field")


def test_band():
    check("band", "the band is %.0f points wide" % (band.BAND_HIGH - band.BAND_LOW),
          band.BAND_HIGH - band.BAND_LOW == 25.0)
    lo, hi = band.wilson(8, 15)
    check("band", "a 15-item screen has an interval wider than the band itself",
          hi - lo > band.BAND_HIGH - band.BAND_LOW,
          "n=15 gives %.0f-%.0f, %.0f points wide" % (lo, hi, hi - lo))
    check("band", "15 items cannot return a verdict",
          band.verdict(8, 15)[0].startswith("UNRESOLVED"))
    check("band", "10 items cannot return a verdict",
          band.verdict(5, 10)[0].startswith("UNRESOLVED"))
    check("band", "a clear floor is still called at small n",
          band.verdict(0, 20)[0].startswith("BELOW"))
    check("band", "a clear ceiling is still called at small n",
          band.verdict(20, 20)[0].startswith("ABOVE"))
    check("band", "no data is not a verdict", band.verdict(0, 0)[0] == "NO DATA")
    need = band.n_for_halfwidth(10.0)
    warn("band", "campaign N_ITEMS=%d resolves the band to +-10 points"
         % config.N_ITEMS, config.N_ITEMS >= need,
         "need %d items per cell for +-10 points, %d for +-5"
         % (need, band.n_for_halfwidth(5.0)))


def test_gold_shape():
    for dataset, gold, expected in (
        (GSM_HARD, "25124292", "ok"),
        (GSM_HARD, "-4487772.5", "ok"),
        (GSM_HARD, "2.0107e-06", "ok"),
        (GSM_HARD, "2040087.3384615383", "over_precise"),
        (GSM_HARD, "7.1466666667", "over_precise"),
        (GSM_HARD, "not a number", "unparseable"),
        (HOTPOTQA, "Animation", "ok"),
        (HOTPOTQA, "Arthur's Magazine", "ok"),
        (HOTPOTQA, "It was held in France from 10 June to 12 July 1998.", "long_span"),
        (HOTPOTQA, "", "unparseable"),
    ):
        got = ds.gold_shape(dataset, gold)
        check("gold shape", "%s %r -> %s" % (dataset, gold[:34], expected),
              got == expected, "" if got == expected else "got %s" % got)
    check("gold shape", "classification never looks at a prediction",
          "predict" not in ds.gold_shape.__code__.co_varnames)


def _raises_type(exc_type, fn):
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


def test_serialisation():
    client = StubClient(["Answer: 1"] * 8)
    errors = []

    def worker():
        try:
            client.call("p", 0.7, 1, {"dataset": GSM_HARD, "item_id": "i"})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("serialisation", "no thread errored", not errors, str(errors[:1]))
    check("serialisation", "never more than one call in flight",
          client.max_concurrent == 1, "peak %d" % client.max_concurrent)
    check("serialisation", "lock released after use", not _CALL_LOCK.locked())


def test_records():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "calls.csv"
        with RecordWriter(path, {"kind": "selftest"}) as writer:
            client = StubClient([answered("42")], writer=writer)
            topologies.get("baseline")(
                client, "TASK", 0.7, 101,
                {"dataset": GSM_HARD, "item_id": "i", "repetition": 0},
                runner.make_validator(GSM_HARD))
        import csv as _csv
        rows = list(_csv.DictReader(open(path, encoding="utf-8")))
        check("records", "one row written per call", len(rows) == 1)
        check("records", "header matches the schema",
              set(rows[0]) == set(FIELDS))
        check("records", "provenance stamped on the row",
              rows[0]["config_hash"] == config.config_hash()
              and rows[0]["prompts_hash"] == chat.prompts_hash())
        check("records", "input and output tokens stay separate",
              rows[0]["prompt_n"] == "11" and rows[0]["predicted_n"] == "7")
        check("records", "no field merges the two counts",
              not any(f in FIELDS for f in ("total_tokens", "tokens", "n_tokens")))
        check("records", "prompt length is stored beside prompt tokens processed",
              rows[0]["prompt_n_total"] == "11" and "slot_cache_n" in rows[0])
        check("records", "no ungraded correctness column on a call row",
              not {"correct", "f1"} & set(FIELDS))
        check("records", "thinking leak is recorded from the raw response",
              rows[0]["thinking_leak"] == "False")
        meta = json.loads(path.with_suffix(".meta.json").read_text())
        check("records", "metadata sidecar records the schema",
              meta.get("schema_fields") == list(FIELDS))

        with RecordWriter(path, {"kind": "selftest"}) as writer:
            writer.write(CallRecord(run_id="second"))
        rows = list(_csv.DictReader(open(path, encoding="utf-8")))
        check("records", "reopening appends rather than truncating", len(rows) == 2)

        check("records", "run id carries the config hash",
              new_run_id(config.config_hash()).endswith(config.config_hash()))

        stale = Path(tmp) / "stale.csv"
        stale.write_text("run_id,dataset\nx,y\n", encoding="utf-8")
        check("records", "appending to a file with a different schema is refused",
              _raises_runtime(lambda: RecordWriter(stale)))


def _write_items(root, n=2):
    """Write a minimal hash-verified item file for every campaign dataset."""
    (root / "data" / "items").mkdir(parents=True)
    for name in config.DATASETS:
        items = [{"id": "%s-%05d" % (name, i), "rank": i,
                  "question": "q%d" % i, "answer": "42",
                  "context": "c%d" % i if name == HOTPOTQA else None,
                  "level": None}
                 for i in range(n)]
        (root / "data" / "items" / ("items_%s.json" % name)).write_text(
            json.dumps({"sha256": ds.items_hash(items), "items": items}))


def test_run_refuses_while_unvalidated():
    """A campaign must not start while any REQUIRED_BEFORE_RUN value is None.

    Driven through Runner.run() rather than through config.validate(), because
    validate() passing its own unit check is exactly what was true while the
    defect was live: the guard worked and nothing on the execution path called
    it. Asserting the refusal anywhere other than the entry point would not
    have caught that, and would not catch it coming back.

    Every required parameter is blanked rather than one, so the check also
    holds once the campaign's own values are filled in, and so the message can
    be asserted to name all of them rather than only the first.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_items(root)
        client = StubClient([answered("42")] * 4000)
        run = runner.Runner(client, root, out_dir=root / "out", run_id="selftest")

        saved = {n: getattr(config, n) for n in config.REQUIRED_BEFORE_RUN}
        handler = signal.getsignal(signal.SIGINT)
        raised = None
        try:
            for name in config.REQUIRED_BEFORE_RUN:
                setattr(config, name, None)
            try:
                run.run()
            except RuntimeError as exc:
                raised = exc
        finally:
            for name, value in saved.items():
                setattr(config, name, value)
            signal.signal(signal.SIGINT, handler)
        written = sorted(p.name for p in run.out.iterdir())

    check("guard", "Runner.run refuses to start while a parameter is unset",
          raised is not None,
          "run() returned instead of raising; nothing calls config.validate()")
    check("guard", "the refusal happens before any model call is issued",
          not client.prompts, "%d calls were issued first" % len(client.prompts))
    missing = [n for n in config.REQUIRED_BEFORE_RUN if n not in str(raised or "")]
    check("guard", "the refusal names every unset parameter, not just the first",
          not missing, "absent from the message: %s" % missing)
    check("guard", "no block table was written before the refusal",
          not written, "wrote %s" % written[:3])


class FakeGatedDevice(client_module.Device):
    """A Device that genuinely gates, standing in for JetsonDevice off-target.

    Also overrides the safety-watchdog hooks, trivially, so this still reads
    as a fully implemented Device to is_stub()'s any-unoverridden-method
    check: a fake meant to stand in for "a real device" that silently never
    implemented hardware safety would be exactly the kind of half-real stub
    is_stub() exists to catch, and this class should not be that.
    """

    def wait_for_gate(self):
        return {"gate_wait_s": 7.5, "gate_timed_out": True}

    def start_safety_watchdog(self):
        pass

    def safety_tripped(self):
        return None

    def read_state(self):
        return dict(client_module.Device.read_state(self), temp_c_soc=44.0)


class FakeClock:
    """Monotonic time and sleep that advance only when sleep is called."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _ramp(values):
    """Read a scripted temperature sequence, holding on the last value."""
    box = list(values)

    def read():
        return box.pop(0) if len(box) > 1 else box[0]
    return read


def test_thermal_gate():
    """The gate policy: both directions, the timeout, and the recorded fields.

    Driven through injected clocks so a five-minute timeout is exercised
    without waiting five minutes, and so the cold branch can be tested at all.
    A device cannot be made cold on demand.
    """
    clock = FakeClock()
    result = jetson.wait_until_in_band(_ramp([45.0]), 45.0, 1.0, 300.0, 2.0,
                                       clock.monotonic, clock.sleep)
    check("thermal", "a device already in band is not held",
          result == {"gate_wait_s": 0.0, "gate_timed_out": False}, str(result))

    clock = FakeClock()
    result = jetson.wait_until_in_band(
        _ramp([60.0, 55.0, 50.0, 45.5]), 45.0, 1.0, 300.0, 2.0,
        clock.monotonic, clock.sleep)
    check("thermal", "a hot device is held until it cools into band",
          result["gate_wait_s"] == 6.0 and not result["gate_timed_out"],
          str(result))

    clock = FakeClock()
    result = jetson.wait_until_in_band(
        _ramp([20.0, 30.0, 40.0, 44.5]), 45.0, 1.0, 300.0, 2.0,
        clock.monotonic, clock.sleep)
    check("thermal", "a cold device is held until it warms into band",
          result["gate_wait_s"] == 6.0 and not result["gate_timed_out"],
          "a gate that only watches for overheating returns 0.0 here: %s"
          % result)

    clock = FakeClock()
    result = jetson.wait_until_in_band(_ramp([90.0]), 45.0, 1.0, 10.0, 2.0,
                                       clock.monotonic, clock.sleep)
    check("thermal", "a device that never reaches band gives up at the timeout",
          result["gate_timed_out"] and result["gate_wait_s"] >= 10.0, str(result))
    check("thermal", "giving up does not overshoot the timeout",
          result["gate_wait_s"] == 10.0, str(result))

    clock = FakeClock()
    jetson.wait_until_in_band(_ramp([90.0]), 45.0, 1.0, 5.0, 2.0,
                              clock.monotonic, clock.sleep)
    check("thermal", "the final poll is clipped so the timeout is not exceeded",
          clock.slept == [2.0, 2.0, 1.0], str(clock.slept))

    check("thermal", "the band is symmetric about the target",
          jetson.wait_until_in_band(_ramp([44.0]), 45.0, 1.0, 1.0, 0.1,
                                    FakeClock().monotonic, FakeClock().sleep
                                    )["gate_timed_out"] is False
          and jetson.wait_until_in_band(_ramp([46.0]), 45.0, 1.0, 1.0, 0.1,
                                        FakeClock().monotonic, FakeClock().sleep
                                        )["gate_timed_out"] is False)

    check("thermal", "millidegrees are converted, not read raw",
          _read_zone_roundtrip() == 45.5)

    check("thermal", "a machine with no thermal zones refuses to construct",
          _raises_type(jetson.ThermalUnavailable,
                       lambda: jetson.JetsonDevice(Path("/nonexistent"))))

    client = StubClient([answered("42")])
    client.device = FakeGatedDevice()
    _, record = client.call("p", 0.7, 1, {"dataset": GSM_HARD})
    check("thermal", "gate_wait_s reaches the call record",
          record.gate_wait_s == 7.5, str(record.gate_wait_s))
    check("thermal", "gate_timed_out reaches the call record",
          record.gate_timed_out is True)
    check("thermal", "the gate waits outside the measured window",
          record.trigger_high_ts >= 0.0 and record.temp_c_soc_before == 44.0)


def test_thermal_safety_watchdog():
    """The hardware safety ceiling: a stricter, separate policy from the gate.

    Exercised the same way wait_until_in_band is: the policy (_step) driven
    directly against a scripted sequence, no real thread and no real elapsed
    time, so the debounce and latching behaviour can be proven without
    waiting on THERMAL_SAFETY_POLL_S in real time.
    """
    clock = FakeClock()
    wd = jetson.ThermalWatchdog(
        [("soc", lambda: 50.0)], limit_c=90.0, poll_s=1.0, consecutive=2,
        clock=clock.monotonic, sleep=clock.sleep)
    out = io.StringIO()
    for _ in range(5):
        wd._step(wd._poll_once(), out=out)
    check("safety", "a device safely under the limit never trips",
          wd.tripped() is None and out.getvalue() == "")

    wd = jetson.ThermalWatchdog(
        [("soc", lambda: 95.0)], limit_c=90.0, poll_s=1.0, consecutive=2,
        clock=clock.monotonic, sleep=clock.sleep)
    out = io.StringIO()
    wd._step(wd._poll_once(), out=out)
    check("safety", "one over-limit poll alone does not trip",
          wd.tripped() is None and out.getvalue() == "",
          "a single noisy reading should not lose a ten-day campaign")
    wd._step(wd._poll_once(), out=out)
    trip = wd.tripped()
    check("safety", "a second consecutive over-limit poll trips",
          trip is not None and trip["zone"] == "soc" and trip["temp_c"] == 95.0,
          str(trip))
    check("safety", "the trip message is loud and names the limit",
          "THERMAL SAFETY WATCHDOG TRIPPED" in out.getvalue()
          and "90.0" in out.getvalue() and "95.0" in out.getvalue())

    box = {"n": 0}
    def flaky():
        box["n"] += 1
        if box["n"] == 2:
            raise OSError("simulated bad read")
        return 95.0
    wd = jetson.ThermalWatchdog(
        [("soc", flaky)], limit_c=90.0, poll_s=1.0, consecutive=2,
        clock=clock.monotonic, sleep=clock.sleep)
    out = io.StringIO()
    wd._step(wd._poll_once(), out=out)   # 95.0, streak 1
    wd._step(wd._poll_once(), out=out)   # read fails, streak untouched
    check("safety", "a failed read does not erase progress toward a trip",
          wd.tripped() is None and wd._over_streak == 1, str(wd._over_streak))
    wd._step(wd._poll_once(), out=out)   # 95.0 again, streak 2, trips
    check("safety", "the streak resumes and trips once real readings return",
          wd.tripped() is not None)

    cooling = _ramp([95.0, 95.0, 85.0, 85.0])
    wd = jetson.ThermalWatchdog(
        [("soc", cooling)], limit_c=90.0, poll_s=1.0, consecutive=3,
        clock=clock.monotonic, sleep=clock.sleep)
    out = io.StringIO()
    for _ in range(4):
        wd._step(wd._poll_once(), out=out)
    check("safety", "a device that cools before reaching consecutive never trips",
          wd.tripped() is None, "streak should have reset when it dropped "
          "under the limit before reaching THERMAL_SAFETY_CONSECUTIVE")

    latch = jetson.ThermalWatchdog(
        [("soc", _ramp([95.0, 95.0, 40.0, 40.0]))], limit_c=90.0, poll_s=1.0,
        consecutive=2, clock=clock.monotonic, sleep=clock.sleep)
    out = io.StringIO()
    for _ in range(4):
        latch._step(latch._poll_once(), out=out)
    check("safety", "a trip latches, a later cool reading does not clear it",
          latch.tripped() is not None,
          "once tripped this device is not to be trusted again this run")

    worst_of = jetson.ThermalWatchdog(
        [("soc", lambda: 50.0), ("gpu", lambda: 92.0)],
        limit_c=90.0, poll_s=1.0, consecutive=1,
        clock=clock.monotonic, sleep=clock.sleep)
    out = io.StringIO()
    worst_of._step(worst_of._poll_once(), out=out)
    trip = worst_of.tripped()
    check("safety", "the watchdog trips on whichever zone is worst, not just soc",
          trip is not None and trip["zone"] == "gpu", str(trip))

    device = jetson.JetsonDevice.__new__(jetson.JetsonDevice)
    device.soc = device.cpu = device.gpu = None
    device._watchdog = None
    check("safety", "safety_tripped() is None before the watchdog is started",
          device.safety_tripped() is None)


class FakeSafetyDevice(client_module.Device):
    """A Device whose safety watchdog is already tripped, for call() tests."""

    def __init__(self, trip):
        self._trip = trip

    def safety_tripped(self):
        return self._trip

    def read_state(self):
        return dict(client_module.Device.read_state(self))


def test_thermal_emergency_stops_the_client():
    """A tripped watchdog refuses the call outright, unlike every other fault.

    This is the one exception to client.py's own "faults are recorded, the
    run continues" rule, so it is tested to a different standard: not just
    that the fault is visible in the row, but that no row and no HTTP call
    happen at all once the watchdog has fired.
    """
    trip = {"zone": "soc", "temp_c": 96.0, "limit_c": 90.0,
            "since_monotonic": 0.0}
    stub = StubClient([answered("42")])
    stub.device = FakeSafetyDevice(trip)
    raised = None
    try:
        stub.call("p", 0.7, 1, {"dataset": GSM_HARD})
    except client_module.ThermalEmergency as exc:
        raised = exc
    check("safety", "a tripped watchdog raises ThermalEmergency, not a fault row",
          raised is not None and "96.0" in str(raised), str(raised))
    check("safety", "no HTTP call was issued once the watchdog had tripped",
          not stub.prompts, "issued %d calls" % len(stub.prompts))

    healthy = StubClient([answered("42")])
    healthy.device = FakeSafetyDevice(None)
    text, record = healthy.call("p", 0.7, 1, {"dataset": GSM_HARD})
    check("safety", "a device that has not tripped calls normally",
          len(healthy.prompts) == 1,
          "safety_tripped() returning None must not block an ordinary call")


def _fake_sysfs(root, zones, trip_points=None):
    """Build a thermal sysfs tree shaped like the kernel's.

    trip_points is optionally {zone_name: [(type, milli), ...]}, mirroring
    the kernel's own trip_point_N_type/trip_point_N_temp files alongside a
    zone's temp file, for exercising check_device.read_trip_points()
    without a real Jetson.
    """
    trip_points = trip_points or {}
    for i, (name, milli) in enumerate(zones):
        zone = root / ("thermal_zone%d" % i)
        zone.mkdir(parents=True)
        (zone / "type").write_text("%s\n" % name, encoding="utf-8")
        (zone / "temp").write_text("%d\n" % milli, encoding="utf-8")
        for n, (kind, trip_milli) in enumerate(trip_points.get(name, ())):
            (zone / ("trip_point_%d_type" % n)).write_text(
                "%s\n" % kind, encoding="utf-8")
            (zone / ("trip_point_%d_temp" % n)).write_text(
                "%d\n" % trip_milli, encoding="utf-8")
    return root


def test_trip_points():
    """The kernel's own thermal trip points, an independent backstop.

    jetson.ThermalWatchdog depends on this process being alive to protect
    the board. check_device.report_trip_points() exists to confirm,
    separately, whether a kernel-enforced 'critical' trip point exists at
    all, since that survives a dead process in a way nothing in this
    codebase does. Exercised against a synthetic sysfs tree, the same
    pattern test_jetson_sysfs already uses, since there is no real Jetson
    available in this environment either.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = _fake_sysfs(
            Path(tmp) / "with_trips", [("tj-therm", 46000), ("gpu-therm", 45000)],
            trip_points={
                "tj-therm": [("passive", 99000), ("critical", 105000)],
                "gpu-therm": [("hot", 95000)],
            })
        zones = jetson.discover_zones(root)
        points = check_device.read_trip_points(zones["tj-therm"])
        check("trips", "trip points are read in order with correct units",
              points == [("passive", 99.0), ("critical", 105.0)], str(points))
        check("trips", "a zone with no critical trip still reports what it has",
              check_device.read_trip_points(zones["gpu-therm"])
              == [("hot", 95.0)])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            found = check_device.report_trip_points(zones)
        check("trips", "report_trip_points finds the critical trip point",
              found is True)
        check("trips", "the critical trip is flagged in the printed report",
              "critical" in out.getvalue()
              and "kernel-enforced shutdown" in out.getvalue())

        no_trips_root = _fake_sysfs(
            Path(tmp) / "no_trips", [("tj-therm", 46000)])
        no_trip_zones = jetson.discover_zones(no_trips_root)
        check("trips", "a zone with no trip_point files reports none",
              check_device.read_trip_points(no_trip_zones["tj-therm"]) == [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            found = check_device.report_trip_points(no_trip_zones)
        check("trips", "report_trip_points correctly reports absence, not a crash",
              found is False and "No 'critical' trip point found" in out.getvalue())


def test_zone_naming_across_jetpack():
    """Zone role resolution must survive NVIDIA's JetPack 5 -> 6 rename.

    L4T r35 names the Tegra234 zones CPU-therm/SOC0-therm/tj-therm; L4T r36
    renames the same nine zones cpu-thermal/soc0-thermal/tj-thermal. An
    earlier version of this module matched the r35 spelling exactly, which
    found no SoC zone at all on JetPack 6 and made JetsonDevice refuse to
    construct on a healthy Orin NX. These are the real zone lists from
    NVIDIA's own device trees for both releases, so a future rename or a
    regression in zone_stem() fails here rather than on the bench.
    """
    r36 = [("cpu-thermal", 46000), ("gpu-thermal", -256000),
           ("cv0-thermal", -256000), ("cv1-thermal", -256000),
           ("cv2-thermal", -256000), ("soc0-thermal", 45000),
           ("soc1-thermal", 44000), ("soc2-thermal", 45500),
           ("tj-thermal", 47000)]
    r35 = [("CPU-therm", 46000), ("GPU-therm", 45000), ("CV0-therm", 44000),
           ("SOC0-therm", 45000), ("SOC1-therm", 44000), ("SOC2-therm", 45500),
           ("tj-therm", 47000)]
    legacy = [("CPU-therm", 46000), ("GPU-therm", 45000),
              ("thermal-fan-est", 44000), ("PMIC-Die", 40000)]

    check("zones", "both suffixes reduce to the same stem",
          jetson.zone_stem("SOC0-therm") == jetson.zone_stem("soc0-thermal")
          == "soc0", jetson.zone_stem("soc0-thermal"))
    check("zones", "a name with no known suffix keeps its whole self",
          jetson.zone_stem("thermal-fan-est") == "thermal-fan-est")

    with tempfile.TemporaryDirectory() as tmp:
        for label, zones, expect_soc in (
                ("JetPack 6 / L4T r36", r36, "tj-thermal"),
                ("JetPack 5 / L4T r35", r35, "tj-therm"),
                ("pre-Orin fan-est", legacy, "thermal-fan-est")):
            root = _fake_sysfs(Path(tmp) / label.replace("/", "_"), zones)
            device = jetson.JetsonDevice(root)
            soc_name = (device.soc.parent / "type").read_text(
                encoding="utf-8").strip()
            check("zones", "%s resolves an SoC zone" % label,
                  soc_name == expect_soc,
                  "picked %s, wanted %s" % (soc_name, expect_soc))
            check("zones", "%s resolves cpu and gpu too" % label,
                  device.cpu is not None and device.gpu is not None)

        # A board with zones but none that could gate must still refuse, and
        # the refusal has to name what it actually saw: the one time this
        # fired for real, the answer was sitting in that list.
        useless = _fake_sysfs(Path(tmp) / "useless", [("PMIC-Die", 40000)])
        raised = None
        try:
            jetson.JetsonDevice(useless)
        except jetson.ThermalUnavailable as exc:
            raised = str(exc)
        check("zones", "a board with no gateable zone still refuses",
              raised is not None)
        check("zones", "the refusal names the zones this board did report",
              raised is not None and "pmic-die" in raised.lower(), raised or "")


def test_jetson_sysfs():
    """JetsonDevice against a synthetic thermal tree.

    The gate policy is tested elsewhere against injected clocks. This is the
    other half: the sysfs reading, which has never run on a Jetson and would
    otherwise reach the campaign with no coverage at all. A synthetic tree
    cannot prove the zone names are the ones an Orin reports, but it does prove
    the discovery, the ordering and the unit conversion.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = _fake_sysfs(Path(tmp) / "full", [
            ("CPU-therm", 47250), ("GPU-therm", 45500), ("tj-therm", 46000),
            ("SOC0-therm", 45750), ("PMIC-Die", 40000)])
        zones = jetson.discover_zones(root)
        check("jetson", "every zone is discovered by the name it reports",
              set(zones) == {"cpu-therm", "gpu-therm", "tj-therm", "soc0-therm",
                             "pmic-die"}, str(sorted(zones)))

        device = jetson.JetsonDevice(root)
        check("jetson", "the junction sensor is preferred for the gate",
              device.soc.parent.name == "thermal_zone2")
        check("jetson", "cpu and gpu zones resolve independently",
              device.cpu.parent.name == "thermal_zone0"
              and device.gpu.parent.name == "thermal_zone1")

        state = device.read_state()
        check("jetson", "temperatures are converted from millidegrees",
              state["temp_c_soc"] == 46.0 and state["temp_c_cpu"] == 47.25,
              str(state))
        check("jetson", "an absent sensor records NaN, never a plausible zero",
              state["ambient_c"] != state["ambient_c"])
        check("jetson", "the gate reads the sensor it selected",
              device.read_soc_temp() == 46.0)

        partial = _fake_sysfs(Path(tmp) / "partial", [("SOC0-therm", 41000)])
        fallback = jetson.JetsonDevice(partial)
        check("jetson", "a device without a junction sensor falls back to SOC0",
              fallback.read_soc_temp() == 41.0)
        missing = fallback.read_state()
        check("jetson", "absent cpu and gpu zones are NaN, not zero",
              missing["temp_c_cpu"] != missing["temp_c_cpu"]
              and missing["temp_c_gpu"] != missing["temp_c_gpu"])

        useless = _fake_sysfs(Path(tmp) / "useless", [("PMIC-Die", 40000)])
        check("jetson", "a tree with zones but no SoC zone still refuses",
              _raises_type(jetson.ThermalUnavailable,
                           lambda: jetson.JetsonDevice(useless)))

        check("jetson", "a real device passes the entry point stub check",
              run_campaign.check_hardware(
                  client_module.Trigger(), device, client_module.EnergyMeter())
              == ["Trigger", "EnergyMeter"])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = check_device.main(["--root", str(root), "--sample", "0"])
        check("jetson", "the device check reports a usable device",
              status == 0 and "READY" in out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = check_device.main(
                ["--root", str(Path(tmp) / "nothing"), "--sample", "0"])
        check("jetson", "the device check refuses a machine with no zones",
              status == 1 and "REFUSED" in out.getvalue())


def _read_zone_roundtrip():
    """Write a sysfs-shaped millidegree file and read it back as Celsius."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "temp"
        path.write_text("45500\n", encoding="utf-8")
        return jetson.read_zone_c(path)


def test_block_settle():
    """The between-block settle, and that an interrupt cuts it short."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_items(root)
        run = runner.Runner(StubClient(), root, out_dir=root / "out",
                            run_id="selftest")

        started = time.monotonic()
        completed = run.settle(0.05)
        elapsed = time.monotonic() - started
        check("settle", "a settle runs to completion and reports it",
              completed is True and elapsed >= 0.05, "%.3fs" % elapsed)

        check("settle", "a zero settle is not an error", run.settle(0) is True)

        runner._STOP["requested"] = True
        try:
            started = time.monotonic()
            interrupted = run.settle(30.0)
            elapsed = time.monotonic() - started
        finally:
            runner._STOP["requested"] = False
        check("settle", "an interrupt cuts a settle short rather than waiting it out",
              interrupted is False and elapsed < 1.0, "%.3fs" % elapsed)
        check("settle", "polling is fine enough to honour an interrupt promptly",
              config.SETTLE_POLL_S <= 5.0)
        check("settle", "the campaign settles between blocks",
              "self.settle()" in Path(runner.__file__).read_text(encoding="utf-8"))


class StopAfter(StubClient):
    """A stub that raises the interrupt flag partway through, as SIGINT would."""

    def __init__(self, calls):
        super().__init__()
        self.budget = calls

    def _post(self, payload):
        self.budget -= 1
        if self.budget <= 0:
            runner._STOP["requested"] = True
        return super()._post(payload)


@contextlib.contextmanager
def campaign_config(n_items=1):
    """Temporarily complete the frozen parameters so a campaign can start.

    Runner.run() refuses while any REQUIRED_BEFORE_RUN value is None, which is
    the guard from Phase 1.1 and is not being bypassed here: the values are set,
    the run is a real one, and everything is restored afterwards. This is what
    run_campaign.py --dry would exercise once the campaign's own values are
    chosen, available now rather than after the hardware exists.
    """
    names = list(config.REQUIRED_BEFORE_RUN) + ["N_ITEMS", "BLOCK_SETTLE_S"]
    saved = {n: getattr(config, n) for n in names}
    placeholders = {
        "MODEL_FILE": "rehearsal.gguf", "MODEL_REVISION": "0" * 40,
        "MODEL_PATH": "models/rehearsal.gguf", "CTX_SIZE": 3072,
        "THERMAL_TARGET_C": 45.0, "NVPMODEL_MODE": 0,
        "TRIGGER_CHIP": "/dev/gpiochip0", "TRIGGER_LINE": 0,
        "PRICE_IN_PER_M": 0.0, "PRICE_OUT_PER_M": 0.0, "PRICE_SOURCE": "rehearsal",
        "N_ITEMS": n_items, "BLOCK_SETTLE_S": 0.0,
    }
    for name in names:
        setattr(config, name, placeholders.get(name, saved[name]))
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(config, name, value)


def _tasks_written(out):
    """Every (block, item, seed) recorded across a run directory.

    The block name is the leading field of the filename, not its stem. These
    files carry two suffixes, so Path.stem on block.tasks.csv leaves the
    ".tasks" attached and no two derived names ever compare equal.
    """
    rows = []
    for path in sorted(Path(out).glob("*.tasks.csv")):
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows.append((path.name.split(".")[0], row["item_id"], row["seed"]))
    return rows


def test_full_campaign_and_resume():
    """Every block start to finish, then an interrupt, then a resume.

    The single-block check cannot see the things that make a ten-day run
    survivable: that all twenty-four blocks execute, that an interrupt on day
    six costs the task in flight and nothing else, and that resuming redoes no
    work. Until now that path had never run beyond one block, and --dry cannot
    rehearse it while the frozen parameters are unset.
    """
    with tempfile.TemporaryDirectory() as tmp, campaign_config() as _:
        root = Path(tmp)
        _write_items(root, n=1)
        expected = len(config.blocks()) * len(config.SEEDS)

        out = root / "out"
        stopper = StopAfter(120)
        first = runner.Runner(stopper, root, out_dir=out, run_id="rehearsal")
        with contextlib.redirect_stderr(io.StringIO()):
            partial = first.run()
        runner._STOP["requested"] = False

        check("campaign", "an interrupt stops the run before it finishes",
              0 < partial < expected, "executed %d of %d" % (partial, expected))
        check("campaign", "the interrupt releases the writer",
              stopper.writer is None)

        resumed = runner.Runner(StubClient(), root, out_dir=out,
                                run_id="rehearsal")
        with contextlib.redirect_stderr(io.StringIO()):
            rest = resumed.run()

        check("campaign", "resuming finishes exactly the outstanding work",
              partial + rest == expected,
              "%d + %d != %d" % (partial, rest, expected))

        written = _tasks_written(out)
        check("campaign", "every block ran to completion across the two sessions",
              len(written) == expected, "%d task rows" % len(written))
        check("campaign", "resuming duplicated no task",
              len(set(written)) == len(written),
              "%d duplicates" % (len(written) - len(set(written))))
        check("campaign", "all %d blocks produced a table" % len(config.blocks()),
              len({b for b, _, _ in written}) == len(config.blocks()))

        blocks_on_disk = {p.name.split(".")[0] for p in out.glob("*.calls.csv")}
        check("campaign", "a call table accompanies every task table",
              blocks_on_disk == {b for b, _, _ in written})

        again = runner.Runner(StubClient(), root, out_dir=out, run_id="rehearsal")
        with contextlib.redirect_stderr(io.StringIO()):
            check("campaign", "a finished campaign resumes to zero work",
                  again.run() == 0)


class OverriddenTrigger(client_module.Trigger):
    """A Trigger that actually drives a line, as a real implementation would."""

    def high(self):
        return 1.0

    def low(self):
        return 2.0

    def status(self):
        return {"trigger_pulse_n": 1, "trigger_edge_us": 3.0, "faults": ()}

    def close(self):
        return None


class HalfTrigger(client_module.Trigger):
    """Drives the line but inherits status(), so every row reports pulse zero.

    The worst of the three shapes, because it is the one that looks healthy.
    Real pulses reach the ESP32 and real energy columns fill in, while the
    ordinal that joins the two stays at zero on every row and the external
    measurement can never be matched to the calls that produced it.
    """

    def high(self):
        return 1.0

    def low(self):
        return 2.0


class InheritedTrigger(client_module.Trigger):
    """A subclass that overrides nothing, which is the accident being guarded.

    Named because this is the shape that defeats a check on class identity: it
    is not Trigger and not NullTrigger, so a type test would admit it, and it
    would record zero joules against every call in the campaign.
    """


def test_entry_point_guards():
    """The gates in run_campaign.py, and the stub detector they rest on.

    Every hardware interface in this repository is still a no-op, and the two
    families are indistinguishable by class: NullTrigger, NullDevice and
    NullEnergyMeter are empty subclasses of stubs. The entry point therefore
    decides by method identity, and that decision is what these checks
    exercise, because it is the only thing standing between the study and
    twenty thousand rows of zero joules that look exactly like a measurement.
    """
    for label, instance, base in (
        ("Trigger", client_module.Trigger(), client_module.Trigger),
        ("Device", client_module.Device(), client_module.Device),
        ("EnergyMeter", client_module.EnergyMeter(), client_module.EnergyMeter),
        ("NullTrigger", client_module.NullTrigger(), client_module.Trigger),
        ("NullDevice", client_module.NullDevice(), client_module.Device),
        ("NullEnergyMeter", client_module.NullEnergyMeter(), client_module.EnergyMeter),
    ):
        check("entry point", "%s is recognised as a stub" % label,
              run_campaign.is_stub(instance, base))

    check("entry point", "a subclass that overrides nothing is still a stub",
          run_campaign.is_stub(InheritedTrigger(), client_module.Trigger),
          "a check on class identity would admit this and record zero joules")
    check("entry point", "a subclass that drives the line is not a stub",
          not run_campaign.is_stub(OverriddenTrigger(), client_module.Trigger),
          "the guard would refuse a real implementation and block the campaign")
    check("entry point", "a trigger that drives the line but inherits status is a stub",
          run_campaign.is_stub(HalfTrigger(), client_module.Trigger),
          "a half-implemented trigger emits real pulses and records pulse 0 on "
          "every row, so the external rig can never be joined to the calls")
    check("entry point", "close is lifecycle, not measurement",
          "close" not in sum(run_campaign.STUB_METHODS.values(), ()),
          "requiring close would flag a real implementation with nothing to "
          "release")

    stubs = run_campaign.check_hardware(
        client_module.Trigger(), client_module.Device(), client_module.EnergyMeter())
    check("entry point", "a live run names every interface still stubbed",
          stubs == ["Trigger", "Device", "EnergyMeter"], str(stubs))
    check("entry point", "a real device is not counted as a stub",
          run_campaign.check_hardware(client_module.Trigger(), FakeGatedDevice(),
                                      client_module.EnergyMeter())
          == ["Trigger", "EnergyMeter"])
    check("entry point", "a measurement run refuses off the Jetson",
          _raises_type(SystemExit, lambda: run_campaign.hardware(False)))
    dry = run_campaign.hardware(True)
    check("entry point", "--dry selects the Null implementations",
          [type(o).__name__ for o in dry]
          == ["NullTrigger", "NullDevice", "NullEnergyMeter"])

    run_id, out = run_campaign.resolve_run("", False)
    check("entry point", "a fresh run_id carries the current config hash",
          run_id.endswith(config.config_hash()))
    check("entry point", "a measurement writes to a run directory",
          out.name == run_id and out.parent.name == "raw")
    _, rehearsal = run_campaign.resolve_run(run_id, True)
    check("entry point", "a rehearsal cannot be pooled with a measurement",
          rehearsal.name == "rehearsal-%s" % run_id)
    check("entry point", "resuming a run from another config is refused",
          _raises_type(SystemExit,
                       lambda: run_campaign.resolve_run(
                           "20260101T000000Z-deadbeefdeadbeef", False)))
    check("entry point", "resuming a run from this config is allowed",
          run_campaign.resolve_run(run_id, False)[0] == run_id)


def _write_hwmon(root, channels):
    """A synthetic hwmon tree. channels is (index, label, mV, mA, uW or None)."""
    hwmon = root / "hwmon0"
    hwmon.mkdir(parents=True)
    for index, label, mv, ma, uw in channels:
        (hwmon / ("in%d_label" % index)).write_text(label)
        if mv is not None:
            (hwmon / ("in%d_input" % index)).write_text(str(mv))
            (hwmon / ("curr%d_input" % index)).write_text(str(ma))
        if uw is not None:
            (hwmon / ("power%d_input" % index)).write_text(str(uw))
    return root


def _series(values, step=1.0):
    """Timestamped single-rail samples, one per step second."""
    return [(i * step, v) for i, v in enumerate(values)]


class ExplodingRail(ina3221.Rail):
    """A rail whose sysfs node has stopped answering mid-campaign."""

    def __init__(self, key):
        super().__init__(key, key.upper(), power_path=Path("/nonexistent"))

    def watts(self):
        raise OSError("EIO")


class SteadyRail(ina3221.Rail):
    """A rail that reads a fixed wattage, so an integral has a known answer."""

    def __init__(self, key, value):
        super().__init__(key, key.upper(), power_path=Path("/nonexistent"))
        self.value = value

    def watts(self):
        return self.value


def _meter(rails, poll_s=0.001, settle=0.05):
    """A JetsonEnergyMeter over injected rails, with its sampler warmed up."""
    meter = jetson.JetsonEnergyMeter(rails=rails, poll_s=poll_s, span_s=10.0)
    time.sleep(settle)
    return meter


def test_gpio_abi():
    """The ioctl ABI, checked against the numbers the kernel headers produce.

    This is the one part of the hardware path that cannot be exercised against
    a synthetic tree: there is no fake /dev/gpiochip. What can be checked is
    that the structures are the size the kernel expects and that the request
    numbers derived from them match, because every field in the v2 ABI is
    fixed width with explicit padding and therefore identical on the machine
    this runs on and on the Jetson.

    Getting this wrong is not a crash. A structure one field short still
    marshals, and the kernel reads the trigger line's number out of whichever
    bytes land at that offset.
    """
    check("gpio", "every v2 structure matches the kernel ABI",
          not gpio.abi_mismatches(), "; ".join(gpio.abi_mismatches()))
    for name, (actual, expected) in sorted(gpio.ABI_REQUESTS.items()):
        check("gpio", "%s encodes to %#x" % (name, expected), actual == expected,
              "got %#x" % actual)
    check("gpio", "the request number is derived from the structure, not typed in",
          gpio._ioc(3, 0x0F, ctypes.sizeof(gpio._LineValues))
          == gpio.GPIO_V2_LINE_SET_VALUES_IOCTL)
    check("gpio", "a missing chip raises GpioError, not a bare OSError",
          _raises_type(gpio.GpioError,
                       lambda: gpio.OutputLine("/nonexistent/gpiochip9", 0)))
    check("gpio", "a trigger with no configured line refuses to construct",
          _raises_type(gpio.GpioError, jetson.JetsonTrigger))


def test_rail_discovery():
    """Rails found by label, and the unit path chosen for each.

    Channel order is a device-tree property, so the tree here deliberately
    puts VDD_IN last. A reader wired to channel 1 would pass every check while
    reporting the SoC rail in the VDD_IN column, which is a mislabelling no
    downstream analysis could detect.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = _write_hwmon(Path(tmp), [
            (1, "VDD_SOC", 5000, 400, None),
            (2, "VDD_CPU_GPU_CV", 5000, 1000, None),
            (3, "VDD_IN", 19000, 1500, None),
            (4, "PMIC_TEMP", None, None, None),
        ])
        rails = ina3221.discover_rails(root)
        check("rails", "every recorded rail is discovered",
              sorted(rails) == sorted(ina3221.RAIL_KEYS), str(sorted(rails)))
        check("rails", "rails resolve by label, not by channel index",
              rails["vdd_in"].label == "VDD_IN"
              and rails["soc"].label == "VDD_SOC",
              "vdd_in resolved to %s" % rails["vdd_in"].label)
        check("rails", "volts times current is preferred over the power node",
              all(r.source == "volt_x_current" for r in rails.values()))
        check("rails", "19000 mV at 1500 mA reads as 28.5 W",
              abs(rails["vdd_in"].watts() - 28.5) < 1e-9,
              "got %r" % rails["vdd_in"].watts())
        strays = [label for _, label in ina3221.unmatched_labels(root)]
        check("rails", "an unrecognised label is reported rather than dropped",
              strays == ["PMIC_TEMP"], str(strays))

    with tempfile.TemporaryDirectory() as tmp:
        root = _write_hwmon(Path(tmp), [(1, "VDD_IN", None, None, 28500000)])
        rails = ina3221.discover_rails(root)
        check("rails", "the power node is used when volts and current are absent",
              rails["vdd_in"].source == "power"
              and abs(rails["vdd_in"].watts() - 28.5) < 1e-9)

    with tempfile.TemporaryDirectory() as tmp:
        root = _write_hwmon(Path(tmp), [(1, "vdd_in_sys", 19000, 1000, None)])
        check("rails", "an alternate label spelling still resolves",
              "vdd_in" in ina3221.discover_rails(root))
        check("rails", "a meter refuses to construct with no rails at all",
              _raises_type(ina3221.RailsUnavailable,
                           lambda: jetson.JetsonEnergyMeter(rails={})))


def test_energy_integration():
    """The integrator, against series whose answers are known analytically.

    A trapezoid is exact for a linear ramp, so both cases below have a single
    right answer rather than a tolerance, and a rectangle-rule regression
    would fail the ramp by half a joule.
    """
    check("energy", "10 W held for 2 s is 20 J",
          ina3221.integrate(_series([10.0, 10.0, 10.0]), 1) == 20.0,
          "got %r" % ina3221.integrate(_series([10.0, 10.0, 10.0]), 1))
    check("energy", "a 0 to 10 W ramp over 2 s is 10 J",
          ina3221.integrate(_series([0.0, 5.0, 10.0]), 1) == 10.0,
          "got %r" % ina3221.integrate(_series([0.0, 5.0, 10.0]), 1))
    check("energy", "a single sample cannot be integrated",
          _isnan(ina3221.integrate(_series([10.0]), 1)))
    check("energy", "an empty window cannot be integrated",
          _isnan(ina3221.integrate([], 1)))
    check("energy", "one unreadable sample voids the window, not just itself",
          _isnan(ina3221.integrate(
              [(0.0, 10.0), (1.0, float("nan")), (2.0, 10.0)], 1)),
          "a partial integral is smaller than the truth by an unknown amount "
          "and looks exactly like a quiet call")
    check("energy", "mean power over a steady window is that power",
          ina3221.mean_watts(_series([7.0, 7.0, 7.0]), 1) == 7.0)
    check("energy", "samples 0.1 s apart report as 10 Hz, not as a sample count",
          ina3221.observed_rate_hz(_series([1.0] * 5, step=0.1)) == 10.0,
          "got %r" % ina3221.observed_rate_hz(_series([1.0] * 5, step=0.1)))


def test_meter_faults():
    """A failing instrument must not stop a run, and must not look like data.

    Each case below is a way the meter can fail that raises nothing. The
    check is always the same pair: the campaign survives, and the row it wrote
    cannot be mistaken for a real measurement.
    """
    meter = _meter({"vdd_in": SteadyRail("vdd_in", 10.0),
                    "cpu_gpu_cv": SteadyRail("cpu_gpu_cv", 4.0),
                    "soc": SteadyRail("soc", 2.0)})
    try:
        meter.start()
        time.sleep(0.05)
        reading = meter.stop()
    finally:
        meter.close()
    check("meter", "a healthy window reports no fault",
          reading["hw_status" if "hw_status" in reading else "faults"] == (),
          str(reading["faults"]))
    check("meter", "a steady 10 W rail integrates to roughly window times 10",
          abs(reading["energy_j_ina_vdd_in"]
              - 10.0 * reading["meter_window_s"]) < 1e-6)
    check("meter", "the sample count travels with the energy",
          reading["meter_samples_n"] > 2 and reading["meter_rate_hz"] > 0,
          str(reading["meter_samples_n"]))
    check("meter", "energy_j_external is never fabricated on the Jetson",
          _isnan(reading["energy_j_external"]),
          "the external rig is on the ESP32's bus and is joined afterwards")

    meter = _meter({"vdd_in": ExplodingRail("vdd_in"),
                    "cpu_gpu_cv": SteadyRail("cpu_gpu_cv", 4.0),
                    "soc": SteadyRail("soc", 2.0)})
    try:
        meter.start()
        time.sleep(0.05)
        reading = meter.stop()
        survived = True
    except Exception:
        reading, survived = {}, False
    finally:
        meter.close()
    check("meter", "a rail that stops answering does not kill the run", survived,
          "a ten-day campaign cannot end on one failed sysfs read")
    if survived:
        check("meter", "a failed rail records NaN, never zero",
              _isnan(reading["energy_j_ina_vdd_in"]),
              "got %r, which is indistinguishable from a rail drawing nothing"
              % reading["energy_j_ina_vdd_in"])
        check("meter", "a failed rail is named in hw_status",
              "meter_rail_unreadable" in reading["faults"],
              str(reading["faults"]))
        check("meter", "the rails that still work are still measured",
              not _isnan(reading["energy_j_ina_soc"]))

    meter = _meter({"vdd_in": SteadyRail("vdd_in", 10.0)})
    try:
        meter.start()
        time.sleep(0.05)
        reading = meter.stop()
    finally:
        meter.close()
    check("meter", "a rail this board lacks is NaN and flagged, not zero",
          _isnan(reading["energy_j_ina_soc"])
          and "meter_rail_missing" in reading["faults"],
          str(reading["faults"]))

    meter = _meter({"vdd_in": SteadyRail("vdd_in", 10.0)}, poll_s=5.0, settle=0.0)
    try:
        meter.start()
        reading = meter.stop()
    finally:
        meter.close()
    check("meter", "a window with too few samples is flagged, not averaged",
          "meter_no_samples" in reading["faults"]
          and _isnan(reading["energy_j_ina_vdd_in"]),
          str(reading["faults"]))

    meter = _meter({"vdd_in": SteadyRail("vdd_in", 10.0),
                    "cpu_gpu_cv": SteadyRail("cpu_gpu_cv", 4.0),
                    "soc": SteadyRail("soc", 2.0)})
    meter.sampler.close()
    meter.start()
    reading = meter.stop()
    check("meter", "a sampler thread that has died is visible on the row",
          "meter_thread_dead" in reading["faults"],
          "an exception inside the sampler kills the thread without reaching "
          "the caller, so the run continues collecting nothing and only this "
          "flag says so")


def test_sysfs_permissions():
    """A path this user may not traverse must not abort a call.

    The EMC clock lives under debugfs, which is root-only, and a campaign has
    no reason to run privileged. Path.exists() stats, and stat raises
    PermissionError rather than returning False, so probing for that file
    politely is what would end a ten-day run on its first call. Found by
    running the suite on a machine with a restricted /sys rather than by
    reasoning about it, which is why it is pinned here.
    """
    with tempfile.TemporaryDirectory() as tmp:
        walled = Path(tmp) / "walled"
        (walled / "thermal_zone0").mkdir(parents=True)
        (walled / "thermal_zone0" / "type").write_text("tj-therm")
        (walled / "thermal_zone0" / "temp").write_text("45000")
        forbidden = Path(tmp) / "forbidden"
        (forbidden / "inner").mkdir(parents=True)
        blocked = forbidden / "inner" / "rate"
        blocked.write_text("204000000")
        forbidden.chmod(0o000)
        try:
            reachable = not os.access(str(blocked), os.R_OK)
            saved = jetson.EMC_FALLBACKS
            jetson.EMC_FALLBACKS = (blocked,)
            try:
                faults = []
                values = jetson.read_frequencies(
                    faults, devfreq_root=Path(tmp) / "nothing",
                    cpu_root=Path(tmp) / "nothing")
                survived = True
            except OSError:
                values, faults, survived = (), [], False
            finally:
                jetson.EMC_FALLBACKS = saved
        finally:
            forbidden.chmod(0o755)

    if not reachable:
        warn("sysfs", "the forbidden-path check could not run", False,
             "this user can read a 000 directory, so it is root; the check "
             "runs unprivileged, which is how the campaign runs")
        return
    check("sysfs", "an unreadable clock path does not raise out of read_state",
          survived,
          "Path.exists() raises PermissionError on a directory this user "
          "cannot traverse, which would end the run on its first call")
    check("sysfs", "the unreadable clock is reported as a fault, not a value",
          values == (0, 0, 0) and "freq_unreadable" in faults, str(faults))
    check("sysfs", "rail discovery survives an untraversable hwmon root",
          ina3221.discover_rails(Path("/proc/1/root/nonexistent")) == {})


def test_hw_status_column():
    """The status vocabulary, and that it reaches the row a campaign writes."""
    check("hw_status", "tokens are sorted and pipe joined",
          records.hw_status(["meter_no_samples", "freq_unreadable"])
          == "freq_unreadable|meter_no_samples")
    check("hw_status", "duplicates collapse",
          records.hw_status(["freq_unreadable"] * 3) == "freq_unreadable")
    check("hw_status", "a clean call writes an empty cell",
          records.hw_status(()) == "")
    check("hw_status", "an unknown token raises rather than being written",
          _raises_type(ValueError, lambda: records.hw_status(["meter_borked"])),
          "a misspelled fault never appears in any count of itself")

    emitted = set()
    faults = []
    jetson.read_frequencies(faults, devfreq_root=Path("/nonexistent"),
                            cpu_root=Path("/nonexistent"))
    emitted.update(faults)
    device = _device_on_synthetic_tree()
    if device is not None:
        emitted.update(device.read_state()["faults"])
    check("hw_status", "every token the implementations emit is in the vocabulary",
          emitted <= records.HW_FAULTS, str(sorted(emitted - records.HW_FAULTS)))

    for name in ("trigger_pulse_n", "trigger_edge_us", "meter_samples_n",
                 "meter_rate_hz", "meter_window_s", "hw_status"):
        check("hw_status", "%s is a recorded column" % name, name in FIELDS)


def _device_on_synthetic_tree():
    """A JetsonDevice over a temporary thermal tree, or None if it cannot build."""
    tmp = tempfile.mkdtemp()
    zone = Path(tmp) / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "type").write_text("tj-therm")
    (zone / "temp").write_text("45000")
    try:
        return jetson.JetsonDevice(Path(tmp))
    except jetson.ThermalUnavailable:
        return None


def test_trigger_pulse_join():
    """The pulse ordinal, which is the only key joining a row to the rig.

    The ESP32 keeps its own clock, so timestamps cannot align the two streams.
    If this column does not increment once per call and reach the CSV, the
    external measurement is a stream of pulses that cannot be matched to
    anything, and no amount of later analysis recovers it.
    """
    class CountingTrigger(client_module.Trigger):
        def __init__(self):
            self.pulse_n = 0

        def high(self):
            self.pulse_n += 1
            return 1.0

        def status(self):
            return {"trigger_pulse_n": self.pulse_n,
                    "trigger_edge_us": 12.5, "faults": ()}

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "calls.csv"
        with RecordWriter(path, {"kind": "selftest"}) as writer:
            client = StubClient([answered("42")] * 6, writer=writer)
            client.trigger = CountingTrigger()
            for i in range(3):
                client.call_with_retries(
                    "p", 0.7, 101 + i,
                    {"dataset": GSM_HARD, "item_id": "i%d" % i},
                    runner.make_validator(GSM_HARD))
        rows = list(_csv_rows(path))
        check("trigger", "the pulse ordinal increments once per call",
              [r["trigger_pulse_n"] for r in rows] == ["1", "2", "3"],
              str([r["trigger_pulse_n"] for r in rows]))
        check("trigger", "the edge cost reaches the row",
              all(r["trigger_edge_us"] == "12.5" for r in rows))
        check("trigger", "a clean run leaves hw_status empty",
              all(r["hw_status"] == "" for r in rows))

    class FailingMeter(client_module.EnergyMeter):
        def stop(self):
            return dict(client_module.EnergyMeter.stop(self),
                        energy_j_ina_soc=float("nan"),
                        faults=("meter_rail_unreadable",))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "calls.csv"
        with RecordWriter(path, {"kind": "selftest"}) as writer:
            client = StubClient([answered("42")], writer=writer)
            client.meter = FailingMeter()
            client.call_with_retries("p", 0.7, 101, {"dataset": GSM_HARD},
                                     runner.make_validator(GSM_HARD))
        row = list(_csv_rows(path))[0]
        check("trigger", "a meter fault reaches hw_status on the row",
              row["hw_status"] == "meter_rail_unreadable", row["hw_status"])
        check("trigger", "the failed rail is nan in the file, not 0.0",
              row["energy_j_ina_soc"] == "nan", row["energy_j_ina_soc"])


def _csv_rows(path):
    import csv as _csv
    return list(_csv.DictReader(open(path, encoding="utf-8")))


def _isnan(value):
    return value != value


def test_runner_end_to_end():
    """One real block, stub model, real files. Covers what unit checks cannot.

    The writer lifetime, the task table, resume, and the fact that a block
    which is already finished does not leave a closed writer bound to the
    client for the next block to trip over.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_items(root)
        client = StubClient([answered("42")] * 4000)
        run = runner.Runner(client, root, out_dir=root / "out", run_id="selftest")
        dataset, condition, temperature = config.DATASETS[0], "baseline", 0.2

        executed = run.run_block(dataset, condition, temperature)
        check("runner", "a block runs every item against every seed",
              executed == 2 * len(config.SEEDS), "executed %d" % executed)
        check("runner", "the writer is released when the block ends",
              client.writer is None)

        tasks = list(csv.DictReader(
            open(run.out / ("%s.tasks.csv" % runner.block_name(
                dataset, condition, temperature)), encoding="utf-8")))
        check("runner", "one task row per item and seed", len(tasks) == executed)
        check("runner", "task header matches the schema",
              tuple(tasks[0]) == runner.TASK_FIELDS)
        check("runner", "grading reaches the task table",
              all(r["correct"] == "True" for r in tasks))
        check("runner", "gold shape travels with the task row",
              all(r["gold_shape"] == "ok" for r in tasks))

        again = run.run_block(dataset, condition, temperature)
        check("runner", "a finished block resumes to zero work", again == 0)
        check("runner", "a skipped block leaves no writer bound",
              client.writer is None)

        client.writer = None
        run.warm_up()
        check("runner", "warm-up calls are never written to disk",
              client.writer is None and len(client.prompts) > executed)

        blocks = runner.ordered_blocks()
        check("runner", "block order is a permutation of the %d blocks"
              % len(config.blocks()),
              sorted(blocks) == sorted(config.blocks()))
        check("runner", "block order is deterministic given ORDER_SEED",
              blocks == runner.ordered_blocks())
        check("runner", "block order is not the natural order",
              blocks != list(config.blocks()))


def test_items_and_config():
    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "items.json"
        items = [{"id": "a", "question": "q", "answer": "1"}]
        good.write_text(json.dumps({"sha256": ds.items_hash(items), "items": items}))
        check("items", "a matching hash loads", ds.load_items(good)["items"] == items)
        bad = Path(tmp) / "bad.json"
        bad.write_text(json.dumps({"sha256": "deadbeef", "items": items}))
        check("items", "a tampered item file is refused", _raises(ds.load_items, bad))

    check("config", "no quantisation", config.QUANTIZATION is None)
    check("config", "native precision", config.PRECISION == "BF16")
    check("config", "prompt caching off", config.CACHE_PROMPT is False)
    check("config", "thinking mode off", config.THINKING_MODE is False)
    check("config", "greedy sampling disabled", config.TOP_P == 1.0 and config.TOP_K == 0)
    check("config", "12 cells, 24 blocks",
          len(config.cells()) == 12 and len(config.blocks()) == 24)
    check("config", "validate blocks while parameters are unset",
          _raises_runtime(config.validate))

    # The dev server is launched by a shell script that does not read
    # LLAMA_FLAGS or CTX_SIZE, so a dry run can be measured at a context the
    # campaign will never use and nothing says so.
    serve = ROOT / "scripts" / "serve_dev.sh"
    if serve.exists():
        text = serve.read_text(encoding="utf-8")
        check("config", "dev server takes its context size from config",
              "config.CTX_SIZE" in text,
              "serve_dev.sh hard-codes a ctx; a dry run measured at a context "
              "the campaign will not use sizes nothing")
        check("config", "dev server launches from LLAMA_FLAGS",
              "config.LLAMA_FLAGS" in text,
              "flags restated in the shell script are frozen parameters that "
              "config_hash() never sees")

    flags = config.LLAMA_FLAGS
    pairs = [(flags[i], flags[i + 1] if i + 1 < len(flags) else None)
             for i in range(len(flags)) if flags[i].startswith("--")]
    boolish = [name for name, value in pairs if value in ("true", "false")]
    check("config", "no flag is passed a bare true/false", not boolish, str(boolish))
    warn("config", "no deprecated mmap flag",
         "--no-mmap" not in flags and "--mmap" not in flags,
         "llama.cpp warns --no-mmap is deprecated; confirm the replacement with "
         "llama-server --help | grep -i load-mode before changing it")

    test_context_budget()


CHARS_PER_TOKEN = 3.0


def _longest_task_tokens():
    """Longest real task in tokens, from whichever item files exist.

    Only the campaign's own datasets count. A rejected screening candidate
    with longer contexts would inflate the budget for prompts the campaign is
    never going to send.

    Conservative: 3 characters per token understates the tokeniser, so the
    budget below errs towards declaring a problem that is not there rather
    than missing one that is.
    """
    longest = 0
    for folder, key in (("items", None), ("screen", "task")):
        for path in sorted((ROOT / "data" / folder).glob("items_*.json")):
            if path.stem[len("items_"):] not in config.DATASETS:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for item in payload.get("items", []):
                if key:
                    text = item.get(key, "")
                elif item.get("context"):
                    text = ds.render_context_task(item["context"], item["question"])
                else:
                    text = item.get("question", "")
                longest = max(longest, len(text))
    return int(longest / CHARS_PER_TOKEN)


def test_context_budget():
    task = _longest_task_tokens()
    if not task:
        check("budget", "item files present to size the context budget", False,
              "no data/items or data/screen item files found")
        return

    system = int(max(len(chat.load(p.stem))
                     for p in chat.PROMPT_DIR.glob("*.txt")) / CHARS_PER_TOKEN)
    out = config.MAX_TOKENS
    worst = {
        "debate synthesiser": system + task + config.DEBATE_AGENTS * out,
        "solver_critic revision": system + task + 2 * out,
        "planner synthesiser": system + task + config.PLANNER_WORKER_SUBTASKS * out,
    }
    check("budget", "longest real task is about %d tokens" % task, True)
    for name, prompt in sorted(worst.items(), key=lambda kv: -kv[1]):
        total = prompt + out
        check("budget", "%s worst case %d tokens fits ctx %d"
              % (name, total, config.CTX_SIZE), total < config.CTX_SIZE,
              "" if total < config.CTX_SIZE
              else "raise CTX_SIZE or lower MAX_TOKENS")


def _raises_runtime(fn):
    try:
        fn()
    except RuntimeError:
        return True
    return False


def main():
    for fn in (test_extraction, test_grading, test_gold_shape, test_prompt_surface,
               test_seed_spacing, test_screen_campaign_agreement, test_topologies,
               test_payload_and_cache, test_debug_truncated_capture, test_band, test_serialisation,
               test_records, test_run_refuses_while_unvalidated,
               test_entry_point_guards, test_thermal_gate,
               test_thermal_safety_watchdog, test_thermal_emergency_stops_the_client,
               test_trip_points, test_zone_naming_across_jetpack,
               test_jetson_sysfs,
               test_block_settle, test_gpio_abi, test_rail_discovery,
               test_energy_integration, test_meter_faults, test_sysfs_permissions,
               test_hw_status_column, test_trigger_pulse_join,
               test_runner_end_to_end, test_full_campaign_and_resume,
               test_items_and_config):
        fn()

    section = None
    for name, label, status, detail in RESULTS:
        if name != section:
            section = name
            print("\n%s" % name.upper())
        line = "  %-4s %s" % (status, label)
        print(line if status == "ok" or not detail
              else "%s\n         %s" % (line, detail))

    failed = [r for r in RESULTS if r[2] == "FAIL"]
    warned = [r for r in RESULTS if r[2] == "WARN"]
    print("\nPROVENANCE")
    print("  config hash:  %s" % config.config_hash())
    print("  prompts hash: %s" % chat.prompts_hash())
    print("  datasets:     %s" % ", ".join(config.DATASETS))
    print("\n%d checks, %d failed, %d warnings"
          % (len(RESULTS), len(failed), len(warned)))
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
