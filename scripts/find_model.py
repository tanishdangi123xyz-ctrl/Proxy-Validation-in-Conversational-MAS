"""List native-precision GGUF files available for the configured model.

We need BF16 or F16, never a Q* file. GGUF is a container, not a quantization
format, so a native-precision GGUF is exactly the unquantized weights.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masenergy import config
from huggingface_hub import HfApi

CANDIDATE_REPOS = [
    config.MODEL_REPO + "-GGUF",
    "unsloth/" + config.MODEL_REPO.split("/")[-1] + "-GGUF",
    "bartowski/" + config.MODEL_REPO.split("/")[-1] + "-GGUF",
]

NATIVE = ("bf16", "f16", "fp16")


def main():
    api = HfApi()
    print("Looking for native-precision GGUF for %s\n" % config.MODEL_REPO)
    found = False
    for repo in CANDIDATE_REPOS:
        try:
            info = api.repo_info(repo, files_metadata=True)
        except Exception as exc:
            print("  %-46s unavailable" % repo)
            continue
        native = [
            f for f in info.siblings
            if f.rfilename.lower().endswith(".gguf")
            and any(tag in f.rfilename.lower() for tag in NATIVE)
        ]
        print("  %s" % repo)
        if not native:
            names = [f.rfilename for f in info.siblings if f.rfilename.endswith(".gguf")]
            print("      no native precision file. present: %s"
                  % (", ".join(sorted(names)[:6]) or "none"))
        for f in native:
            size = (f.size or 0) / 1e9
            print("      %-44s %5.2f GB" % (f.rfilename, size))
            found = True
        print()

    print("Also verifying the source repo exists for conversion fallback:")
    try:
        api.repo_info(config.MODEL_REPO)
        print("  %s  OK  (convert with llama.cpp convert_hf_to_gguf.py --outtype bf16)"
              % config.MODEL_REPO)
    except Exception as exc:
        print("  %s  unavailable: %s" % (config.MODEL_REPO, exc))

    if not found:
        print("\nNo prebuilt native GGUF found. Use the conversion route.")


if __name__ == "__main__":
    main()
