"""
NIS Wizard v3 — Flask Backend
The Levy Group — Amazon Intelligence
Port: 5000
"""

import os
import re
import json
import csv
import zipfile
import shutil
import copy
import io
import traceback
from datetime import datetime
from pathlib import Path
from collections import defaultdict

from flask import Flask, request, jsonify, render_template, send_file, abort
from flask_cors import CORS
import openpyxl
from openpyxl.styles import PatternFill, Font, Border, Alignment
from openpyxl.utils import get_column_letter, column_index_from_string

# ── App setup ──────────────────────────────────────────────────────────────────
import threading

BASE_DIR = Path(__file__).parent

# Progress tracking for NIS generation
nis_progress = {"total": 0, "completed": 0, "current_style": "", "status": "idle", "started_at": None}
UPLOAD_TEMPLATES = BASE_DIR / "uploads" / "templates"
UPLOAD_PRODUCTS  = BASE_DIR / "uploads" / "products"
UPLOAD_KEYWORDS  = BASE_DIR / "uploads" / "keywords"
UPLOAD_OUTPUT    = BASE_DIR / "uploads" / "output"
FEEDBACK_FILE    = BASE_DIR / "feedback" / "content_feedback.jsonl"
DEFAULT_TEMPLATE = UPLOAD_TEMPLATES / "Dresses-Training.xlsm"
BRAND_CONFIGS_DIR = BASE_DIR / "brand_configs"

for d in [UPLOAD_TEMPLATES, UPLOAD_PRODUCTS, UPLOAD_KEYWORDS, UPLOAD_OUTPUT, BRAND_CONFIGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
CORS(app)

# ── Brand configs ──────────────────────────────────────────────────────────────
BRAND_CONFIGS = {
    "Stella Parker": {
        "vendor_code_prefix": "FC0C0",
        "vendor_code_full": "Stella Parker Sportswear, us_apparel, FC0C0",
        "default_upf": "30+",
        "default_fabric": "95% Polyester, 5% Spandex",
        "default_coo": "MX",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "Womens",
        "bullet_1_focus": "UPF sun protection",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, UPF {upf}, {color}, {size}",
        "never_words": [],
    },
    "Novelle Fashion": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "79% Nylon, 21% Spandex",
        "default_coo": "BD",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "Womens",
        "bullet_1_focus": "Butterlux fabric softness",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": ["affordable"],
    },
    "Volcom": {
        "vendor_code_prefix": "7E8G6",
        "vendor_code_full": "Volcom, us_apparel, 7E8G6",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "",
        "gender": "",
        "department": "",
        "bullet_1_focus": "Brand lifestyle and quality",
        "title_formula": "{brand} {gender} {style_name} {product_type}",
        "never_words": [],
    },
    "Roxy": {
        "vendor_code_prefix": "PG823",
        "vendor_code_full": "Roxy Women's Swimwear, us_apparel, PG823",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "",
        "gender": "Female",
        "department": "Womens",
        "bullet_1_focus": "Beach/surf lifestyle",
        "title_formula": "{brand} {gender} {style_name} {product_type}",
        "never_words": [],
    },
    "Nautica": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "Womens",
        "bullet_1_focus": "Nautical inspired style",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": [],
    },
    "Ben Sherman": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "Womens",
        "bullet_1_focus": "British mod heritage style",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": [],
    },
    "Spyder": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "Womens",
        "bullet_1_focus": "Performance athletic design",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": [],
    },
    "Tahari": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "Dry Clean",
        "gender": "Female",
        "department": "Womens",
        "bullet_1_focus": "Sophisticated tailored design",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": [],
    },
    "Sage": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "Womens",
        "bullet_1_focus": "Effortless everyday style",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": [],
    },
}

# ── Color maps ─────────────────────────────────────────────────────────────────
COLOR_MAP = {
    "MAUVE": "Pink", "ROSE": "Pink", "BLUSH": "Pink", "PINK": "Pink",
    "CORAL": "Pink", "HOT PINK": "Pink", "MAGENTA": "Pink", "FUCHSIA": "Pink",
    "RED": "Red", "CRIMSON": "Red", "BURGUNDY": "Red", "WINE": "Red",
    "MAROON": "Red", "BRICK": "Red", "CHERRY": "Red",
    "BLUE": "Blue", "NAVY": "Blue", "COBALT": "Blue", "ROYAL BLUE": "Blue",
    "SKY BLUE": "Blue", "PERIWINKLE": "Blue", "DENIM": "Blue", "INDIGO": "Blue",
    "TEAL": "Teal", "TURQUOISE": "Teal", "AQUA": "Teal", "CYAN": "Teal",
    "GREEN": "Green", "OLIVE": "Green", "SAGE": "Green", "FOREST": "Green",
    "EMERALD": "Green", "MINT": "Green", "LIME": "Green", "HUNTER": "Green",
    "KHAKI": "Khaki", "TAN": "Khaki", "CAMEL": "Khaki", "BEIGE": "Beige",
    "IVORY": "Ivory", "CREAM": "Ivory", "OFF WHITE": "Ivory",
    "WHITE": "White", "BRIGHT WHITE": "White",
    "BLACK": "Black", "JET BLACK": "Black", "ONYX": "Black",
    "GREY": "Grey", "GRAY": "Grey", "CHARCOAL": "Grey", "SILVER": "Silver",
    "PURPLE": "Purple", "LAVENDER": "Purple", "VIOLET": "Purple",
    "PLUM": "Purple", "LILAC": "Purple", "VRYVIOLET": "Purple",
    "ORANGE": "Orange", "RUST": "Orange", "PUMPKIN": "Orange",
    "AMBER": "Orange", "TERRACOTTA": "Orange",
    "YELLOW": "Yellow", "GOLD": "Gold", "MUSTARD": "Yellow",
    "BROWN": "Brown", "CHOCOLATE": "Brown", "ESPRESSO": "Brown", "MOCHA": "Brown",
    "MULTI": "Multicolor", "MULTICOLOR": "Multicolor", "PRINT": "Multicolor",
    "COMBO": "Multicolor", "FLORAL": "Multicolor",
}

SIZE_MAP = {
    "XS": "X-Small", "S": "Small", "M": "Medium", "L": "Large",
    "XL": "X-Large", "XXL": "XX-Large", "2XL": "XX-Large",
    "XXXL": "3X-Large", "3XL": "3X-Large", "1X": "1X-Large",
    "2X": "2X-Large", "3X": "3X-Large", "4X": "4X-Large",
    "0X": "0X-Large", "0": "0", "2": "2", "4": "4", "6": "6",
    "8": "8", "10": "10", "12": "12", "14": "14", "16": "16",
}

# ── Template-to-product-type routing ──────────────────────────────────────────
# Maps sub_class values to template product type names
TEMPLATE_PRODUCT_TYPE_MAP = {
    # Dresses
    "Day Dress": "Dresses",
    "Cocktail Dress": "Dresses",
    "Active Dress": "Dresses",
    "Swimdress": "Dresses",
    "Maxi Dress": "Dresses",
    "Mini Dress": "Dresses",
    "Wrap Dress": "Dresses",
    "Shirt Dress": "Dresses",
    "Shift Dress": "Dresses",
    "A-Line Dress": "Dresses",
    "Sundress": "Dresses",
    "Bodycon Dress": "Dresses",
    # Shirts / Other_Shirts
    "Polo": "Other_Shirts",
    "Tee": "Other_Shirts",
    "Shirt": "Other_Shirts",
    "Blouse": "Other_Shirts",
    "Tank": "Other_Shirts",
    # Shorts
    "Board Short": "Shorts",
    "Chino Short": "Shorts",
    # Jackets and Coats
    "Jacket": "Jackets_and_Coats",
    "Coat": "Jackets_and_Coats",
    "Hoodie": "Jackets_and_Coats",
    "Pullover": "Jackets_and_Coats",
    # Skirts
    "Skirt": "Skirts",
}

SUBCLASS_CATEGORY_MAP = {
    "Day Dress": "casual-and-day-dresses",
    "Cocktail Dress": "special-occasion-dresses",
    "Maxi Dress": "maxi-dresses",
    "Mini Dress": "mini-dresses",
    "Active Dress": "active-dresses",
    "Wrap Dress": "wrap-dresses",
    "Shirt Dress": "shirt-dresses",
    "Shift Dress": "casual-and-day-dresses",
    "A-Line Dress": "special-occasion-dresses",
    "Sundress": "casual-and-day-dresses",
    "Bodycon Dress": "casual-and-day-dresses",
}

SUBCLASS_SUBCATEGORY_MAP = {
    "Day Dress": "casual-dresses",
    "Cocktail Dress": "cocktail-and-party-dresses",
    "Maxi Dress": "maxi-dresses",
    "Mini Dress": "mini-dresses",
    "Active Dress": "active-dresses",
    "Wrap Dress": "wrap-dresses",
    "Shirt Dress": "shirt-dresses",
}

DESCRIPTION_OPENERS = [
    "Elevate your wardrobe with the {brand} {style_name} — a versatile piece designed for the modern woman.",
    "Introducing the {brand} {style_name}, where effortless style meets all-day comfort.",
    "Step into confidence with the {brand} {style_name}, crafted for women who refuse to compromise on style.",
    "The {brand} {style_name} is your go-to choice for a polished look from morning to evening.",
    "Discover the {brand} {style_name} — thoughtfully designed for women who love beautiful, functional fashion.",
    "Meet the {brand} {style_name}: a wardrobe essential that blends timeless design with contemporary flair.",
    "The {brand} {style_name} brings together sophisticated design and everyday wearability in one stunning piece.",
    "Designed for the woman on the move, the {brand} {style_name} delivers style without sacrificing comfort.",
]

DESCRIPTION_OPENERS_ROTATION = {}  # style_num -> opener_index

# ── Helper utilities ───────────────────────────────────────────────────────────
def _safe(v):
    return str(v).strip() if v is not None else ""

def normalize_color(raw_color):
    """Map raw color to Amazon color family."""
    if not raw_color:
        return ""
    upper = raw_color.upper().strip()
    for key, val in COLOR_MAP.items():
        if key in upper:
            return val
    # Title case fallback
    return raw_color.title()

def normalize_size(raw_size):
    """Standardize size string."""
    if not raw_size:
        return ""
    return SIZE_MAP.get(str(raw_size).strip().upper(), str(raw_size).strip())

def parse_fabric(raw_fabric):
    """Parse fabric string like '95 POLY 5 SPAN' → '95% Polyester, 5% Spandex'."""
    if not raw_fabric:
        return ""
    abbreviations = {
        "POLY": "Polyester", "SPAN": "Spandex", "COTT": "Cotton",
        "NYLON": "Nylon", "RAYON": "Rayon", "LINEN": "Linen",
        "SILK": "Silk", "WOOL": "Wool", "MODAL": "Modal",
        "ACRY": "Acrylic", "LYOCEL": "Lyocell", "TENCEL": "Tencel",
        "VISCOSE": "Viscose", "BAMBOO": "Bamboo",
    }
    s = str(raw_fabric).strip()
    # Already in percentage format
    if "%" in s:
        return s
    # Try to parse "95 POLY 5 SPAN" format
    parts = re.findall(r'(\d+)\s*([A-Za-z]+)', s)
    if parts:
        result = []
        for pct, fiber in parts:
            full = abbreviations.get(fiber.upper(), fiber.title())
            result.append(f"{pct}% {full}")
        return ", ".join(result)
    return s

def derive_neck_type(style_name):
    """Derive neck type from style name."""
    name = style_name.upper()
    mappings = [
        ("V NECK", "V-Neck"), ("V-NECK", "V-Neck"), ("VNECK", "V-Neck"),
        ("HALTER", "Halter"), ("CREW", "Crew Neck"), ("SCOOP", "Scoop Neck"),
        ("SQUARE", "Square Neck"), ("COWL", "Cowl Neck"), ("MOCK", "Mock Neck"),
        ("TURTLENECK", "Turtleneck"), ("HIGH NECK", "High Neck"),
        ("SWEETHEART", "Sweetheart"), ("OFF THE SHOULDER", "Off Shoulder"),
        ("OFF SHLD", "Off Shoulder"), ("OFF-SHOULDER", "Off Shoulder"),
        ("BAND NECK", "Band Neck"), ("BAND NCK", "Band Neck"),
        ("YOKE NECK", "Yoke Neck"), ("YOKE NCK", "Yoke Neck"),
        ("PINTUCK", "V-Neck"), ("KEYHOLE", "Keyhole"),
    ]
    for pattern, neck in mappings:
        if pattern in name:
            return neck
    return ""

def derive_sleeve_type(style_name):
    """Derive sleeve type from style name."""
    name = style_name.upper()
    mappings = [
        ("SLEEVELESS", "Sleeveless"), ("SLVLES", "Sleeveless"),
        ("SLVLS", "Sleeveless"), ("SLV", "Short Sleeve"),
        ("FLUTTER", "Flutter Sleeve"), ("FLUTTER SLV", "Flutter Sleeve"),
        ("FLUTTER SLEEVE", "Flutter Sleeve"),
        ("RUFFLE SLV", "Ruffle Sleeve"), ("RFL SLV", "Ruffle Sleeve"),
        ("OFF SHOULDER", "Off-Shoulder"), ("OFF SHLD", "Off-Shoulder"),
        ("BALLOON SL", "Balloon Sleeve"), ("CAP SLEEVE", "Cap Sleeve"),
        ("SHORT SLEEVE", "Short Sleeve"), ("LONG SLEEVE", "Long Sleeve"),
        ("3/4 SLEEVE", "3/4 Sleeve"),
    ]
    for pattern, sleeve in mappings:
        if pattern in name:
            return sleeve
    return "Sleeveless"

def derive_silhouette(sub_subclass):
    """Derive silhouette from sub_subclass."""
    if not sub_subclass:
        return ""
    s = str(sub_subclass).strip()
    mapping = {
        "Shift Dress": "Shift",
        "A-Line Dress": "A-Line",
        "Fit & Flare Dress": "Fit & Flare",
        "Dress with Shorts": "Romper/Shortall",
        "Wrap Dress": "Wrap",
        "Sheath Dress": "Sheath",
        "Maxi Dress": "Maxi",
        "Mini Dress": "Mini",
        "Bodycon Dress": "Bodycon",
    }
    return mapping.get(s, s.replace(" Dress", ""))

def style_descriptor_from_name(style_name):
    """Extract a clean style descriptor for use in titles."""
    name = str(style_name).upper()
    # Clean up common abbreviations
    replacements = {
        "SLVLES": "Sleeveless", "SLVLS": "Sleeveless", "SLV": "Sleeve",
        "DRS": "Dress", "DRSS": "Dress", "NCK": "Neck",
        "SHLD": "Shoulder", "RFL": "Ruffle", "BBYDOLL": "Baby Doll",
        "SHRT": "Short", "CINCH WST": "Cinch Waist",
        "TSL": "Tassel", "FR": "Front", "FIT": "Fit",
        "FLR": "Flare", "ZIP": "Zip", "BTN": "Button",
    }
    result = name
    for abbr, full in replacements.items():
        result = re.sub(r'\b' + abbr + r'\b', full, result)
    # Remove "DRESS" from end since product_type covers it
    result = re.sub(r'\bDRESS\b', '', result).strip()
    # Title case
    return result.title().strip()

def generate_title(brand_cfg, brand, style_name, product_type, color, size, upf=""):
    """Generate Amazon-compliant title per brand formula. Max 200 chars."""
    formula = brand_cfg.get("title_formula", "{brand} {style_descriptor} {product_type}, {color}, {size}")
    descriptor = style_descriptor_from_name(style_name)
    
    title = formula.format(
        brand=brand,
        style_descriptor=descriptor,
        style_name=style_name.title(),
        product_type=product_type.title() if product_type else "Dress",
        color=color.title() if color else "",
        size=normalize_size(size),
        upf=upf or brand_cfg.get("default_upf", ""),
        gender=brand_cfg.get("gender", "Women's"),
    )
    # Clean up double spaces, leading/trailing punctuation
    title = re.sub(r'\s+', ' ', title).strip()
    title = re.sub(r',\s*,', ',', title)
    title = re.sub(r',\s*$', '', title)
    return title[:200]

