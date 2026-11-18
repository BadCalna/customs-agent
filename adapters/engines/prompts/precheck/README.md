# P16 injected precheck JSON contract

`PrecheckEngine(model=..., hs_source=..., elements_source=..., history_source=None,
notes_source=None)` is standalone; it is not selected by settings/get_engine and
must not be connected to production workflows yet. No network or DB client is
created. Injected adapters must themselves obey that restriction.

`model.complete(request: dict) -> str | dict` runs once, synchronously. The request
contains `system`, `facts`, `history_reference`, `classification_notes`, and
`response_schema` (the generated strict JSON schema is the executable contract).
For example, this is a **synthetic shape example**, not an actual classification:

```json
{"candidates":[{"hs":"8544422900","evidence_ids":[]}],"suggested_hs":"8544422900","missing_facts":[]}
```

All keys are required; extra keys at any level, numeric/Unicode/8-digit HS,
confidence/probability/NaN, duplicate keys/candidates, unknown references and
out-of-scope evidence are rejected as a sanitized `EngineTechnicalError`.
Markdown fences and free-form model reasons/evidence are deliberately not
accepted. Reasons/issues are rendered by the engine, preventing invented clauses
or arbitrary model-generated supplier instructions. Every confidence remains null.

## Order and evidence

1. Optional history retrieval uses P15 `HistoryQuery`. No default history source
   is instantiated (P15's default would otherwise load a synthetic fixture).
2. Optional `notes_source.search(facts: dict, basis: QueryBasis)` returns a list of
   this module's `ClassificationNote` or dictionaries with that exact shape.
3. Model generates independent candidates, referencing only retrieved note IDs.
4. Every candidate is checked with P14 HS and declaration-elements adapters.
5. `compare(CompareInput, *, basis=None)` delegates to
   `hs_compare.compare_original_checked` with this engine's HS source and **no
   invented query basis**. `run_precheck(RecommendInput, *, supplier_hs_raw,
   association_ok=True, association_note="", supplier_hs_format_valid=True,
   supplier_hs_validity_note="") -> PrecheckOutput` first recommends, then compares
   using the explicitly supplied RecommendInput.query_basis. The helper snapshots
   context and never includes original HS or association fields in model input.
   CompareInput alone has no basis, so callers must supply it explicitly to
   compare or use the helper. P14 responses are revalidated and their code/query
   basis/record version must match the exact request, not merely be valid DTOs.

A ClassificationNote has `evidence: EvidenceEntry`, `hs_codes: list[str]`,
`tariff_version: str`, `basis: QueryBasis`, `authoritative: bool`, `verified: bool`,
and `synthetic: bool | null`. This metadata must come from the trusted source
callback, NEVER from a model. The evidence query must equal:

```python
{"hs_codes": note.hs_codes, "tariff_version": note.tariff_version,
 "basis": basis.model_dump()}
```

To clear classification support gaps the note needs explicit `synthetic=False`,
verified authoritative metadata, obtained official evidence with document,
locator, excerpt, valid retrieval date and an explicit effective_from whose
window covers the supplied declaration date (invalid/expired/future/unknown note
windows cannot support classification), exactly matching query basis and P14
verified tariff version. P14 must additionally have declaration_ready,
usable_as_basis, authoritative, explicit non-synthetic datasets, and sufficient
declaration elements. Code existence plus complete elements is NOT a
classification justification. History is ALWAYS reference-only, never this
support. No notes means no gap-free result. In live mode synthetic or undeclared
history/notes are excluded; mock context never upgrades synthetic sources into
real evidence. Even explicit `synthetic=False` metadata in unit fixtures only
tests the gate and does not prove genuine data provenance.

Only P14 `insufficient` gaps can target supplier; model missing_facts, image
failure, source failure and unverified sources target expert/operator. Source
failures, not-found and version-unverified remain separate evidence statuses.
No-image is not read-failed; only supplied visible descriptions are model facts.

## Input boundary / limits

Attributes use an explicit commodity-fact allowlist. Execution context, business
IDs, mock scenario, source paths/URLs, raw image error strings, cells, source
reviewer identity, arbitrary notes and query-basis notes are not passed to the
model. Recognizable embedded secret/ID/path/serialized-object containers in
fact text are rejected. Model responses and dependency exception messages are
never interpolated into technical errors. Source query-failed diagnostics are
replaced by safe fixed messages.

This is defense in depth, NOT a general secret-detection guarantee: caller must
provide fact-only input. Product names, attributes, elements text, visible image
descriptions and historical references can naturally contain HS numbers or
hostile instructions. The request and confidence note explicitly retain this
anchoring/injection risk; no claim of complete de-anchoring is made. Real source
and model quality, calibrated probability, production integration, and full
expert checking are not implemented or evaluated here. `expert_check` raises
`NotImplementedError`.
