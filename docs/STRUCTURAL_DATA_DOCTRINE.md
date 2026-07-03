# Structural-Data Doctrine — Lenses as Facet Projectors

> **Status:** Binding methodology for ingesting **structured / tabular / numeric** data into Calyx.
> Extends `DOCTRINE.md`; does not override it. Where this doc and the charter conflict, the charter wins.
> **Scope:** any dataset whose signal lives in *columns and outcomes*, not in free text
> (sports stats, telemetry, finance, sensor logs, relational rows). For free-text signal, use embedder lenses as normal.

---

## 0. The one-sentence rule

> **For structural data, a lens is a *facet projector*, not an embedder.**
> Each lens measures one coherent group of columns (a *facet*) and emits that facet's real,
> normalized numbers as its own typed slot — kept separate from every other facet (A3 no-flatten).

Embedders exist for the case where meaning lives in prose. Tabular data has no prose; its meaning is
the numbers and their relationship to outcomes. A neural embedding of `"goals=2 xg=1.7 poss=58"` throws
that away. A facet projector keeps it.

---

## 1. Why the naive approach fails (anti-pattern — refuse it)

Encoding an entity as **one text line** (`"goals_2 xg_1.7 poss_58 ..."`) and measuring it with
`algorithmic:byte-features` / `token-hash` / `scalar` lenses is the tempting first move. **It is wrong**
and produces measurably dead signal:

| Symptom | Root cause | Error you will hit |
|---|---|---|
| Lens carries no outcome signal | `ByteFeatures(16)` = char-class ratios + FNV hashes of the *whole line*; `Scalar(1)` = mean-byte/255 of the *whole line*. The number `2` is never exposed as a feature. | `CALYX_ASSAY_LOW_SIGNAL` |
| Extra lenses add nothing | Multiple hash-lenses over the *same line* are near-duplicates. | `CALYX_ASSAY_REDUNDANT` (corr > 0.6) |
| Weak kernel / noisy associations | Loom cross-terms and Lodestar run over vectors that don't encode the columns. | Poor groundedness, low recall |

**Rule:** never represent a structural record as a single opaque hash line. Represent it as a
**constellation of facet slots, each carrying that facet's actual numbers.**

---

## 2. How to build facet lenses (two doctrine-legal paths)

Every lens sees the **same input bytes** (one `Input = { modality, bytes }` per record; the CLI cannot
feed different bytes to different slots). Facet separation therefore comes from **what each lens extracts**,
not from different inputs. Two supported ways:

### 2A. `external-cmd` facet projector — CLI-legal, no engine edit (preferred)

`calyx add-lens <vault> --name <facet> --runtime external-cmd --endpoint <cmd> --shape "Dense(<d>)" --modality <text|structured>`

Calyx spawns `<cmd>` **fresh per micro-batch** and speaks a 4-byte-big-endian length-prefixed JSON frame
(source: `crates/calyx-registry/src/runtime/external_cmd.rs`):

```
stdin  ← [u32 BE len][ {"modality":"…","inputs":[[<byte>,…], …]} ]
stdout → [u32 BE len][ {"vectors":[[<f32>,…], …]} ]      # one vector per input, each EXACTLY Dense(d), all finite
```

Contract the script MUST honor, or the measurement fails closed:
- Emit exactly `inputs.len()` vectors; each exactly `d` floats (`CALYX_LENS_DIM_MISMATCH` otherwise).
- No NaN/Inf (`CALYX_LENS_NUMERICAL_INVARIANT`).
- Exit 0 within 30 s (`CALYX_LENS_UNREACHABLE` otherwise).
- The lens is **frozen**: its identity is `sha256(cmd, args)`. Changing the script = a new lens. Never
  mutate a projector's math in place (A4 frozen lenses).

Each facet gets its own script that parses the shared line and returns **only its columns**, normalized:
`attack → [goals, shots, sot, xg, corners]`, `defense → [conceded, saves, clean_sheet]`, etc.
Different columns ⇒ decorrelated slots ⇒ they pass A7.

### 2B. Library `VaultStore::put` ingester — for maximum fidelity

For large or performance-critical loads, write a Rust ingester against `calyx-core` that assembles a
`Constellation` with a distinct `SlotVector` per facet and calls `VaultStore::put`. This is **using the
API, not rewriting the engine** — allowed. It removes subprocess overhead and gives exact per-slot control.
Use when 2A's per-record subprocess cost is too high.

