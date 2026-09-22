# Credit-risk evaluators

Evaluator code for the credit-risk monitoring workflow. Nothing here is part
of the platform: each file is dropped onto an Evaluator node like any other
evaluation code.

- `monitoring_report.py` — per company: caught / missed, lead days, false
  alarms on verified negatives, weekly level against the countdown band,
  steps whose reasoning names a future date. Timing: after the entire batch.
  Objective metric: usually `detection_rate` (maximize).
- `labels.py` — turns a versioned release's reviewed outcomes into label
  records and saves them as a data resource. Attach it under the Evaluator's
  "Separate labels". The contemporary dataset needs no labels: its records
  carry their outcome in `sample_json`.
