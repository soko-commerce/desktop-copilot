"""
Heuristic part classifier — maps natural language part descriptions to VIDA categories.

No LLM calls. Uses keyword matching against known VIDA category structure.
Fast (<1ms) and deterministic.
"""

import re
import logging

logger = logging.getLogger(__name__)

# VIDA top-level categories with keyword associations
# Built from real VIDA catalog structure observed across multiple vehicles
CATEGORY_MAP = {
    "2 Engine with mountings and equipment": {
        "keywords": [
            "engine", "motor", "cylinder", "piston", "crankshaft", "camshaft",
            "valve", "turbo", "turbocharger", "oil pump", "oil filter", "oil cooler",
            "engine mount", "timing belt", "timing chain", "spark plug", "ignition",
            "intake manifold", "exhaust manifold", "throttle", "injector",
            "fuel injector", "gasket", "head gasket", "oil pan", "sump",
            "flywheel", "engine block", "connecting rod", "rocker",
        ],
        "part_family": "2 Engine",
    },
    "3 Fuel system, exhaust system": {
        "keywords": [
            "fuel pump", "fuel tank", "fuel line", "fuel filter", "fuel rail",
            "exhaust", "catalytic", "catalyst", "muffler", "silencer", "exhaust pipe",
            "lambda", "oxygen sensor", "dpf", "particulate", "egr",
            "charcoal canister", "evap", "fuel cap", "fuel filler",
        ],
        "part_family": "3 Fuel/Exhaust",
    },
    "3 Electrical system": {
        "keywords": [
            "battery", "alternator", "starter", "wiring", "harness", "fuse",
            "relay", "headlight", "headlamp", "tail light", "rear light",
            "fog light", "indicator", "turn signal", "horn", "wiper", "wiper motor",
            "wiper blade", "washer", "window motor", "window regulator",
            "central locking", "door lock", "key", "immobilizer", "ecu", "module",
            "sensor", "abs sensor", "speed sensor", "temperature sensor",
            "parking sensor", "camera", "radio", "speaker", "amplifier",
            "navigation", "display", "instrument cluster", "gauge",
            "bulb", "led", "lamp", "light",
        ],
        "part_family": "3 Electrical",
    },
    "4 Power transmission": {
        "keywords": [
            "gearbox", "transmission", "clutch", "clutch plate", "clutch kit",
            "flywheel", "dual mass", "torque converter", "transfer case",
            "propeller shaft", "prop shaft", "drive shaft", "driveshaft",
            "cv joint", "cv boot", "differential", "diff", "axle shaft",
            "half shaft", "gear", "synchronizer", "shift", "selector",
            "gear lever", "gear knob", "linkage",
        ],
        "part_family": "4 Power transmission",
    },
    "5 Brakes": {
        "keywords": [
            "brake", "brake pad", "brake pads", "brake disc", "brake disk",
            "brake rotor", "brake caliper", "brake hose", "brake line",
            "brake fluid", "brake master", "brake booster", "brake servo",
            "handbrake", "parking brake", "brake shoe", "brake drum",
            "abs", "brake sensor", "wear indicator", "brake kit",
        ],
        "part_family": "5 Brakes",
    },
    "6 Suspension and steering": {
        "keywords": [
            "suspension", "shock", "shock absorber", "strut", "spring",
            "coil spring", "leaf spring", "control arm", "wishbone",
            "ball joint", "tie rod", "track rod", "steering rack",
            "steering pump", "power steering", "steering column",
            "stabilizer", "sway bar", "anti roll", "bushing", "bush",
            "subframe", "front beam", "crossmember", "front axle beam",
            "engine cradle", "suspension frame", "bearing", "wheel bearing",
            "hub", "wheel hub", "knuckle", "steering knuckle",
            "top mount", "strut mount", "drop link",
        ],
        "part_family": "6 Suspension/Steering",
    },
    "7 Springs and wheels": {
        "keywords": [
            "wheel", "rim", "tyre", "tire", "wheel bolt", "wheel nut",
            "wheel cap", "hub cap", "wheel cover", "alloy",
            "spare wheel", "jack", "tow",
        ],
        "part_family": "7 Springs/Wheels",
    },
    "8 Body and interior": {
        "keywords": [
            "bumper", "fender", "wing", "bonnet", "hood", "boot", "trunk",
            "door", "door handle", "door panel", "door trim", "door seal",
            "mirror", "side mirror", "wing mirror", "rear view",
            "windscreen", "windshield", "rear window", "quarter glass",
            "roof", "sunroof", "panoramic", "spoiler", "grille", "grill",
            "seat", "seat belt", "headrest", "armrest", "console",
            "dashboard", "dash", "carpet", "floor mat", "trim",
            "glove box", "visor", "sun visor", "roof lining",
            "tailgate", "hatch", "boot lid",
        ],
        "part_family": "8 Body/Interior",
    },
    "9 Accessories": {
        "keywords": [
            "accessory", "roof rack", "roof box", "towbar", "tow bar",
            "mud flap", "splash guard", "cargo net", "load cover",
            "child seat", "dog guard",
        ],
        "part_family": "9 Accessories",
    },
}


def is_exact_part_number(query: str) -> bool:
    """Check if query looks like an exact Volvo part number."""
    cleaned = query.strip()
    # Must be a single token (no spaces)
    if " " in cleaned:
        return False
    # Must have at least one digit
    if not re.search(r"\d", cleaned):
        return False
    # Must be alphanumeric (with optional hyphens), 5+ chars
    if not re.match(r"^[A-Za-z0-9\-]{5,}$", cleaned):
        return False
    return True


def classify_part(query: str) -> dict:
    """
    Classify a part query into VIDA categories using keyword matching.

    Returns:
        {
            "predicted_paths": [{"category": str, "subcategory": "", "confidence": float, "reasoning": str}],
            "is_exact_part_number": bool,
            "part_family": str,
        }
    """
    if is_exact_part_number(query):
        return {
            "predicted_paths": [],
            "is_exact_part_number": True,
            "part_family": "exact_part_number",
        }

    query_lower = query.lower().strip()
    scores = []

    for category, info in CATEGORY_MAP.items():
        best_score = 0.0
        best_keyword = ""

        for keyword in info["keywords"]:
            # Exact phrase match (highest confidence)
            if keyword in query_lower:
                # Score based on how much of the query the keyword covers
                coverage = len(keyword) / len(query_lower)
                score = 0.5 + (coverage * 0.5)  # 0.5 to 1.0
                if score > best_score:
                    best_score = score
                    best_keyword = keyword

        if best_score > 0:
            scores.append({
                "category": category,
                "subcategory": "",
                "confidence": round(min(best_score, 0.99), 2),
                "reasoning": f"Query '{query}' matches keyword '{best_keyword}'",
                "part_family": info["part_family"],
            })

    # Sort by confidence descending
    scores.sort(key=lambda x: x["confidence"], reverse=True)

    # Take top 3
    predicted = scores[:3]
    part_family = predicted[0]["part_family"] if predicted else "unknown"

    return {
        "predicted_paths": [
            {"category": s["category"], "subcategory": s["subcategory"],
             "confidence": s["confidence"], "reasoning": s["reasoning"]}
            for s in predicted
        ],
        "is_exact_part_number": False,
        "part_family": part_family,
    }