> **Both paths honor §5 (plug-in lenses is THE key) and the "no engine-source rewriting" rule (§2.4).**
> Do **not** add bespoke encoder kinds to `calyx-registry` to serve one dataset; project in a lens instead.

---

## 3. The design law: orthogonal, informative facets

The objective is **panel sufficiency** = `I(panel ; outcome)` — total bits the facet-panel carries about the
grounded outcome (bounded by the DPI ceiling `I(panel; anchor)`). Maximize it subject to A7 on every lens:

- **Informative:** each facet ≥ `0.05` bits about the outcome (`MIN_SIGNAL_BITS`). Below → Park/Retire.
- **Orthogonal:** each facet ≤ `0.6` pairwise correlation (`MAX_PAIRWISE_CORR`). Above → one is redundant, retire it.

> **Design rule:** choose facets that are *individually predictive* and *mutually orthogonal*.
> Then Loom's cross-terms capture the *interactions* (facet A × facet B predicts when neither alone does),
> and Anneal tunes the fusion weights. That is the whole optimization — it is measurable, not aesthetic.

Minimum panel: **≥ 2 content (non-retrieval) slots** or `weave-loom` and cross-lens consensus fail closed.

---

## 4. Ex-ante vs ex-post — the law of prediction

To predict an **unplayed / future** record, the panel may use **only facets knowable before the outcome.**

| Ex-ante facets (PREDICTORS — go in the predictive panel) | Ex-post facets (OUTCOMES — become the anchors) |
|---|---|
| pedigree/priors (rank, rating, value, history) | realized result / label |
| trailing form (last-N aggregates) | in-event stats that only exist after the fact |
| context (home/away, stage, rest, venue) | post-hoc quality proxies |

**Mixing an ex-post facet into a predictive panel leaks the answer** and produces a model that cannot run
on a future record. Keep two panels when needed: an *explanatory* panel (all facets, for understanding a
completed record) and a *predictive* panel (ex-ante only, for forecasting). Ground both to the ex-post outcome.

---

## 5. Grounding (mandatory — A2)

`calyx anchor <vault> <cx_id> --kind label:<axis> --value <v> --confidence <0..1] --source <s>`
(or inline `anchors` in a `--batch` JSONL row).

- **Grounded = Trusted** iff `source` is non-blank **and** `0 < confidence ≤ 1`. Blank source or `confidence=0`
  downgrades every downstream result to **Provisional** and poisons `bits`/`kernel`. Always pass both.
- **≥ 50 anchored outcomes per axis**, with **both classes present and reasonably balanced**
  (`MIN_ANCHORS = MIN_ASSAY_SAMPLES = MIN_BAD_SCORES = 50`). Heavily skewed axes read as low-signal;
  a genuinely rare-but-critical class may still qualify via the stratified `sole_carrier` override.
- One axis = one question. Prefer several clean binary/enum axes over one muddled label.

---

## 6. Canonical build order (run exactly in this sequence)

```
create-vault                       # pick a template or start bare
add-lens  (× ≥2 facet projectors)  # §2; ≥2 content slots required
ingest    --batch <jsonl>          # idempotent; content-addressed cx_id
anchor    (≥50 balanced, sourced)  # §5 ; or inline in the batch
bits      <axis>                   # writes Assay rows; REQUIRED before Ward + Oracle
weave-loom                         # writes XTerm + association-graph CFs (needs ≥2 content slots)
kernel-build                       # needs weave-loom graph + anchored nodes; recall-gated ≥0.95
guard     calibrate                # needs ≥50 bad scores; writes per-slot τ profiles
rebuild-search-index               # after ANY panel change (add/retire lens); NOT after a plain anchor
# then: search / kernel-answer / guard check / oracle readbacks
```

Dependency facts: `bits` must have written before Ward required-slots and Oracle's honesty gate can read.
`kernel-build` fails closed with no anchored nodes (`CALYX_KERNEL_UNGROUNDED`) and below `0.95` recall
(`CALYX_KERNEL_RECALL_BELOW_GATE`). `kernel-first` fusion needs ≥ 10 M rows — for smaller vaults use
`kernel-answer` (internal kernel-first at k=10 over grounded docs).

---

## 7. Predicting all predictable futures (Oracle)

Oracle is reached via `calyx readback`:

