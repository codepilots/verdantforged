# VerdantForged Skill Library — API reference

Base URL: `$SKILL_LIBRARY_URL` (default `http://127.0.0.1:8091`).

All write endpoints (POST/PUT/DELETE) require the header
`Authorization: Bearer *** KEY>`. If
`SKILL_LIBRARY_API_KEY` is unset on the server, writes return 503.

Interactive Swagger UI: `GET /docs`. Raw OpenAPI 3.1: `GET /openapi.json`.

---

## `GET /healthz`

```json
{ "ok": true, "db": "ok", "efs": "ok" }
```

Public, no auth. Returns 200 if the DB is reachable AND the files directory
exists.

---

## Public reads (no auth)

### `GET /v1/library/skills`

Returns one entry per skill-name+version pair (so a name with 3 versions
shows up 3 times).

```json
{
  "skills": [
    {
      "name": "summarize",
      "version": "1.0.0",
      "summary": "One-paragraph summary of a passage of text.",
      "file_count": 3,
      "total_bytes": 5421
    }
  ]
}
```

### `GET /v1/library/skills/{name}`

Lists all registered versions of `name`.

```json
{
  "name": "summarize",
  "versions": [
    { "version": "1.0.0", "description": "...", "summary": "..." }
  ]
}
```

### `GET /v1/library/skills/{name}@{version}`

Returns the full card + file list:

```json
{
  "name": "summarize", "version": "1.0.0",
  "description": "...", "license": "Apache-2.0",
  "summary": "...", "sha256_card": "...",
  "created_at": "2026-06-29T16:30:00+00:00",
  "files": [
    { "filename": "SKILL.md",      "sha256": "...", "size_bytes": 894, "content_type": "text/markdown" },
    { "filename": "skill.wasm",     "sha256": "...", "size_bytes": 182765, "content_type": "application/wasm" }
  ],
  "total_bytes": 183659
}
```

### `GET /v1/library/skills/{name}@{version}/manifest`

Same shape as above but `files` only (no description/license/summary).
Convenient for clients that want to forward a card to the broker.

### `GET /v1/library/skills/{name}@{version}/files/{filename}`

Returns the raw bytes of the file. `Content-Type` is the
content-type recorded at upload time. `Content-Disposition:
attachment; filename="..."` to encourage save-as.

---

## Bearer-protected writes

### `POST /v1/library/skills`

Register a new card (or a new version of an existing card).

**Body:**
```json
{
  "name": "summarize",
  "version": "1.0.0",
  "description": "One-paragraph summary of a passage of text.",
  "license": "Apache-2.0",
  "summary": "Optional one-line, surfaces in listings"
}
```

**Returns 201** with `{name, version, sha256_card}`. 409 if the
`(name, version)` pair already exists.

### `POST /v1/library/skills/{name}@{version}/files/{filename}`

Upload one file. Body is raw bytes. Two optional headers:

- `X-File-Sha256: <64-char hex>` — if present, server recomputes and 400s on mismatch.
- `Content-Type: <mime>` — recorded for later download.

Returns 201 `{filename, sha256, size_bytes}`. Idempotent: re-uploading a
file with the same `filename` overwrites the old blob (atomic via
`.tmp` → fsync → rename).

### `DELETE /v1/library/skills/{name}@{version}`

Removes the card AND every blob file. 204 on success. Idempotent at the
level of "missing card" returns 404.

### `POST /v1/library/skills/{name}@{version}/sync-to-broker`

Pushes the card to the live broker. The library service holds
`BROKER_BASE_URL` + `BROKER_SKILLS_API_KEY` in its env (NOT the same
key as the library's own write key). Forwards to:

1. `POST {broker_base_url}/v1/skills` with the manifest body
2. `POST {broker_base_url}/v1/skills/{name}/wasm` with the binary blob
   (only if the card has a `*.wasm` file)

Returns `{forwarded: [{step, status, body}, ...]}`.

---

## Error shape

All errors return JSON `{"error": "<msg>", "code": "<machine-readable>"}`
plus optional context fields (`registered_sha256`, `actual_sha256`, etc.).

| HTTP | Code | When |
|---|---|---|
| 400 | `bad_ref` | ref missing `@` |
| 400 | `sha256_bad_format` | X-File-Sha256 not 64-char hex |
| 400 | `sha256_mismatch` | X-File-Sha256 != computed sha256 |
| 401 | `library_auth_required` | missing `Authorization: Bearer` |
| 401 | `library_auth_invalid` | wrong bearer token |
| 404 | `skill_not_found` | no card with that name/version |
| 404 | `file_not_found` | no file with that filename in the card |
| 409 | `skill_already_registered` | duplicate registration |
| 503 | `library_auth_not_configured` | SKILL_LIBRARY_API_KEY unset |
| 503 | `broker_forwarding_not_configured` | sync-to-broker without broker key |
