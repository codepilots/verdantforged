---
name: code-review
description: Structured code review with concrete suggestions. Pass/fail per finding.
license: Apache-2.0
---

# Code Review

Review a code snippet for correctness, style, and security.

## Inputs

- `code` (string, required) — the snippet to review
- `language` (string, optional) — "python", "typescript", "rust", etc.

## Output

JSON object on stdout:

```json
{
  "findings": [
    {
      "line": "<approx line number or null>",
      "severity": "error|warning|info",
      "message": "<concrete suggestion>"
    }
  ],
  "verdict": "approve|request_changes|comment"
}
```

## Implementation

1. Call `inference.local` with a structured review prompt
2. Parse the JSON response from the model
3. Print the parsed object on stdout

The broker proxy provides per-job usage tracking and forwards to the
upstream LLM with the ephemeral token issued at submit time.
