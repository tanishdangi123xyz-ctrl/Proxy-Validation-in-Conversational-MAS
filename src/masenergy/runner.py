"""Campaign driver.

Iterates blocks in an order randomised from ORDER_SEED, so thermal drift and
any slow degradation cannot align with temperature or condition.

Resumable by construction. A ten-day run will be interrupted at least once,
and a crash on day six must not cost six days: completed tasks are recovered
from the task tables already on disk, and only the remainder is executed.

Writes three tables per block. Call-level rows are the unit of analysis;
task-level rows carry grading and let energy be summed per task; idle rows
carry the baseline that gets subtracted.

Standard library only, Python 3.10 compatible.
"""

import csv
import json
import random
import signal
import sys
import time
from pathlib import Path

from . import chat, config, topologies
from . import datasets as ds
from .records import RecordWriter, new_run_id, utc_now

TASK_FIELDS = (
    "run_id", "config_hash", "timestamp_utc",
    "dataset", "item_id", "topology", "temperature", "seed", "repetition",
    "n_calls", "answer_extracted", "gold",
    "parse_ok", "correct", "f1", "plan_ok", "wall_s",
)

IDLE_FIELDS = (
    "run_id", "timestamp_utc", "dataset", "topology", "temperature",
    "calls_elapsed", "idle_w_external", "idle_w_ina_vdd_in", "duration_s",
)

_STOP = {"requested": False}


def _handle_sigint(signum, frame):
    if _STOP["requested"]:
        sys.stderr.write("\nSecond interrupt, exiting now.\n")
        sys.exit(1)
    _STOP["requested"] = True
    sys.stderr.write("\nInterrupt received. Finishing current task, then stopping.\n")


def block_name(dataset, condition, temperature):
    return "%s__%s__t%s" % (dataset, condition, str(temperature).replace(".", "p"))


def _append(path, fields, row):
    exists = path.exists() and path.stat().st_size > 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(row)
        fh.flush()


