---
name: tee-broker-code-review
version: 0.1.0
description: >
  Use when reviewing submitted source code for security, correctness, and style on the
  VerdantForged TEE broker. Performs deterministic static checks inside the worker
  enclave and returns structured findings plus a review verdict.
category: tee-broker
tags: [tee-broker, code-review, static-analysis, demo-skill, enclave]
metadata:
  hermes:
    related_skills: [tee-broker-submit-job]
    changelog:
      - 0.1.0 (2026-07-03) — initial demo release: style and correctness checks for Python.
---

# TEE Broker Code Review

Demo skill for the VerdantForged broker. This skill is intentionally constrained to
deterministic static checks so it can run inside an attested worker enclave without
network access or nondeterministic tooling. It mirrors the frontmatter/runtime shape
used by `tee-broker-submit-job`.

## When to use this skill

Use this skill when:
- The broker dispatches a `code-review` job to a worker.
- You need deterministic review output inside an enclave.
- You need a demo skill shape for submitting code-review jobs via `tee-broker-submit-job`.

Do NOT use this skill when:
- You need network-aware or LLM-assisted review outside enclave constraints.
- You need deployment or config guidance; use `tee-broker-deploy-config` instead.
- You need attestation validation; use `verify-attestation` instead.

## Trigger

Invoke when the broker dispatches a `code-review` job to this worker. The worker
receives the source in `encrypted_data` and must return `output` plus structured
findings.

## Input contract

- `language`: optional, defaults to `python`
- `source`: required source text
- `checks`: optional list from `style`, `complexity`, `assertions`, `secrets`

## Deterministic review rules

1. **Style**: flag bare `except:`, missing docstrings on top-level functions/classes,
   lines longer than 120 chars.
2. **Complexity**: flag functions with more than 20 statements in their body.
3. **Assertions**: flag loops without upper-bound protection when iterating mutable
   inputs.
4. **Secrets**: flag strings matching `AKIA[0-9A-Z]{16}`, `-----BEGIN ... PRIVATE KEY-----`,
   generic password assignment patterns.

## Output contract

Return minified JSON in `output` with:

```json
{
  "verdict": "pass|warn|fail",
  "findings": [
    {"kind": "style|complexity|assertion|secret", "line": 12, "msg": "..."}
  ],
  "summary": "one-line assessment"
}
```

- `pass` = 0 findings
- `warn` = findings, none `secret` or `assertion`
- `fail` = any `secret` or `assertion` finding

Keep findings deterministic: same input yields identical output.

## Completion criterion

The skill is complete only when it writes `output` JSON and sets `result_hash` from
its serialized output.

## Common Pitfalls

1. **Non-deterministic tooling**: do not call LLMs, OS rand, or network from inside
   the enclave. Use regex-based checks only.
2. **Mutable input aliasing**: evaluate the original text only; do not rely on runtime
   objects outside enclave memory.
3. **Large outputs**: findings JSON is capped at 1 MiB. Truncate oldest findings if
   exceeded, and add `truncated: true`.

## Verification Checklist

- [ ] Input contract fields present before reviewing
- [ ] No network or shell invocation in review logic
- [ ] Deterministic `verdict` from findings only
- [ ] Output JSON valid and under cap
- [ ] `result_hash` set from serialized output bytes