def generate_bullets(brand_cfg, brand, style_name, sub_subclass, fabric, care, color, upf=""):
    """Generate 5 bullet points per brand + style context."""
    silhouette = derive_silhouette(sub_subclass)
    sleeve = derive_sleeve_type(style_name)
    neck = derive_neck_type(style_name)
    focus = brand_cfg.get("bullet_1_focus", "Style and quality")
    actual_fabric = fabric or brand_cfg.get("default_fabric", "")
    actual_care = care or brand_cfg.get("default_care", "Machine Wash")
    actual_upf = upf or brand_cfg.get("default_upf", "")

    # Bullet 1: brand-specific focus
    if "upf" in focus.lower():
        upf_val = actual_upf or "30+"
        b1 = f"UPF {upf_val} SUN PROTECTION — Built with {upf_val} ultraviolet protection factor fabric, this dress shields your skin from harmful UV rays, making it ideal for outdoor activities, beach days, and sunny adventures."
    elif "butterlux" in focus.lower():
        b1 = f"BUTTERLUX FABRIC — Crafted from our signature Butterlux material, this {brand} dress delivers an extraordinarily soft, silky touch against your skin for all-day luxurious comfort."
    elif "beach" in focus.lower() or "surf" in focus.lower():
        b1 = f"SURF & BEACH READY — Designed with {brand}'s surf heritage in mind, this dress combines ocean-inspired style with durable, resort-worthy construction perfect for any beach escape."
    elif "nautical" in focus.lower():
        b1 = f"NAUTICAL INSPIRED — Rooted in {brand}'s rich maritime heritage, this dress features classic nautical design elements that bring timeless, sophisticated style to every occasion."
    elif "british" in focus.lower() or "mod" in focus.lower():
        b1 = f"BRITISH MOD HERITAGE — Influenced by {brand}'s iconic British mod aesthetic, this dress delivers bold, fashion-forward style that stands out from the crowd."
    elif "performance" in focus.lower():
        b1 = f"PERFORMANCE DESIGN — Engineered with {brand}'s performance expertise, this dress combines athletic functionality with fashionable styling for active women on the go."
    elif "tailored" in focus.lower() or "sophisticated" in focus.lower():
        b1 = f"SOPHISTICATED TAILORING — {brand}'s expert tailoring creates a refined, polished silhouette that transitions effortlessly from office to evening occasions."
    else:
        b1 = f"QUALITY CRAFTSMANSHIP — {brand} brings signature quality to every stitch of this {style_name.title()} dress, combining premium materials with expert construction for lasting style."

    # Bullet 2: Style-specific, must vary per style
    style_features = []
    if neck:
        style_features.append(f"flattering {neck} neckline")
    if sleeve and sleeve != "Sleeveless":
        style_features.append(f"{sleeve.lower()} detail")
    elif sleeve == "Sleeveless":
        style_features.append("sleeveless design")
    if "PLEATED" in style_name.upper():
        style_features.append("elegant pleated front")
    if "RUFFLE" in style_name.upper() or "RFL" in style_name.upper():
        style_features.append("playful ruffle accents")
    if "FLUTTER" in style_name.upper():
        style_features.append("delicate flutter sleeves")
    if "PINTUCK" in style_name.upper():
        style_features.append("refined pintuck detailing")
    if "KEYHOLE" in style_name.upper():
        style_features.append("sophisticated keyhole cutout")
    if "COLORBLOCK" in style_name.upper():
        style_features.append("bold colorblock paneling")
    if "SWING" in style_name.upper():
        style_features.append("flirty swing silhouette")
    if "BALLOON" in style_name.upper():
        style_features.append("statement balloon sleeves")
    if "BABYDOLL" in style_name.upper().replace(" ", "") or "BBYDOLL" in style_name.upper():
        style_features.append("romantic baby doll cut")
    if "YOKE" in style_name.upper():
        style_features.append("structured yoke detailing")
    if "LUNA" in style_name.upper():
        style_features.append("graceful feminine silhouette")
    if "CINCH" in style_name.upper():
        style_features.append("cinched waist for definition")
    if "ZIP" in style_name.upper():
        style_features.append("convenient front zip closure")

    feature_str = ", ".join(style_features[:3]) if style_features else "thoughtfully designed details"
    b2 = f"DESIGNED TO IMPRESS — This {style_name.title()} features {feature_str} that create a {silhouette or 'flattering'} silhouette perfect for making a lasting impression wherever you go."

    # Bullet 3: Fit & sizing
    if silhouette:
        fit_desc = f"{silhouette} silhouette"
    else:
        fit_desc = "flattering cut"
    b3 = f"PERFECT FIT & COMFORT — The {fit_desc} is designed to flatter a range of body types, with a relaxed yet refined fit that moves with you throughout the day. Available in sizes XS–3X to ensure every woman finds her perfect match."

    # Bullet 4: Fabric + care
    if actual_fabric:
        b4 = f"PREMIUM FABRIC — Made from {actual_fabric}, this dress offers a smooth, comfortable feel with just the right amount of stretch. {actual_care} for easy home care."
    else:
        b4 = f"EASY CARE FABRIC — Crafted for effortless wearability with a smooth, comfortable construction. {actual_care} for convenient upkeep."

    # Bullet 5: Cross-sell + complete the look
    b5 = f"COMPLETE THE LOOK — Pair this {color.title() if color else ''} dress with strappy sandals and a clutch for evenings out, or dress it down with white sneakers and a denim jacket for a casual daytime look. The perfect versatile addition to your {brand} wardrobe."

    return [b1, b2, b3, b4, b5]

def generate_description(brand_cfg, brand, style_num, style_name, sub_subclass, fabric, care, color, upf=""):
    """Generate product description. Max 2000 chars, uses rotating openers."""
    global DESCRIPTION_OPENERS_ROTATION
    
    silhouette = derive_silhouette(sub_subclass)
    sleeve = derive_sleeve_type(style_name)
    neck = derive_neck_type(style_name)
    actual_fabric = fabric or brand_cfg.get("default_fabric", "")
    actual_care = care or brand_cfg.get("default_care", "Machine Wash")
    actual_upf = upf or brand_cfg.get("default_upf", "")

    # Get opener (rotate per style)
    if style_num not in DESCRIPTION_OPENERS_ROTATION:
        idx = len(DESCRIPTION_OPENERS_ROTATION) % len(DESCRIPTION_OPENERS)
        DESCRIPTION_OPENERS_ROTATION[style_num] = idx
    opener_template = DESCRIPTION_OPENERS[DESCRIPTION_OPENERS_ROTATION[style_num]]
    opener = opener_template.format(brand=brand, style_name=style_name.title())

    parts = [opener]

    # Style details paragraph
    style_details = []
    if neck:
        style_details.append(f"the elegant {neck} neckline frames your face beautifully")
    if sleeve and sleeve != "Sleeveless":
        style_details.append(f"{sleeve.lower()} detailing adds movement and visual interest")
    elif sleeve == "Sleeveless":
        style_details.append("the sleeveless construction keeps you cool and comfortable")
    if silhouette:
        style_details.append(f"the {silhouette} silhouette creates a polished, put-together look")
    
    if style_details:
        parts.append("From " + ", ".join(style_details[:2]) + " — every element is thoughtfully considered.")

    # Fabric paragraph
    if actual_fabric:
        fabric_para = f"Constructed from {actual_fabric}"
        if actual_upf:
            fabric_para += f" with UPF {actual_upf} sun protection built right in"
        fabric_para += f", this dress provides all-day comfort with a flattering drape. {actual_care} for easy upkeep."
        parts.append(fabric_para)

    # Occasion paragraph
    parts.append(f"Versatile enough for brunches, work events, weekend outings, and special occasions, this {brand} dress is a true wardrobe workhorse. Dress it up with heels and statement jewelry, or keep it casual with flat sandals and a crossbody bag.")

    desc = " ".join(parts)
    return desc[:2000]

def generate_title_why(brand_cfg, brand, style_name, title, upf, has_keywords):
    """Generate 'why' explanation for the title."""
    char_count = len(title)
    parts = []
    # Gender format
    parts.append('"Women\'s" format used — outperforms "for Women" in Amazon search CTR.')
    # UPF
    if upf or brand_cfg.get("default_upf"):
        parts.append(f'UPF {upf or brand_cfg.get("default_upf")} placed after brand as lead differentiator — detected in product data.')
    # Keywords
    if has_keywords:
        parts.append('Top keyword from uploaded Helium 10 data incorporated into title.')
    else:
        parts.append('No keyword data uploaded — category defaults used for title structure.')
    parts.append(f'{char_count}/200 characters used.')
    return ' '.join(parts)


def generate_bullet_why(idx, brand_cfg, brand, style_name, sub_subclass, upf, fabric, has_keywords):
    """Generate 'why' explanation for a bullet."""
    focus = brand_cfg.get("bullet_1_focus", "Style and quality")
    if idx == 0:
        if "upf" in focus.lower():
            return f'UPF {upf or brand_cfg.get("default_upf", "30+")} detected in product data. Sun protection leads for this brand based on category positioning — highest differentiator for outdoor/activewear.'
        elif "butterlux" in focus.lower():
            return f'Butterlux fabric is {brand}\'s signature material — used as Bullet 1 per brand config.'
        elif "beach" in focus.lower() or "surf" in focus.lower():
            return f'{brand}\'s surf heritage is the primary brand differentiator — configured as Bullet 1 focus.'
        else:
            return f'Brand quality and craftsmanship leads Bullet 1 per brand config setting: "{focus}".'
    elif idx == 1:
        neck = derive_neck_type(style_name)
        sleeve = derive_sleeve_type(style_name)
        features = []
        if neck: features.append(f'"{neck}" detected in style name')
        if sleeve: features.append(f'"{sleeve}" detected in style name')
        if "PLEATED" in style_name.upper(): features.append('"PLEATED" → hourglass effect copy')
        if "RUFFLE" in style_name.upper() or "RFL" in style_name.upper(): features.append('"RUFFLE" → playful accent copy')
        if features:
            return f'Style-specific features derived from name: {", ".join(features[:2])}. This bullet varies per style to avoid duplicate content.'
        return 'Style details derived from style name analysis. This bullet varies per style to avoid duplicate content.'
    elif idx == 2:
        silhouette = derive_silhouette(sub_subclass)
        return f'Fit & sizing copy generated from silhouette: "{silhouette or "flattering"}". Size range XS–3X included per Amazon best practice for apparel.'
    elif idx == 3:
        actual_fabric = parse_fabric(fabric) or brand_cfg.get("default_fabric", "")
        if actual_fabric:
            return f'Fabric composition "{actual_fabric}" from product data. Care instructions from product data or brand defaults. Premium fabric positioning improves conversion.'
        return f'Fabric information not found in product data — using brand default. Care instructions from brand config. Upload product data with fabric column for style-specific copy.'
    elif idx == 4:
        return 'Cross-sell bullet drives average order value by suggesting complementary styling. Color-specific to each variant (updated per color in NIS output).'
    return ''


def generate_description_why(brand_cfg, style_num, opener_idx, has_keywords):
    """Generate 'why' explanation for description."""
    total_openers = len(DESCRIPTION_OPENERS)
    opener_num = (opener_idx % total_openers) + 1
    parts = [
        f'Opener #{opener_num} of {total_openers} used — rotated per style to avoid duplicate content flags.',
        'Three-paragraph structure: opener + style details + fabric/care + occasion/versatility.',
    ]
    if not has_keywords:
        parts.append('No keyword data uploaded — keyword integration uses category defaults. Upload Helium 10 CSV for optimized keyword placement.')
    return ' '.join(parts)


def generate_keywords_why(brand, keywords_list, result_kw, has_keywords):
    """Generate 'why' explanation for backend keywords."""
    byte_count = len(result_kw.encode('utf-8'))
    term_count = len(result_kw.split()) if result_kw else 0
    if has_keywords:
        top_kw = [k['keyword'] for k in keywords_list[:3]] if keywords_list else []
        return (f'{byte_count}/250 bytes used. {term_count} terms: top keywords from uploaded Helium 10 data '
                f'({(", ".join(top_kw)) or "none"}) plus category defaults. '
                f'Brand name excluded (Amazon penalizes brand repetition in backend keywords).')
    return (f'{byte_count}/250 bytes used. {term_count} terms derived from category defaults + style name analysis. '
            f'No keyword data uploaded — upload Helium 10 CSV for search-volume-ranked backend keywords. '
            f'Brand name excluded per Amazon guidelines.')


def generate_backend_keywords(brand, style_name, sub_subclass, color, fabric, upf=""):
    """Generate backend keywords. Max 250 bytes, lowercase, no brand, no title duplicates."""
    brand_lower = brand.lower()
    
    candidates = [
        "womens dress", "women dress", "ladies dress",
        f"{sub_subclass.lower() if sub_subclass else 'dress'}",
        f"{derive_silhouette(sub_subclass).lower() if sub_subclass else ''} dress",
        f"{derive_sleeve_type(style_name).lower()} dress",
        f"{derive_neck_type(style_name).lower() if derive_neck_type(style_name) else ''} dress",
        "casual dress", "everyday dress", "versatile dress",
        "comfortable dress", "flattering dress", "stylish dress",
        "summer dress", "spring dress", "all season dress",
        "work dress", "office dress", "occasion dress",
        f"women {normalize_color(color).lower() if color else ''} dress",
    ]
    
    if upf:
        candidates.append(f"upf {upf} dress")
        candidates.append("sun protective clothing")
        candidates.append("uv protection dress")
    
    if fabric:
        if "polyester" in fabric.lower():
            candidates.append("polyester dress")
        if "spandex" in fabric.lower() or "elastane" in fabric.lower():
            candidates.append("stretchy dress")
        if "nylon" in fabric.lower():
            candidates.append("nylon dress")
    
    # Filter: no brand name, no empty, no duplicates
    seen = set()
    result = []
    for kw in candidates:
        kw = kw.strip().lower()
        if not kw or brand_lower in kw or kw in seen:
            continue
        # Remove empty/whitespace-only from cleaning
        if re.match(r'^\s*dress$', kw):
            continue
        seen.add(kw)
        result.append(kw)
    
    # Join and cap at 250 bytes
    joined = " ".join(result)
    while len(joined.encode('utf-8')) > 250 and result:
        result.pop()
        joined = " ".join(result)
    
    return joined

# ── Template parsing ───────────────────────────────────────────────────────────
def parse_template_columns(template_path):
    """Parse .xlsm template rows 3 (headers) and 4 (field IDs). Returns col_map."""
    wb = openpyxl.load_workbook(template_path, keep_vba=True, read_only=True)
    ws = None
    for name in wb.sheetnames:
        if "template" in name.lower() or "dress" in name.lower():
            ws = wb[name]
            break
    if ws is None:
        ws = wb.active
    
    col_map = {}
    for col in range(1, (ws.max_column or 300) + 1):
        header = _safe(ws.cell(row=3, column=col).value)
        field_id = _safe(ws.cell(row=4, column=col).value)
        if header or field_id:
            col_map[col] = {"header": header, "field_id": field_id}
    
    wb.close()
    return col_map

_template_col_map_cache = {}

def get_template_col_map(template_path=None):
    if template_path is None:
        template_path = str(DEFAULT_TEMPLATE)
    if template_path not in _template_col_map_cache:
        _template_col_map_cache[template_path] = parse_template_columns(template_path)
    return _template_col_map_cache[template_path]

def find_col_by_field_id(col_map, field_id_pattern):
    """Find column number(s) by field_id pattern match."""
    results = []
    for col, info in col_map.items():
        if field_id_pattern.lower() in info["field_id"].lower():
            results.append(col)
    return results

def find_col_exact(col_map, field_id):
    """Find exact column number by field_id."""
    for col, info in col_map.items():
        if info["field_id"].lower() == field_id.lower():
            return col
    return None

# ── Product data parsing ───────────────────────────────────────────────────────
PRODUCT_HEADER_ALIASES = {
    "season code": "season_code",
    "season added to amzn": "season_added",
    "brand": "brand",
    "division": "division",
    "sub-class name": "subclass",
    "sub sub-class name": "sub_subclass",
    "style #": "style_num",
    "style name": "style_name",
    "style description": "style_desc",
    "color code": "color_code",
    "list price": "list_price",
    "cost price": "cost_price",
    "color name": "color_name",
    "product - size": "size",
    "size": "size",
    "upc": "upc",
    "casin": "casin",
    "child asin": "child_asin",
    "parent asin": "parent_asin",
    "model name": "model_name",
    "case pack": "case_pack",
    "country of origin": "coo",
    "fabric": "fabric",
    "material": "fabric",
    "care": "care",
    "care instructions": "care",
    "upf": "upf",
    "sku": "sku",
}

def fuzzy_match_headers(headers):
    """Fuzzy-match raw headers to internal field names."""
    mapping = {}  # internal_field -> col_index (0-based)
    for idx, h in enumerate(headers):
        if not h:
            continue
        key = str(h).strip().lower()
        if key in PRODUCT_HEADER_ALIASES:
            internal = PRODUCT_HEADER_ALIASES[key]
            if internal not in mapping:
                mapping[internal] = idx
    return mapping

def parse_product_file(file_path):
    """Parse product Excel/CSV file. Returns (rows, errors, warnings)."""
    ext = Path(file_path).suffix.lower()
    raw_rows = []
    
    if ext in [".xlsx", ".xls", ".xlsm"]:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        wb.close()
        
        # Find header row
        header_row_idx = None
        for i, row in enumerate(all_rows):
            non_empty = sum(1 for c in row if c is not None)
            # Look for a row that has many non-empty cells and contains typical headers
            if non_empty >= 5:
                row_str = " ".join(str(c).lower() for c in row if c is not None)
                if any(kw in row_str for kw in ["style", "brand", "color", "size", "upc", "price"]):
                    header_row_idx = i
                    break
        
        if header_row_idx is None:
            return [], ["Could not find header row in file"], []
        
        headers = [str(c).strip() if c is not None else "" for c in all_rows[header_row_idx]]
        for row in all_rows[header_row_idx + 1:]:
            if any(c is not None for c in row):
                raw_rows.append(row)
        
    elif ext in [".csv", ".tsv"]:
        delimiter = "\t" if ext == ".tsv" else ","
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f, delimiter=delimiter)
            all_csv = list(reader)
        if not all_csv:
            return [], ["Empty CSV file"], []
        headers = all_csv[0]
        raw_rows = [tuple(row) for row in all_csv[1:]]
    else:
        return [], [f"Unsupported file type: {ext}"], []
    
    col_map = fuzzy_match_headers(headers)
    
    errors = []
    warnings = []
    styles = {}  # style_num -> {style_info, variants:[]}
    
    for row_idx, row in enumerate(raw_rows, start=1):
        def get(field):
            idx = col_map.get(field)
            if idx is None or idx >= len(row):
                return ""
            return _safe(row[idx])
        
        style_num = get("style_num")
        style_name = get("style_name") or get("style_desc")
        brand = get("brand")
        subclass = get("subclass")
        sub_subclass = get("sub_subclass")
        color_name = get("color_name")
        color_code = get("color_code")
        size = get("size")
        upc = get("upc")
        list_price = get("list_price")
        cost_price = get("cost_price")
        parent_asin = get("parent_asin")
        child_asin = get("child_asin")
        model_name = get("model_name")
        season_code = get("season_code")
        fabric = get("fabric")
        care = get("care")
        upf = get("upf")
        coo = get("coo")
        sku = get("sku")
        
        if not style_num:
            continue
        
        # Validation
        row_errors = []
        row_warnings = []
        
        if not style_name:
            row_errors.append(f"Row {row_idx}: Missing style name for style {style_num}")
        
        # UPC validation
        if upc:
            upc_clean = re.sub(r'\D', '', str(upc))
            if len(upc_clean) != 12:
                row_errors.append(f"Row {row_idx}: UPC '{upc}' is not 12 digits (style {style_num}, color {color_name}, size {size})")
        else:
            row_warnings.append(f"Row {row_idx}: Missing UPC for style {style_num}, color {color_name}, size {size}")
        
        # Price validation
        try:
            lp = float(list_price) if list_price else 0
            cp = float(cost_price) if cost_price else 0
            if lp > 0 and cp > 0:
                if cp > lp:
                    row_errors.append(f"Row {row_idx}: Cost (${cp}) > List price (${lp}) for style {style_num}")
                elif cp > 0.8 * lp:
                    row_warnings.append(f"Row {row_idx}: CRITICAL: Cost (${cp}) is >80% of List (${lp}) for style {style_num}")
                elif cp > 0.6 * lp:
                    row_warnings.append(f"Row {row_idx}: Cost (${cp}) is >60% of List (${lp}) for style {style_num}")
        except (ValueError, TypeError):
            pass
        
        errors.extend(row_errors)
        warnings.extend(row_warnings)
        
        # Build style entry
        if style_num not in styles:
            styles[style_num] = {
                "style_num": style_num,
                "style_name": style_name or style_num,
                "brand": brand,
                "subclass": subclass,
                "sub_subclass": sub_subclass,
                "list_price": list_price,
                "cost_price": cost_price,
                "parent_asin": parent_asin,
                "model_name": model_name,
                "season_code": season_code,
                "fabric": fabric,
                "care": care,
                "upf": upf,
                "coo": coo,
                "variants": [],
                "errors": [],
                "warnings": [],
            }
        
        styles[style_num]["errors"].extend(row_errors)
        styles[style_num]["warnings"].extend(row_warnings)
        
        # Deduplicate style-level info
        if style_name and not styles[style_num]["style_name"]:
            styles[style_num]["style_name"] = style_name
        if fabric and not styles[style_num]["fabric"]:
            styles[style_num]["fabric"] = fabric
        if care and not styles[style_num]["care"]:
            styles[style_num]["care"] = care
        if upf and not styles[style_num]["upf"]:
            styles[style_num]["upf"] = upf
        if coo and not styles[style_num]["coo"]:
            styles[style_num]["coo"] = coo
        if parent_asin and not styles[style_num]["parent_asin"]:
            styles[style_num]["parent_asin"] = parent_asin
        
        variant = {
            "color_name": color_name,
            "color_code": color_code,
            "size": size,
            "upc": upc,
            "child_asin": child_asin,
            "sku": sku,
            "errors": row_errors,
            "warnings": row_warnings,
        }
        styles[style_num]["variants"].append(variant)
    
    return list(styles.values()), errors, warnings

