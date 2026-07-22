"""Gold-standard answer pipeline (Piece 2).

Per unambiguous question:
  1. Codegen (Claude/Vertex) → analysis script
  2. Dual-model script review (Claude + Azure OpenAI) — disagreements → review queue
  3. Sandboxed execution (no network, RLIMIT, 300s)
  4. Result validation against the answer-type schema
  5. Repair loop on failure (max 3 iterations)
"""
