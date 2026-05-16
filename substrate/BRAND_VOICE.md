# Brand Voice — Audit + Gap Map (Step 1)

> Read-only doc. No code yet. The purpose is to map what brand-voice infrastructure exists today across the codebase, where it leaks, and what's missing before a Brand Voice editor + drift detector loop can ship.
> Audit date: 2026-05-16, SHA `564946e`

This file is the prerequisite for steps 2 (editor) and 3 (drift detector). It is also a forcing function: writing it down keeps the team from quietly building two parallel voice systems again.

---

## Headline finding: there are TWO voice stores, and they don't sync

Atlas has two source-of-truth files for brand voice, and they were designed independently:

| Store | What it is | Who reads it | Updated how |
|---|---|---|---|
| **`brand_configs/<Brand>.json`** | Flat JSON files on disk, one per brand | NIS generator (`generate_content_llm`), Image → NIS generator, every `generate_*` helper that takes `brand_cfg` | Hand-edited JSON file. No UI. |
| **`brand_profile` Postgres table** | Versioned, JSONB-rich, workspace-keyed | Marketing wizard (`_read_brand_profile` in `substrate/marketing_wizard.py`), Memory's `brand_profile_version` field on every decision_event | Seeded once by `substrate/brand_profile_seed.py`. No UI for revision. |

**Novelle's own JSON file admits this is the wrong shape.** From `brand_configs/Novelle.json`:

> *"the brand_profile table in Postgres carries the canonical voice; this JSON exists only to register Novelle in the dashboard's brand list"*

But the NIS generator never reads `brand_profile`. It only reads the JSON. So today the "canonical voice" in Postgres is a placeholder that no NIS run consults, and the JSON it points to is itself half-empty (Novelle.json has `never_words` but no voice rules, signature phrases, hero adjectives, etc.).

**Concrete consequence for Novelle right now:** if an operator wanted to change Novelle's voice, there is no surface to do it. They would have to SSH into the box, edit a JSON file, restart the app, AND remember to re-run the `brand_profile_seed.py` migration. Neither store is editable from the dashboard.

---

## What each store contains (Novelle, as of today)

### `brand_configs/Novelle.json`

```json
{
  "vendor_code_full": "",
  "default_upf": "",
  "default_coo": "",
  "default_care": "Machine Wash Cold",
  "gender": "Female",
  "bullet_1_focus": "fabric_performance",
  "never_words": ["cheap", "fast fashion", "knockoff", "basic"],
  "_atlas_seed_v0": true,
  "_atlas_note": "Initial Novelle config seeded May 16, 2026. Pre-launch."
}
```

Eight keys. Only three of them are voice-shaped (`never_words`, `bullet_1_focus`, and obliquely `gender`). The rest are operational defaults (care, coo, upf, vendor code).

### `brand_profile` table — Novelle row

From `substrate/brand_profile_seed.py`:

```python
{
  "workspace_id": "novelle",
  "profile_version": "novelle_v1.0",
  "brand_name": "Novelle",
  "category_scope": "activewear",
  "tier_scope": "premium",
  "stage_scope": "launch",
  "voice_rules": [{"_placeholder": True, "_note": "needs operator input"}],
  "banned_words": ["cheap", "fast fashion", "knockoff", "basic"],
  "required_words": [],
  "signature_phrases": [],
  "custom": {
    "target_customer": "young adults, 18-40, aspirational",
    "competitor_set": ["CRZ Yoga"],
    "current_product_focus": "leggings only",
    "_voice_status": "placeholder \u2014 awaits operator worksheet",
  },
}
```

Twelve fields, properly structured for voice. But:
- `voice_rules` is literally a placeholder list with one entry that says `_placeholder: true`
- `signature_phrases` is empty
- `required_words` is empty
- The custom block has business context (target customer, competitors) but no tone, no hero adjectives, no banned phrasings (vs. banned single words)

**Banned words appear in both stores.** This is duplication, not synchronization — the values happen to agree today because they were seeded together, but nothing enforces that.

---

## What the NIS prompt actually uses (lines 2055-2061 of app.py)

The LLM prompt for NIS title/bullets/description embeds these voice fields verbatim:

```
=== BRAND CONTEXT ===
BRAND: {clean_brand}
BRAND VOICE: {clean_brand} is a {bullet_1_focus}-focused brand for {audience}. {brief or ''}
HERO FEATURE for bullet 1 if no override: {bullet_1_focus}
NEVER USE these words: {never_words_str}
```

That's the entire voice surface area in the NIS prompt today. Three fields, all from the JSON:

