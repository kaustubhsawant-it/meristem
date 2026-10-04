# Paper

`meristem-paper.pdf` is the preprint describing Meristem 0.3.0: design, evaluation and limitations.

Read first: section 6 (Limitations) states what is not yet proven. In particular:

- The benchmarks are small and self-authored (six questions per synthetic corpus, three real questions on one repository).
- The `hipporag` and `mem0` columns are labelled approximations, not the published systems.
- The deployment telemetry (section 4.4) comes from private repositories and cannot be reproduced from this repository.
- Halt-before-hallucination is a protocol the agent is asked to follow, not an enforced mechanism.

Status of reproducibility: the evaluation harness (`tools/eval_harness.py`), the test suite and the telemetry script referenced in sections 4.1 and 4.4 are not yet in this repository, so Tables 1 and 2 cannot yet be reproduced from it. They will be added here; until then treat the figures as the author's reported results.

Contact: Kaustubh Sawant, parabkaustubh13@gmail.com
