"""Frozen experiment parameters.

Every value here is fixed for the whole campaign. Changing one mid-run
invalidates cross-block comparability, which is why config_hash() is stamped
onto every call record.

Values not yet measured or chosen are None. validate() refuses to let a run
start while any remain unset.

Python 3.10 compatible, standard library only. Runs on the Jetson.
Rationale lives in the design analysis in the doc folder.
"""

import hashlib
import json
from itertools import product
from pathlib import Path

MODEL_REPO = "unsloth/Qwen3-1.7B-GGUF"
MODEL_SOURCE_REPO = "Qwen/Qwen3-1.7B"
MODEL_FILE = "Qwen3-1.7B-BF16.gguf"
MODEL_REVISION = "d7f544eead698dbd1f15126ef60b45a1e1933222"
MODEL_PATH = "models/Qwen3-1.7B-BF16.gguf"

PRECISION = "BF16"
QUANTIZATION = None
KV_CACHE_TYPE = "f16"
THINKING_MODE = False

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8080
SERVER_TIMEOUT_S = 600.0
CTX_SIZE = 3072

LLAMA_FLAGS = (
    "--n-gpu-layers", "999",
    "--parallel", "1",
    "--no-context-shift",
    "--no-mmap",
    "--no-cont-batching",
)

CACHE_PROMPT = False
WARMUP_CALLS = 5

CONDITIONS = ("baseline", "debate", "solver_critic", "planner_worker")
TEMPERATURES = (0.2, 0.7, 1.0)

MAX_TOKENS = 512
TOP_P = 1.0
TOP_K = 0

DEBATE_AGENTS = 2
DEBATE_ROUNDS = 2
DEBATE_AGGREGATION = "synthesis"
SOLVER_CRITIC_MAX_ITERS = 3
PLANNER_WORKER_SUBTASKS = 2

MAX_REPROMPTS = 2

THERMAL_TARGET_C = None
THERMAL_TOLERANCE_C = 1.0
THERMAL_TIMEOUT_S = 300.0
BLOCK_SETTLE_S = 300.0

IDLE_WINDOW_S = 10.0
IDLE_EVERY_N_CALLS = 20

NVPMODEL_MODE = None
FAN_PWM = 255

DATASETS = ("gsm_hard", "hotpotqa")
N_ITEMS = 80
SEEDS = (101, 202, 303)
ORDER_SEED = 20260820

PRICE_IN_PER_M = None
PRICE_OUT_PER_M = None
PRICE_SOURCE = None

REQUIRED_BEFORE_RUN = (
    "MODEL_FILE", "MODEL_REVISION", "MODEL_PATH",
    "CTX_SIZE",
    "THERMAL_TARGET_C",
    "NVPMODEL_MODE",
    "PRICE_IN_PER_M", "PRICE_OUT_PER_M", "PRICE_SOURCE",
)


def repo_root():
    """Repository root, so paths resolve identically on laptop and Jetson."""
    return Path(__file__).resolve().parent.parent.parent


def resolve_model_path():
    """Absolute path to the weights on whichever machine this is running on."""
    return repo_root() / MODEL_PATH


def cells():
    """The 12 factorial cells: (condition, temperature)."""
    return tuple(product(CONDITIONS, TEMPERATURES))


def blocks():
    """The 24 run blocks: (dataset, condition, temperature)."""
    return tuple(product(DATASETS, CONDITIONS, TEMPERATURES))


def calls_per_item():
    """Expected model calls per item, summed over all four conditions."""
    debate = DEBATE_AGENTS * DEBATE_ROUNDS + 1
    solver_critic = 2 * (SOLVER_CRITIC_MAX_ITERS + 1) / 2.0
    planner_worker = 1 + PLANNER_WORKER_SUBTASKS + 1
    return 1 + debate + solver_critic + planner_worker


def estimated_calls():
    """Total model calls across the whole campaign."""
    return int(
        N_ITEMS
        * len(TEMPERATURES)
        * calls_per_item()
        * len(SEEDS)
        * len(DATASETS)
    )


def validate():
    """Raise if any parameter that must be measured or chosen is unset.

    Also enforces the three settings most likely to be flipped during later
    debugging: native precision, prompt caching off, thinking mode off.
    """
    g = globals()
    missing = [n for n in REQUIRED_BEFORE_RUN if g.get(n) is None]
    if missing:
        raise RuntimeError(
            "Cannot start: unset parameters -> " + ", ".join(missing)
        )
    if QUANTIZATION is not None:
        raise RuntimeError("QUANTIZATION must stay None; this study is native precision")
    if CACHE_PROMPT:
        raise RuntimeError("CACHE_PROMPT must be False")
    if THINKING_MODE:
        raise RuntimeError("THINKING_MODE must be False")


def snapshot():
    """Return the full parameter dict and a short hash of it."""
    g = globals()
    params = {
        k: v for k, v in sorted(g.items())
        if k.isupper() and not k.startswith("_")
    }
    blob = json.dumps(params, sort_keys=True, default=str)
    return params, hashlib.sha256(blob.encode()).hexdigest()[:16]


def config_hash():
    """Short hash stamped onto every call record."""
    return snapshot()[1]


if __name__ == "__main__":
    print("cells (condition x temperature):", len(cells()))
    print("blocks (x dataset):            ", len(blocks()))
    print("calls per item (all conditions):", calls_per_item())
    print("estimated total calls:          ", estimated_calls())
    print("config hash:                    ", config_hash())
    try:
        validate()
        print("validate: OK")
    except RuntimeError as e:
        print("validate: BLOCKED ->", e)
