# Goal

One paragraph: what is true when this task is done, in the language of the theorem or artifact.

# Scope

Files and declarations this task may touch (must match `scope` in task.yaml). Everything else is read-only.

# Definition of done

Mechanical (checked by the Steward with the workload's checker):
- build passes
- no `sorry` in the named declarations
- `#print axioms` ⊆ allowed set

Judgment (the Steward decides, and says why in review.md):
-

# Out of scope / do not

- Do not change any theorem statement, definition, or axiom. If the statement seems false or too strong, stop and report `omri:decide` in unlocks.

# Strategy hints

Optional. If this task is a race, each attempt gets a different hint here.

# Context to read first

Filled by the Steward from `scr graph context <id>`.
