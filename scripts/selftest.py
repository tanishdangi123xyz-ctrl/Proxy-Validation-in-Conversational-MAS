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

import csv
import json
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from masenergy import band, chat, config, runner, topologies
from masenergy import datasets as ds
from masenergy.client import LlamaClient, ServerError, _CALL_LOCK
from masenergy.records import CallRecord, FIELDS, RecordWriter, new_run_id

import prepare_datasets as prep
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


def test_runner_end_to_end():
    """One real block, stub model, real files. Covers what unit checks cannot.

    The writer lifetime, the task table, resume, and the fact that a block
    which is already finished does not leave a closed writer bound to the
    client for the next block to trip over.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "data" / "items").mkdir(parents=True)
        for name in config.DATASETS:
            items = [{"id": "%s-%05d" % (name, i), "rank": i,
                      "question": "q%d" % i, "answer": "42",
                      "context": "c%d" % i if name == HOTPOTQA else None,
                      "level": None}
                     for i in range(2)]
            (root / "data" / "items" / ("items_%s.json" % name)).write_text(
                json.dumps({"sha256": ds.items_hash(items), "items": items}))

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
               test_payload_and_cache, test_band, test_serialisation,
               test_records, test_runner_end_to_end, test_items_and_config):
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
