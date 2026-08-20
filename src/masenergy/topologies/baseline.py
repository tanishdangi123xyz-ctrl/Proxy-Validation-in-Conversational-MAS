"""Single call, no coordination.

The zero-coordination reference every topology is measured against. It is not
a topology in the same sense as the other three.
"""

from .. import chat


def run(client, task, temperature, seed, ctx, validator):
    prompt = chat.build(chat.load("baseline_solver"), task)
    context = dict(ctx, topology="baseline", role="solver",
                   round_index=0, call_index_in_task=0)
    _, answer, ok, records = client.call_with_retries(
        prompt, temperature, chat.call_seed(seed, 0), context, validator
    )
    return {"answer": answer, "parse_ok": ok,
            "n_calls": len(records), "records": records}