def completed_tasks(path):
    """Recover (item_id, seed) pairs already finished in this block."""
    if not path.exists():
        return set()
    done = set()
    with open(path, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            done.add((row["item_id"], int(row["seed"])))
    return done


def make_validator(dataset):
    """Answer extraction as a validator, identical for every topology."""
    def validator(text):
        answer = ds.extract_answer(dataset, text)
        return answer is not None, answer
    return validator


def load_all_items(root):
    items = {}
    for name in config.DATASETS:
        payload = ds.load_items(Path(root) / "data" / "items" / ("items_%s.json" % name))
        items[name] = payload
    return items


def ordered_blocks():
    """Blocks in randomised order, deterministic given ORDER_SEED."""
    blocks = list(config.blocks())
    random.Random(config.ORDER_SEED).shuffle(blocks)
    return blocks


class Runner:
    """Executes the campaign, one block at a time."""

    def __init__(self, client, root, out_dir=None, run_id=None):
        self.client = client
        self.root = Path(root)
        self.run_id = run_id or new_run_id(config.config_hash())
        self.out = Path(out_dir) if out_dir else self.root / "data" / "raw" / self.run_id
        self.out.mkdir(parents=True, exist_ok=True)
        self.items = load_all_items(self.root)
        self.calls_since_idle = 0
        self.durations = []

    def warm_up(self):
        """Discarded calls after a server start, never written to disk."""
        prompt = chat.build(chat.load("baseline_solver"), "What is two plus two?")
        for i in range(config.WARMUP_CALLS):
            self.client.call(prompt, config.TEMPERATURES[0],
                             chat.call_seed(0, i), {"role": "warmup"})

    def _maybe_idle(self, dataset, condition, temperature, path):
        if config.IDLE_EVERY_N_CALLS <= 0:
            return
        if self.calls_since_idle < config.IDLE_EVERY_N_CALLS:
            return
        reading = self.client.meter.measure_idle(config.IDLE_WINDOW_S)
        _append(path, IDLE_FIELDS, {
            "run_id": self.run_id, "timestamp_utc": utc_now(),
            "dataset": dataset, "topology": condition, "temperature": temperature,
            "calls_elapsed": self.calls_since_idle,
            "idle_w_external": reading.get("idle_w_external", 0.0),
            "idle_w_ina_vdd_in": reading.get("idle_w_ina_vdd_in", 0.0),
            "duration_s": reading.get("duration_s", 0.0),
        })
        self.calls_since_idle = 0

    def run_block(self, dataset, condition, temperature):
        name = block_name(dataset, condition, temperature)
        calls_path = self.out / ("%s.calls.csv" % name)
        tasks_path = self.out / ("%s.tasks.csv" % name)
        idle_path = self.out / ("%s.idle.csv" % name)

        payload = self.items[dataset]
        items = payload["items"]
        done = completed_tasks(tasks_path)
        planned = [(it, s) for it in items for s in config.SEEDS]
        remaining = [(it, s) for it, s in planned if (it["id"], s) not in done]

        sys.stderr.write("%-34s %3d/%3d remaining\n"
                         % (name, len(remaining), len(planned)))
        if not remaining:
            return 0

        meta = {
            "run_id": self.run_id,
            "config_hash": config.config_hash(),
            "config": config.snapshot()[0],
            "prompts_hash": chat.prompts_hash(),
            "dataset": dataset,
            "items_sha256": payload["sha256"],
            "condition": condition,
            "temperature": temperature,
        }
        validator = make_validator(dataset)
        executed = 0

        with RecordWriter(calls_path, meta) as writer:
            self.client.writer = writer
            for item, seed in remaining:
                if _STOP["requested"]:
                    break
                started = time.monotonic()
                ctx = {"dataset": dataset, "item_id": item["id"],
                       "repetition": config.SEEDS.index(seed)}
                task = ds.build_task_text(dataset, item)

                result = topologies.get(condition)(
                    self.client, task, temperature, seed, ctx, validator
                )
                grade = ds.grade(dataset, result["answer"], item["answer"])
                elapsed = time.monotonic() - started

                _append(tasks_path, TASK_FIELDS, {
                    "run_id": self.run_id,
                    "config_hash": config.config_hash(),
                    "timestamp_utc": utc_now(),
                    "dataset": dataset, "item_id": item["id"],
                    "topology": condition, "temperature": temperature,
                    "seed": seed, "repetition": ctx["repetition"],
                    "n_calls": result["n_calls"],
                    "answer_extracted": result["answer"] or "",
                    "gold": item["answer"],
                    "parse_ok": grade["parse_ok"],
                    "correct": grade["correct"],
                    "f1": grade["f1"],
                    "plan_ok": result.get("plan_ok", ""),
                    "wall_s": round(elapsed, 3),
                })

                self.calls_since_idle += result["n_calls"]
                self.durations.append(elapsed)
                executed += 1
                self._maybe_idle(dataset, condition, temperature, idle_path)

        return executed

    def eta(self, tasks_left):
        if not self.durations:
            return "unknown"
        mean = sum(self.durations[-50:]) / len(self.durations[-50:])
        hours = mean * tasks_left / 3600.0
        return "%.1f h" % hours

    def run(self):
        signal.signal(signal.SIGINT, _handle_sigint)
        blocks = ordered_blocks()
        per_block = config.N_ITEMS * len(config.SEEDS)
        total = len(blocks) * per_block

        sys.stderr.write("run_id %s\n%d blocks, %d tasks\n\n"
                         % (self.run_id, len(blocks), total))
        self.warm_up()

        completed = 0
        for i, (dataset, condition, temperature) in enumerate(blocks, 1):
            if _STOP["requested"]:
                break
            completed += self.run_block(dataset, condition, temperature)
            left = total - (i * per_block)
            sys.stderr.write("  block %d/%d done, eta %s\n"
                             % (i, len(blocks), self.eta(max(left, 0))))

        sys.stderr.write("\n%d tasks executed this session.\n" % completed)
        return completed
