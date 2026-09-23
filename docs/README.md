# Documentation map

The repository root is kept for the app entry points and the documents people
most often need. Supporting guides and reference material are grouped here.

## Find a document

| If you need… | Go to… |
|---|---|
| Setup, operating, and feature guides | [`guides/`](guides/) |
| Thunder measurements and inference-stack notes | [`guides/THUNDER_COMPUTE.md`](guides/THUNDER_COMPUTE.md), [`operations/`](operations/) |
| Model and adapter evaluation results | [`results/`](results/), [`../RECIPES.md`](../RECIPES.md), [`../RESULTS_INDEX.md`](../RESULTS_INDEX.md) |
| Voice adapter source provenance | [`../lora_models/provenance.csv`](../lora_models/provenance.csv) |
| Project overview and serving instructions | [`wiki/`](wiki/Home.md) |
| Historical handoffs and dated investigations | [`history/`](history/README.md) |
| Audits, benchmark records, and screenshots | [`audits/`](audits/), [`benchmarks/`](benchmarks/raw/README.md), [`screenshots/`](screenshots/) |
| Voice-feature benchmark utility | [`tools/experiments/voice_feature_benchmark.py`](../tools/experiments/voice_feature_benchmark.py) |

## Root documents

- [`../README.md`](../README.md) is the app and project starting point; [`../README_CN.md`](../README_CN.md) is its Chinese edition.
- [`../GOALS.md`](../GOALS.md) records project goals and evidence; [`../RECIPES.md`](../RECIPES.md) records tested settings and results.
- [`../RESULTS_INDEX.md`](../RESULTS_INDEX.md) indexes experiment artifacts; [`../HF_MODEL_GUIDE.md`](../HF_MODEL_GUIDE.md) explains the public model releases.
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) covers contributions. `CLAUDE.md` contains repository-agent workflow rules.

Pinokio launcher files remain at the root because Pinokio expects them there.
