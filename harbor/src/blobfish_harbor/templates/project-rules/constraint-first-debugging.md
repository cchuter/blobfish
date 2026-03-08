---
paths:
  - "**/*.{c,cc,cpp,cxx,h,hpp,rs,go,py,java,js,jsx,ts,tsx,sh}"
---

# Constraint-First Debugging

- Start by identifying explicit hard constraints such as byte limits, file counts, schema/format rules, compile requirements, or feasibility checks.
- Before deeper implementation work, produce the smallest artifact that satisfies those hard constraints.
- If the current artifact is already known-invalid on a hard constraint, fix that first instead of continuing to debug or extend it.
- After each meaningful change, re-check the hard constraints before optimizing for secondary goals like speed, quality, or completeness.
- Avoid heavyweight installs or background jobs until a minimal valid baseline exists, unless the task explicitly requires them.