# ── Session state (in-memory, per app restart) ─────────────────────────────────
session_data = {
    "brand": None,
    "vendor_code": None,
    "template_path": str(DEFAULT_TEMPLATE),
    "col_map": None,
    "product_file": None,
    "styles": [],
    "keywords": [],
    "analytics": [],
    "generated_content": {},
    # Multi-template: maps product_type -> path, e.g. {"Dresses": "/path/to/Dresses.xlsm"}
    "templates": {},
}

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/session-restore")
def session_restore():
    """Return current session state so frontend can restore after page refresh."""
    return jsonify({
        "brand": session_data.get("brand"),
        "vendor_code": session_data.get("vendor_code"),
        "template_loaded": session_data.get("col_map") is not None,
        "template_columns": len(session_data.get("col_map") or {}),
        "styles_count": len(session_data.get("styles", [])),
        "styles": session_data.get("styles", []),
        "keywords_loaded": len(session_data.get("keywords", [])) > 0,
        "analytics_loaded": len(session_data.get("analytics", [])) > 0,
        "content_generated": len(session_data.get("generated_content", {})) > 0,
        "generated_content": session_data.get("generated_content", {}),
        "brand_config": BRAND_CONFIGS.get(session_data.get("brand"), {}),
    })

@app.route("/api/session-reset", methods=["POST"])
def session_reset():
    """Clear all session state for a fresh start."""
    session_data["brand"] = None
    session_data["vendor_code"] = None
    session_data["template_path"] = str(DEFAULT_TEMPLATE)
    session_data["col_map"] = None
    session_data["product_file"] = None
    session_data["styles"] = []
    session_data["keywords"] = []
    session_data["analytics"] = []
    session_data["generated_content"] = {}
    session_data["templates"] = {}
    return jsonify({"ok": True})

@app.route("/api/brand-config", methods=["POST"])
def brand_config():
    data = request.get_json(force=True)
    brand = data.get("brand", "")
    if brand not in BRAND_CONFIGS:
        return jsonify({"error": f"Unknown brand: {brand}"}), 400
    cfg = BRAND_CONFIGS[brand]
    session_data["brand"] = brand
    session_data["vendor_code"] = data.get("vendor_code", cfg.get("vendor_code_full", ""))
    return jsonify({"brand": brand, "config": cfg})

@app.route("/api/upload-template", methods=["POST"])
def upload_template():
    if "file" not in request.files:
        # Use default template
        template_path = str(DEFAULT_TEMPLATE)
        session_data["template_path"] = template_path
        session_data["col_map"] = get_template_col_map(template_path)
        col_count = len(session_data["col_map"])
        return jsonify({
            "template": "Dresses-Training.xlsm",
            "columns_mapped": col_count,
            "message": f"Dresses template — {col_count} columns mapped",
            "template_path": template_path,
        })
    
    f = request.files["file"]
    if not f.filename.endswith(".xlsm"):
        return jsonify({"error": "Template must be a .xlsm file"}), 400
    
    save_path = UPLOAD_TEMPLATES / f.filename
    f.save(str(save_path))
    
    try:
        col_map = get_template_col_map(str(save_path))
        session_data["template_path"] = str(save_path)
        session_data["col_map"] = col_map
        return jsonify({
            "template": f.filename,
            "columns_mapped": len(col_map),
            "message": f"{f.filename} — {len(col_map)} columns mapped",
            "template_path": str(save_path),
        })
    except Exception as e:
        return jsonify({"error": f"Failed to parse template: {str(e)}"}), 500


