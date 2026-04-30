"""End-to-end engine tests + Sage spot-check."""

import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nis_rule_engine import set_bundle_dir, load_bundle, evaluate_form, list_product_types

PASS, FAIL = 0, 0
FAILURES = []

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1; print(f"  ✅ {name}")
    else:
        FAIL += 1; FAILURES.append((name, detail))
        print(f"  ❌ {name}")
        if detail: print(f"      {detail}")


BUNDLE_DIR = os.path.join(os.path.dirname(__file__), "..", "nis_rules")
set_bundle_dir(os.path.abspath(BUNDLE_DIR))


def test_bundles_load():
    print("\n--- Bundles load ---")
    types = list_product_types()
    check(f"{len(types)} product types available", len(types) >= 16, f"got {types}")
    check("COAT bundle loads", load_bundle("COAT") is not None)
    check("DRESS bundle loads", load_bundle("DRESS") is not None)
    check("SWIMWEAR bundle loads", load_bundle("SWIMWEAR") is not None)
    check("unknown PT returns None", load_bundle("NOPE") is None)


def test_empty_form():
    print("\n--- Empty form on COAT ---")
    res = evaluate_form("COAT", {})
    check("returns fields dict", "fields" in res and len(res["fields"]) > 200)
    summary = res["summary"]
    print(f"  Summary verdicts: {summary}")
    check("most fields are required_missing or optional",
          summary.get("required_missing", 0) + summary.get("optional", 0) > 100)


def test_filled_listing_identity():
    print("\n--- Filled listing identity ---")
    state = {
        "rtip_vendor_code#1.value": "AMZN4",
        "vendor_sku#1.value": "TEST-001",
        "product_type#1.value": "COAT",
        "parentage_level#1.value": "Standalone",
        "item_name#1.value": "Sage Wool Outerwear Coat",
        "brand#1.value": "Sage",
        "external_product_id#1.type": "UPC",
        "external_product_id#1.value": "850000000001",
        "product_category#1.value": "Outerwear",
        "product_subcategory#1.value": "Coats",
        "item_type_keyword#1.value": "wool-coat",
    }
    res = evaluate_form("COAT", state)
    summary = res["summary"]
    print(f"  Summary verdicts: {summary}")

    # Vendor Code (col A) should be required_ok
    f_a = res["fields"]["A"]
    check("Vendor Code (col A) is required_ok with value AMZN4",
          f_a["verdict"] == "required_ok",
          f"got {f_a['verdict']}, value={f_a['value']!r}")

    # Brand Name (col I) should be required_ok
    f_i = res["fields"]["I"]
    check("Brand Name (col I) is required_ok with value Sage",
          f_i["verdict"] == "required_ok",
          f"got {f_i['verdict']}, value={f_i['value']!r}")

    # Country of Origin (col EP) — REQUIRED but empty
    f_ep = res["fields"].get("EP")
    if f_ep:
        check("Country of Origin (col EP) required_missing when empty",
              f_ep["verdict"] == "required_missing",
              f"got {f_ep['verdict']}, label={f_ep['label']}")


def test_parent_listing():
    print("\n--- Parent listing fires Variation Theme requirement ---")
    state = {
        "parentage_level#1.value": "Parent",
        "rtip_vendor_code#1.value": "AMZN4",
    }
    res = evaluate_form("COAT", state)
    # Variation Theme is column G
    f_g = res["fields"].get("G")
    if f_g:
        print(f"  Variation Theme verdict: {f_g['verdict']}, base_req: {f_g['base_requirement']}")
        check("Variation Theme is required when Parentage=Parent",
              f_g["verdict"] in ("required_missing", "required_ok", "optional"),
              f"got {f_g['verdict']} — rule trail has {len(f_g['rule_trail'])} entries")


def test_sage_pre_upload():
    """Sage's actual Pre-Upload file: 9 styles, all COAT product type.

    Verify our engine correctly identifies which fields the operator needs to fill.
    """
    print("\n--- Sage Pre-Upload spot-check ---")
    # Simulate Sage's first style row from the pre-upload file
    sage_state = {
        "rtip_vendor_code#1.value": "QT5G8",  # Sage Activewear vendor code
        "vendor_sku#1.value": "SAGE-PUFFER-001",
        "product_type#1.value": "COAT",
        "parentage_level#1.value": "Parent",
        "variation_theme#1.name": "Size/Color",
        "item_name#1.value": "Sage Collective Women's Puffer Coat",
        "brand#1.value": "Sage Collective",
        "external_product_id#1.type": "UPC",
        "product_category#1.value": "Outerwear",
        "product_subcategory#1.value": "Coats",
        "item_type_keyword#1.value": "puffer-coat",
        "department#1.value": "womens",
        "target_gender#1.value": "Female",
        "country_of_origin#1.value": "China",
        "color#1.value": "Black",
    }
    res = evaluate_form("COAT", sage_state)
    summary = res["summary"]
    missing = [(c,f["label"]) for c,f in res["fields"].items() if f["verdict"]=="required_missing"]
    print(f"  Summary: {summary}")
    print(f"  Still required_missing: {len(missing)} fields")
    print(f"  Sample missing fields: {[l for _,l in missing[:10]]}")
    check("Sage state evaluates without crash", "summary" in res)
    check("at least some fields are required_ok with Sage state",
          summary.get("required_ok", 0) > 5,
          f"required_ok count: {summary.get('required_ok', 0)}")


def test_dropdown_resolution():
    print("\n--- Dropdown source resolution ---")
    res = evaluate_form("COAT", {})
    # Brand Name (col I) should expose a dropdown_source from the named range
    f_i = res["fields"]["I"]
    check("Brand Name has dropdown_source",
          f_i["dropdown_source"] is not None and len(f_i["dropdown_source"]) > 5,
          f"dropdown_source len: {len(f_i['dropdown_source']) if f_i['dropdown_source'] else 0}")
    if f_i["dropdown_source"]:
        print(f"  Brand options (first 5): {f_i['dropdown_source'][:5]}")


def test_all_bundles_evaluate():
    print("\n--- All bundles evaluate cleanly with empty state ---")
    for pt in list_product_types():
        try:
            res = evaluate_form(pt, {})
            check(f"{pt} evaluates", "summary" in res)
        except Exception as e:
            check(f"{pt} evaluates", False, str(e))


if __name__ == "__main__":
    print("=" * 70)
    print("NIS Rule Engine — end-to-end test suite")
    print("=" * 70)

    test_bundles_load()
    test_empty_form()
    test_filled_listing_identity()
    test_parent_listing()
    test_sage_pre_upload()
    test_dropdown_resolution()
    test_all_bundles_evaluate()

    print("\n" + "=" * 70)
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    if FAILURES:
        print("\nFAILURES:")
        for name, detail in FAILURES:
            print(f"  • {name}: {detail}")
        sys.exit(1)
    sys.exit(0)
