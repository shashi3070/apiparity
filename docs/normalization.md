# Normalization rules

Normalization runs **after** both responses are captured and **before** comparison.
It never mutates the originals — `NormalizedPair` holds derived views. Every rule
below has a test in `tests/unit/test_normalization.py` and/or `tests/unit/test_comparison.py`.

## 1. Header normalization

- All header names are lowercased for matching (`Content-Type` ≡ `content-type`).
- Values are compared as **ordered lists** — a header sent twice
  (`Set-Cookie: a=1`, `Set-Cookie: b=2`) must appear in the same order on both sides.
- **Default-ignored headers** (dropped before comparison):

  | Header | Why |
  |---|---|
  | `date` | Changes every response |
  | `content-length` | Transport-computed; bodies are compared separately |
  | `server` | Server banner (Werkzeug vs uvicorn), not app behavior |
  | `connection` | Hop-by-hop transport header (ADR #11) |

- Configure via `normalization.ignored_headers` (config) or `--ignore-header`
  (repeatable; CLI **adds** to the effective set for that run). Matching is
  case-insensitive exact name — there are no wildcards.
- If you set `ignored_headers` in the **config file**, it **replaces** the default
  list; include the four defaults explicitly if you still want them.
- Headers listed in the **corpus** (`normalization.ignored_headers`) are always
  **additive** — they union with the config-file set (which may itself be the
  built-in defaults). Same additive behavior applies to corpus
  `ignored_json_pointers`. Removed headers → `header_removed`; added →
  `header_added`; different values → `header_value`.

Example — Flask emits `Set-Cookie: session=abc` (a real behavioral difference you
*want* to see), while uvicorn would not; that shows up as `header_value` on
`set-cookie` unless you ignore it:

```yaml
normalization:
  ignored_headers: [date, content-length, server, connection, set-cookie]
```

## 2. Ignored JSON pointers (dynamic fields)

RFC 6901 ([JSON Pointer](https://datatracker.ietf.org/doc/html/rfc6901)) paths into
JSON response bodies. A value at an ignored pointer is removed from **both** sides
before the structural diff, so a field that legitimately changes every run
(`request_id`, `generated_at`, an order id after a POST) cannot fail the case.

Syntax:

```
""                          the whole document
/metadata/request_id        a nested key
/items/2/name               array index (0-based)
/a~1b                       key "a/b"   (~1 → /)
/m~0n                       key "m~n"   (~0 → ~)
```

Configure in the corpus (`normalization.ignored_json_pointers`, applied to all cases)
or in the config file / `--ignore-pointer` (repeatable):

```yaml
normalization:
  ignored_json_pointers:
    - /metadata/request_id
    - /metadata/generated_at
    - /data/password          # also prevents the value ever entering a Difference
```

Notes:

- Pointers that don't match anything in a given response are silently inert for that
  case (no error) — the same corpus can serve responses with and without the field.
- Ignoring happens **before** differences are produced, so an ignored field can never
  appear as a `json_key_removed` or `json_value` difference.
- Ignore a parent to cover a subtree: `/metadata` drops everything under it.
- Use redaction pointers (below) instead of ignoring when you want to *detect that a
  secret changed* without seeing its value.

## 3. JSON structural diff (what normalization does NOT change)

By default: object **key order ignored**, array **order preserved**, `null` ≠ missing,
type changes detected. See [`architecture.md`](architecture.md#comparison-semantics)
for the full semantics — normalization only removes ignored pointers and applies the
two rules below.

## 4. Whitespace normalization (opt-in)

```yaml
normalization:
  whitespace:
    text: true
```

- Applies to **text** bodies only (non-JSON or JSON that failed to parse): runs of
  whitespace collapse to single spaces, and leading/trailing whitespace is stripped,
  on both sides before comparison.
- Default: **off** — text bodies compare byte-for-byte after decoding.
- JSON bodies never take this path: they are compared structurally, which already
  ignores insignificant whitespace and key order.

## 5. Array order (per-pointer, opt-in)

```yaml
normalization:
  array_order:
    /results: sort
    /tags: sort
```

- Default for every array: `preserve` — `[1, 2]` ≠ `[2, 1]`.
- `sort`: arrays at the pointer (and, recursively, arrays nested under it) are sorted
  before comparison, so set-like responses match regardless of server order.
- Sort key: elements are ordered by their canonical JSON serialization (deterministic
  for all JSON-serializable values, including mixed-type arrays).
- Pointers that match nothing are inert. Setting a pointer under `array_order` does
  not imply `ignored_json_pointers`.

Use `sort` for genuinely unordered collections (tag lists, id sets); leave `preserve`
for sequences where order is behavior (timeline events, pagination).

## 6. Timing

Captured per case on both sides; never affects normalization of payloads.

```yaml
normalization:
  timing:
    mode: "warn"      # "off" (default) | "warn" | "fail"
    max_ms: 250
```

- `off`: timing is recorded as metadata only.
- `warn`: a case slower than `max_ms` on either side gets a `timing` difference with
  severity `warning` — it does not change the case status.
- `fail`: severity `error` — the run exits `1` if any case exceeds `max_ms`.
- `max_ms` is required when `mode` is `warn` or `fail`.

## Where rules are applied, in order

1. Response captured (size limit / transport errors first — a failed capture is never
   normalized into a match).
2. Header names lowercased; ignored headers dropped.
3. JSON parsed (if applicable); ignored pointers removed from both sides.
4. Array-order rules applied at configured pointers.
5. Text whitespace rules applied if enabled.
6. Comparison produces `Difference` objects.
7. Sensitive values replaced with `[REDACTED]` at construction time (redaction, not
   normalization — see [`safety.md`](safety.md#redaction)).

## Worked example

Corpus:

```yaml
version: 1
normalization:
  ignored_json_pointers: ["/metadata/request_id", "/metadata/generated_at"]
  array_order: {/tags: sort}
cases:
  - id: product
    method: GET
    path: /api/products/p-1
```

Baseline returns:

```json
{"product": {"id": "p-1", "price": 9.5}, "tags": ["b", "a"],
 "metadata": {"request_id": "1111", "generated_at": "2026-01-01"}}
```

Candidate returns:

```json
{"metadata": {"generated_at": "2026-01-02", "request_id": "2222"},
 "tags": ["a", "b"], "product": {"price": 9.5, "id": "p-1"}}
```

Result: **match** — request_id/generated_at removed by pointer, `tags` sorted by the
array rule, remaining JSON identical (key order is never compared).
