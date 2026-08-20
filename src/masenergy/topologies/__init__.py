"""Topology registry."""

from . import baseline, debate, planner_worker, solver_critic

REGISTRY = {
    "baseline": baseline.run,
    "debate": debate.run,
    "solver_critic": solver_critic.run,
    "planner_worker": planner_worker.run,
}


def get(name):
    """Return the run function for a named topology."""
    if name not in REGISTRY:
        raise KeyError("Unknown topology: %s" % name)
    return REGISTRY[name]
