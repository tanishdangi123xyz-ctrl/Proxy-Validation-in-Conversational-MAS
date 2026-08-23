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

from masenergy import chat, config, runner, topologies
from masenergy import datasets as ds
from masenergy.client import LlamaClient, _CALL_LOCK
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
        meta = json.loads(path.with_suffix(".meta.json").read_text())
        check("records", "metadata sidecar records the schema",
              meta.get("schema_fields") == list(FIELDS))

        with RecordWriter(path, {"kind": "selftest"}) as writer:
            writer.write(CallRecord(run_id="second"))
        rows = list(_csv.DictReader(open(path, encoding="utf-8")))
        check("records", "reopening appends rather than truncating", len(rows) == 2)

        check("records", "run id carries the config hash",
              new_run_id(config.config_hash()).endswith(config.config_hash()))


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
    for fn in (test_extraction, test_grading, test_prompt_surface, test_seed_spacing,
               test_screen_campaign_agreement, test_topologies, test_serialisation,
               test_records, test_items_and_config):
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
