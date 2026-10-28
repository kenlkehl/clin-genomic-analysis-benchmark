# clin-genomic-analysis-benchmark adapter template

Drop-in skeleton for plugging your agent into the benchmark harness.

## Contract

The harness invokes your adapter once per (question, stage):

```
$ ./run.sh --question-file <abs question.json> --output <abs result.json>
```

- Exit 0 = success. A nonzero exit, timeout, missing/invalid JSON, or schema
  violation is retried by the harness (three attempts by default). If every
  attempt fails, the stage fails and downstream stages are not attempted.
- Per-stage default timeouts: 3600s (assess) / 3600s (analyze). Analyze runs
  only when the agent judged every component clear in assess.
- Stdout + stderr are captured to `<stage>.agent.log` in the per-question run directory.

### `question.json`

```jsonc
{
  "contract_version": "4",
  "question_id": "bladder_1.2-Qabc12345",
  "question_text": "What proportion of patients ...",
  "cohort": "bladder_1.2",
  "stage": "assess" | "analyze",
  "cohort_dir": "/abs/path/.../bpc_from_synapse/bladder_1.2",
  "data_dictionary_path": "/abs/path/.../simple_variable_synopsis.xlsx",
  "scratch_dir": "/abs/path/.../scratch",
  "instructions": "Stage-specific instructions from the harness",
  "assessment_components": [
    {"id": "denominator", "label": "Cohort denominator", "description": "..."},
    {"id": "outcome", "label": "Outcome", "description": "..."},
    {"id": "covariates", "label": "Independent variables and covariates", "description": "..."},
    {"id": "analysis_plan", "label": "Statistical analysis plan", "description": "..."}
  ],
  "prior_classification": "unambiguous"   // present on the analyze stage
}
```

### `result.json` per stage

- `assess`  → one verdict per component, keyed by the IDs in
  `question["assessment_components"]`:
  ```json
  { "denominator":   {"needs_clarification": false, "rationale": "..."},
    "outcome":       {"needs_clarification": true,  "rationale": "..."},
    "covariates":    {"needs_clarification": false, "rationale": "..."},
    "analysis_plan": {"needs_clarification": false, "rationale": "..."} }
  ```
  The question counts as ambiguous when any component needs clarification.
- `analyze` → `{ "answer_type": "...", "answer": {...typed fields...}, "methods": "...", "supporting_evidence": {...} }`

See `clin_genomic_analysis_benchmark/agent/contract.py` for full JSON schemas.

## Wiring up your adapter

1. Edit `adapter.py` and implement `answer_assess` and `answer_analyze`.
2. Put every model-controlled CLI subprocess through
   `clin_genomic_analysis_benchmark.agent.isolation.sandboxed_agent_command`.
   Do not pass host paths into its prompt; use `sandbox_question_view`.
3. Register the reviewed adapter in `_SUPPORTED_ADAPTER_DIRS` in
   `agent/isolation.py`. The harness intentionally fails closed for an
   unregistered adapter.
4. Make sure `run.sh` is executable.
5. Run the harness:
   ```
   clingen-bench eval --agent "bash adapters/<your-name>/run.sh" --agent-name <your-name> --cohort bladder_1.2
   ```

## Notes

- The harness is model-agnostic — your adapter is the only model-specific code.
- The template itself is not registered or safe to evaluate until its
  model-controlled subprocess uses the mandatory outer sandbox.
- Privacy: the cohort directory may contain de-identified PHI. Only transmit data over BAA-covered endpoints.
- The reference adapter at `adapters/claude_code/` demonstrates a working implementation against Claude Code on Vertex.
