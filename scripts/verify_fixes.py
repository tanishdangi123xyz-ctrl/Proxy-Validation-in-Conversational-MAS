"""Regression check for the extraction and grading repair.

Every extraction case below is a string shape actually observed in the stage-1
screen, not an invented one. Every grading case fixes one end of the leniency
decision: what containment must accept, and what it must refuse.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masenergy import chat, config
from masenergy import datasets as ds

sys.path.insert(0, str(ROOT / "scripts"))

import prepare_datasets as prep
import screen_datasets as screen

GSM_HARD, HOTPOTQA = ds.GSM_HARD, ds.HOTPOTQA

HOTPOT_ROW = {
    "question": "  Which magazine was started first, Arthur's or First for Women?  ",
    "answer": "Arthur's Magazine",
    "level": "medium",
    "supporting_facts": {"title": ["Arthur's Magazine", "First for Women"]},
    "context": {
        "title": ["Arthur's Magazine", "Radio City", "First for Women"],
        "sentences": [
            ["Arthur's Magazine was an American periodical. ", "It was published in 1844."],
            ["Radio City is India's first private FM radio station."],
            ["First for Women is a womans magazine. ", "It was started in 1989."],
        ],
    },
}

GSM_HARD_ROW = {"input": "  Janet has 4219328.6 ducks.  ", "target": 8.0}

EXTRACTION = [
    ("echoed slot, answer on same line",
     HOTPOTQA, "reasoning\nAnswer: <your final answer> 49.6", "49.6"),
    ("echoed slot, answer on next line",
     HOTPOTQA, "reasoning\nAnswer: <your final answer>\n49.6", "49.6"),
    ("echoed slot, numeric branch",
     GSM_HARD, "reasoning\nAnswer: <your final answer> 49.6", "49.6"),
    ("display math unwrapped across delimiter lines",
     HOTPOTQA, "so we get\n$$\n\\frac{1}{2}\n$$", "\\frac{1}{2}"),
    ("delimiters with no content are a parse failure",
     HOTPOTQA, "$$\n$$", None),
    ("walk stops at the first content line",
     HOTPOTQA, "first line\nsecond line\n$$", "second line"),
    ("boxed latex answer",
     HOTPOTQA, "work\nAnswer: $\\boxed{\\frac{1}{2}}$", "\\frac{1}{2}"),
    ("inline math wrapper",
     HOTPOTQA, "work\nAnswer: $42$", "42"),
    ("bare answer survives untouched",
     HOTPOTQA, "work\nAnswer: Treaty of Amiens", "Treaty of Amiens"),
    ("dollar amount is not a wrapper",
     GSM_HARD, "work\nAnswer: $5", "5"),
    ("trailing period stripped",
     HOTPOTQA, "work\nAnswer: Marie Curie.", "Marie Curie"),
    ("last marker wins",
     HOTPOTQA, "Answer: wrong\nreconsidering\nAnswer: right", "right"),
    ("no marker, plain last line",
     HOTPOTQA, "some reasoning\nTreaty of Amiens", "Treaty of Amiens"),
    ("empty response is a parse failure",
     HOTPOTQA, "   ", None),
    ("slot with nothing after it is a parse failure",
     HOTPOTQA, "Answer: <your final answer>", None),
]

GRADING = [
    ("exact match", "Treaty of Amiens", "Treaty of Amiens", (), True),
    ("case and article insensitive", "the treaty of amiens", "Treaty of Amiens", (), True),
    ("short prose wrapper accepted",
     "The Treaty of Amiens happened first", "Treaty of Amiens", (), True),
    ("alias accepted", "NYC", "New York City", ("NYC",), True),
    ("long paragraph spanning gold refused",
     "There were many candidates under discussion that year and after weighing "
     "them all the Treaty of Amiens is the one that came first in the sequence",
     "Treaty of Amiens", (), False),
    ("wrong answer refused", "Treaty of Paris", "Treaty of Amiens", (), False),
    ("single token gold gets slack", "the year was 1996", "1996", (), True),
    ("gold not present refused", "something else entirely", "Treaty of Amiens", (), False),
]

_NUMBERED = re.compile(r"^\s*(\d)[\.\)]\s*(.+)$", re.MULTILINE)

PLAN_SAMPLE = (
    "1. Which treaty ended the war between Britain and France in 1802?\n"
    "2. In which year was the Treaty of Luneville signed?"
)


def main():
    failures = 0

    print("EXTRACTION")
    for label, dataset, text, expected in EXTRACTION:
        got = ds.extract_answer(dataset, text)
        ok = got == expected
        failures += not ok
        print("  %-4s %-44s %r" % ("ok" if ok else "FAIL", label, got))
        if not ok:
            print("       expected %r" % (expected,))

    print("\nGRADING")
    for label, predicted, gold, aliases, expected in GRADING:
        got = ds.span_correct(predicted, gold, aliases)
        ok = got == expected
        failures += not ok
        print("  %-4s %-44s %s" % ("ok" if ok else "FAIL", label, got))

    print("\nPLANNER PARSE")
    found = [b.strip() for _, b in _NUMBERED.findall(PLAN_SAMPLE)]
    ok = len(found) == config.PLANNER_WORKER_SUBTASKS
    failures += not ok
    print("  %-4s %d subtasks parsed from the new planner format"
          % ("ok" if ok else "FAIL", len(found)))

    print("\nPROMPT SURFACE")
    blocks = {}
    for name in ("baseline_solver", "debate_agent", "debate_synthesiser",
                 "planner_synthesiser", "solver", "worker"):
        text = chat.load(name)
        blocks[name] = text[text.index("Work through the problem"):]
    identical = len(set(blocks.values())) == 1
    failures += not identical
    print("  %-4s answer block identical across %d prompts"
          % ("ok" if identical else "FAIL", len(blocks)))

    leaked = [p.name for p in sorted(chat.PROMPT_DIR.glob("*.txt"))
              if "<" in p.read_text(encoding="utf-8")]
    failures += bool(leaked)
    print("  %-4s no template slots remain%s"
          % ("ok" if not leaked else "FAIL", "" if not leaked else " -> %s" % leaked))

    longest = max(len(chat.load(p.stem)) for p in chat.PROMPT_DIR.glob("*.txt"))
    print("  ..   longest system prompt: %d chars, roughly %d tokens"
          % (longest, longest // 4))

    print("\nSCREEN AND CAMPAIGN AGREE")
    screened = screen._build_hotpotqa(HOTPOT_ROW, 0)["task"]
    campaign = ds.build_task_text(HOTPOTQA, prep.build_hotpotqa(HOTPOT_ROW, 0, 0))
    ok = screened == campaign
    failures += not ok
    print("  %-4s hotpotqa renders to the same prompt in both paths"
          % ("ok" if ok else "FAIL"))
    if not ok:
        print("       screen  : %r" % screened[:120])
        print("       campaign: %r" % campaign[:120])

    screened = screen._build_gsm_hard(GSM_HARD_ROW, 0)
    campaign_item = prep.build_gsm_hard(GSM_HARD_ROW, 0, 0)
    ok = screened["task"] == ds.build_task_text(GSM_HARD, campaign_item)
    failures += not ok
    print("  %-4s gsm_hard renders to the same prompt in both paths"
          % ("ok" if ok else "FAIL"))

    ok = screened["answer"] == campaign_item["answer"] == "8"
    failures += not ok
    print("  %-4s gsm_hard gold formats identically (%r, not '8.0')"
          % ("ok" if ok else "FAIL", campaign_item["answer"]))

    ok = "gsm-hard" in prep.GSM_HARD_REPOS[0] and prep.GSM_HARD_SPLIT == "train"
    failures += not ok
    print("  %-4s campaign preparer targets %s split %s"
          % ("ok" if ok else "FAIL", prep.GSM_HARD_REPOS[0], prep.GSM_HARD_SPLIT))

    screen_names = {c["name"] for c in screen.CANDIDATES}
    ok = set(config.DATASETS) <= screen_names
    failures += not ok
    print("  %-4s every campaign dataset %s was actually screened"
          % ("ok" if ok else "FAIL", config.DATASETS))

    modes = {c["extract_as"] for c in screen.CANDIDATES if c["name"] in config.DATASETS}
    ok = modes == set(config.DATASETS)
    failures += not ok
    print("  %-4s screen grades the survivors under their own mode, not a stand-in"
          % ("ok" if ok else "FAIL"))

    print("\nPROVENANCE")
    print("  config hash:  %s" % config.config_hash())
    print("  prompts hash: %s" % chat.prompts_hash())

    print("\n%s" % ("all checks passed" if not failures else "%d CHECKS FAILED" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
