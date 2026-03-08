---
name: constraint-first-debugging
description: Use when debugging, implementing, or optimizing under explicit hard constraints like size limits, schema requirements, compile/runtime gates, or feasibility checks. Focus on the smallest valid baseline first, then iterate.
---

# Constraint-First Debugging

Use this workflow when success depends on satisfying hard constraints before quality improvements.

1. List the hard constraints that must be true for any solution to count.
2. Check whether the current artifact already violates any of them.
3. If it does, stop extending that branch and reduce it to the smallest valid baseline.
4. Re-check the hard constraints after each meaningful change.
5. Only optimize secondary goals after the baseline is valid.

Use this skill to avoid spending time debugging or polishing an approach that is already known-invalid.
