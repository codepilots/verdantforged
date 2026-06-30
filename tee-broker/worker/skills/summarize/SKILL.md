---
name: summarize
description: One-paragraph summary of a passage of text. Honest, concise, no embellishment.
license: Apache-2.0
---

# Summarize

Produce a one-paragraph summary of the input text.

## Inputs

- `text` (string, required) — the passage to summarize

## Output

JSON object on stdout:

```json
{
  "output": "<one-paragraph summary>"
}
```

## Implementation

1. Call `inference.local` with the prompt "Summarize the following text in one paragraph: <text>"
2. Parse the response, extract the assistant message
3. Print `{"output": "<summary>"}` on stdout

The broker proxy intercepts `inference.local` and routes to the upstream
LLM with the per-job ephemeral token. The token never leaves the sandbox.
