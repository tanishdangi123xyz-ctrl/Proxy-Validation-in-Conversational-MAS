"""Hierarchical delegation. Workers never see each other, only the planner.

Subtask count is fixed rather than planner-chosen, so call count stays
comparable across items. If the planner fails to emit a usable plan after its
retries, workers are dispatched on the raw task: the structure and call count
are preserved and the failure is visible in the planner's parse_ok, rather
than the item silently costing fewer calls than its peers.

The planner is told to write self-contained subtasks and a 1.7B model does not
comply: it writes "How much did Mishka spend on the shorts?" and keeps the
prices to itself. A worker handed that alone cannot answer, burns every retry,
and the item costs twice the calls of its neighbours while producing nothing.
The problem statement therefore travels with the subtask as background.
Workers still never see each other, which is what this topology is about.
"""

import re

from .. import chat, config

_NUMBERED = re.compile(r"^\s*(\d)[\.\)]\s*(.+)$", re.MULTILINE)


def _plan_validator(text):
    found = _NUMBERED.findall(text or "")
    subtasks = [body.strip() for _, body in found if body.strip()]
    if len(subtasks) < config.PLANNER_WORKER_SUBTASKS:
        return False, None
    return True, subtasks[:config.PLANNER_WORKER_SUBTASKS]


def run(client, task, temperature, seed, ctx, validator):
    records = []
    index = 0

    context = dict(ctx, topology="planner_worker", role="planner",
                   round_index=0, call_index_in_task=index)
    _, subtasks, plan_ok, recs = client.call_with_retries(
        chat.build(chat.load("planner"), task), temperature,
        chat.call_seed(seed, index), context, _plan_validator
    )
    records.extend(recs)
    index += 1

    if not plan_ok or not subtasks:
        subtasks = [task] * config.PLANNER_WORKER_SUBTASKS

    worker_system = chat.load("worker")
    results = []
    for n, subtask in enumerate(subtasks):
        context = dict(ctx, topology="planner_worker", role="worker_%d" % (n + 1),
                       round_index=1, call_index_in_task=index)
        if config.PLANNER_WORKER_SHOWS_TASK:
            worker_user = ("Background, for reference only:\n%s\n\n"
                           "Your subtask:\n%s" % (task, subtask))
        else:
            worker_user = subtask
        text, _, _, recs = client.call_with_retries(
            chat.build(worker_system, worker_user), temperature,
            chat.call_seed(seed, index), context, validator
        )
        records.extend(recs)
        results.append("Subtask %d: %s\nResult:\n%s" % (n + 1, subtask, text))
        index += 1

    synth_user = "%s\n\n%s" % (task, "\n\n".join(results))
    context = dict(ctx, topology="planner_worker", role="synthesiser",
                   round_index=2, call_index_in_task=index)
    _, answer, ok, recs = client.call_with_retries(
        chat.build(chat.load("planner_synthesiser"), synth_user), temperature,
        chat.call_seed(seed, index), context, validator
    )
    records.extend(recs)

    return {"answer": answer, "parse_ok": ok, "plan_ok": plan_ok,
            "n_calls": len(records), "records": records}