@app.route("/api/upload-category-template", methods=["POST"])
def upload_category_template():
    """Upload a .xlsm template for a specific product type (multi-template support)."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    f = request.files["file"]
    product_type = request.form.get("product_type", "").strip()
    
    if not f.filename.endswith(".xlsm"):
        return jsonify({"error": "Template must be a .xlsm file"}), 400
    if not product_type:
        return jsonify({"error": "product_type is required"}), 400
    
    # Save as {product_type}.xlsm
    safe_name = re.sub(r'[^\w]', '_', product_type)
    save_path = UPLOAD_TEMPLATES / f"{safe_name}.xlsm"
    f.save(str(save_path))
    
    try:
        col_map = get_template_col_map(str(save_path))
        # Register in session multi-template map
        session_data["templates"][product_type] = str(save_path)
        # If this is the first/only template, also set as default
        if not session_data.get("col_map"):
            session_data["template_path"] = str(save_path)
            session_data["col_map"] = col_map
        return jsonify({
            "product_type": product_type,
            "template": f.filename,
            "columns_mapped": len(col_map),
            "message": f"{product_type} template loaded — {len(col_map)} columns mapped",
            "loaded_templates": list(session_data["templates"].keys()),
        })
    except Exception as e:
        return jsonify({"error": f"Failed to parse template: {str(e)}"}), 500


@app.route("/api/templates")
def list_templates():
    """Return all loaded templates with their product types."""
    templates = session_data.get("templates", {})
    result = []
    for pt, path in templates.items():
        p = Path(path)
        result.append({
            "product_type": pt,
            "filename": p.name,
            "exists": p.exists(),
        })
    # Also include the default template if no multi-templates are registered
    if not result and session_data.get("template_path"):
        p = Path(session_data["template_path"])
        result.append({
            "product_type": "Dresses",
            "filename": p.name,
            "exists": p.exists(),
            "is_default": True,
        })
    return jsonify({"templates": result})


@app.route("/api/upload-product-data", methods=["POST"])
def upload_product_data():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    f = request.files["file"]
    ext = Path(f.filename).suffix.lower()
    if ext not in [".xlsx", ".xls", ".xlsm", ".csv", ".tsv"]:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400
    
    save_path = UPLOAD_PRODUCTS / f.filename
    f.save(str(save_path))
    session_data["product_file"] = str(save_path)
    
    try:
        styles, errors, warnings = parse_product_file(str(save_path))
        session_data["styles"] = styles
        
        total_variants = sum(len(s["variants"]) for s in styles)
        
        # Detect which product types are present but have no template loaded
        present_types = set()
        type_counts = defaultdict(int)
        for s in styles:
            pt = TEMPLATE_PRODUCT_TYPE_MAP.get(s.get("subclass", ""), None)
            if pt:
                present_types.add(pt)
                type_counts[pt] += 1
        loaded_templates = session_data.get("templates", {})
        missing_templates = [
            {"product_type": pt, "style_count": type_counts[pt]}
            for pt in sorted(present_types)
            if pt not in loaded_templates
        ]
        
        return jsonify({
            "total_styles": len(styles),
            "total_variants": total_variants,
            "errors": errors,
            "warnings": warnings,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "styles": styles,
            "missing_templates": missing_templates,
            "present_product_types": list(present_types),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Failed to parse product data: {str(e)}"}), 500

@app.route("/api/upload-keywords", methods=["POST"])
def upload_keywords():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    f = request.files["file"]
    save_path = UPLOAD_KEYWORDS / f.filename
    f.save(str(save_path))
    
    keywords = []
    try:
        ext = Path(f.filename).suffix.lower()
        if ext in [".csv", ".tsv"]:
            delimiter = "\t" if ext == ".tsv" else ","
            with open(str(save_path), "r", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh, delimiter=delimiter)
                for row in reader:
                    kw = row.get("Keyword Phrase") or row.get("keyword") or row.get("Search Query", "")
                    volume = row.get("Search Volume") or row.get("volume", "0")
                    if kw:
                        try:
                            vol = int(str(volume).replace(",", ""))
                        except (ValueError, AttributeError):
                            vol = 0
                        keywords.append({"keyword": kw.strip().lower(), "volume": vol})
        
        # Sort by volume
        keywords.sort(key=lambda x: x["volume"], reverse=True)
        session_data["keywords"] = keywords
        
        top5 = [k["keyword"] for k in keywords[:5]]
        return jsonify({
            "total_keywords": len(keywords),
            "top5": top5,
            "message": f"{len(keywords)} keywords loaded. Top 5: {', '.join(top5)}",
            "keywords": keywords[:50],
        })
    except Exception as e:
        return jsonify({"error": f"Failed to parse keyword file: {str(e)}"}), 500

@app.route("/api/upload-analytics", methods=["POST"])
def upload_analytics():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    f = request.files["file"]
    save_path = UPLOAD_KEYWORDS / f.filename
    f.save(str(save_path))
    
    analytics = []
    try:
        ext = Path(f.filename).suffix.lower()
        if ext in [".csv", ".tsv"]:
            delimiter = "\t" if ext == ".tsv" else ","
            with open(str(save_path), "r", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh, delimiter=delimiter)
                for row in reader:
                    analytics.append(dict(row))
        
        session_data["analytics"] = analytics
        return jsonify({
            "total_rows": len(analytics),
            "message": f"{len(analytics)} analytics rows loaded",
        })
    except Exception as e:
        return jsonify({"error": f"Failed to parse analytics file: {str(e)}"}), 500

@app.route("/api/generate-content", methods=["POST"])
def generate_content():
    data = request.get_json(force=True)
    brand = data.get("brand") or session_data.get("brand")
    if not brand:
        return jsonify({"error": "No brand selected"}), 400
    
    styles = data.get("styles") or session_data.get("styles", [])
    if not styles:
        return jsonify({"error": "No product data loaded"}), 400
    
    # Load brand config from file if available, fall back to in-memory
    brand_cfg = _load_brand_config_data(brand)
    
    content_map = {}
    has_keywords = len(session_data.get("keywords", [])) > 0
    
    # Reset rotation for consistent results
    global DESCRIPTION_OPENERS_ROTATION
    DESCRIPTION_OPENERS_ROTATION = {}
    
    for style in styles:
        style_num = style["style_num"]
        style_name = style["style_name"]
        subclass = style.get("subclass", "")
        sub_subclass = style.get("sub_subclass", "")
        fabric = parse_fabric(style.get("fabric", "")) or brand_cfg.get("default_fabric", "")
        care = style.get("care", "") or brand_cfg.get("default_care", "")
        upf = style.get("upf", "") or brand_cfg.get("default_upf", "")
        coo = style.get("coo", "") or brand_cfg.get("default_coo", "")
        
        # Get first color for preview title
        first_variant = style["variants"][0] if style["variants"] else {}
        first_color = first_variant.get("color_name", "")
        first_size = first_variant.get("size", "")
        
        title = generate_title(brand_cfg, brand, style_name, "Dress", first_color, first_size, upf)
        bullets = generate_bullets(brand_cfg, brand, style_name, sub_subclass, fabric, care, first_color, upf)
        description = generate_description(brand_cfg, brand, style_num, style_name, sub_subclass, fabric, care, first_color, upf)
        backend_kw = generate_backend_keywords(brand, style_name, subclass, first_color, fabric, upf)
        
        # Derived attributes
        neck = derive_neck_type(style_name)
        sleeve = derive_sleeve_type(style_name)
        silhouette = derive_silhouette(sub_subclass)
        color_map_val = normalize_color(first_color)
        
        category = SUBCLASS_CATEGORY_MAP.get(subclass, "casual-and-day-dresses")
        subcategory = SUBCLASS_SUBCATEGORY_MAP.get(subclass, "casual-dresses")
        
        # Generate "why" explanations for each field
        opener_idx = DESCRIPTION_OPENERS_ROTATION.get(style_num, 0)
        bullet_whys = [
            generate_bullet_why(i, brand_cfg, brand, style_name, sub_subclass, upf, fabric, has_keywords)
            for i in range(5)
        ]
        
        content_map[style_num] = {
            "style_num": style_num,
            "style_name": style_name,
            "title": title,
            "title_why": generate_title_why(brand_cfg, brand, style_name, title, upf, has_keywords),
            "bullets": bullets,
            "bullet_whys": bullet_whys,
            "description": description,
            "description_why": generate_description_why(brand_cfg, style_num, opener_idx, has_keywords),
            "backend_keywords": backend_kw,
            "backend_keywords_why": generate_keywords_why(brand, session_data.get("keywords", []), backend_kw, has_keywords),
            "neck_type": neck,
            "sleeve_type": sleeve,
            "silhouette": silhouette,
            "color_map": color_map_val,
            "category": category,
            "sub_class": subclass,
            "subcategory": subcategory,
            "fabric": fabric,
            "care": care,
            "upf": upf,
            "coo": coo,
        }
    
    session_data["generated_content"] = content_map
    return jsonify({"content": content_map, "total": len(content_map)})

@app.route("/api/submit-feedback", methods=["POST"])
def submit_feedback():
    data = request.get_json(force=True)
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "brand": session_data.get("brand"),
        "style_num": data.get("style_num"),
        "feedback": data.get("feedback"),
        "field": data.get("field"),
        "original": data.get("original"),
        "updated": data.get("updated"),
    }
    try:
        FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(str(FEEDBACK_FILE), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/feedback")
def get_feedback():
    """Return all feedback entries for a given brand."""
    brand = request.args.get("brand", "")
    entries = []
    if FEEDBACK_FILE.exists():
        with open(str(FEEDBACK_FILE), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if not brand or entry.get("brand") == brand:
                        entries.append(entry)
                except json.JSONDecodeError:
                    pass
    # Sort newest first
    entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return jsonify({"brand": brand, "entries": entries, "total": len(entries)})


@app.route("/api/feedback/summary")
def feedback_summary():
    """Return feedback count per brand."""
    counts = defaultdict(int)
    if FEEDBACK_FILE.exists():
        with open(str(FEEDBACK_FILE), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    brand = entry.get("brand") or "Unknown"
                    counts[brand] += 1
                except json.JSONDecodeError:
                    pass
    return jsonify({"counts": dict(counts), "total": sum(counts.values())})


@app.route("/api/generate-nis", methods=["POST"])
def generate_nis():
    data = request.get_json(force=True)
    brand = data.get("brand") or session_data.get("brand")
    styles = data.get("styles") or session_data.get("styles", [])
    content_map = data.get("content") or session_data.get("generated_content", {})
    vendor_code = data.get("vendor_code") or session_data.get("vendor_code") or ""
    template_path = data.get("template_path") or session_data.get("template_path") or str(DEFAULT_TEMPLATE)
    
    if not brand:
        return jsonify({"error": "No brand selected"}), 400
    if not styles:
        return jsonify({"error": "No product data loaded"}), 400
    if not content_map:
        return jsonify({"error": "Content not yet generated. Run Step 6 first."}), 400
    
    brand_cfg = BRAND_CONFIGS.get(brand, BRAND_CONFIGS["Stella Parker"])
    
    # Clear output dir
    for f in UPLOAD_OUTPUT.glob("*.xlsm"):
        f.unlink()
    
    results = []
    errors = []
    
    # Update progress tracker
    nis_progress["total"] = len(styles)
    nis_progress["completed"] = 0
    nis_progress["status"] = "running"
    nis_progress["started_at"] = datetime.now().isoformat()
    nis_progress["current_style"] = ""
    
    # Build multi-template lookup: product_type -> template_path
    template_map = dict(session_data.get("templates", {}))
    
    for i, style in enumerate(styles):
        style_num = style["style_num"]
        style_name = style["style_name"]
        content = content_map.get(style_num, {})
        
        # Update progress
        nis_progress["current_style"] = f"{style_num} — {style_name}"
        
        if not content:
            errors.append(f"No content for style {style_num}")
            nis_progress["completed"] = i + 1
            continue
        
        # Route to the correct template based on sub_class
        subclass = style.get("subclass", "")
        product_type_for_template = TEMPLATE_PRODUCT_TYPE_MAP.get(subclass, None)
        if product_type_for_template and product_type_for_template in template_map:
            style_template_path = template_map[product_type_for_template]
        else:
            style_template_path = template_path  # fall back to default
        
        try:
            output_path = do_xlsm_surgery(
                template_path=style_template_path,
                brand=brand,
                brand_cfg=brand_cfg,
                vendor_code=vendor_code,
                style=style,
                content=content,
            )
            filename = Path(output_path).name
            results.append({
                "style_num": style_num,
                "style_name": style_name,
                "rows": len(style["variants"]) + 1,  # +1 parent
                "filename": filename,
                "path": output_path,
            })
        except Exception as e:
            traceback.print_exc()
            errors.append(f"Failed to generate NIS for style {style_num}: {str(e)}")
        
        nis_progress["completed"] = i + 1
    
    nis_progress["status"] = "done"
    nis_progress["current_style"] = ""
    
    return jsonify({
        "results": results,
        "errors": errors,
        "total": len(results),
    })

@app.route("/api/generate-progress")
def generate_progress():
    elapsed = ""
    eta = ""
    if nis_progress["started_at"] and nis_progress["completed"] > 0:
        started = datetime.fromisoformat(nis_progress["started_at"])
        elapsed_sec = (datetime.now() - started).total_seconds()
        per_style = elapsed_sec / nis_progress["completed"]
        remaining = nis_progress["total"] - nis_progress["completed"]
        eta_sec = per_style * remaining
        elapsed = f"{int(elapsed_sec)}s"
        if eta_sec > 60:
            eta = f"~{int(eta_sec / 60)}m {int(eta_sec % 60)}s remaining"
        else:
            eta = f"~{int(eta_sec)}s remaining"
    return jsonify({
        "total": nis_progress["total"],
        "completed": nis_progress["completed"],
        "current_style": nis_progress["current_style"],
        "status": nis_progress["status"],
        "elapsed": elapsed,
        "eta": eta,
        "percent": round((nis_progress["completed"] / max(nis_progress["total"], 1)) * 100, 1)
    })

def do_xlsm_surgery(template_path, brand, brand_cfg, vendor_code, style, content):
    """
    .xlsm surgery:
    1. Load template with keep_vba=True
    2. Find Template-DRESS sheet
    3. Capture cell styles from row 7
    4. Clear rows 7+
    5. Write new rows (parent + children)
    6. Save as new file
    """
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(template_path, keep_vba=True)
    
    ws = None
    for name in wb.sheetnames:
        if "template" in name.lower() or "dress" in name.lower():
            ws = wb[name]
            break
    if ws is None:
        ws = wb.active
    
    # Build column map from sheet rows 3/4
    col_map = {}
    max_col = ws.max_column or 254
    for col in range(1, max_col + 1):
        h = _safe(ws.cell(row=3, column=col).value)
        fid = _safe(ws.cell(row=4, column=col).value)
        col_map[col] = {"header": h, "field_id": fid}
    
    def find_col(field_id_substr):
        for c, info in col_map.items():
            if field_id_substr.lower() in info["field_id"].lower():
                return c
        return None
    
    def find_col_exact(field_id_exact):
        for c, info in col_map.items():
            if info["field_id"].lower() == field_id_exact.lower():
                return c
        return None
    
    # Column lookups
    COL_VENDOR_CODE = find_col("rtip_vendor_code")
    COL_VENDOR_SKU  = find_col("vendor_sku")
    COL_PRODUCT_TYPE = find_col("product_type")
    COL_PARENTAGE   = find_col("parentage_level")
    COL_CHILD_REL   = find_col("child_relationship_type")
    COL_PARENT_SKU  = find_col("parent_sku")
    COL_VAR_THEME   = find_col("variation_theme")
    COL_ITEM_NAME   = find_col("item_name")
    COL_BRAND       = find_col("brand#1")
    COL_EXT_ID_TYPE = find_col("external_product_id#1.type")
    COL_EXT_ID_VAL  = find_col("external_product_id#1.value")
    COL_PROD_CAT    = find_col("product_category")
    COL_PROD_SUBCAT = find_col("product_subcategory")
    COL_ITK         = find_col("item_type_keyword")
    COL_MODEL_NUM   = find_col("model_number")
    COL_MODEL_NAME  = find_col("model_name")
    COL_BULLET1     = find_col_exact("bullet_point#1.value")
    COL_BULLET2     = find_col_exact("bullet_point#2.value")
    COL_BULLET3     = find_col_exact("bullet_point#3.value")
    COL_BULLET4     = find_col_exact("bullet_point#4.value")
    COL_BULLET5     = find_col_exact("bullet_point#5.value")
    COL_GENERIC_KW  = find_col_exact("generic_keyword#1.value")
    COL_DEPT        = find_col("department#1")
    COL_GENDER      = find_col("target_gender")
    COL_SIZE_SYS    = find_col_exact("apparel_size#1.size_system")
    COL_SIZE_CLASS  = find_col_exact("apparel_size#1.size_class")
    COL_SIZE_VAL    = find_col_exact("apparel_size#1.size")
    COL_MAT1        = find_col_exact("material#1.value")
    COL_FABRIC_TYPE = find_col("fabric_type")
    COL_DESC        = find_col("rtip_product_description")
    COL_COLOR_MAP   = find_col("color#1.standardized")
    COL_COLOR       = find_col_exact("color#1.value")
    COL_CARE        = find_col("care_instructions")
    COL_UPF         = find_col("ultraviolet_protection")
    COL_COO         = find_col("country_of_origin")
    COL_LIST_PRICE  = find_col("list_price") or find_col("standard_price")
    COL_COST_PRICE  = find_col("cost_price") or find_col("map_price")
    COL_SWATCH      = find_col("swatch_product_image")
    COL_SILHOUETTE  = find_col("apparel_silhouette")
    COL_SLEEVE_LEN  = find_col("sleeve_length_description")
    COL_SLEEVE_TYPE = find_col("sleeve_type")
    COL_NECK        = find_col_exact("collar_style#1.value")
    COL_ITEM_LEN    = find_col("item_length_description")
    COL_NUMBER_ITEMS= find_col("number_of_items")
    COL_SKIP_OFFER  = find_col("skip_offer")
    COL_LIFECYCLE   = find_col("product_lifecycle_supply_type") or find_col("lifecycle")
    COL_PART_NUM    = find_col("part_number")
    
    # Capture style from row 7 template (first data row)
    style_cache = {}
    for col in range(1, max_col + 1):
        cell = ws.cell(row=7, column=col)
        style_cache[col] = {
            "font": copy.copy(cell.font) if cell.font else None,
            "fill": copy.copy(cell.fill) if cell.fill else None,
            "border": copy.copy(cell.border) if cell.border else None,
            "alignment": copy.copy(cell.alignment) if cell.alignment else None,
            "number_format": cell.number_format,
        }
    
    # Clear existing data rows (7+)
    for row_idx in range(7, (ws.max_row or 100) + 1):
        for col in range(1, max_col + 1):
            ws.cell(row=row_idx, column=col).value = None
    
    style_num = style["style_num"]
    style_name = style["style_name"]
    variants = style["variants"]
    list_price = style.get("list_price", "")
    cost_price = style.get("cost_price", "")
    parent_asin = style.get("parent_asin", "")
    model_name_raw = style.get("model_name", "") or style_name
    
    bullets = content.get("bullets", [])
    description = content.get("description", "")
    backend_kw = content.get("backend_keywords", "")
    neck_type = content.get("neck_type", "")
    sleeve_type = content.get("sleeve_type", "")
    silhouette = content.get("silhouette", "")
    category = content.get("category", "casual-and-day-dresses")
    subcategory = content.get("subcategory", "casual-dresses")
    fabric = content.get("fabric", "") or brand_cfg.get("default_fabric", "")
    care = content.get("care", "") or brand_cfg.get("default_care", "")
    upf = content.get("upf", "") or brand_cfg.get("default_upf", "")
    coo = content.get("coo", "") or brand_cfg.get("default_coo", "")
    
    # Determine parent SKU
    # Group variants by color
    color_groups = defaultdict(list)
    for v in variants:
        color_groups[v.get("color_name", "")].append(v)
    
    # Parent SKU for NIS = style_num (vendor-side SKU, not ASIN)
    parent_sku = style_num
    
    # Helper to write a row
    def write_row(row_idx, values_dict):
        for col_num, value in values_dict.items():
            if col_num is None:
                continue
            cell = ws.cell(row=row_idx, column=col_num)
            cell.value = value
            # Apply style from row 7
            cached = style_cache.get(col_num, {})
            if cached.get("font"):
                cell.font = copy.copy(cached["font"])
            if cached.get("fill"):
                cell.fill = copy.copy(cached["fill"])
            if cached.get("border"):
                cell.border = copy.copy(cached["border"])
            if cached.get("alignment"):
                cell.alignment = copy.copy(cached["alignment"])
            if cached.get("number_format"):
                cell.number_format = cached["number_format"]
    
    current_row = 7
    
    # Write parent row
    parent_row = {}
    if COL_VENDOR_CODE: parent_row[COL_VENDOR_CODE] = vendor_code or brand_cfg.get("vendor_code_full", "")
    if COL_VENDOR_SKU:  parent_row[COL_VENDOR_SKU]  = parent_sku
    if COL_PRODUCT_TYPE: parent_row[COL_PRODUCT_TYPE] = "DRESS"
    if COL_PARENTAGE:   parent_row[COL_PARENTAGE]   = "Parent"
    if COL_VAR_THEME:   parent_row[COL_VAR_THEME]   = "COLOR/SIZE"
    if COL_ITEM_NAME:   parent_row[COL_ITEM_NAME]   = content.get("title", style_name)
    if COL_BRAND:       parent_row[COL_BRAND]       = brand
    if COL_PROD_CAT:    parent_row[COL_PROD_CAT]    = category
    if COL_PROD_SUBCAT: parent_row[COL_PROD_SUBCAT] = subcategory
    if COL_ITK:         parent_row[COL_ITK]         = "fashion-dresses"
    if COL_MODEL_NUM:   parent_row[COL_MODEL_NUM]   = style_num
    if COL_MODEL_NAME:  parent_row[COL_MODEL_NAME]  = model_name_raw or style_name
    if COL_BULLET1 and bullets: parent_row[COL_BULLET1] = bullets[0][:500] if bullets else ""
    if COL_BULLET2 and len(bullets) > 1: parent_row[COL_BULLET2] = bullets[1][:500]
    if COL_BULLET3 and len(bullets) > 2: parent_row[COL_BULLET3] = bullets[2][:500]
    if COL_BULLET4 and len(bullets) > 3: parent_row[COL_BULLET4] = bullets[3][:500]
    if COL_BULLET5 and len(bullets) > 4: parent_row[COL_BULLET5] = bullets[4][:500]
    if COL_GENERIC_KW:  parent_row[COL_GENERIC_KW] = backend_kw
    if COL_DESC:        parent_row[COL_DESC]        = description
    if COL_DEPT:        parent_row[COL_DEPT]        = brand_cfg.get("department", "Womens")
    if COL_GENDER:      parent_row[COL_GENDER]      = brand_cfg.get("gender", "Female")
    if COL_MAT1 and fabric: parent_row[COL_MAT1]    = fabric
    if COL_FABRIC_TYPE: parent_row[COL_FABRIC_TYPE] = "Knit"
    if COL_CARE and care: parent_row[COL_CARE]      = care
    if COL_UPF and upf: parent_row[COL_UPF]         = upf
    if COL_COO and coo: parent_row[COL_COO]         = coo
    if COL_LIST_PRICE and list_price:
        try: parent_row[COL_LIST_PRICE] = float(list_price)
        except: parent_row[COL_LIST_PRICE] = list_price
    if COL_NECK and neck_type: parent_row[COL_NECK] = neck_type
    if COL_SLEEVE_TYPE and sleeve_type: parent_row[COL_SLEEVE_TYPE] = sleeve_type
    if COL_SILHOUETTE and silhouette: parent_row[COL_SILHOUETTE] = silhouette
    if COL_NUMBER_ITEMS: parent_row[COL_NUMBER_ITEMS] = 1
    if COL_SKIP_OFFER:  parent_row[COL_SKIP_OFFER] = "No"
    if COL_LIFECYCLE:   parent_row[COL_LIFECYCLE]  = "new"
    
    write_row(current_row, parent_row)
    current_row += 1
    
    # Write child rows (one per variant)
    for v in variants:
        color_name = v.get("color_name", "")
        size = v.get("size", "")
        upc = v.get("upc", "")
        child_asin = v.get("child_asin", "")
        sku = v.get("sku", "") or f"{style_num}-{color_name}-{size}".replace(" ", "-")
        
        color_normalized = normalize_color(color_name)
        size_normalized = normalize_size(size)
        
        # Generate per-variant title
        variant_title = generate_title(
            brand_cfg, brand, style_name, "Dress",
            color_name, size, upf
        )
        
        child_row = {}
        if COL_VENDOR_CODE: child_row[COL_VENDOR_CODE] = vendor_code or brand_cfg.get("vendor_code_full", "")
        if COL_VENDOR_SKU:  child_row[COL_VENDOR_SKU]  = sku
        if COL_PRODUCT_TYPE: child_row[COL_PRODUCT_TYPE] = "DRESS"
        if COL_PARENTAGE:   child_row[COL_PARENTAGE]   = "Child"
        if COL_CHILD_REL:   child_row[COL_CHILD_REL]   = "Variation"
        if COL_PARENT_SKU:  child_row[COL_PARENT_SKU]  = parent_sku
        if COL_VAR_THEME:   child_row[COL_VAR_THEME]   = "COLOR/SIZE"
        if COL_ITEM_NAME:   child_row[COL_ITEM_NAME]   = variant_title
        if COL_BRAND:       child_row[COL_BRAND]       = brand
        if COL_EXT_ID_TYPE and upc: child_row[COL_EXT_ID_TYPE] = "UPC"
        if COL_EXT_ID_VAL and upc:  child_row[COL_EXT_ID_VAL]  = re.sub(r'\D', '', str(upc))
        if child_asin:
            try:
                asin_col = find_col("merchant_suggested_asin")
                if asin_col: child_row[asin_col] = child_asin
            except: pass
        if COL_PROD_CAT:    child_row[COL_PROD_CAT]    = category
        if COL_PROD_SUBCAT: child_row[COL_PROD_SUBCAT] = subcategory
        if COL_ITK:         child_row[COL_ITK]         = "fashion-dresses"
        if COL_MODEL_NUM:   child_row[COL_MODEL_NUM]   = style_num
        if COL_MODEL_NAME:  child_row[COL_MODEL_NAME]  = model_name_raw or style_name
        if COL_BULLET1 and bullets: child_row[COL_BULLET1] = bullets[0][:500]
        if COL_BULLET2 and len(bullets) > 1: child_row[COL_BULLET2] = bullets[1][:500]
        if COL_BULLET3 and len(bullets) > 2: child_row[COL_BULLET3] = bullets[2][:500]
        if COL_BULLET4 and len(bullets) > 3: child_row[COL_BULLET4] = bullets[3][:500]
        if COL_BULLET5 and len(bullets) > 4: child_row[COL_BULLET5] = bullets[4][:500]
        if COL_GENERIC_KW:  child_row[COL_GENERIC_KW] = backend_kw
        if COL_DESC:        child_row[COL_DESC]        = description
        if COL_DEPT:        child_row[COL_DEPT]        = brand_cfg.get("department", "Womens")
        if COL_GENDER:      child_row[COL_GENDER]      = brand_cfg.get("gender", "Female")
        if COL_SIZE_SYS:    child_row[COL_SIZE_SYS]    = "US"
        if COL_SIZE_CLASS:  child_row[COL_SIZE_CLASS]  = "Regular"
        if COL_SIZE_VAL:    child_row[COL_SIZE_VAL]    = size_normalized or size
        if COL_COLOR_MAP:   child_row[COL_COLOR_MAP]   = color_normalized
        if COL_COLOR:       child_row[COL_COLOR]       = color_name.title() if color_name else ""
        if COL_MAT1 and fabric: child_row[COL_MAT1]   = fabric
        if COL_FABRIC_TYPE: child_row[COL_FABRIC_TYPE] = "Knit"
        if COL_CARE and care: child_row[COL_CARE]      = care
        if COL_UPF and upf: child_row[COL_UPF]         = upf
        if COL_COO and coo: child_row[COL_COO]         = coo
        if COL_LIST_PRICE and list_price:
            try: child_row[COL_LIST_PRICE] = float(list_price)
            except: child_row[COL_LIST_PRICE] = list_price
        if COL_COST_PRICE and cost_price:
            try: child_row[COL_COST_PRICE] = float(v.get("cost_price") or cost_price)
            except: child_row[COL_COST_PRICE] = cost_price
        if COL_NECK and neck_type: child_row[COL_NECK] = neck_type
        if COL_SLEEVE_TYPE and sleeve_type: child_row[COL_SLEEVE_TYPE] = sleeve_type
        if COL_SILHOUETTE and silhouette: child_row[COL_SILHOUETTE] = silhouette
        if COL_NUMBER_ITEMS: child_row[COL_NUMBER_ITEMS] = 1
        if COL_SKIP_OFFER:  child_row[COL_SKIP_OFFER] = "No"
        if COL_LIFECYCLE:   child_row[COL_LIFECYCLE]  = "new"
        
        write_row(current_row, child_row)
        current_row += 1
    
    # Save
    safe_name = re.sub(r'[^\w\-]', '_', style_num)
    output_filename = f"NIS_{brand.replace(' ', '_')}_{safe_name}.xlsm"
    output_path = str(UPLOAD_OUTPUT / output_filename)
    
    import warnings as w
    with w.catch_warnings():
        w.simplefilter("ignore")
        wb.save(output_path)
    wb.close()
    
    return output_path

@app.route("/api/download/<filename>")
def download_file(filename):
    # Sanitize
    filename = Path(filename).name
    file_path = UPLOAD_OUTPUT / filename
    if not file_path.exists():
        abort(404)
    return send_file(
        str(file_path),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.ms-excel.sheet.macroEnabled.12",
    )

@app.route("/api/download-all")
def download_all():
    xlsm_files = list(UPLOAD_OUTPUT.glob("*.xlsm"))
    if not xlsm_files:
        return jsonify({"error": "No generated files found"}), 404
    
    zip_path = UPLOAD_OUTPUT / "NIS_All_Files.zip"
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for f in xlsm_files:
            zf.write(str(f), f.name)
    
    return send_file(
        str(zip_path),
        as_attachment=True,
        download_name="NIS_All_Files.zip",
        mimetype="application/zip",
    )

@app.route("/api/download-combined")
def download_combined():
    """Download ALL styles combined into a single .xlsm file."""
    brand = session_data.get("brand", "Brand")
    styles = session_data.get("styles", [])
    content_map = session_data.get("generated_content", {})
    template_path = session_data.get("template_path", str(DEFAULT_TEMPLATE))
    brand_cfg = _load_brand_config_data(brand)
    vendor_code = session_data.get("vendor_code", brand_cfg.get("vendor_code_full", ""))
    
    if not styles or not content_map:
        return jsonify({"error": "No generated content. Run Generate Content first."}), 400
    
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(template_path, keep_vba=True)
    
    ws = None
    for name in wb.sheetnames:
        if "template" in name.lower() or "dress" in name.lower():
            ws = wb[name]
            break
    if ws is None:
        ws = wb.active
    
    # Build column map
    col_map = {}
    max_col = ws.max_column or 254
    for col in range(1, max_col + 1):
        h = _safe(ws.cell(row=3, column=col).value)
        fid = _safe(ws.cell(row=4, column=col).value)
        col_map[col] = {"header": h, "field_id": fid}
    
    def find_col(fid_sub):
        for c, info in col_map.items():
            if fid_sub.lower() in info["field_id"].lower():
                return c
        return None
    def find_col_exact(fid_exact):
        for c, info in col_map.items():
            if info["field_id"].lower() == fid_exact.lower():
                return c
        return None
    
    # Same column lookups as do_xlsm_surgery
    COL = {
        "vendor_code": find_col("rtip_vendor_code"),
        "vendor_sku": find_col("vendor_sku"),
        "product_type": find_col("product_type"),
        "parentage": find_col("parentage_level"),
        "child_rel": find_col("child_relationship_type"),
        "parent_sku": find_col("parent_sku"),
        "var_theme": find_col("variation_theme"),
        "item_name": find_col("item_name"),
        "brand": find_col("brand#1"),
        "ext_id_type": find_col("external_product_id#1.type"),
        "ext_id_val": find_col("external_product_id#1.value"),
        "itk": find_col("item_type_keyword"),
        "model_num": find_col("model_number"),
        "model_name": find_col("model_name"),
        "bullet1": find_col_exact("bullet_point#1.value"),
        "bullet2": find_col_exact("bullet_point#2.value"),
        "bullet3": find_col_exact("bullet_point#3.value"),
        "bullet4": find_col_exact("bullet_point#4.value"),
        "bullet5": find_col_exact("bullet_point#5.value"),
        "keywords": find_col_exact("generic_keyword#1.value"),
        "dept": find_col("department#1"),
        "gender": find_col("target_gender"),
        "size_sys": find_col_exact("apparel_size#1.size_system"),
        "size_class": find_col_exact("apparel_size#1.size_class"),
        "size_val": find_col_exact("apparel_size#1.size"),
        "material": find_col_exact("material#1.value"),
        "desc": find_col("rtip_product_description"),
        "color_map": find_col("color#1.standardized"),
        "color": find_col_exact("color#1.value"),
        "care": find_col("care_instructions"),
        "upf": find_col("ultraviolet_protection"),
        "coo": find_col("country_of_origin"),
        "list_price": find_col("list_price") or find_col("standard_price"),
    }
    
    # Capture styles from row 7
    cell_styles = {}
    for col in range(1, max_col + 1):
        cell = ws.cell(row=7, column=col)
        cell_styles[col] = {
            "font": copy(cell.font), "fill": copy(cell.fill),
            "border": copy(cell.border), "alignment": copy(cell.alignment),
            "number_format": cell.number_format,
        }
    
    # Clear existing data rows
    for row in range(7, ws.max_row + 1):
        for col in range(1, max_col + 1):
            ws.cell(row=row, column=col).value = None
    
    # Write ALL styles into the same file
    current_row = 7
    for style in styles:
        style_num = style["style_num"]
        content = content_map.get(style_num, {})
        if not content:
            continue
        
        parent_sku = f"{brand_cfg.get('vendor_code_prefix', '')}-{style_num}".strip("-")
        category = content.get("category", "casual-and-day-dresses")
        sub_class = style.get("sub_class", "")
        product_type = "DRESS"  # from template
        
        # Parent row
        def write_cell(row, col_key, value):
            c = COL.get(col_key)
            if c and value:
                cell = ws.cell(row=row, column=c)
                cell.value = str(value)
                for prop, sval in cell_styles.get(c, {}).items():
                    if prop == "number_format":
                        cell.number_format = sval
                    else:
                        setattr(cell, prop, sval)
        
        write_cell(current_row, "vendor_code", vendor_code)
        write_cell(current_row, "vendor_sku", parent_sku)
        write_cell(current_row, "product_type", product_type)
        write_cell(current_row, "parentage", "Parent")
        write_cell(current_row, "var_theme", "ColorSize")
        write_cell(current_row, "item_name", content.get("title", ""))
        write_cell(current_row, "brand", brand)
        write_cell(current_row, "itk", category)
        write_cell(current_row, "model_num", style_num)
        write_cell(current_row, "model_name", style.get("style_name", "").title())
        write_cell(current_row, "bullet1", content.get("bullet_1", ""))
        write_cell(current_row, "bullet2", content.get("bullet_2", ""))
        write_cell(current_row, "bullet3", content.get("bullet_3", ""))
        write_cell(current_row, "bullet4", content.get("bullet_4", ""))
        write_cell(current_row, "bullet5", content.get("bullet_5", ""))
        write_cell(current_row, "keywords", content.get("backend_keywords", ""))
        write_cell(current_row, "desc", content.get("description", ""))
        write_cell(current_row, "dept", brand_cfg.get("department", "Womens"))
        write_cell(current_row, "gender", brand_cfg.get("gender", "Female"))
        current_row += 1
        
        # Child rows
        for var in style.get("variants", []):
            color = var.get("color", "")
            size = var.get("size", "")
            upc = var.get("upc", "")
            child_sku = f"{parent_sku}-{color}-{size}".replace(" ", "_")
            child_title = content.get("title", "").replace(style.get("style_name", "").title(), style.get("style_name", "").title())
            if color:
                child_title = child_title.split(",")[0] + f", {color.title()}, {SIZE_MAP.get(size, size)}"
            
            write_cell(current_row, "vendor_code", vendor_code)
            write_cell(current_row, "vendor_sku", child_sku)
            write_cell(current_row, "product_type", product_type)
            write_cell(current_row, "parentage", "Child")
            write_cell(current_row, "child_rel", "Variation")
            write_cell(current_row, "parent_sku", parent_sku)
            write_cell(current_row, "var_theme", "ColorSize")
            write_cell(current_row, "item_name", child_title)
            write_cell(current_row, "brand", brand)
            if upc:
                write_cell(current_row, "ext_id_type", "UPC")
                write_cell(current_row, "ext_id_val", upc)
            write_cell(current_row, "itk", category)
            write_cell(current_row, "model_num", style_num)
            write_cell(current_row, "bullet1", content.get("bullet_1", ""))
            write_cell(current_row, "bullet2", content.get("bullet_2", ""))
            write_cell(current_row, "bullet3", content.get("bullet_3", ""))
            write_cell(current_row, "bullet4", content.get("bullet_4", ""))
            write_cell(current_row, "bullet5", content.get("bullet_5", ""))
            write_cell(current_row, "keywords", content.get("backend_keywords", ""))
            write_cell(current_row, "desc", content.get("description", ""))
            write_cell(current_row, "color_map", COLOR_MAP_LOOKUP.get(color.upper(), "Multicolour"))
            write_cell(current_row, "color", color.title())
            write_cell(current_row, "size_sys", "US")
            write_cell(current_row, "size_class", "Alpha")
            write_cell(current_row, "size_val", SIZE_MAP.get(size, size))
            write_cell(current_row, "material", brand_cfg.get("default_fabric", ""))
            write_cell(current_row, "care", brand_cfg.get("default_care", "Machine Wash"))
            write_cell(current_row, "coo", brand_cfg.get("default_coo", ""))
            write_cell(current_row, "dept", brand_cfg.get("department", "Womens"))
            write_cell(current_row, "gender", brand_cfg.get("gender", "Female"))
            if brand_cfg.get("default_upf"):
                write_cell(current_row, "upf", f"UPF {brand_cfg['default_upf']}")
            write_cell(current_row, "list_price", var.get("list_price", ""))
            current_row += 1
    
    # Save combined file
    safe_brand = brand.replace(" ", "_")
    combined_name = f"NIS_{safe_brand}_ALL_STYLES.xlsm"
    combined_path = UPLOAD_OUTPUT / combined_name
    wb.save(str(combined_path))
    
    return send_file(
        str(combined_path),
        as_attachment=True,
        download_name=combined_name,
        mimetype="application/vnd.ms-excel.sheet.macroEnabled.12",
    )


@app.route("/api/download-category/<category>")
def download_category(category):
    """Download all styles of a specific category combined into one .xlsm file."""
    brand = session_data.get("brand", "Brand")
    styles = session_data.get("styles", [])
    content_map = session_data.get("generated_content", {})
    template_path = session_data.get("template_path", str(DEFAULT_TEMPLATE))
    brand_cfg = _load_brand_config_data(brand)
    vendor_code = session_data.get("vendor_code", brand_cfg.get("vendor_code_full", ""))
    
    if not styles or not content_map:
        return jsonify({"error": "No generated content. Run Generate Content first."}), 400
    
    # Filter styles by category
    filtered_styles = [s for s in styles if s.get("sub_class", "").lower() == category.lower() 
                       or s.get("category", "").lower() == category.lower()]
    
    if not filtered_styles:
        return jsonify({"error": f"No styles found for category '{category}'"}), 404
    
    # Use the combined download logic but only for filtered styles
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(template_path, keep_vba=True)
    
    ws = None
    for name in wb.sheetnames:
        if "template" in name.lower() or "dress" in name.lower():
            ws = wb[name]
            break
    if ws is None:
        ws = wb.active
    
    col_map = {}
    max_col = ws.max_column or 254
    for col in range(1, max_col + 1):
        h = _safe(ws.cell(row=3, column=col).value)
        fid = _safe(ws.cell(row=4, column=col).value)
        col_map[col] = {"header": h, "field_id": fid}
    
    def find_col(fid_sub):
        for c, info in col_map.items():
            if fid_sub.lower() in info["field_id"].lower():
                return c
        return None
    def find_col_exact(fid_exact):
        for c, info in col_map.items():
            if info["field_id"].lower() == fid_exact.lower():
                return c
        return None
    
    COL = {
        "vendor_code": find_col("rtip_vendor_code"), "vendor_sku": find_col("vendor_sku"),
        "product_type": find_col("product_type"), "parentage": find_col("parentage_level"),
        "child_rel": find_col("child_relationship_type"), "parent_sku": find_col("parent_sku"),
        "var_theme": find_col("variation_theme"), "item_name": find_col("item_name"),
        "brand": find_col("brand#1"), "ext_id_type": find_col("external_product_id#1.type"),
        "ext_id_val": find_col("external_product_id#1.value"), "itk": find_col("item_type_keyword"),
        "model_num": find_col("model_number"), "model_name": find_col("model_name"),
        "bullet1": find_col_exact("bullet_point#1.value"), "bullet2": find_col_exact("bullet_point#2.value"),
        "bullet3": find_col_exact("bullet_point#3.value"), "bullet4": find_col_exact("bullet_point#4.value"),
        "bullet5": find_col_exact("bullet_point#5.value"), "keywords": find_col_exact("generic_keyword#1.value"),
        "dept": find_col("department#1"), "gender": find_col("target_gender"),
        "size_sys": find_col_exact("apparel_size#1.size_system"), "size_class": find_col_exact("apparel_size#1.size_class"),
        "size_val": find_col_exact("apparel_size#1.size"), "material": find_col_exact("material#1.value"),
        "desc": find_col("rtip_product_description"), "color_map": find_col("color#1.standardized"),
        "color": find_col_exact("color#1.value"), "care": find_col("care_instructions"),
        "upf": find_col("ultraviolet_protection"), "coo": find_col("country_of_origin"),
        "list_price": find_col("list_price") or find_col("standard_price"),
    }
    
    cell_styles = {}
    for col in range(1, max_col + 1):
        cell = ws.cell(row=7, column=col)
        cell_styles[col] = {
            "font": copy(cell.font), "fill": copy(cell.fill),
            "border": copy(cell.border), "alignment": copy(cell.alignment),
            "number_format": cell.number_format,
        }
    
    for row in range(7, ws.max_row + 1):
        for col in range(1, max_col + 1):
            ws.cell(row=row, column=col).value = None
    
    current_row = 7
    for style in filtered_styles:
        style_num = style["style_num"]
        content = content_map.get(style_num, {})
        if not content:
            continue
        
        parent_sku = f"{brand_cfg.get('vendor_code_prefix', '')}-{style_num}".strip("-")
        cat_kw = content.get("category", "casual-and-day-dresses")
        
        def write_cell(row, col_key, value):
            c = COL.get(col_key)
            if c and value:
                cell = ws.cell(row=row, column=c)
                cell.value = str(value)
                for prop, sval in cell_styles.get(c, {}).items():
                    if prop == "number_format": cell.number_format = sval
                    else: setattr(cell, prop, sval)
        
        write_cell(current_row, "vendor_code", vendor_code)
        write_cell(current_row, "vendor_sku", parent_sku)
        write_cell(current_row, "product_type", "DRESS")
        write_cell(current_row, "parentage", "Parent")
        write_cell(current_row, "var_theme", "ColorSize")
        write_cell(current_row, "item_name", content.get("title", ""))
        write_cell(current_row, "brand", brand)
        write_cell(current_row, "itk", cat_kw)
        write_cell(current_row, "model_num", style_num)
        write_cell(current_row, "model_name", style.get("style_name", "").title())
        write_cell(current_row, "bullet1", content.get("bullet_1", ""))
        write_cell(current_row, "bullet2", content.get("bullet_2", ""))
        write_cell(current_row, "bullet3", content.get("bullet_3", ""))
        write_cell(current_row, "bullet4", content.get("bullet_4", ""))
        write_cell(current_row, "bullet5", content.get("bullet_5", ""))
        write_cell(current_row, "keywords", content.get("backend_keywords", ""))
        write_cell(current_row, "desc", content.get("description", ""))
        current_row += 1
        
        for var in style.get("variants", []):
            color = var.get("color", "")
            size = var.get("size", "")
            upc = var.get("upc", "")
            child_sku = f"{parent_sku}-{color}-{size}".replace(" ", "_")
            child_title = content.get("title", "").split(",")[0] + f", {color.title()}, {SIZE_MAP.get(size, size)}" if color else content.get("title", "")
            
            write_cell(current_row, "vendor_code", vendor_code)
            write_cell(current_row, "vendor_sku", child_sku)
            write_cell(current_row, "product_type", "DRESS")
            write_cell(current_row, "parentage", "Child")
            write_cell(current_row, "child_rel", "Variation")
            write_cell(current_row, "parent_sku", parent_sku)
            write_cell(current_row, "var_theme", "ColorSize")
            write_cell(current_row, "item_name", child_title)
            write_cell(current_row, "brand", brand)
            if upc:
                write_cell(current_row, "ext_id_type", "UPC")
                write_cell(current_row, "ext_id_val", upc)
            write_cell(current_row, "itk", cat_kw)
            write_cell(current_row, "model_num", style_num)
            write_cell(current_row, "bullet1", content.get("bullet_1", ""))
            write_cell(current_row, "bullet2", content.get("bullet_2", ""))
            write_cell(current_row, "bullet3", content.get("bullet_3", ""))
            write_cell(current_row, "bullet4", content.get("bullet_4", ""))
            write_cell(current_row, "bullet5", content.get("bullet_5", ""))
            write_cell(current_row, "keywords", content.get("backend_keywords", ""))
            write_cell(current_row, "desc", content.get("description", ""))
            write_cell(current_row, "color_map", COLOR_MAP_LOOKUP.get(color.upper(), "Multicolour"))
            write_cell(current_row, "color", color.title())
            write_cell(current_row, "size_sys", "US")
            write_cell(current_row, "size_class", "Alpha")
            write_cell(current_row, "size_val", SIZE_MAP.get(size, size))
            write_cell(current_row, "material", brand_cfg.get("default_fabric", ""))
            write_cell(current_row, "care", brand_cfg.get("default_care", "Machine Wash"))
            write_cell(current_row, "coo", brand_cfg.get("default_coo", ""))
            if brand_cfg.get("default_upf"):
                write_cell(current_row, "upf", f"UPF {brand_cfg['default_upf']}")
            write_cell(current_row, "list_price", var.get("list_price", ""))
            current_row += 1
    
    safe_brand = brand.replace(" ", "_")
    safe_cat = category.replace(" ", "_").replace("/", "_")
    fname = f"NIS_{safe_brand}_{safe_cat}.xlsm"
    fpath = UPLOAD_OUTPUT / fname
    wb.save(str(fpath))
    
    return send_file(str(fpath), as_attachment=True, download_name=fname,
                     mimetype="application/vnd.ms-excel.sheet.macroEnabled.12")

@app.route("/api/categories")
def get_categories():
    """Return list of unique categories from uploaded styles."""
    styles = session_data.get("styles", [])
    cats = {}
    for s in styles:
        cat = s.get("sub_class", "Uncategorized") or "Uncategorized"
        if cat not in cats:
            cats[cat] = {"name": cat, "count": 0, "variants": 0}
        cats[cat]["count"] += 1
        cats[cat]["variants"] += len(s.get("variants", []))
    return jsonify(list(cats.values()))


@app.route("/api/session-state")
def session_state():
    return jsonify({
        "brand": session_data.get("brand"),
        "vendor_code": session_data.get("vendor_code"),
        "template_path": session_data.get("template_path"),
        "styles_loaded": len(session_data.get("styles", [])),
        "keywords_loaded": len(session_data.get("keywords", [])),
        "content_generated": len(session_data.get("generated_content", {})),
    })


# ── Brand config file helpers ──────────────────────────────────────────────────
def _load_brand_config_data(brand):
    """Load brand config from file if saved, else from in-memory BRAND_CONFIGS."""
    brand_file = BRAND_CONFIGS_DIR / f"{re.sub(r'[^\w]', '_', brand)}.json"
    if brand_file.exists():
        try:
            with open(str(brand_file), "r", encoding="utf-8") as f:
                saved = json.load(f)
            # Merge with in-memory (saved overrides in-memory defaults)
            base = dict(BRAND_CONFIGS.get(brand, BRAND_CONFIGS.get("Stella Parker", {})))
            base.update(saved)
            return base
        except Exception:
            pass
    return dict(BRAND_CONFIGS.get(brand, BRAND_CONFIGS.get("Stella Parker", {})))


@app.route("/api/save-brand-config", methods=["POST"])
def save_brand_config():
    data = request.get_json(force=True)
    brand = data.get("brand", "")
    config = data.get("config", {})
    if not brand:
        return jsonify({"error": "No brand provided"}), 400
    
    brand_file = BRAND_CONFIGS_DIR / f"{re.sub(r'[^\w]', '_', brand)}.json"
    try:
        with open(str(brand_file), "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return jsonify({"ok": True, "brand": brand})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/load-brand-config", methods=["GET"])
def load_brand_config_endpoint():
    brand = request.args.get("brand", "")
    if not brand:
        return jsonify({"error": "No brand provided"}), 400
    
    cfg = _load_brand_config_data(brand)
    brand_file = BRAND_CONFIGS_DIR / f"{re.sub(r'[^\w]', '_', brand)}.json"
    return jsonify({
        "brand": brand,
        "config": cfg,
        "has_saved_config": brand_file.exists(),
    })


@app.route("/api/regenerate-field", methods=["POST"])
def regenerate_field():
    """Generate an alternative version of a single field."""
    data = request.get_json(force=True)
    style_id = data.get("style_id", "")
    field = data.get("field", "")
    current_content = data.get("current_content", "")
    
    brand = data.get("brand") or session_data.get("brand", "")
    styles = session_data.get("styles", [])
    style = next((s for s in styles if s["style_num"] == style_id), None)
    
    if not style or not brand:
        return jsonify({"error": "Style or brand not found"}), 400
    
    brand_cfg = _load_brand_config_data(brand)
    style_name = style["style_name"]
    subclass = style.get("subclass", "")
    sub_subclass = style.get("sub_subclass", "")
    fabric = parse_fabric(style.get("fabric", "")) or brand_cfg.get("default_fabric", "")
    care = style.get("care", "") or brand_cfg.get("default_care", "")
    upf = style.get("upf", "") or brand_cfg.get("default_upf", "")
    first_variant = style["variants"][0] if style.get("variants") else {}
    first_color = first_variant.get("color_name", "")
    first_size = first_variant.get("size", "")
    has_keywords = len(session_data.get("keywords", [])) > 0
    
    try:
        if field == "title":
            # Generate alternative title using different formula variation
            alt_title = generate_title(brand_cfg, brand, style_name, "Dress", first_color, first_size, upf)
            # Vary: swap color position or add style descriptor variation
            descriptor = style_descriptor_from_name(style_name)
            alt_title2 = f"{brand} {descriptor} Dress, {first_color.title() if first_color else ''}, {first_size}".strip(", ")
            content = alt_title2[:200] if alt_title2 != current_content else alt_title[:200]
            why = generate_title_why(brand_cfg, brand, style_name, content, upf, has_keywords) + " [Alternative format.]"
        
        elif field.startswith("bullet_"):
            # Extract bullet index
            try:
                bullet_idx = int(field.split("_")[1]) - 1
            except (IndexError, ValueError):
                bullet_idx = 0
            bullets = generate_bullets(brand_cfg, brand, style_name, sub_subclass, fabric, care, first_color, upf)
            # Rotate bullet labels for variation
            labels = ["OUTSTANDING FEATURE", "STYLE HIGHLIGHT", "DESIGN DETAIL", "FASHION FORWARD", "KEY BENEFIT"]
            if bullet_idx < len(bullets):
                b = bullets[bullet_idx]
                # Replace first word segment (the all-caps label) with an alternative
                alt_b = re.sub(r'^[A-Z\s&/]+—', labels[bullet_idx % len(labels)] + ' —', b, count=1)
                content = alt_b if alt_b != current_content else b
            else:
                content = current_content
            why = generate_bullet_why(bullet_idx, brand_cfg, brand, style_name, sub_subclass, upf, fabric, has_keywords) + " [Alternative phrasing.]"
        
        elif field == "description":
            # Use a different opener index
            total = len(DESCRIPTION_OPENERS)
            current_idx = DESCRIPTION_OPENERS_ROTATION.get(style_id, 0)
            alt_idx = (current_idx + 1) % total
            DESCRIPTION_OPENERS_ROTATION[style_id] = alt_idx
            content = generate_description(brand_cfg, brand, style_id, style_name, sub_subclass, fabric, care, first_color, upf)
            why = generate_description_why(brand_cfg, style_id, alt_idx, has_keywords) + " [Alternative opener used.]"
        
        elif field == "backend_keywords":
            # Reorder keywords
            kw_list = generate_backend_keywords(brand, style_name, subclass, first_color, fabric, upf)
            words = kw_list.split()
            # Shuffle order (deterministic rotation)
            mid = len(words) // 2
            alt_words = words[mid:] + words[:mid]
            alt_kw = " ".join(alt_words)
            while len(alt_kw.encode('utf-8')) > 250 and alt_words:
                alt_words.pop()
                alt_kw = " ".join(alt_words)
            content = alt_kw
            why = generate_keywords_why(brand, session_data.get("keywords", []), content, has_keywords) + " [Alternative keyword order.]"
        
        else:
            return jsonify({"error": f"Unknown field: {field}"}), 400
        
        return jsonify({"content": content, "why": why})
    
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-csv", methods=["POST"])
def generate_csv():
    """Generate CSV output for all styles — no template required."""
    data = request.get_json(force=True)
    brand = data.get("brand") or session_data.get("brand", "")
    styles = data.get("styles") or session_data.get("styles", [])
    content_map = data.get("content") or session_data.get("generated_content", {})
    
    if not styles:
        return jsonify({"error": "No product data loaded"}), 400
    if not content_map:
        return jsonify({"error": "No generated content. Run Generate Content first."}), 400
    
    output = io.StringIO()
    fieldnames = ["Style #", "Title", "Bullet 1", "Bullet 2", "Bullet 3", "Bullet 4", "Bullet 5",
                  "Description", "Backend Keywords", "Color", "Size", "UPC", "Price", "Category", "Brand"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    
    for style in styles:
        style_num = style["style_num"]
        content = content_map.get(style_num, {})
        bullets = content.get("bullets", [])
        
        for variant in style.get("variants", []):
            color = variant.get("color_name", "")
            size = variant.get("size", "")
            upc = variant.get("upc", "")
            
            # Per-variant title
            brand_cfg = _load_brand_config_data(brand)
            upf = style.get("upf", "") or brand_cfg.get("default_upf", "")
            var_title = generate_title(brand_cfg, brand, style["style_name"], "Dress", color, size, upf)
            
            writer.writerow({
                "Style #": style_num,
                "Title": var_title,
                "Bullet 1": bullets[0][:500] if len(bullets) > 0 else "",
                "Bullet 2": bullets[1][:500] if len(bullets) > 1 else "",
                "Bullet 3": bullets[2][:500] if len(bullets) > 2 else "",
                "Bullet 4": bullets[3][:500] if len(bullets) > 3 else "",
                "Bullet 5": bullets[4][:500] if len(bullets) > 4 else "",
                "Description": content.get("description", ""),
                "Backend Keywords": content.get("backend_keywords", ""),
                "Color": color,
                "Size": normalize_size(size),
                "UPC": upc,
                "Price": style.get("list_price", ""),
                "Category": style.get("subclass", ""),
                "Brand": brand,
            })
    
    output.seek(0)
    safe_brand = re.sub(r'[^\w]', '_', brand)
    filename = f"NIS_{safe_brand}_Content.csv"
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        as_attachment=True,
        download_name=filename,
        mimetype="text/csv",
    )


# ── Main ───────────────────────────────────────────────────────────────────────


# ═══ CATALOG HEALTH ═══
import csv
# numpy removed — not needed

# ── Catalog Health: in-memory session storage ──────────────────────────────────
catalog_health_state = {
    "catalog_data": None,        # list of dicts (rows)
    "sales_data": None,          # list of dicts (rows)
    "analysis": None,            # full analysis result
    "detected_fields": None,     # mapping of internal_field -> column_name
    "detected_format": None,     # e.g. "Vendor Central", "Seller Central", "Custom"
    "progress": {"status": "idle", "processed": 0, "total": 0, "message": ""},
}
catalog_health_lock = threading.Lock()

# ── Fuzzy column detection maps ───────────────────────────────────────────────
CATALOG_FIELD_MAP = {
    "asin":             ["asin", "asin1", "child asin", "child_asin"],
    "parent_asin":      ["parent asin", "parent_asin", "parent sku", "parent_sku"],
    "sku":              ["sku", "seller-sku", "seller_sku", "vendor sku", "item_sku"],
    "title":            ["title", "item_name", "item-name", "product title", "item name"],
    "brand":            ["brand", "brand_name", "brand name"],
    "color":            ["color", "color name", "color_name", "color map", "color_map"],
    "size":             ["size", "product - size", "size_name", "apparel size value", "apparel_size"],
    "bullet_1":         ["bullet point 1", "bullet_point1", "key product features 1", "bullet1"],
    "bullet_2":         ["bullet point 2", "bullet_point2", "key product features 2", "bullet2"],
    "bullet_3":         ["bullet point 3", "bullet_point3", "key product features 3", "bullet3"],
    "bullet_4":         ["bullet point 4", "bullet_point4", "key product features 4", "bullet4"],
    "bullet_5":         ["bullet point 5", "bullet_point5", "key product features 5", "bullet5"],
    "description":      ["description", "product_description", "product description"],
    "backend_keywords": ["generic keywords", "generic_keywords", "search terms", "search_terms", "backend keywords"],
    "main_image":       ["main image url", "main_image_url", "image-url", "main image"],
    "other_images":     ["other image url", "other_image_url", "other_image_url1", "image url 2", "image-url-2"],
    "price":            ["price", "list price", "standard_price", "your price"],
    "quantity":         ["quantity", "amzn ioh", "fulfillable quantity", "quantity available"],
    "category":         ["sub-class name", "sub_class_name", "product_type", "item_type", "category"],
    "subcategory":      ["sub sub-class name", "sub_sub_class_name", "subcategory"],
    "style":            ["style #", "style number", "model number", "style_num", "style_number"],
    "parent_child":     ["parent_child", "parentage level", "parentage", "parent/child"],
    "variation_theme":  ["variation_theme", "variation theme name", "variation theme"],
    "status":           ["status", "listing status"],
    "image_count":      ["image count"],
}

SALES_FIELD_MAP = {
    "asin":     ["asin", "child asin"],
    "sessions": ["sessions", "glance views", "glance_views", "page views"],
    "units":    ["units ordered", "shipped units", "shipped_units", "units"],
    "revenue":  ["ordered product sales", "shipped revenue", "shipped_revenue", "revenue"],
    "cvr":      ["unit session percentage", "conversion rate", "conversion_rate", "cvr"],
}

SEVERITY_WEIGHTS = {
    "Orphan (no parent link)":         10,
    "Missing from variation matrix":    8,
    "Zero traffic / suppressed":        9,
    "Missing all bullet points":        6,
    "Missing main image":               7,
    "Missing backend keywords":         4,
    "Short title (<80 chars)":          3,
    "Missing description":              3,
    "Inconsistent title format":        2,
    "Single-child parent":              2,
    "Duplicate variation":              5,
    "Wrong parent link (brand mismatch)": 6,
    "Broken variation theme":           5,
    "Content issue killing conversion": 8,
}


def _norm(s):
    """Normalize a column header for fuzzy matching."""
    return str(s).lower().strip().replace("_", " ").replace("-", " ")


def detect_columns(headers, field_map):
    """
    Fuzzy-match headers to internal field names.
    Returns {internal_field: actual_header} for matched fields.
    """
    detected = {}
    header_norm = {_norm(h): h for h in headers}
    
    for field, candidates in field_map.items():
        for cand in candidates:
            cand_norm = _norm(cand)
            # Exact normalized match
            if cand_norm in header_norm:
                detected[field] = header_norm[cand_norm]
                break
            # Substring match
            for hn, orig in header_norm.items():
                if cand_norm in hn or hn in cand_norm:
                    detected[field] = orig
                    break
            if field in detected:
                break
    
    return detected


def detect_format(headers, detected_fields):
    """Guess whether this looks like Vendor Central, Seller Central, or custom."""
    header_set = {_norm(h) for h in headers}
    if any("vendor" in h for h in header_set):
        return "Vendor Central"
    if any("seller" in h or "seller-sku" in _norm(h) for h in header_set):
        return "Seller Central"
    if "asin" in detected_fields:
        return "Custom (ASIN-based)"
    return "Custom"


def read_file_to_rows(file_storage):
    """Read uploaded file (CSV, TSV, XLSX) into list of dicts. No pandas needed."""
    filename = file_storage.filename.lower()
    content = file_storage.read()
    
    if filename.endswith(".xlsx") or filename.endswith(".xls") or filename.endswith(".xlsm"):
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        raw_headers = next(rows_iter, None)
        if not raw_headers:
            return [], []
        headers = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(raw_headers)]
        records = []
        for row_vals in rows_iter:
            row_dict = {}
            for i, val in enumerate(row_vals):
                if i < len(headers):
                    row_dict[headers[i]] = str(val).strip() if val is not None else ""
            if any(v for v in row_dict.values()):
                records.append(row_dict)
        wb.close()
        return records, headers
    else:
        # CSV or TSV
        text = content.decode("utf-8", errors="replace")
        # Detect separator
        first_line = text.split("\n")[0] if text else ""
        sep = "\t" if "\t" in first_line else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=sep)
        headers = [str(f).strip() for f in (reader.fieldnames or [])]
        records = []
        for row in reader:
            cleaned = {str(k).strip(): str(v).strip() if v else "" for k, v in row.items()}
            if any(v for v in cleaned.values()):
                records.append(cleaned)
        return records, headers


def score_content(row, detected_fields):
    """Compute 0-100 content completeness score for a single ASIN row."""
    score = 0
    issues = []

    def get(field):
        col = detected_fields.get(field)
        return str(row.get(col, "")).strip() if col else ""

    # Title: 15 pts
    title = get("title")
    if title:
        if 80 <= len(title) <= 200:
            score += 15
        elif len(title) < 80:
            score += 7
            issues.append("Short title (<80 chars)")
        else:
            score += 12  # Over 200 but present
    
    # Bullets: 15 pts (3 per bullet)
    for b in ["bullet_1", "bullet_2", "bullet_3", "bullet_4", "bullet_5"]:
        btext = get(b)
        if btext and len(btext) >= 50:
            score += 3
        elif btext:
            score += 1  # Partial credit
    if not any(get(f"bullet_{i}") for i in range(1, 6)):
        issues.append("Missing all bullet points")
    
    # Description: 10 pts
    desc = get("description")
    if desc and len(desc) >= 200:
        score += 10
    elif desc:
        score += 5
    else:
        issues.append("Missing description")
    
    # Backend keywords: 10 pts
    kw = get("backend_keywords")
    if kw:
        kw_bytes = len(kw.encode("utf-8"))
        score += 10 if kw_bytes <= 250 else 7
    else:
        issues.append("Missing backend keywords")
    
    # Main image: 10 pts
    if get("main_image"):
        score += 10
    else:
        issues.append("Missing main image")
    
    # Other images (6+): 10 pts
    img_count = 0
    if detected_fields.get("image_count"):
        try:
            img_count = int(get("image_count") or 0)
        except:
            pass
    else:
        for i in range(2, 10):
            col = detected_fields.get("other_images")
            if col:
                # Multi-image columns: check count
                img_count = 1 if get("other_images") else 0
                break
    img_count_bonus = img_count
    # Also count any columns that look like image URLs
    for col in [c for c in row if "image" in _norm(c) and c != detected_fields.get("main_image")]:
        if str(row.get(col, "")).strip():
            img_count_bonus += 1
    if img_count_bonus >= 6:
        score += 10
    elif img_count_bonus >= 3:
        score += 5

    # Price: 10 pts
    try:
        price_raw = get("price").replace("$", "").replace(",", "").strip()
        if price_raw and float(price_raw) > 0:
            score += 10
    except:
        pass
    
    # Brand: 5 pts
    if get("brand"):
        score += 5
    
    # Color + Size: 5 pts
    if get("color") or get("size"):
        score += 5
    
    # Category: 10 pts
    if get("category"):
        score += 10
    
    return min(100, score), issues


def score_color(score):
    if score >= 90:
        return "green"
    elif score >= 70:
        return "yellow"
    elif score >= 50:
        return "orange"
    return "red"


def run_catalog_analysis(rows, detected_fields, sales_lookup=None):
    """
    Full catalog health analysis. Returns structured result dict.
    Progress is updated via catalog_health_state["progress"].
    """
    state = catalog_health_state

    def get(row, field):
        col = detected_fields.get(field)
        return str(row.get(col, "")).strip() if col else ""

    total = len(rows)
    state["progress"] = {"status": "running", "processed": 0, "total": total, "message": "Starting analysis..."}

    # Build lookup structures
    parent_map = {}        # parent_asin -> list of child rows
    asin_map = {}          # asin -> row
    real_parents = set()   # ASINs that actually have parentage="parent"
    
    for i, row in enumerate(rows):
        asin = get(row, "asin") or get(row, "sku")
        if asin:
            asin_map[asin] = row
        p_asin = get(row, "parent_asin")
        pc = _norm(get(row, "parent_child"))
        if pc in ("parent",):
            real_parents.add(asin)
            if asin not in parent_map:
                parent_map[asin] = []
        elif p_asin:
            if p_asin not in parent_map:
                parent_map[p_asin] = []
            parent_map[p_asin].append(row)
        
        if (i + 1) % 1000 == 0:
            state["progress"]["processed"] = i + 1
            state["progress"]["message"] = f"Building lookup structures... {i+1}/{total}"

    # Content scoring + structural checks per ASIN
    issues_list = []
    scored_rows = []
    
    brands_seen = set()
    categories_seen = set()
    subcategories_seen = set()
    
    score_dist = {"green": 0, "yellow": 0, "orange": 0, "red": 0}
    
    for i, row in enumerate(rows):
        asin = get(row, "asin") or get(row, "sku") or f"row_{i}"
        title = get(row, "title")
        brand = get(row, "brand")
        category = get(row, "category")
        subcategory = get(row, "subcategory")
        p_asin = get(row, "parent_asin")
        pc = _norm(get(row, "parent_child"))
        
        if brand:
            brands_seen.add(brand)
        if category:
            categories_seen.add(category)
        if subcategory:
            subcategories_seen.add(subcategory)
        
        content_score, content_issues = score_content(row, detected_fields)
        color = score_color(content_score)
        score_dist[color] += 1
        
        structural_issues = []
        
        # Orphan check: parent must actually exist as a parent row in dataset
        if pc in ("child", "variation") or p_asin:
            if not p_asin:
                structural_issues.append("Orphan (no parent link)")
            elif p_asin not in real_parents and p_asin not in asin_map:
                structural_issues.append("Orphan (no parent link)")
        
        # Wrong parent link (brand mismatch)
        if p_asin and p_asin in asin_map:
            parent_brand = get(asin_map[p_asin], "brand")
            if parent_brand and brand and parent_brand.lower() != brand.lower():
                structural_issues.append("Wrong parent link (brand mismatch)")
        
        # Single-child parent
        if pc == "parent":
            children = parent_map.get(asin, [])
            if len(children) == 1:
                structural_issues.append("Single-child parent")
        
        # Broken variation theme
        if detected_fields.get("variation_theme"):
            vt = get(row, "variation_theme")
            if vt and pc in ("child", "variation"):
                if not get(row, "color") and not get(row, "size"):
                    structural_issues.append("Broken variation theme")
        
        # Revenue cross-reference
        rev_impact = 0.0
        revenue_issues = []
        if sales_lookup and asin in sales_lookup:
            sale = sales_lookup[asin]
            try:
                sessions = float(str(sale.get("sessions", 0)).replace(",", "") or 0)
                units = float(str(sale.get("units", 0)).replace(",", "") or 0)
                revenue = float(str(sale.get("revenue", 0)).replace("$", "").replace(",", "") or 0)
                
                rev_impact = revenue
                
                if sessions == 0 and structural_issues:
                    revenue_issues.append("Zero traffic / suppressed")
                elif sessions > 0 and units == 0 and content_score < 70:
                    revenue_issues.append("Content issue killing conversion")
            except:
                pass
        elif sales_lookup and asin not in sales_lookup:
            # Check if it's an orphan with siblings
            if "Orphan (no parent link)" in structural_issues and p_asin:
                siblings = parent_map.get(p_asin, [])
                sibling_rev = []
                for sib in siblings:
                    sib_asin = get(sib, "asin") or get(sib, "sku")
                    if sib_asin and sib_asin in sales_lookup:
                        try:
                            sibling_rev.append(float(str(sales_lookup[sib_asin].get("revenue", 0)).replace("$","").replace(",","") or 0))
                        except:
                            pass
                if sibling_rev:
                    rev_impact = sum(sibling_rev) / len(sibling_rev)
        
        all_issues = structural_issues + content_issues + revenue_issues
        
        row_result = {
            "asin": asin,
            "title": title[:80] + ("..." if len(title) > 80 else "") if title else "",
            "brand": brand,
            "category": category,
            "subcategory": subcategory,
            "content_score": content_score,
            "score_color": color,
            "parent_asin": p_asin,
            "parent_child": pc,
            "issues": all_issues,
            "revenue_impact": round(rev_impact, 2),
        }
        
        # Compute priority score for each issue
        for issue in all_issues:
            severity = SEVERITY_WEIGHTS.get(issue, 2)
            priority = severity * max(1, rev_impact / 100 if rev_impact > 0 else 1) if rev_impact > 0 else severity
            issues_list.append({
                "priority": round(priority, 2),
                "asin": asin,
                "title": row_result["title"],
                "brand": brand,
                "category": category,
                "issue": issue,
                "severity": severity,
                "severity_label": _severity_label(severity),
                "revenue_impact": round(rev_impact, 2),
                "content_score": content_score,
                "fix_action": _fix_action(issue),
            })
        
        scored_rows.append(row_result)
        
        if (i + 1) % 500 == 0:
            state["progress"]["processed"] = i + 1
            state["progress"]["message"] = f"Analyzing ASINs... {i+1}/{total}"
    
    # Missing variation matrix check
    variation_matrix = {}
    for p_asin, children in parent_map.items():
        colors = set()
        sizes = set()
        present = set()
        for child in children:
            c = get(child, "color")
            s = get(child, "size")
            if c:
                colors.add(c)
            if s:
                sizes.add(s)
            if c and s:
                present.add((c, s))
        if colors and sizes:
            expected = {(c, s) for c in colors for s in sizes}
            missing = expected - present
            for (mc, ms) in missing:
                issues_list.append({
                    "priority": 8,
                    "asin": p_asin,
                    "title": f"[Parent] {asin_map.get(p_asin, {}).get(detected_fields.get('title',''), '')[:60]}",
                    "brand": get(asin_map.get(p_asin, {}), "brand") if p_asin in asin_map else "",
                    "category": get(asin_map.get(p_asin, {}), "category") if p_asin in asin_map else "",
                    "issue": "Missing from variation matrix",
                    "severity": 8,
                    "severity_label": "High",
                    "revenue_impact": 0,
                    "content_score": 0,
                    "fix_action": f"Add variant: Color={mc}, Size={ms}",
                })
            if colors and sizes:
                variation_matrix[p_asin] = {
                    "colors": sorted(colors),
                    "sizes": sorted(sizes),
                    "present": [list(pair) for pair in present],
                    "missing": [list(pair) for pair in missing],
                }
    
    # Duplicate children check
    seen_variants = {}
    for row in rows:
        p = get(row, "parent_asin")
        c = get(row, "color")
        s = get(row, "size")
        if p and c and s:
            key = (p, _norm(c), _norm(s))
            if key in seen_variants:
                asin = get(row, "asin") or get(row, "sku")
                issues_list.append({
                    "priority": 5,
                    "asin": asin,
                    "title": get(row, "title")[:60],
                    "brand": get(row, "brand"),
                    "category": get(row, "category"),
                    "issue": "Duplicate variation",
                    "severity": 5,
                    "severity_label": "Medium",
                    "revenue_impact": 0,
                    "content_score": 0,
                    "fix_action": f"Duplicate of {seen_variants[key]}. Remove one.",
                })
            else:
                seen_variants[key] = get(row, "asin") or get(row, "sku")
    
    # Sort issues by priority (desc)
    issues_list.sort(key=lambda x: x["priority"], reverse=True)
    
    # Summary stats
    total_parents = sum(1 for r in scored_rows if r["parent_child"] == "parent")
    total_children = sum(1 for r in scored_rows if r["parent_child"] in ("child", "variation"))
    if total_parents == 0 and total_children == 0:
        total_parents = len(parent_map)
        total_children = total - total_parents
    
    avg_score = round(sum(r["content_score"] for r in scored_rows) / max(1, len(scored_rows)), 1)
    critical_count = sum(1 for iss in issues_list if iss["severity"] >= 8)
    total_revenue_at_risk = round(sum(iss["revenue_impact"] for iss in issues_list if iss["revenue_impact"] > 0), 2)
    
    state["progress"] = {"status": "done", "processed": total, "total": total, "message": "Analysis complete"}
    
    return {
        "summary": {
            "total_asins": total,
            "total_parents": total_parents,
            "total_children": total_children,
            "avg_score": avg_score,
            "critical_issues": critical_count,
            "total_issues": len(issues_list),
            "revenue_at_risk": total_revenue_at_risk,
            "score_distribution": score_dist,
            "brands": sorted(brands_seen),
            "categories": sorted(categories_seen),
            "subcategories": sorted(subcategories_seen),
        },
        "issues": issues_list[:5000],  # cap for response size
        "scored_rows": scored_rows[:5000],
        "variation_matrix": variation_matrix,
        "has_sales_data": sales_lookup is not None,
    }


def _severity_label(weight):
    if weight >= 9:
        return "Critical"
    elif weight >= 7:
        return "High"
    elif weight >= 4:
        return "Medium"
    return "Low"


def _fix_action(issue):
    actions = {
        "Orphan (no parent link)":          "Set parent_asin field to link this child to its parent",
        "Missing from variation matrix":    "Create new child ASIN for this color/size combination",
        "Zero traffic / suppressed":        "Check listing status, parent link, and image compliance",
        "Missing all bullet points":        "Write 5 bullet points, each >50 characters",
        "Missing main image":               "Upload a compliant main image (white background, 1000px+)",
        "Missing backend keywords":         "Add search terms (<250 bytes, no repeated words)",
        "Short title (<80 chars)":          "Expand title to 80-200 characters with key attributes",
        "Missing description":              "Write product description >200 characters",
        "Inconsistent title format":        "Align title format with brand title formula",
        "Single-child parent":              "Add more child variations or merge into standalone ASIN",
        "Duplicate variation":              "Remove or merge the duplicate child ASIN",
        "Wrong parent link (brand mismatch)":"Correct parent_asin or update brand to match parent",
        "Broken variation theme":           "Add required variation fields (color, size) for this child",
        "Content issue killing conversion": "Improve bullets, images, and description to boost CVR",
    }
    return actions.get(issue, "Review and fix this field")


# ── CATALOG HEALTH ENDPOINTS ───────────────────────────────────────────────────

@app.route("/api/catalog/upload-catalog", methods=["POST"])
def catalog_upload_catalog():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    
    try:
        rows, headers = read_file_to_rows(f)
        
        if len(rows) > 60000:
            return jsonify({"error": "File too large. Max 60,000 rows."}), 400
        
        detected_fields = detect_columns(headers, CATALOG_FIELD_MAP)
        fmt = detect_format(headers, detected_fields)
        
        mapped_count = len(detected_fields)
        total_fields = len(CATALOG_FIELD_MAP)
        missing_fields = [k for k in CATALOG_FIELD_MAP if k not in detected_fields]
        
        with catalog_health_lock:
            catalog_health_state["catalog_data"] = rows
            catalog_health_state["detected_fields"] = detected_fields
            catalog_health_state["detected_format"] = fmt
            catalog_health_state["analysis"] = None
            catalog_health_state["progress"] = {"status": "idle", "processed": 0, "total": 0, "message": ""}
        
        # Run analysis in background thread
        sales_lookup = None
        if catalog_health_state.get("sales_data"):
            sales_data = catalog_health_state["sales_data"]
            sales_fields = catalog_health_state.get("sales_fields", {})
            def sg(row, field):
                col = sales_fields.get(field)
                return str(row.get(col, "")).strip() if col else ""
            sales_lookup = {sg(r, "asin"): r for r in sales_data if sg(r, "asin")}
        
        def run_analysis():
            result = run_catalog_analysis(rows, detected_fields, sales_lookup)
            with catalog_health_lock:
                catalog_health_state["analysis"] = result
        
        t = threading.Thread(target=run_analysis, daemon=True)
        t.start()
        
        return jsonify({
            "ok": True,
            "format": fmt,
            "rows": len(rows),
            "mapped_count": mapped_count,
            "total_fields": total_fields,
            "missing_fields": missing_fields,
            "detected_fields": {k: v for k, v in detected_fields.items()},
            "detection_summary": f"Detected {mapped_count} of {total_fields} fields. Missing: {', '.join(missing_fields) if missing_fields else 'none'}",
        })
    
    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {str(e)}"}), 500


@app.route("/api/catalog/upload-sales", methods=["POST"])
def catalog_upload_sales():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    
    try:
        rows, headers = read_file_to_rows(f)
        sales_fields = detect_columns(headers, SALES_FIELD_MAP)
        
        def sg(row, field):
            col = sales_fields.get(field)
            return str(row.get(col, "")).strip() if col else ""
        
        sales_lookup = {sg(r, "asin"): r for r in rows if sg(r, "asin")}
        
        with catalog_health_lock:
            catalog_health_state["sales_data"] = rows
            catalog_health_state["sales_fields"] = sales_fields
        
        # Re-run analysis if catalog is already loaded
        if catalog_health_state.get("catalog_data"):
            catalog_rows = catalog_health_state["catalog_data"]
            detected_fields = catalog_health_state["detected_fields"]
            
            def run_analysis():
                result = run_catalog_analysis(catalog_rows, detected_fields, sales_lookup)
                with catalog_health_lock:
                    catalog_health_state["analysis"] = result
            
            t = threading.Thread(target=run_analysis, daemon=True)
            t.start()
        
        return jsonify({
            "ok": True,
            "rows": len(rows),
            "asins_matched": len(sales_lookup),
            "fields": list(sales_fields.keys()),
        })
    
    except Exception as e:
        return jsonify({"error": f"Failed to parse sales file: {str(e)}"}), 500


@app.route("/api/catalog/results")
def catalog_results():
    progress = catalog_health_state.get("progress", {})
    analysis = catalog_health_state.get("analysis")
    
    if not analysis and progress.get("status") == "running":
        return jsonify({
            "status": "running",
            "progress": progress,
        })
    
    if not analysis:
        return jsonify({"status": "idle"})
    
    return jsonify({
        "status": "done",
        "analysis": analysis,
        "progress": progress,
    })


@app.route("/api/catalog/progress")
def catalog_progress():
    progress = catalog_health_state.get("progress", {"status": "idle", "processed": 0, "total": 0, "message": ""})
    pct = 0
    if progress.get("total", 0) > 0:
        pct = round(progress["processed"] / progress["total"] * 100)
    return jsonify({**progress, "percent": pct})


@app.route("/api/catalog/fix-file")
def catalog_fix_file():
    analysis = catalog_health_state.get("analysis")
    if not analysis:
        return jsonify({"error": "No analysis available"}), 404
    
    issues = analysis.get("issues", [])
    
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["ASIN", "Title", "Brand", "Category", "Issue", "Severity", "Revenue Impact", "Fix Action"])
    writer.writeheader()
    for iss in issues:
        writer.writerow({
            "ASIN": iss["asin"],
            "Title": iss["title"],
            "Brand": iss["brand"],
            "Category": iss["category"],
            "Issue": iss["issue"],
            "Severity": iss["severity_label"],
            "Revenue Impact": f"${iss['revenue_impact']:,.2f}" if iss["revenue_impact"] else "",
            "Fix Action": iss["fix_action"],
        })
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        as_attachment=True,
        download_name="Catalog_Fix_File.csv",
        mimetype="text/csv",
    )


@app.route("/api/catalog/export")
def catalog_export():
    analysis = catalog_health_state.get("analysis")
    if not analysis:
        return jsonify({"error": "No analysis available"}), 404
    
    scored_rows = analysis.get("scored_rows", [])
    
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["ASIN", "Title", "Brand", "Category", "Subcategory", "Content Score", "Score Grade", "Parent ASIN", "Issues", "Revenue Impact"])
    writer.writeheader()
    for r in scored_rows:
        writer.writerow({
            "ASIN": r["asin"],
            "Title": r["title"],
            "Brand": r["brand"],
            "Category": r["category"],
            "Subcategory": r.get("subcategory", ""),
            "Content Score": r["content_score"],
            "Score Grade": r["score_color"].upper(),
            "Parent ASIN": r.get("parent_asin", ""),
            "Issues": "; ".join(r.get("issues", [])),
            "Revenue Impact": f"${r['revenue_impact']:,.2f}" if r.get("revenue_impact") else "",
        })
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        as_attachment=True,
        download_name="Catalog_Health_Full_Analysis.csv",
        mimetype="text/csv",
    )



# ═══ MERGE LISTINGS MODULE ═══════════════════════════════════════════════════

# In-memory merge state
merge_state = {
    "plan": None,          # list of merge actions
    "approved": {},        # action_id -> True/False
    "generated_at": None,
}
merge_lock = threading.Lock()


def _build_merge_plan(catalog_data, detected_fields):
    """
    Analyse catalog_data and produce a list of merge action dicts.
    """
    def get(row, field):
        col = detected_fields.get(field)
        return str(row.get(col, "")).strip() if col else ""

    # Build structures
    asin_map = {}          # asin -> row
    parent_map = {}        # parent_asin -> [child rows]
    real_parents = set()   # ASINs that have parentage == "parent"
    model_to_asins = {}    # model_name -> [asin list]

    for row in catalog_data:
        asin = get(row, "asin") or get(row, "sku")
        if not asin:
            continue
        asin_map[asin] = row
        pc = get(row, "parent_child").lower()
        if pc == "parent":
            real_parents.add(asin)
            if asin not in parent_map:
                parent_map[asin] = []
        p_asin = get(row, "parent_asin")
        if p_asin and pc != "parent":
            if p_asin not in parent_map:
                parent_map[p_asin] = []
            parent_map[p_asin].append(row)
        # Model name grouping
        model = get(row, "model_name") or get(row, "sku")
        if model:
            model_base = re.split(r"[-_](?:XS|S|M|L|XL|XXL|2XL|3XL|BLACK|WHITE|RED|BLUE|GREEN|NAVY|[A-Z]{1,2}\d{0,2})$",
                                   model.upper())[0].strip()
            if model_base not in model_to_asins:
                model_to_asins[model_base] = []
            model_to_asins[model_base].append(asin)

    actions = []
    action_id = 0

    # ── 1. Split families: same model_name → multiple parent ASINs ──────────
    for model_base, asins_in_family in model_to_asins.items():
        if len(asins_in_family) < 2:
            continue
        parents_in_family = [a for a in asins_in_family if a in real_parents]
        if len(parents_in_family) <= 1:
            continue
        # Primary = the one with most children
        primary = max(parents_in_family, key=lambda p: len(parent_map.get(p, [])))
        secondary_parents = [p for p in parents_in_family if p != primary]
        for sec_parent in secondary_parents:
            children_to_move = parent_map.get(sec_parent, [])
            affected = [get(c, "asin") or get(c, "sku") for c in children_to_move] + [sec_parent]
            affected = [a for a in affected if a]
            primary_title = get(asin_map.get(primary, {}), "title")[:60] if asin_map.get(primary) else primary
            sec_title = get(asin_map.get(sec_parent, {}), "title")[:60] if asin_map.get(sec_parent) else sec_parent
            action_id += 1
            actions.append({
                "id": f"action_{action_id}",
                "action_type": "reassign",
                "affected_asins": affected,
                "from_parent": sec_parent,
                "to_parent": primary,
                "reasoning": f"Model family '{model_base}' is split across {len(parents_in_family)} parent ASINs. "
                             f"Primary parent {primary} has {len(parent_map.get(primary,[]))} children; "
                             f"{sec_parent} has {len(children_to_move)}. Consolidating under primary.",
                "confidence": "High" if len(children_to_move) > 0 else "Medium",
                "from_parent_title": sec_title,
                "to_parent_title": primary_title,
            })

    # ── 2. Orphan fix: children with no valid parent ────────────────────────
    for asin, row in asin_map.items():
        pc = get(row, "parent_child").lower()
        if pc in ("child", "variation", ""):
            p_asin = get(row, "parent_asin")
            if not p_asin or p_asin == asin or p_asin not in asin_map:
                # Try to find a parent by model name
                model = get(row, "model_name") or get(row, "sku")
                model_base = re.split(r"[-_](?:XS|S|M|L|XL|XXL|2XL|3XL|BLACK|WHITE|RED|BLUE|GREEN|NAVY|[A-Z]{1,2}\d{0,2})$",
                                       (model or "").upper())[0].strip() if model else ""
                suggested_parent = None
                if model_base and model_base in model_to_asins:
                    candidates = [a for a in model_to_asins[model_base]
                                  if a in real_parents and a != asin]
                    if candidates:
                        suggested_parent = max(candidates, key=lambda p: len(parent_map.get(p, [])))
                reason = (
                    f"ASIN {asin} has no valid parent link (parent_asin='{p_asin or 'empty'}'). "
                    + (f"Best match found: parent {suggested_parent} in same model family '{model_base}'."
                       if suggested_parent else "No matching parent found — may need to be made standalone or a new parent created.")
                )
                action_id += 1
                actions.append({
                    "id": f"action_{action_id}",
                    "action_type": "orphan_fix",
                    "affected_asins": [asin],
                    "from_parent": p_asin or "",
                    "to_parent": suggested_parent or "",
                    "reasoning": reason,
                    "confidence": "High" if suggested_parent else "Low",
                    "from_parent_title": "",
                    "to_parent_title": get(asin_map.get(suggested_parent, {}), "title")[:60] if suggested_parent else "",
                })

    # ── 3. Category mismatch: child's category differs from parent's ─────────
    for p_asin, children in parent_map.items():
        if p_asin not in asin_map:
            continue
        parent_cat = get(asin_map[p_asin], "category")
        for child_row in children:
            child_asin = get(child_row, "asin") or get(child_row, "sku")
            child_cat = get(child_row, "category")
            if parent_cat and child_cat and parent_cat.lower() != child_cat.lower():
                action_id += 1
                actions.append({
                    "id": f"action_{action_id}",
                    "action_type": "category_fix",
                    "affected_asins": [child_asin],
                    "from_parent": p_asin,
                    "to_parent": p_asin,
                    "reasoning": f"Child {child_asin} has category '{child_cat}' but its parent {p_asin} has category '{parent_cat}'. Child should match parent category.",
                    "confidence": "Medium",
                    "from_parent_title": get(asin_map[p_asin], "title")[:60],
                    "to_parent_title": get(asin_map[p_asin], "title")[:60],
                })

    return actions


@app.route("/api/merge/analyze", methods=["POST"])
def merge_analyze():
    catalog_data = catalog_health_state.get("catalog_data")
    detected_fields = catalog_health_state.get("detected_fields")
    if not catalog_data or not detected_fields:
        return jsonify({"error": "No catalog data loaded. Run Catalog Health upload first."}), 400
    try:
        actions = _build_merge_plan(catalog_data, detected_fields)
        with merge_lock:
            merge_state["plan"] = actions
            merge_state["approved"] = {a["id"]: True for a in actions}
            merge_state["generated_at"] = datetime.now().isoformat()

        # Summary
        split_families = sum(1 for a in actions if a["action_type"] == "reassign")
        orphans = sum(1 for a in actions if a["action_type"] == "orphan_fix")
        category_fixes = sum(1 for a in actions if a["action_type"] == "category_fix")

        return jsonify({
            "ok": True,
            "plan": actions,
            "summary": {
                "split_families": split_families,
                "orphaned_asins": orphans,
                "category_mismatches": category_fixes,
                "total_actions": len(actions),
            },
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/merge/plan", methods=["GET"])
def merge_plan():
    plan = merge_state.get("plan")
    if plan is None:
        return jsonify({"plan": None, "summary": None})
    approved = merge_state.get("approved", {})
    split_families = sum(1 for a in plan if a["action_type"] == "reassign")
    orphans = sum(1 for a in plan if a["action_type"] == "orphan_fix")
    category_fixes = sum(1 for a in plan if a["action_type"] == "category_fix")
    return jsonify({
        "plan": plan,
        "approved": approved,
        "summary": {
            "split_families": split_families,
            "orphaned_asins": orphans,
            "category_mismatches": category_fixes,
            "total_actions": len(plan),
        },
        "generated_at": merge_state.get("generated_at"),
    })


@app.route("/api/merge/approve", methods=["POST"])
def merge_approve():
    data = request.get_json(force=True) or {}
    action_id = data.get("action_id")
    approved = data.get("approved", True)
    if not action_id:
        return jsonify({"error": "action_id required"}), 400
    with merge_lock:
        if merge_state["plan"] is None:
            return jsonify({"error": "No plan loaded"}), 400
        ids = {a["id"] for a in merge_state["plan"]}
        if action_id not in ids:
            return jsonify({"error": "Unknown action_id"}), 404
        merge_state["approved"][action_id] = bool(approved)
    return jsonify({"ok": True, "action_id": action_id, "approved": bool(approved)})


@app.route("/api/merge/generate-fix", methods=["POST"])
def merge_generate_fix():
    plan = merge_state.get("plan")
    approved = merge_state.get("approved", {})
    if not plan:
        return jsonify({"error": "No merge plan. Run analyze first."}), 400

    approved_actions = [a for a in plan if approved.get(a["id"], True)]
    if not approved_actions:
        return jsonify({"error": "No approved actions to generate."}), 400

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ASIN", "Action", "Current Parent", "New Parent",
                     "Variation Theme", "Parentage Level", "Notes"])

    catalog_data = catalog_health_state.get("catalog_data", [])
    detected_fields = catalog_health_state.get("detected_fields", {})

    def get_field(row, field):
        col = detected_fields.get(field)
        return str(row.get(col, "")).strip() if col else ""

    asin_row_map = {}
    for row in catalog_data:
        a = get_field(row, "asin") or get_field(row, "sku")
        if a:
            asin_row_map[a] = row

    for action in approved_actions:
        vt = ""
        for asin in action["affected_asins"]:
            row = asin_row_map.get(asin, {})
            vt = get_field(row, "variation_theme") if row else ""
            parentage = "child"
            if action["action_type"] == "reassign":
                notes = f"Reassign from parent {action['from_parent']} to {action['to_parent']}"
            elif action["action_type"] == "orphan_fix":
                notes = f"Orphan fix — assign to parent {action['to_parent'] or 'TBD'}"
            else:
                notes = f"Category fix under parent {action['to_parent']}"
            writer.writerow([
                asin,
                action["action_type"].replace("_", " ").title(),
                action["from_parent"],
                action["to_parent"],
                vt,
                parentage,
                notes,
            ])

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        as_attachment=True,
        download_name=f"Merge_Fix_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mimetype="text/csv",
    )


# ═══ INTEL MODULE ════════════════════════════════════════════════════════════

intel_state = {
    "recommendations": None,
    "dismissed": set(),
    "accepted": {},
    "generated_at": None,
}
intel_lock = threading.Lock()


def _build_intel_recommendations(catalog_data, detected_fields,
                                  nis_state=None, feedback_data=None):
    """
    Generate ranked intelligence recommendations from all available data.
    """
    def get(row, field):
        col = detected_fields.get(field) if detected_fields else None
        return str(row.get(col, "")).strip() if col else ""

    recs = []
    rec_id = 0

    def new_id():
        nonlocal rec_id
        rec_id += 1
        return f"intel_{rec_id}"

    if not catalog_data:
        return recs

    # Build data structures
    asin_map = {}
    parent_map = {}
    real_parents = set()
    bullet_sets = {}   # bullet_index -> list of (asin, text)
    title_lengths = []

    for row in catalog_data:
        asin = get(row, "asin") or get(row, "sku")
        if not asin:
            continue
        asin_map[asin] = row
        pc = get(row, "parent_child").lower()
        if pc == "parent":
            real_parents.add(asin)
        p_asin = get(row, "parent_asin")
        if p_asin and pc != "parent":
            if p_asin not in parent_map:
                parent_map[p_asin] = []
            parent_map[p_asin].append(asin)

        # Collect bullets
        for i in range(1, 6):
            bullet = get(row, f"bullet_{i}")
            if bullet:
                if i not in bullet_sets:
                    bullet_sets[i] = []
                bullet_sets[i].append((asin, bullet))

        # Title length
        title = get(row, "title")
        if title:
            title_lengths.append((asin, len(title)))

    total_asins = len(asin_map)

    # ── 1. Duplicate bullets across ASINs ────────────────────────────────────
    for bullet_idx, entries in bullet_sets.items():
        text_to_asins = {}
        for asin, text in entries:
            t = text.lower().strip()
            if t:
                if t not in text_to_asins:
                    text_to_asins[t] = []
                text_to_asins[t].append(asin)
        for text, asins in text_to_asins.items():
            if len(asins) >= 5:
                severity = "High" if len(asins) >= 20 else "Medium"
                recs.append({
                    "id": new_id(),
                    "type": "content_duplicate",
                    "severity": severity,
                    "title": f"Bullet {bullet_idx} is identical across {len(asins)} ASINs",
                    "description": f"The same bullet point text is used word-for-word on {len(asins)} listings. "
                                   f"Amazon may suppress duplicate content and customers see no differentiation.",
                    "why": f"Exact duplicate: \"{text[:120]}...\" appears on {len(asins)} ASINs. "
                           f"Unique bullet copy improves relevance signals and conversion on style-differentiated products.",
                    "affected_asins": asins[:50],
                    "estimated_impact": "Medium — duplicate content can reduce relevance scoring",
                    "suggested_action": f"Rewrite Bullet {bullet_idx} for each style to highlight unique attributes (color, fit, occasion).",
                    "action_type": "change_bullet",
                })

    # ── 2. Title length optimization ─────────────────────────────────────────
    short_titles = [(a, ln) for a, ln in title_lengths if ln < 80]
    if short_titles:
        severity = "High" if len(short_titles) > total_asins * 0.3 else "Medium"
        recs.append({
            "id": new_id(),
            "type": "title_optimization",
            "severity": severity,
            "title": f"{len(short_titles)} titles are under 80 characters",
            "description": f"{len(short_titles)} of {total_asins} titles use fewer than 80 chars of the 200-char limit. "
                           f"Longer titles with relevant keywords improve search visibility.",
            "why": f"Amazon allows 200 characters for titles. Short titles leave keyword space unused. "
                   f"Adding size range, key features, or occasion keywords can meaningfully improve CTR.",
            "affected_asins": [a for a, _ in short_titles[:50]],
            "estimated_impact": "High — titles are the #1 ranking signal for search",
            "suggested_action": "Expand short titles to include: key feature, target customer, or occasion. Aim for 130-180 chars.",
            "action_type": "change_title",
        })

    very_long_titles = [(a, ln) for a, ln in title_lengths if ln > 190]
    if very_long_titles:
        recs.append({
            "id": new_id(),
            "type": "title_optimization",
            "severity": "Medium",
            "title": f"{len(very_long_titles)} titles exceed 190 characters (at risk of truncation)",
            "description": f"Titles over 200 chars are truncated by Amazon, cutting off important keywords.",
            "why": "Amazon truncates titles at 200 chars in some views. End-of-title keywords are the most likely to be cut.",
            "affected_asins": [a for a, _ in very_long_titles[:50]],
            "estimated_impact": "Medium — truncated titles lose keyword visibility",
            "suggested_action": "Trim these titles to under 190 characters, keeping the most important keywords first.",
            "action_type": "change_title",
        })

    # ── 3. Missing backend keywords (description empty) ───────────────────────
    no_desc = [asin for asin, row in asin_map.items()
               if len(get(row, "description")) < 100]
    if no_desc:
        severity = "Critical" if len(no_desc) > total_asins * 0.5 else "High"
        recs.append({
            "id": new_id(),
            "type": "content_quality",
            "severity": severity,
            "title": f"Description under 100 chars on {len(no_desc)} ASINs",
            "description": f"{len(no_desc)} listings have minimal or empty descriptions. "
                           f"Descriptions provide keyword real estate and help customers make purchase decisions.",
            "why": "Product descriptions index for search and provide backend keyword coverage. "
                   "Empty descriptions miss out on long-tail keyword coverage and reduce Buy Box competitiveness.",
            "affected_asins": no_desc[:50],
            "estimated_impact": "High — descriptions contribute to A9 indexing",
            "suggested_action": "Write 200-500 char descriptions highlighting fabric, fit, care instructions, and occasion suitability.",
            "action_type": "add_keyword",
        })

    # ── 4. Variation gap: parents with fewer children than average ──────────
    child_counts = [len(children) for p, children in parent_map.items() if p in real_parents]
    if child_counts:
        avg_children = sum(child_counts) / len(child_counts)
        thin_parents = [(p, len(parent_map[p])) for p in real_parents
                        if len(parent_map.get(p, [])) < max(2, avg_children * 0.4)]
        if thin_parents:
            recs.append({
                "id": new_id(),
                "type": "variation_gap",
                "severity": "Medium",
                "title": f"{len(thin_parents)} parent ASINs have fewer variations than catalog average",
                "description": f"Catalog average is {avg_children:.1f} children per parent. "
                               f"{len(thin_parents)} parents have significantly fewer. "
                               f"Thin variation families miss size/color opportunities.",
                "why": f"Parents with {avg_children:.0f}+ children capture more organic traffic across size and color searches. "
                       f"Adding common sizes (S-2X) or seasonal colors can significantly expand reach.",
                "affected_asins": [p for p, _ in thin_parents[:50]],
                "estimated_impact": "Medium — more variants = more search coverage",
                "suggested_action": f"Review thin families and consider adding missing sizes or colors. Catalog average is {avg_children:.1f} variants.",
                "action_type": "add_variant",
            })

    # ── 5. Missing bullets ────────────────────────────────────────────────────
    no_bullet_5 = [asin for asin, row in asin_map.items()
                   if not get(row, "bullet_5")]
    if no_bullet_5 and len(no_bullet_5) > 5:
        recs.append({
            "id": new_id(),
            "type": "content_quality",
            "severity": "Medium",
            "title": f"{len(no_bullet_5)} ASINs missing Bullet Point 5",
            "description": f"Amazon allows 5 bullet points. {len(no_bullet_5)} listings only use 4 or fewer, "
                           f"leaving keyword and content opportunity on the table.",
            "why": "Each bullet point is a separate keyword indexing opportunity. "
                   "Bullet 5 is often used for care instructions, compatibility, or brand story — all indexable.",
            "affected_asins": no_bullet_5[:50],
            "estimated_impact": "Low — incremental keyword coverage",
            "suggested_action": "Add Bullet 5 with care instructions, size guidance, or brand/warranty information.",
            "action_type": "change_bullet",
        })

    # ── 6. A/B test suggestions for high-count duplicate bullet families ──────
    families_with_many_dupes = []
    for bullet_idx, entries in bullet_sets.items():
        text_to_asins = {}
        for asin, text in entries:
            t = text.lower().strip()
            if t:
                if t not in text_to_asins:
                    text_to_asins[t] = []
                text_to_asins[t].append(asin)
        most_common = max(text_to_asins.items(), key=lambda x: len(x[1]), default=(None, []))
        if most_common[0] and len(most_common[1]) >= 10:
            families_with_many_dupes.append((bullet_idx, most_common[0], most_common[1]))

    if families_with_many_dupes:
        bullet_idx, text, asins = families_with_many_dupes[0]
        recs.append({
            "id": new_id(),
            "type": "ab_test_suggestion",
            "severity": "Low",
            "title": f"A/B test opportunity: Bullet {bullet_idx} variant on {len(asins)} ASINs",
            "description": f"The most-used bullet ({len(asins)} ASINs) is a candidate for A/B testing. "
                           f"Test a feature-focused variant against the current generic version.",
            "why": f"Current bullet: \"{text[:100]}...\". "
                   f"With {len(asins)} ASINs using this identical copy, a small CTR improvement "
                   f"on a test variant could justify a full rollout.",
            "affected_asins": asins[:20],
            "estimated_impact": "Medium — A/B tests on high-volume bullet copy can yield 5-15% CVR lift",
            "suggested_action": f"Draft an alternative Bullet {bullet_idx} emphasizing a specific feature or benefit. Test on 5-10 ASINs for 30 days.",
            "action_type": "change_bullet",
        })

    # Sort by severity
    severity_order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    recs.sort(key=lambda r: severity_order.get(r["severity"], 4))

    return recs


@app.route("/api/intel/analyze", methods=["POST"])
def intel_analyze():
    catalog_data = catalog_health_state.get("catalog_data")
    detected_fields = catalog_health_state.get("detected_fields")
    if not catalog_data or not detected_fields:
        return jsonify({"error": "No catalog data loaded. Upload data in Catalog Health first."}), 400
    try:
        recs = _build_intel_recommendations(catalog_data, detected_fields)
        with intel_lock:
            intel_state["recommendations"] = recs
            intel_state["dismissed"] = set()
            intel_state["accepted"] = {}
            intel_state["generated_at"] = datetime.now().isoformat()
        critical = sum(1 for r in recs if r["severity"] == "Critical")
        high = sum(1 for r in recs if r["severity"] == "High")
        quick_wins = sum(1 for r in recs
                         if r["severity"] in ("High", "Critical")
                         and r["action_type"] in ("change_bullet", "change_title"))
        return jsonify({
            "ok": True,
            "recommendations": recs,
            "summary": {
                "total": len(recs),
                "critical": critical,
                "high": high,
                "quick_wins": quick_wins,
            },
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/recommendations", methods=["GET"])
def intel_recommendations():
    recs = intel_state.get("recommendations")
    dismissed = intel_state.get("dismissed", set())
    accepted = intel_state.get("accepted", {})
    if recs is None:
        return jsonify({"recommendations": None, "summary": None})
    visible = [r for r in recs if r["id"] not in dismissed]
    critical = sum(1 for r in visible if r["severity"] == "Critical")
    high = sum(1 for r in visible if r["severity"] == "High")
    quick_wins = sum(1 for r in visible
                     if r["severity"] in ("High", "Critical")
                     and r["action_type"] in ("change_bullet", "change_title"))
    return jsonify({
        "recommendations": visible,
        "accepted": accepted,
        "summary": {
            "total": len(visible),
            "critical": critical,
            "high": high,
            "quick_wins": quick_wins,
        },
        "generated_at": intel_state.get("generated_at"),
    })


@app.route("/api/intel/accept", methods=["POST"])
def intel_accept():
    data = request.get_json(force=True) or {}
    rec_id = data.get("rec_id")
    note = data.get("note", "")
    if not rec_id:
        return jsonify({"error": "rec_id required"}), 400
    with intel_lock:
        if intel_state["recommendations"] is None:
            return jsonify({"error": "No recommendations loaded"}), 400
        ids = {r["id"] for r in intel_state["recommendations"]}
        if rec_id not in ids:
            return jsonify({"error": "Unknown rec_id"}), 404
        intel_state["accepted"][rec_id] = {
            "accepted_at": datetime.now().isoformat(),
            "note": note,
        }
    return jsonify({"ok": True, "rec_id": rec_id})


@app.route("/api/intel/dismiss", methods=["POST"])
def intel_dismiss():
    data = request.get_json(force=True) or {}
    rec_id = data.get("rec_id")
    if not rec_id:
        return jsonify({"error": "rec_id required"}), 400
    with intel_lock:
        if intel_state["recommendations"] is None:
            return jsonify({"error": "No recommendations loaded"}), 400
        intel_state["dismissed"].add(rec_id)
    return jsonify({"ok": True, "rec_id": rec_id})


if __name__ == "__main__":
    print("NIS Wizard v3 — TLG Amazon Intelligence starting on http://localhost:5000")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
