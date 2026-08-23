"""Symmetric peers, each round conditioned on the other agents' prior answers.

Round one is parallel in its conditioning only: no agent sees another's
answer. Execution is strictly serial, because two overlapping calls cannot be
separated by a single trigger line.

Call count is fixed by DEBATE_AGENTS and DEBATE_ROUNDS, never by a stopping
condition, so the structure stays comparable across items.

Every call is stateless, so an agent's own previous answer has to be replayed
into its prompt or it does not have one. Shown only its peer's answer and told
to reconsider, an agent has nothing to reconsider against and adopts what it
was shown; two agents then swap answers each round, which looks like vigorous
disagreement in the change-rate check while no deliberation is happening.
"""

from .. import chat, config


def run(client, task, temperature, seed, ctx, validator):
    records = []
    index = 0
    system = chat.load("debate_agent")
    texts = [""] * config.DEBATE_AGENTS

    for round_no in range(config.DEBATE_ROUNDS):
        revised = []
        for agent in range(config.DEBATE_AGENTS):
            if round_no == 0:
                user = task
            else:
                peers = "\n\n".join(
                    "Solver %d answered:\n%s" % (j + 1, texts[j])
                    for j in range(config.DEBATE_AGENTS) if j != agent
                )
                if config.DEBATE_SHOWS_OWN_PRIOR:
                    user = ("%s\n\nYour own previous answer:\n%s\n\n%s\n\n"
                            "Reconsider your own answer in light of the above, "
                            "then give your answer."
                            % (task, texts[agent], peers))
                else:
                    user = ("%s\n\n%s\n\nReconsider your own answer in light of "
                            "the above, then give your answer." % (task, peers))

            context = dict(ctx, topology="debate", role="agent_%d" % (agent + 1),
                           round_index=round_no + 1, call_index_in_task=index)
            text, _, _, recs = client.call_with_retries(
                chat.build(system, user), temperature,
                chat.call_seed(seed, index), context, validator
            )
            records.extend(recs)
            revised.append(text)
            index += 1
        texts = revised

    summary = "\n\n".join(
        "Solver %d final answer:\n%s" % (i + 1, t) for i, t in enumerate(texts)
    )
    context = dict(ctx, topology="debate", role="synthesiser",
                   round_index=config.DEBATE_ROUNDS + 1,
                   call_index_in_task=index)
    _, answer, ok, recs = client.call_with_retries(
        chat.build(chat.load("debate_synthesiser"), "%s\n\n%s" % (task, summary)),
        temperature, chat.call_seed(seed, index), context, validator
    )
    records.extend(recs)

    return {"answer": answer, "parse_ok": ok,
            "n_calls": len(records), "records": records}
