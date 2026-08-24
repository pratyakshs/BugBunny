# Evaluation results

This directory contains immutable BugBunny experiment bundles. Each experiment
keeps the full generation artifacts, export manifests, judge pair matrix, and
statistical audit needed to inspect a result without rerunning model calls.

Every bundle also contains:

- `experiment.json`: machine-readable provenance, input hashes, model routes,
  effective settings, commands, validation counts, and primary metrics;
- `README.md`: a human-readable reproduction and interpretation guide;
- `bundle_manifest.json`: the SHA-256 and byte length of every archived file
  other than the manifest itself.

API keys and dotenv files must never be stored here. The upstream benchmark
input is pinned by repository commit and content hashes rather than copied into
every bundle. This keeps the source fixture independent of generated exports
and lets a reproducer reject the wrong dataset before making a model call.

## Apples-to-apples checklist

For a model sweep to be directly comparable within an experiment, keep all of
the following fixed: benchmark bytes and case selection, BugBunny code commit,
review profile and policy, context mode, calibrated operating point, verifier,
judge, generation and judge concurrency, timeouts, finding stages, bootstrap
seed, and bootstrap sample count. Model-specific declared context capacity is
part of the treatment and must be recorded explicitly; never infer it from a
model name.

Across experiments, compare only tracks with the same finding stage. The
`balanced` track is the predeclared primary operating point. The `generator`
and `family` tracks are diagnostic views and should not silently replace it.

## Experiments

- [`codereviewbench-2026-08-23-gpt-5.6-luna-terra-sol`](codereviewbench-2026-08-23-gpt-5.6-luna-terra-sol/README.md)

