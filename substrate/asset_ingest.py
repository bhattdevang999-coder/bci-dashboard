"""Atlas Creative Studio — asset ingest pipeline.

Scans the workspace for Novelle assets, infers asset_type / tags / surfaces /
status from path + filename patterns, and produces a classification record per
file. Designed to be:

  - Idempotent. Re-running on the same file produces the same record.
  - Pure-function for the classifier (no DB writes). The DB write path is
    a separate `apply_to_substrate(records)` function so we can dry-run.
  - Conservative. When in doubt, mark `asset_type=unknown` and `status=draft`
    rather than guess. The operator stars/approves manually after review.

Usage:
    from substrate.asset_ingest import classify_workspace, apply_to_substrate

    records = classify_workspace("/home/user/workspace")
    # Review records as JSON, then:
    apply_to_substrate(records, workspace_id="novelle")

Per CONTINUOUS_LEARNING_ARCHITECTURE.md v1.2 — M7 Day 2.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("atlas.substrate.asset_ingest")


# =============================================================================
# Asset record — what one classified file looks like before DB write.
# =============================================================================

@dataclass
class AssetRecord:
    file_path: str
    workspace_id: str = "novelle"

    # Image library core
    asset_type: str = "unknown"          # see ASSET_TYPES below
    status: str = "draft"                # draft | approved | archived
    starred: bool = False

    # Derived metadata
    file_name: str = ""
    file_hash: str = ""                  # SHA-256 of bytes (for dedup)
    bytes_size: int = 0
    mime_type: str = ""

    # AI-generation hints (when filename pattern suggests it)
    ai_generated: bool = False
    generation_model: Optional[str] = None
    generation_prompt: Optional[str] = None

    # Brand voice line paired with this asset (filled when known)
    brand_voice_line: Optional[str] = None

    # Tags (key -> list of values)
    tags: dict[str, list[str]] = field(default_factory=dict)

    # Surfaces (list of (surface_type, surface_ref) tuples)
    surfaces: list[tuple[str, str]] = field(default_factory=list)

    # Provenance / version
    parent_image_id: Optional[str] = None    # set later if a parent is found
    notes: str = ""


# =============================================================================
# Vocabularies (kept here so classifier rules can be reviewed in one place).
# =============================================================================

ASSET_TYPES = (
    "hero",                 # full editorial hero shots
    "flatlay",              # flat product photography
    "infographic",          # text+image gallery infographic
    "model",                # on-model product shots
    "city",                 # NY location editorial
    "logo",                 # logos, monograms, lockups
    "video",                # video content
    "story_frame",          # IG story 9:16
    "reel_cover",           # IG reel cover frame
    "size_guide",           # size charts
    "aplus_module",         # A+ premium content panels
    "pdp_gallery",          # Amazon PDP gallery slot
    "colorways",            # 4-color flatlays
    "construction",         # waistband / pocket / fabric / dim diagrams
    "highlight_cover",      # IG highlight cover squares
    "lifestyle",            # 6am/9am/6pm/9pm scene shots
    "screenshot",           # rendered PDP page screenshots
    "unknown",
)

# Filename → asset_type rules. Order matters; first match wins.
# Patterns are evaluated against the lowercased basename.
TYPE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(highlight_\d|highlight_)"),                "highlight_cover"),
    (re.compile(r"(reel_cover|reel_video)"),                   "reel_cover"),
    (re.compile(r"(profile_logo|ig_profile_logo)"),            "logo"),
    (re.compile(r"^story[1-9]_|story_|^story\d+"),             "story_frame"),
    (re.compile(r"(_logo|^logo_|logo_horiz|logo_stacked|n_monogram|logo_official|logo_on_green|logo_white|logo_dark|logo_pink|logo_ink)"), "logo"),
    (re.compile(r"^(infographic|10_infographic|11_infographic|12_infographic)"), "infographic"),
    (re.compile(r"_infographic_|infographic_"),                "infographic"),
    (re.compile(r"(size_guide|size_chart|size_gallery)"),      "size_guide"),
    (re.compile(r"(stretch_diagram|dims_diagram|fabric_demo|aplus_dims|aplus_stretch)"), "construction"),
    (re.compile(r"(jpocket|pocket_detail|pocket_keys|pocket_airpods|aplus_pocket|^04_pocket|waistband|fabric_detail|fabric_stretch)"), "construction"),
    (re.compile(r"(colorway|flatlay_4color|^08_colorways|aplus_colorways)"), "colorways"),
    (re.compile(r"(brooklyn|aplus_brooklyn)"),                 "city"),
    (re.compile(r"(soho|aplus_soho|hero_ny|ny_hero|hero_night_nyc|aplus_night|aplus_banner|aplus_ny|hero_chip|night_nyc|manhattan|aplus_closer)"), "city"),
    (re.compile(r"(yoga_studio|aplus_yoga|morning_coffee|aplus_coffee|life_\d+|lifestyle_\d+|aplus_6am|aplus_9am|aplus_6pm|aplus_9pm)"), "lifestyle"),
    (re.compile(r"(squat|kettlebell|bodyweight|^07_squat|aplus_bend)"), "model"),
    (re.compile(r"(^01_front|^02_side|^03_back|^08_back|product_front|product_back|back_onmodel|side_onmodel|back_walking|side_profile|true_side|slot1_model|slot1_white|^05_features|hero_mockup|^pdp_(front|back|hero))"), "model"),
    (re.compile(r"(^06_fabric|aplus_fabric|^_fabric)"),         "construction"),
    (re.compile(r"(price_proof|same_factory|honest_price)"),   "infographic"),
    (re.compile(r"(\.mp4$)"),                                  "video"),
    (re.compile(r"(pdp_live|pdp_v2_s\d|pdp_review|v2_s\d)"),   "screenshot"),
    (re.compile(r"(post_hero|day1_post|hero$|classic_hero|pdp_hero)"), "hero"),
    (re.compile(r"(classic_versatility_street|pdp_yoga_lifestyle)"), "lifestyle"),
    (re.compile(r"classic_4color_flatlay"),                    "colorways"),
    (re.compile(r"classic_clean_line"),                        "construction"),
    (re.compile(r"(pdp_front_full|pdp_back)"),                 "model"),
    (re.compile(r"^novelle_slot5_"),                           "pdp_gallery"),
    (re.compile(r"(^aplus_)"),                                 "aplus_module"),
    (re.compile(r"(^\d{2}_)"),                                 "pdp_gallery"),  # ordered slot images
]


# Tag rules. Each rule returns dict[key]=list[values] when its pattern matches.
TAG_RULES: list[tuple[re.Pattern[str], dict[str, list[str]]]] = [
    # Subject — places (NY-specific)
    (re.compile(r"brooklyn"),       {"subject_place": ["brooklyn-bridge"]}),
    (re.compile(r"soho"),           {"subject_place": ["soho"]}),
    (re.compile(r"manhattan|empire|night_nyc|ny_hero|hero_ny|aplus_banner|aplus_night|rooftop"), {"subject_place": ["manhattan-rooftop"]}),
    (re.compile(r"yoga_studio|aplus_yoga"),     {"subject_place": ["brooklyn-yoga-studio"]}),
    (re.compile(r"morning_coffee|aplus_coffee"),{"subject_place": ["west-village-coffee"]}),

    # Subject — product detail
    (re.compile(r"jpocket|pocket_detail|aplus_pocket"),  {"subject_object": ["pocket-iphone"]}),
    (re.compile(r"pocket_keys"),                {"subject_object": ["pocket-keys"]}),
    (re.compile(r"pocket_airpods"),             {"subject_object": ["pocket-airpods"]}),
    (re.compile(r"waistband"),                  {"subject_object": ["waistband"]}),
    (re.compile(r"fabric_stretch|stretch_diagram|fabric_detail|aplus_fabric|aplus_stretch"), {"subject_object": ["fabric-stretch"]}),
    (re.compile(r"squat|kettlebell|bodyweight|aplus_bend"),    {"subject_object": ["squat-action"]}),
    (re.compile(r"colorway|flatlay_4color"),    {"subject_object": ["colorways-4"]}),
    (re.compile(r"dims_diagram|aplus_dims"),    {"subject_object": ["construction-dims"]}),

    # Time-of-day for lifestyle
    (re.compile(r"6am"),  {"time_of_day": ["6am-morning"]}),
    (re.compile(r"9am"),  {"time_of_day": ["9am-coffee"]}),
    (re.compile(r"6pm"),  {"time_of_day": ["6pm-evening"]}),
    (re.compile(r"9pm"),  {"time_of_day": ["9pm-night"]}),
    (re.compile(r"12pm"), {"time_of_day": ["12pm-midday"]}),

    # Style cues
    (re.compile(r"_v\d|_v\d_|v\d\.|v\d_"),       {"style": ["versioned"]}),  # has explicit version suffix
    (re.compile(r"clean|white_clean|white_bg"),  {"style": ["clean-white-bg"]}),
    (re.compile(r"premium|night_nyc|hero_ny|aplus_banner"),  {"style": ["editorial-warm"]}),
    (re.compile(r"infographic"),                 {"style": ["editorial-infographic"]}),

    # Palette
    (re.compile(r"on_green|logo_on_green"),       {"palette": ["dark-forest-green"]}),
    (re.compile(r"cream|highlight_"),             {"palette": ["cream-paper"]}),
    (re.compile(r"white_bg|white_clean|01_front_white|slot1_white|slot1_model"), {"palette": ["pure-white"]}),
]


# Surface rules. Each rule maps to (surface_type, surface_ref_template).
# {name} placeholders interpolate from the parent path or matched groups.
def derive_surfaces(rel_path: str, basename: str) -> list[tuple[str, str]]:
    """Return list of (surface_type, surface_ref) tuples for this asset."""
    surfaces: list[tuple[str, str]] = []
    p = rel_path.lower()
    n = basename.lower()

    # Amazon PDP surfaces — novelle-pdp-v2/img/{XX_name.jpg}
    if "novelle-pdp-v2/img/" in p:
        m = re.match(r"(\d{2})_", n)
        if m:
            surfaces.append(("pdp_gallery", f"velune_v3_slot_{int(m.group(1))}"))
        elif n.startswith("aplus_"):
            mod = n.replace("aplus_", "").rsplit(".", 1)[0]
            surfaces.append(("aplus_module", f"velune_v3_{mod}"))
        elif n.startswith("logo"):
            surfaces.append(("brand_logo", f"velune_v3_{n.rsplit('.',1)[0]}"))
        elif "product_video" in n:
            surfaces.append(("pdp_video", "velune_v3"))

    # Legacy Velune PDPs / storefront
    if "novelle-amazon-pdp/img/" in p:
        if re.match(r"\d{2}_", n):
            surfaces.append(("pdp_gallery", f"velune_legacy_{n.rsplit('.',1)[0]}"))
        elif n.startswith("aplus_"):
            surfaces.append(("aplus_module", f"velune_legacy_{n.rsplit('.',1)[0]}"))
        elif n.startswith("life_"):
            surfaces.append(("aplus_module", f"velune_legacy_{n.rsplit('.',1)[0]}"))
        elif n.startswith("logo"):
            surfaces.append(("brand_logo", f"velune_legacy_{n.rsplit('.',1)[0]}"))

    if "novelle-velune-pdp/img/" in p or "novelle-velune-storefront/img/" in p:
        kind = "pdp" if "novelle-velune-pdp" in p else "storefront"
        surfaces.append((f"velune_{kind}", n.rsplit(".", 1)[0]))

    # IG Day 1
    if "novelle_ig_day1/" in p:
        if "post_hero" in n:
            surfaces.append(("ig_feed", "day1_pinned_hero"))
        elif n.startswith("story") or "_shes_here" in n or "_tag_woman" in n or "_shop_now" in n:
            m = re.match(r"story(\d+)_", n)
            ref = f"day1_story_{m.group(1)}" if m else f"day1_{n.rsplit('.',1)[0]}"
            surfaces.append(("ig_story", ref))
        elif n.startswith("highlight_"):
            label = n.replace("highlight_", "").rsplit(".", 1)[0]
            surfaces.append(("highlight_cover", label))
        elif "profile_logo" in n:
            surfaces.append(("ig_profile_photo", "current"))

    # IG Day 2
    if "novelle_ig_day2/" in p:
        m = re.match(r"story(\d+)_", n)
        ref = f"day2_story_{m.group(1)}" if m else f"day2_{n.rsplit('.',1)[0]}"
        surfaces.append(("ig_story", ref))

    # IG Day 3
    if "novelle_ig_day3/" in p:
        if "reel_cover" in n:
            surfaces.append(("ig_reel_cover", "day3_fabric_demo"))
        elif "reel_video" in n:
            surfaces.append(("ig_reel_video", "day3_fabric_demo"))

    return surfaces


# Voice line associations — when a filename suggests a known headline pairing.
VOICE_LINE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(ny_hero|hero_night_nyc|aplus_banner|day1_post_hero|story1_shes_here)"), "She doesn't explain herself."),
    (re.compile(r"(aplus_closer|story3_shop_now)"),                "Come to me."),
    (re.compile(r"(price_proof)"),                                  "$98 fabric. $26.99 price."),
    (re.compile(r"(brooklyn|aplus_brooklyn)"),                     "From mat to Manhattan."),
    (re.compile(r"(infographic_fabric|fabric_stretch|stretch_diagram)"), "Buttery soft. Second skin."),
    (re.compile(r"(infographic_pocket|jpocket|pocket_detail|pocket_keys|pocket_airpods)"),  "Phone. Keys. Cards. Nothing falls out."),
    (re.compile(r"(infographic_squat|aplus_bend|squat_25lb|bodyweight_squat)"), "Squat-proof. Full depth."),
    (re.compile(r"(dims_diagram|aplus_dims)"),                     "Engineered for movement."),
]


# =============================================================================
# Classifier
# =============================================================================

def classify_one(file_path: Path, workspace_root: Path, workspace_id: str = "novelle") -> AssetRecord:
    """Classify a single file. Returns AssetRecord with type, tags, surfaces."""
    rel = str(file_path.relative_to(workspace_root))
    basename = file_path.name
    n = basename.lower()

    rec = AssetRecord(
        file_path=str(file_path),
        workspace_id=workspace_id,
        file_name=basename,
        bytes_size=file_path.stat().st_size if file_path.exists() else 0,
    )

    # MIME from extension
    ext = file_path.suffix.lower()
    rec.mime_type = {
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".mp4":  "video/mp4",
        ".gif":  "image/gif",
    }.get(ext, "application/octet-stream")

    # File hash (cheap on small workspace; defer if perf becomes an issue)
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        rec.file_hash = h.hexdigest()
    except Exception:
        rec.file_hash = ""

    # asset_type — first matching rule wins
    for pat, atype in TYPE_RULES:
        if pat.search(n):
            rec.asset_type = atype
            break

    # ai_generated — anything we generated lives in workspace; assume true
    # for files matching our naming conventions (novelle_*, generated assets).
    # Conservative: only flag true for files with obvious gen patterns.
    if any(k in n for k in ("hero_premium", "infographic_", "slot1_model", "slot1_white",
                              "ny_hero", "fabric_demo", "pocket_keys", "pocket_airpods",
                              "price_proof", "highlight_", "size_guide_v")):
        rec.ai_generated = True
        rec.generation_model = "gpt_image_2"  # default; overridden by Day 3 if logged

    # tags from rules
    for pat, kv in TAG_RULES:
        if pat.search(n):
            for k, vs in kv.items():
                rec.tags.setdefault(k, [])
                for v in vs:
                    if v not in rec.tags[k]:
                        rec.tags[k].append(v)

    # source-folder context tag (helps query "all v3 PDP assets")
    if "novelle-pdp-v2/img" in rel:
        rec.tags.setdefault("source_folder", []).append("velune_v3_pdp")
    elif "novelle-amazon-pdp/img" in rel:
        rec.tags.setdefault("source_folder", []).append("velune_legacy_pdp")
    elif "novelle-velune-pdp/img" in rel:
        rec.tags.setdefault("source_folder", []).append("velune_pdp_old")
    elif "novelle-velune-storefront/img" in rel:
        rec.tags.setdefault("source_folder", []).append("velune_storefront")
    elif "novelle_ig_day1" in rel:
        rec.tags.setdefault("source_folder", []).append("ig_day1")
    elif "novelle_ig_day2" in rel:
        rec.tags.setdefault("source_folder", []).append("ig_day2")
    elif "novelle_ig_day3" in rel:
        rec.tags.setdefault("source_folder", []).append("ig_day3")

    # surfaces
    rec.surfaces = derive_surfaces(rel, basename)

    # voice line
    for pat, line in VOICE_LINE_RULES:
        if pat.search(n):
            rec.brand_voice_line = line
            break

    # status — simple inference. Files in v3 PDP folder are 'approved' (used in
    # the actual built PDP). IG launch packs are 'approved'. Everything else
    # is 'draft' until operator stars it.
    if "novelle-pdp-v2/img" in rel or "novelle_ig_day" in rel:
        rec.status = "approved"
    elif "novelle-amazon-pdp/img" in rel or "novelle-velune-" in rel:
        # Legacy. Mark archived so it doesn't pollute the default view.
        rec.status = "archived"
    else:
        rec.status = "draft"

    return rec


def classify_workspace(workspace_root: str | Path = "/home/user/workspace",
                       workspace_id: str = "novelle") -> list[AssetRecord]:
    """Walk the workspace and classify every Novelle asset.

    Files included:
      - Workspace root: novelle_*.{png,jpg,jpeg,mp4}
      - novelle-pdp-v2/img/* (current Velune PDP)
      - novelle-amazon-pdp/img/* (legacy Velune PDP)
      - novelle-velune-pdp/img/* + novelle-velune-storefront/img/* (old Velune)
      - novelle_ig_day1, novelle_ig_day2, novelle_ig_day3 (IG launch packs)
    """
    root = Path(workspace_root)
    targets: list[Path] = []

    # Root files
    for ext in ("png", "jpg", "jpeg", "mp4"):
        targets.extend(root.glob(f"novelle_*.{ext}"))

    # Project folders
    for folder in (
        "novelle-pdp-v2/img",
        "novelle-amazon-pdp/img",
        "novelle-velune-pdp/img",
        "novelle-velune-storefront/img",
        "novelle_ig_day1",
        "novelle_ig_day2",
        "novelle_ig_day3",
    ):
        p = root / folder
        if p.exists():
            for f in p.iterdir():
                if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".gif"):
                    targets.append(f)

    # Dedup by absolute path
    seen: set[str] = set()
    unique = []
    for t in targets:
        s = str(t.resolve())
        if s not in seen:
            seen.add(s)
            unique.append(t)

    records: list[AssetRecord] = []
    for f in unique:
        try:
            records.append(classify_one(f, root, workspace_id=workspace_id))
        except Exception as e:
            logger.warning("classify failed for %s: %s", f, e)

    return records


# =============================================================================
# Substrate writer (separated so we can dry-run before touching DB)
# =============================================================================

def apply_to_substrate(records: list[AssetRecord], workspace_id: str = "novelle") -> dict[str, int]:
    """Write classified records to substrate. Idempotent via file_hash UNIQUE.

    Returns a summary dict with counts: inserted, updated, skipped, errored.

    Schema reference: schema.sql v13 — image_library, image_surfaces,
    image_tags. caption_library and pdp_variants are written by other
    modules.
    """
    from .db import get_pool  # local import so this module is testable without psycopg

    pool = get_pool()
    summary = {"inserted": 0, "updated": 0, "skipped": 0, "errored": 0}

    with pool.connection() as conn:
        with conn.cursor() as cur:
            for r in records:
                try:
                    # Upsert image_library by (workspace_id, file_hash)
                    image_id = str(uuid.uuid4())
                    cur.execute("""
                        INSERT INTO image_library
                            (image_id, workspace_id, uploaded_by, file_hash, file_name,
                             mime_type, bytes, storage_url, ai_generated, generation_prompt,
                             generation_model, generation_params, meta,
                             status, starred, asset_type, brand_voice_line)
                        VALUES
                            (%s, %s, 'devang', %s, %s,
                             %s, %s, %s, %s, %s,
                             %s, %s, %s,
                             %s, %s, %s, %s)
                        ON CONFLICT (workspace_id, file_hash) DO UPDATE SET
                            status            = EXCLUDED.status,
                            asset_type        = EXCLUDED.asset_type,
                            brand_voice_line  = EXCLUDED.brand_voice_line,
                            file_name         = EXCLUDED.file_name,
                            storage_url       = EXCLUDED.storage_url
                        RETURNING image_id, (xmax = 0) AS inserted
                    """, (
                        image_id, r.workspace_id, r.file_hash, r.file_name,
                        r.mime_type, r.bytes_size, r.file_path,
                        r.ai_generated, r.generation_prompt,
                        r.generation_model, None, '{}',
                        r.status, r.starred, r.asset_type, r.brand_voice_line,
                    ))
                    row = cur.fetchone()
                    actual_id = row[0]
                    if row[1]:
                        summary["inserted"] += 1
                    else:
                        summary["updated"] += 1

                    # Tags — replace all (idempotent) and re-insert
                    cur.execute("DELETE FROM image_tags WHERE image_id = %s", (actual_id,))
                    for k, vs in r.tags.items():
                        for v in vs:
                            cur.execute(
                                "INSERT INTO image_tags (image_id, tag_key, tag_value) "
                                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                (actual_id, k, v),
                            )

                    # Surfaces — replace all
                    cur.execute(
                        "DELETE FROM image_surfaces WHERE image_id = %s AND workspace_id = %s",
                        (actual_id, r.workspace_id),
                    )
                    for stype, sref in r.surfaces:
                        cur.execute(
                            "INSERT INTO image_surfaces "
                            "(image_id, workspace_id, surface_type, surface_ref, surface_status) "
                            "VALUES (%s, %s, %s, %s, 'live') ON CONFLICT DO NOTHING",
                            (actual_id, r.workspace_id, stype, sref),
                        )

                except Exception as e:
                    logger.warning("apply failed for %s: %s", r.file_path, e)
                    summary["errored"] += 1

        conn.commit()

    return summary


# =============================================================================
# CLI / dry-run helper
# =============================================================================

def to_json_records(records: list[AssetRecord]) -> list[dict[str, Any]]:
    """Convert records to JSON-serializable dicts."""
    out = []
    for r in records:
        d = asdict(r)
        d["surfaces"] = [list(s) for s in r.surfaces]
        out.append(d)
    return out


if __name__ == "__main__":
    import json
    import sys
    records = classify_workspace()
    out = to_json_records(records)

    if "--summary" in sys.argv:
        from collections import Counter
        types = Counter(r.asset_type for r in records)
        statuses = Counter(r.status for r in records)
        ai_gen = sum(1 for r in records if r.ai_generated)
        with_voice = sum(1 for r in records if r.brand_voice_line)
        with_surfaces = sum(1 for r in records if r.surfaces)
        with_tags = sum(1 for r in records if r.tags)

        print(f"Classified {len(records)} assets")
        print(f"\nasset_type breakdown:")
        for t, n in sorted(types.items(), key=lambda x: -x[1]):
            print(f"  {t:20s} {n}")
        print(f"\nstatus breakdown:")
        for s, n in sorted(statuses.items(), key=lambda x: -x[1]):
            print(f"  {s:20s} {n}")
        print(f"\nai_generated:        {ai_gen}/{len(records)}")
        print(f"with brand_voice:    {with_voice}/{len(records)}")
        print(f"with surface links:  {with_surfaces}/{len(records)}")
        print(f"with tags:           {with_tags}/{len(records)}")
    else:
        print(json.dumps(out, indent=2))