```
calyx readback oracle_predict        --vault <d> --domain <s>            # forward: context → grounded outcome
calyx readback oracle_expand         --vault <d> --domain <s> --depth 4  # butterfly tree (depth≤4, ×0.7/hop, prune<0.05)
calyx readback reverse_query         --vault <d> --domain <s>            # abductive: outcome → likely causes (≤3 hops)
calyx readback oracle_sufficiency    --vault <d> --domain <s>            # CAN the panel predict this at all?
calyx readback oracle_self_consistency --vault <d> --domain <s>
calyx readback super_intelligence    --vault <d> --domain <s>            # 6-tier readiness scorecard
```

Data shape Oracle reads: domain/action metadata on the constellations
(`oracle.domain|domain`, `oracle.action|action`) and outcome anchors (`outcome_anchor|oracle_verdict = {"value": …}`);
consequence edges `{action_or_event, domain, outcome:{value}, grounded?, provisional?}`; reverse edges use
`oracle.effect` + `oracle.structural_confidence`. **Ingest this metadata at load time** if you want Oracle.

**The honesty gate is the definition of "predictable."** Oracle returns `Insufficient` (with per-lens deficits)
precisely when `panel_bits < H(outcome)`, and caps confidence at `min(raw, self_consistency_ceiling, dpi_ceiling)`.
So "predict every future the dataset can support" is operationally: **run Oracle; trust what it answers, and
treat every `Insufficient` as a grounding/lens deficit to close (add a facet, add anchors) — never as a prompt
to fabricate.** Use `oracle_sufficiency` first to see whether an axis is predictable at all.

---

## 8. Reference constants (verbatim from source)

| Constant | Value | Meaning |
|---|---|---|
| `MIN_ANCHORS` / `MIN_ASSAY_SAMPLES` / `MIN_BAD_SCORES` | `50` | floor for bits / estimators / guard calibration |
| `MIN_SIGNAL_BITS` | `0.05` | a lens must add ≥ this about the outcome (A7) |
| `MAX_PAIRWISE_CORR` | `0.6` | lens pair above this is redundant (A7) |
| kernel recall gate | `0.95` | A10 trust gate for `kernel-build` |
| `FUNNEL_MIN_VAULT_SIZE` | `10,000,000` | below this, use `kernel-answer` not `kernel-first` |
| Guard FAR ceilings | `0.01 / 0.03 / 0.05` | Identity / Content / Stylistic slots |
| `RRF_K` | `60` | reciprocal-rank-fusion constant |
| edge cos threshold | `0.5` | Loom default edge keep |
| oracle hop attenuation | `0.7` | butterfly per-hop decay |

## 9. Failure codes → remediation

| Code | Fix |
|---|---|
| `CALYX_ASSAY_INSUFFICIENT_SAMPLES` | anchor ≥50 grounded, balanced outcomes for the axis first |
| `CALYX_ASSAY_LOW_SIGNAL` | facets don't expose the numbers — use projector lenses (§2), not line-hashes |
| `CALYX_ASSAY_REDUNDANT` | retire one of two correlated facets (corr > 0.6) |
| `CALYX_KERNEL_UNGROUNDED` | attach anchors with real `--source`/confidence |
| `CALYX_KERNEL_RECALL_BELOW_GATE` | improve facet quality / add support members (don't just lower the gate) |
| `weave-loom` needs ≥2 content slots | add a second facet projector |
| `CALYX_GUARD_PROVISIONAL` | `guard calibrate` with ≥50 bad scores |
| `CALYX_STALE_DERIVED` | `rebuild-search-index` |
| `CALYX_INDEX_FUNNEL_VAULT_TOO_SMALL` | use `rrf` / `kernel-answer` |

---

## 10. What "optimize" means here (and what it never means)

- **Optimize = add facets, decorrelate them, ground more outcomes, then let Anneal tune fusion/τ.**
  The scarce resources are *facet orthogonality*, *grounding breadth*, and *corpus breadth* — the two whitepaper levers.
- **Never** rewrite engine math/thresholds to make a demo pass — that is the Goodhart failure the honesty gate
  exists to prevent (§9 anti-patterns). Do not lower `MIN_SIGNAL_BITS`, `0.95` recall, or `MIN_ANCHORS` to force green.
- **Never** flatten facets into one slot; **never** label an ungrounded/Provisional result "trusted";
  **never** add a per-dataset encoder to the core when a projector lens does the job.

*A return value is a claim. The source of truth is the bytes. Read the bytes.* (Cardinal rule §0.)
