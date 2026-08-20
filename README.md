# Conversational MAS Energy Measurement

Direct hardware energy measurement on conversational multi-agent systems built
from small language models, checking whether token count, wall-clock time, and
monetary cost track measured energy.

Design rationale lives in `doc/`. This README covers only structure and setup.

## What runs where

| Component | Host | Why |
|---|---|---|
| llama.cpp server | Jetson Orin NX 8GB | Inference under measurement |
| Orchestrator (`src/masenergy`) | Jetson | Drives the GPIO trigger; must be local |
| ESP32 sampler firmware | ESP32 | Reads INA226, watches trigger |
| Serial capture (`host/`) | Laptop | ESP32 is powered by and logs to the laptop |
| Analysis (`analysis/`) | Laptop | Offline |

Develop on the laptop, deploy `src/` to the Jetson.

## Layout

```
doc/                    Design analysis, INA226 guide, electronics build brief
src/masenergy/
  config.py             Frozen experiment parameters
  client.py             call() - the single choke point: GPIO + HTTP + log
  topologies/           baseline, debate, solver_critic, planner_worker
  prompts/              Role prompts, one file each
  datasets.py           GSM8K / HotpotQA loading, answer extraction, grading
  runner.py             Driver: configs x items x seeds, randomised order
  records.py            Append-only per-call log
firmware/esp32/         Sampler firmware
host/capture.py         Laptop-side ESP32 serial capture
scripts/
  jetson_prepare.sh     nvpmodel, jetson_clocks, fan pin, timer masking
  serve.sh              llama.cpp launch with pinned flags
analysis/               Regressions and figures
data/raw/               Run outputs (gitignored)
```

## Frozen parameters

Anything here is fixed for the whole campaign. Changing one invalidates
cross-block comparability. See `doc/MAS_Jetson_Design_Analysis.md`.

- Precision: native BF16/FP16. **No quantization anywhere.**
- Prompt caching: off (`cache_prompt: false`)
- Concurrency: strictly one call in flight, ever
- Constrained decoding: none — retries are data
- Context size: one fixed value for all 12 configs
- Power mode: fixed `nvpmodel`, `jetson_clocks` locked, fan at fixed PWM

## Setup

TBD - see steps below as they are completed.

- [x] 1. Project skeleton
- [ ] 2. Python environment
- [ ] 3. Config module
- [ ] 4. llama.cpp on the Jetson
- [ ] 5. Model download and throughput calibration
- [ ] 6. call() and the record schema
- [ ] 7. Topologies
- [ ] 8. Datasets and grading
- [ ] 9. Runner
- [ ] 10. Thermal gate
- [ ] 11. GPIO trigger
- [ ] 12. Host capture
