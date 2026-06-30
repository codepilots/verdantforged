---
name: photo-glow-up
description: Local photo enhancement using WASM Rust binary. No external API.
license: Apache-2.0
---

# Photo Glow-Up

Enhance a portrait photo using the bundled Rust/WASM glow-up binary.
Demonstrates that NemoClaw sandboxes can run real compute (not just LLM
calls) — the WASM binary lives at /opt/skills/photo-glow-up/glow_up.wasm
inside the sandbox.

## Inputs

- `image_path` (string, required) — path inside the sandbox
- `strength` (number, optional, default 0.5) — 0.0 to 1.0

## Output

JSON object on stdout:

```json
{
  "output_path": "/sandbox/output_enhanced.jpg",
  "processing_ms": 1234
}
```

## Implementation

1. Resolve image_path inside the sandbox
2. Invoke the WASM binary with the image + strength arg
3. Copy the output back to a sandbox-accessible path
4. Print the result JSON on stdout

This skill demonstrates **real compute**, not just LLM call delegation.
The broker proxy handles LLM traffic; NemoClaw sandboxes run code.
