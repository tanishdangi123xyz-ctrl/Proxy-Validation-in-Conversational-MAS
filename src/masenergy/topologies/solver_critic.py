"""Asymmetric feedback loop between two fixed roles.

Call count genuinely varies between items, which is the point: this is the
retry mechanism the temperature-as-cause question depends on. It is not
smoothed or capped tighter than SOLVER_CRITIC_MAX_ITERS for tidiness.

What the solver is shown on revision is the critic's full response, not the
extracted verdict. Passing only ACCEPT or REJECT would make this a blind retry
loop wearing the costume of a feedback topology, and the energy it spent on
criticism would buy nothing.
"""

import re

from .. import chat, config

_VERDICT = re.compile(r"verdict\s*[:\-]\s*(accept|reject)", re.IGNORECASE)


def _verdict_validator(text):
    found = _VERDICT.findall(text or "")
    if not found:
        return False, None
    return True, found[-1].upper()


def run(client, task, temperature, seed, ctx, validator):
    records = []
    index = 0
    solver_system = chat.load("solver")
    critic_system = chat.load("critic")

    context = dict(ctx, topology="solver_critic", role="solver",
                   round_index=0, call_index_in_task=index)
    draft, answer, ok, recs = client.call_with_retries(
        chat.build(solver_system, task), temperature,
        chat.call_seed(seed, index), context, validator
    )
    records.extend(recs)
    index += 1

    for iteration in range(config.SOLVER_CRITIC_MAX_ITERS):
        critic_user = "Problem:\n%s\n\nSolver's attempt:\n%s" % (task, draft)
        context = dict(ctx, topology="solver_critic", role="critic",
                       round_index=iteration + 1, call_index_in_task=index)
        critique, verdict, _, recs = client.call_with_retries(
            chat.build(critic_system, critic_user), temperature,
            chat.call_seed(seed, index), context, _verdict_validator
        )
        records.extend(recs)
        index += 1

        if verdict == "ACCEPT" or iteration == config.SOLVER_CRITIC_MAX_ITERS - 1:
            break

        feedback = critique
        revise_user = ("%s\n\nYour previous attempt:\n%s\n\nCritic feedback:\n%s"
                       % (task, draft, feedback))
        context = dict(ctx, topology="solver_critic", role="solver",
                       round_index=iteration + 1, call_index_in_task=index)
        draft, answer, ok, recs = client.call_with_retries(
            chat.build(solver_system, revise_user), temperature,
            chat.call_seed(seed, index), context, validator
        )
        records.extend(recs)
        index += 1

    return {"answer": answer, "parse_ok": ok,
            "n_calls": len(records), "records": records}
