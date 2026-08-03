# clin-genomic-analysis-benchmark adapter template

Drop-in skeleton for plugging your agent into the benchmark harness.

## Contract

The harness invokes your adapter once per (question, stage):

```
$ ./run.sh --question-file <abs question.json> --output <abs result.json>
```

- Exit 0 = success. Any nonzero exit = failure for this stage; downstream stages are not attempted.
- Per-stage default timeouts: 600s (classify) / 300s (disambiguate) / 1800s (analyze).
- Stdout + stderr are captured to `<stage>.agent.log` in the per-question run directory.

### `question.json`

```jsonc
{
  "contract_version": "2",
  "question_id": "bladder_1.2-Qabc12345",
  "question_text": "What proportion of patients ...",
  "cohort": "bladder_1.2",
  "category": 3,
  "stage": "classify" | "disambiguate" | "analyze",
  "cohort_dir": "/abs/path/.../bpc_from_synapse/bladder_1.2",
  "data_dictionary_path": "/abs/path/.../simple_variable_synopsis.xlsx",
  "scratch_dir": "/abs/path/.../scratch",
  "instructions": "Stage-specific instructions from the harness",
  "disambiguation_concept_menu": [
    {"id": "ANATOMIC_HISTOLOGIC_SCOPE", "label": "...", "description": "..."},
    {"id": "DISEASE_EXTENT_SCOPE", "label": "...", "description": "..."}
    // ...the complete canonical menu...
  ],
  "prior_classification": "ambiguous"     // present on disambiguate / analyze stages
}
```

### `result.json` per stage

- `classify`     → `{ "classification": "ambiguous"|"unambiguous", "rationale": "..." }`
- `disambiguate` → `{ "concept_ids": ["OUTCOME_METRIC", "TIME_ORIGIN"] }`
  using only IDs from `question["disambiguation_concept_menu"]`
- `analyze`      → `{ "answer_type": "...", "answer": {...typed fields...}, "methods": "...", "supporting_evidence": {...} }`

See `clin_genomic_analysis_benchmark/agent/contract.py` for full JSON schemas.

## Wiring up your adapter

1. Edit `adapter.py` and implement `answer_classify`, `answer_disambiguate`, `answer_analyze`.
2. Make sure `run.sh` is executable.
3. Run the harness:
   ```
   clingen-bench eval --agent "bash adapters/<your-name>/run.sh" --agent-name <your-name> --cohort bladder_1.2
   ```

## Notes

- The harness is model-agnostic — your adapter is the only model-specific code.
- Privacy: the cohort directory may contain de-identified PHI. Only transmit data over BAA-covered endpoints.
- The reference adapter at `adapters/claude_code/` demonstrates a working implementation against Claude Code on Vertex.
