# Pass 5 — UI polish · Runtime Trace

> Agency R0 sprint, items 6, 7, 12. The smallest pass of the sprint — three single-file fixes plus one defensive guard for a regression I couldn't reproduce.

---

## Item 7 — Bullet formatter double-headlines on ALLCAPS+colon

**Confidence going in:** H (exact lines located in Pass 0).

**Reproduction.** `normalize_bullet("ELEVATED WARMTH: The quilted puffer construction traps heat.")` → `"ELEVATED WARMTH: THE — quilted puffer construction traps heat."`. The detector at the old line 217 checked for ` — ` and ` - ` only. The agency's input had `: ` so it fell through to the "derive a headline from first 3-4 words" branch, which uppercased "THE" and injected an em-dash on top of an already-correct headline.

**Fix.** `nis_engine/content_rules.py:205` — `normalize_bullet` now treats em-dash, en-dash, hyphen-dash, AND colon as valid headline separators. First-match-wins. Behavior:

| Input | Output | Why |
|---|---|---|
| `ELEVATED WARMTH: rest` | `ELEVATED WARMTH: rest` | head was ALL CAPS + colon → preserve colon (operator's house style) |
| `premium warmth: rest` | `PREMIUM WARMTH — rest` | head was lowercase → promote to caps + em-dash (consistency) |
| `PREMIUM FEEL — rest` | `PREMIUM FEEL — rest` | unchanged |
| `warm quilted hood` | `WARM QUILTED — hood` | no separator → derive headline (unchanged) |

Also fixed `qa_check`'s validator at the bottom of the same file — it was warning "missing ALL-CAPS headline + em-dash format" on colon-separated bullets even when they were correctly formatted. Now accepts em-dash, en-dash, hyphen-dash, or colon.

---

## Item 12 — All Fields > Content section showed original, not edits

**Confidence going in:** L. Two possible causes documented in Pass 0:
1. The All Fields tab reader pulls `state.generatedContent` (the original) instead of `operator_response.final_value` (the edit).
2. It's a duplicate of the Content tab anyway — agency suggested deleting it.

**Decision.** Option B (deletion). Reasons:
- Title/bullets/description already have their own first-class editor in the Content tab.
- Duplicating them in All Fields invited the desync — the same data living in two surfaces always rots.
- Less code, less surface to maintain.

**Fix.** `templates/index.html` `wsRenderFieldGroups` — removed `{ label: 'Content', icon: '📝', test: f => f.col >= 30 && f.col <= 35 || f.col === 67 }` from the GROUPS array. Pinned an explanatory comment in-place so a future hand doesn't accidentally re-add it. Content fields (cols 30-35, 67) are still reachable via the Content tab — they don't disappear, they just stop being mirrored.

---

## Item 6 — Style blocks blacked out unless hovered

**Confidence going in:** L. Documented in the Pass 0 tracker.

**What I tried.** Searched the entire codebase for:
- `:not(:hover)` selectors → none found
- `opacity:` with values below 0.7 → none on `.ws-style-row` or `.style-card` or `.ws-fields-table` rows
- `filter: brightness(…)` → none
- The commit the tracker suggested as the regression source (`7162149` "M6 UX pass 1: fix dark-theme contrast") → that commit was scoped to the catalog page, not the NIS upload flow

**The honest answer:** I could not reproduce the agency's symptom from the codebase as it stands. The screenshot the agency referenced wasn't checked in, and the surface they were looking at when they reported "blacked out" isn't identified in the feedback xlsx.

**What I shipped.** A defensive CSS guard. `.ws-style-row` and the upload-summary table rows now have explicit `opacity: 1; background: var(--card-bg, #fff)`. If a future theme regression dims them, these explicits will keep them readable. The QA test asserts the guard is in place + that the documenting comment is present (so a "cleanup" pass doesn't strip the guard thinking it's redundant).

**This is not a real fix.** If the agency reports the symptom again on the live Render deploy, I need:
1. A screenshot showing exactly which page + which style block.
2. Browser devtools "computed styles" panel for the dimmed element.
3. The exact PT they were uploading (different PTs hit different render paths).

Then it's a 10-minute fix once the offending selector is identified.

---

## QA harness state

After Pass 5: **20 pass · 0 fail · 2 pending** (Pass 6 strategic only).

| Pass | Items | Status |
|---|---|---|
| 0-4 | 1-3, 5, 8-11, 13-15 + Strategic 1 | done |
| 5 | 6, 7, 12 | done |
| 6 | Strategic 2 — template v2 | last pass remaining |

---

## Bias to flag

Item 6's "fix" is a defensive CSS guard plus a test that locks the guard in place. That's substrate-style discipline (lock in the invariant we want regardless of the specific bug). It is also a way to ship "done" on an L-confidence item without doing the debugging work. Atlas's bias is toward shipping more — flagging this so the user can choose whether to push back on it. If the agency hits the symptom again, the right move is to reopen item 6, not to declare it permanently closed.
