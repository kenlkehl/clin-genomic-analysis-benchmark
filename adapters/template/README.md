# clin-genomic-analysis-benchmark adapter template

Drop-in skeleton for plugging your agent into the benchmark harness.

## Contract

Agents run only the **analyze** task, and only on questions whose gold says
every component is clear. (The assess task is a single prompt sent straight to
a model API; it has no adapter. See `clingen-bench assess`.) The harness
invokes your adapter once per question:

```
$ ./run.sh --question-file <abs question.json> --output <abs result.json>
```

- Exit 0 = success. A nonzero exit, timeout, missing/invalid JSON, or schema
  violation is retried by the harness (three attempts by default).
- Default timeout: 3600s per question.
- Stdout + stderr are captured to `analyze.agent.log` in the per-question run directory.
- The system prompt is `clin_genomic_analysis_benchmark.instructions.analyze_rulebook()`:
  `ANALYZE_INSTRUCTIONS.md` followed by `CONVENTIONS.md`.

### `question.json`

```jsonc
{
  "contract_version": "5",
  "question_id": "bladder_1.2-Qabc12345",
  "question_text": "What proportion of patients ...",
  "cohort": "bladder_1.2",
  "cohort_dir": "/abs/path/.../bpc_from_synapse/bladder_1.2",
  "data_dictionary_path": "/abs/path/.../simple_variable_synopsis.xlsx",
  "scratch_dir": "/abs/path/.../scratch",
  "instructions": "Harness instructions for the analyze task",
  "max_runtime_seconds": 3600
}
```

### `result.json`

`{ "answer_type": "...", "answer": {...typed fields...}, "methods": "...", "supporting_evidence": {...} }`

See `clin_genomic_analysis_benchmark/agent/contract.py` for the full JSON schemas.

## Wiring up your adapter

1. Edit `adapter.py` and implement `answer_analyze`.
2. Put every model-controlled CLI subprocess through
   `clin_genomic_analysis_benchmark.agent.isolation.sandboxed_agent_command`.
   Do not pass host paths into its prompt; use `sandbox_question_view`.
3. Register the reviewed adapter in `_SUPPORTED_ADAPTER_DIRS` in
   `agent/isolation.py`. The harness intentionally fails closed for an
   unregistered adapter.
4. Make sure `run.sh` is executable.
5. Run the harness:
   ```
   clingen-bench analyze --agent "bash adapters/<your-name>/run.sh" --agent-name <your-name> --cohort bladder_1.2
   ```

## Notes

- The harness is model-agnostic — your adapter is the only model-specific code.
- The template itself is not registered or safe to evaluate until its
  model-controlled subprocess uses the mandatory outer sandbox.
- Privacy: the cohort directory may contain de-identified PHI. Only transmit data over BAA-covered endpoints.
- The reference adapter at `adapters/claude_code/` demonstrates a working implementation against Claude Code on Vertex.