1. **`bullet_1_focus`** — a single short string ("fabric_performance"). Used twice in the prompt.
2. **`never_words`** — a flat list. Atlas tells the LLM what NOT to write but never tells it what TO write in terms of voice.
3. **`brief`** — a per-style or per-PT product brief. Optional.

**Things conspicuously NOT in the NIS prompt:**
- The voice_rules placeholder list (lives in brand_profile, unread)
- Signature phrases (lives in brand_profile, unread)
- Required words (lives in brand_profile, unread)
- Tier/stage/category scope (lives in brand_profile, unread)
- Target customer (lives in brand_profile, unread)
- Hero adjectives (don't exist as a field anywhere)
- Tone descriptors (don't exist as a field anywhere)
- Reading-level target (doesn't exist as a field anywhere)
- "Like / not like" examples (don't exist as a field anywhere)

The NIS prompt instructs the LLM to write a "brand-voice opener (2-3 sentences)" in the description \u2014 but never tells it what the brand's voice actually sounds like.

---

## What the Marketing wizard prompt uses (lines 174-200 of marketing_wizard.py)

Marketing reads `brand_profile` (the better store). Its prompt embeds:

- `brand_name`
- `category_scope`, `tier_scope`, `stage_scope`
- `banned_words` (first 20)
- `custom.target_customer`
- `custom.competitor_set`

It does NOT embed:
- `voice_rules` (would be the place for tone, but it's a placeholder anyway)
- `signature_phrases`
- `required_words`
- `bullet_1_focus` (because that lives on the JSON store, not on `brand_profile`)

So Marketing has access to the richer store but doesn't use most of it. The Marketing wizard's understanding of "Novelle's voice" is effectively: a brand name + three scope tags + a banned-words list + target customer + competitors.

---

## Where the two stores leak into each other (and don't)

Substrate writes use `brand_profile_version` as the marker for "which version of the brand profile produced this decision." That's correct \u2014 it's the audit-trail link from a decision_event back to the voice that informed it.

Look at how each module fills the field:

| Caller | What it passes as `brand_profile_version` |
|---|---|
| `app.py:3423` (NIS generate-content) | `brand_cfg.get('_version') or f"{workspace_id}_legacy"` \u2014 i.e. the JSON file's `_version` key (which Novelle.json doesn't have, so it falls back to `'novelle_legacy'`) |
| `substrate/budget.py:152` (budget set) | `f"{workspace_id}_legacy"` (hardcoded) |
| `app.py:12146` (Image \u2192 NIS) | Same as above |
| Marketing wizard (`marketing_wizard.py`) | Reads `brand_profile` table directly; doesn't use this field for matching |

**This means:** every decision_event in the substrate today is tagged `novelle_legacy` because nothing writes a real profile_version. The seeded `novelle_v1.0` row in `brand_profile` exists but no decision_event references it. The audit-trail link is broken at the version level.

---

## What's needed for the brand voice loop (Steps 2 and 3)

If we want the loop you described \u2014 *"settle on the actual brand voice over time, machine shows where to change and what"* \u2014 here's what's specifically missing:

### Schema-level

1. **Pick ONE canonical store.** `brand_profile` is the better-designed one. Decision required: collapse `brand_configs/*.json` into `brand_profile` (write all NIS-relevant fields into `custom`) and read from `brand_profile` everywhere. Otherwise step 2 ships another disconnected surface.

2. **Add voice-shape fields that don't exist anywhere.** Specifically:
   - `tone_descriptors` (3-5 adjectives: e.g. "confident, warm, plainspoken")
   - `hero_adjectives` (5-10: e.g. "buttery, all-day, signature")
   - `banned_phrasings` (list of phrasings, not just words: e.g. "limited time only", "must have")
   - `like_examples` / `unlike_examples` (2-3 each: short sentences that exemplify or violate the voice)
   - `reading_level_target` (Flesch-Kincaid grade, optional)

   These belong in `voice_rules` as structured entries, not free-text. Schema would look like:
   ```json
   {"type": "tone_descriptor", "value": "warm"}
   {"type": "hero_adjective", "value": "buttery"}
   {"type": "like_example", "text": "Buttery wool that feels like a second skin."}
   {"type": "unlike_example", "text": "Limited time deal! Premium wool blazer for less!"}
   ```

3. **Make `brand_profile_version` actually point to a real version.** When the operator edits voice, bump the version (`novelle_v1.1`, `novelle_v1.2`, ...) and every subsequent decision tags with the new version. Required for the drift detector \u2014 you need to know which voice was in force when a given edit happened.

### Module enum

4. **Add `Module.BRAND_VOICE`** to `substrate/schema.py`. Every voice edit writes a decision_event with `module='brand_voice'`, `field_name='tone_descriptor' | 'hero_adjective' | 'banned_phrasing' | ...`. Same audit pattern as Budget. Mirrors how Budget treats its allocations.

### Prompt updates

5. **NIS prompt needs to be voice-aware.** Replace the current 3-line BRAND CONTEXT block with a structured block that includes tone_descriptors, hero_adjectives, signature_phrases, banned_phrasings, and like/unlike examples. The LLM gets a real voice spec instead of a single focus keyword.

6. **Image \u2192 NIS prompt needs the same treatment.** Currently it calls the same `generate_content_llm()` so the fix propagates.

7. **Marketing wizard prompt should consume voice fields too.** Tone-aware keyword brainstorming is meaningfully different from generic SEO brainstorming (a "warm + plainspoken" brand should not chase "ultra-luxe" search terms).

### The drift detector (Step 3, biggest unknown)

8. **Drift detector logic** \u2014 reads NIS / Image-NIS `decision_event` rows + their `operator_response` rows. For each accepted/edited event:
   - Did the atlas_output contain a voice term? (signature phrase, hero adjective)
   - Did the operator's edit remove it? Replace it with something else?
   - Aggregate by voice term over rolling N edits. Flag terms that operators systematically remove (signal: not actually voice) or systematically add (signal: should be in the declared voice but isn't).

9. **Surface threshold + presentation.** Two real risks:
   - **False positives.** One typo fix shouldn't flag the voice. Need minimum N events (10? 20?) per pattern before surfacing.
   - **Tone of the surfaced suggestion.** "Your declared voice says X but you keep editing toward Y \u2014 want to update?" must read as collaborative, not judgmental. The Memory tab's "Confound view" tone is the right reference \u2014 nothing claims, everything juxtaposes.

10. **The bootstrap problem.** With 2 NIS decision_events in the live db today, the drift detector has nothing to detect. It will need 4-8 weeks of real Novelle uploads to produce useful signal. Same accumulation problem as Phase 2 attribution. **Build the editor first (step 2), then accumulate, then ship the detector.**

---

## Honest take on sequencing

The original instinct \u2014 "Novelle brand voice flows into content + keywords, then the machine shows drift" \u2014 is correct as a roadmap. But the work decomposes into:

| Step | Description | Blocked on | Estimate |
|---|---|---|---|
| **1 (DONE: this doc)** | Audit current state. Identify two-store split. Propose schema. | nothing | ~30 min |
| **2 (next code)** | Collapse to single store + add voice-shape fields + ship editor UI + log to substrate as `Module.BRAND_VOICE`. NIS prompt rewrite to consume real voice. | nothing | 1-2 days |
| **3 (later)** | Drift detector + Memory surface. | Needs 4-8 weeks of real NIS edit traffic to be honest. | unknown until data exists |

Step 2 unblocks the operator (they can finally edit voice from the dashboard) AND makes NIS / Image-NIS / Marketing prompts richer immediately. That's the high-leverage commit. Step 3 is the celebrated insight piece but it's not the bottleneck \u2014 the bottleneck is "we have no editable voice today."

---

## Open questions before step 2

These are decisions I'd want explicit input on before writing code:

1. **Should `brand_configs/*.json` go away entirely**, or stay as a fallback for fields like `default_care` and `default_upf` that aren't really voice? My instinct: keep operational defaults in JSON, move all voice fields to `brand_profile.custom`/`voice_rules`. Don't mix concerns.

2. **Editor placement.** Three options:
   - New top-level sidebar item ("Brand Voice")
   - Sub-tab under existing Marketing
   - Settings-style page under User directions
   Best fit is probably its own sidebar item, because voice feeds NIS *and* Marketing *and* future modules \u2014 it's not Marketing-specific.

3. **Version bumping.** When the operator saves a voice edit, do we auto-bump the profile_version (`novelle_v1.0` \u2192 `novelle_v1.1`), or only on explicit "publish"? Auto-bumping every save = many versions; explicit publish = simpler but adds a click.

4. **Backfilling existing decision_events.** Today every decision is tagged `novelle_legacy`. If we ship `novelle_v1.1` next week, do we leave old events on the legacy tag (honest) or rewrite them (clean but rewrites history)? I'd vote leave them \u2014 history is append-only.

---

## Version history

Append below this line. Do not edit entries above.

- **v1.0 \u2014 2026-05-16, SHA `564946e`** \u2014 Initial audit. Two-store problem documented (`brand_configs/*.json` vs. `brand_profile` table). Novelle voice content enumerated. NIS prompt voice surface area enumerated (3 fields). Drift detector decomposed into 10 specific gaps. Step 2 (editor) recommended as next code; Step 3 (drift detector) blocked on data accumulation.
