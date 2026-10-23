# Goal

A synthetic stand-in for the real T-0021 record (which stays in the state repo): T-0021 was merged
on the blank review template, the case T-0081's review guard exists for. `test_review_guard` copies
this folder in with status `submitted` and checks that landing and `task set accepted` both refuse it.

# Scope

`QuantumInsertion/A.lean`. Nothing else.

# Definition of done

- The review guard refuses this record until review.md has a Decision.
