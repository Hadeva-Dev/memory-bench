# Third-party notices

memory-bench's own code is under the Apache License 2.0 (`LICENSE`, `NOTICE`) and its data under CC BY 4.0
(`LICENSE-DATA`). This file lists the material from other projects that the repository carries or fetches, with its
source and license.

## LoCoMo

- Source: the LoCoMo dataset and evaluation code (Maharana et al., ACL 2024),
  [snap-research/locomo](https://github.com/snap-research/locomo) at commit
  `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376` (`LOCOMO_COMMIT` in `harness/external/fetch.py`).
- License: CC BY-NC 4.0 (the repository's `LICENSE.txt`): attribution required, non-commercial use only.
- Code: the repository has no separate code license; `LICENSE.txt` is its only license file and the Python files
  carry no license header, so its code falls under the same CC BY-NC 4.0.
- Used here: `harness/external/fetch.py` downloads `data/locomo10.json` and `LICENSE.txt` at run time into
  `external/locomo/raw/` (gitignored); the data is never redistributed in this repository.
  `harness/external/locomo.py` carries the paper's answer prompts `QA_PROMPT` and `QA_PROMPT_CAT_5` verbatim, from
  `task_eval/gpt_utils.py` at the same commit.

## Mem0 judge prompt

- Source: [mem0ai/mem0](https://github.com/mem0ai/mem0), `evaluation/metrics/llm_judge.py` at commit `aae5989`, the
  last commit to touch `evaluation/` before that directory was retired.
- License: Apache License 2.0.
- Used here: `harness/external/locomo.py` carries its `ACCURACY_PROMPT` verbatim for the LoCoMo J score.

## LongMemEval

- Source: LongMemEval (Wu et al., ICLR 2025), [xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval) at
  commit `9e0b455f4ef0e2ab8f2e582289761153549043fc` (`LME_REPO_COMMIT` in `harness/external/fetch.py`), and the
  cleaned dataset [xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)
  on Hugging Face at revision `98d7416c24c778c2fee6e6f3006e7a073259d48f`.
- License: MIT for both (the code repository's `LICENSE` and the dataset card).
- Used here: `harness/external/fetch.py` downloads the dataset and the code repository's `LICENSE` at run time into
  `external/longmemeval/raw/` (gitignored); the data is never redistributed in this repository.
  `harness/external/longmemeval.py` carries the authors' judge prompts (`get_anscheck_prompt` from
  `src/evaluation/evaluate_qa.py`) verbatim and their reader prompt from `src/generation/run_generation.py`, adapted
  to injected memory.

## spaCy `en_core_web_sm`

- Source: the `en_core_web_sm` 3.8.0 model wheel from
  [explosion/spacy-models](https://github.com/explosion/spacy-models/releases/tag/en_core_web_sm-3.8.0), pinned by
  URL in `pyproject.toml` `[tool.uv.sources]`.
- License: MIT.
- Used here: only by the `competitors` extra, for Mem0's `[nlp]` hybrid search in the `mem0` adapter.

Python package dependencies are listed in `pyproject.toml` and `uv.lock` under their own licenses.
