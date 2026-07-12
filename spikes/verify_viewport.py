"""Verify F-COORD (Canvas v2): the coordinate contract (world/viewport).

Two halves:
  * BACKEND: /viewport GET/POST round-trips {panX,panY,zoom}, clamps zoom to
    [0.35, 3.0], and persists to state.json (survives a fresh load()).
  * MATH (mirror of static/app.js Viewport.toWorld/toScreen): the transform is
    invertible — toWorld(toScreen(p)) == p at pan/zoom extremes, a pinned world
    coord keeps its world value while its SCREEN pixels move under pan/zoom, and
    zoom clamps. This guards the FORMULA the JS implements; the OWNER VISUAL GATE
    (not this spike) proves every render site routes through it.

Run from the hub root with the hub venv (kill any hub on :5055 first):
    .venv/Scripts/python.exe spikes/verify_viewport.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Windows-native repo: force UTF-8 stdout so the math symbols in detail strings
# (e.g. ∘) don't crash the default cp1252 console.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))

from hub import state  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(prefix="hub_vpspike_", suffix=".json", delete=False)
_tmp.close()
TMP = Path(_tmp.name)
state.STATE_FILE = TMP
state.save({"panels": {}})

from app import app  # noqa: E402

client = app.test_client()
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ---- python mirror of the documented JS formulas (origin 0 0, stage W x H px) ----
STAGE_W, STAGE_H = 1200.0, 800.0


def to_world(sx, sy, panx, pany, zoom):
    return ((sx - panx) / zoom) / STAGE_W * 100, ((sy - pany) / zoom) / STAGE_H * 100


def to_screen(wx, wy, panx, pany, zoom):
    return (wx / 100 * STAGE_W) * zoom + panx, (wy / 100 * STAGE_H) * zoom + pany


print("== F-COORD coordinate contract verification ==")

# ---- (1) transform is invertible at pan/zoom extremes ----
print("\n(1) toWorld(toScreen(p)) == p at pan/zoom extremes")
EXTREMES = [(0.0, 0.0, 1.0), (250.0, -130.0, 0.35), (-600.0, 400.0, 3.0)]
ok_inv = True
for (px, py, z) in EXTREMES:
    for (wx, wy) in [(0, 0), (50, 50), (100, 100), (12.5, 87.3)]:
        sx, sy = to_screen(wx, wy, px, py, z)
        rx, ry = to_world(sx, sy, px, py, z)
        if abs(rx - wx) > 1e-6 or abs(ry - wy) > 1e-6:
            ok_inv = False
check("round-trip identity holds at all extremes", ok_inv, "toWorld∘toScreen != identity")

# ---- (2) a pinned world coord keeps its world value while screen px move ----
print("\n(2) a pinned world point keeps its WORLD value; its SCREEN pixels move with pan/zoom")
s_home = to_screen(50, 50, 0, 0, 1)
s_pan = to_screen(50, 50, 200, 0, 1)
s_zoom = to_screen(50, 50, 0, 0, 2)
check("same world point -> DIFFERENT screen px under pan", s_home[0] != s_pan[0], f"{s_home} vs {s_pan}")
check("same world point -> DIFFERENT screen px under zoom", s_home != s_zoom, f"{s_home} vs {s_zoom}")
# and toWorld of the SAME cursor px yields different world under different viewports (sanity)
check("toWorld depends on the viewport (not a constant)", to_world(600, 400, 0, 0, 1) != to_world(600, 400, 200, 0, 2), "viewport ignored")

# ---- (3) /viewport round-trips + clamps + persists ----
print("\n(3) /viewport GET default, POST round-trip, zoom clamp, persistence")
g0 = client.get("/viewport").get_json()
check("GET /viewport default is home (zoom 1)", g0 == {"panX": 0.0, "panY": 0.0, "zoom": 1.0}, f"{g0}")
p = client.post("/viewport", json={"panX": 120.5, "panY": -40.0, "zoom": 1.75})
check("POST /viewport returns the stored viewport", p.status_code == 200 and p.get_json().get("zoom") == 1.75, f"{p.get_json()}")
g1 = client.get("/viewport").get_json()
check("GET reflects the POSTed viewport", g1 == {"panX": 120.5, "panY": -40.0, "zoom": 1.75}, f"{g1}")
pc = client.post("/viewport", json={"panX": 0, "panY": 0, "zoom": 99}).get_json()
check("zoom clamps to ZMAX 3.0", pc.get("zoom") == 3.0, f"{pc}")
pc2 = client.post("/viewport", json={"panX": 0, "panY": 0, "zoom": 0.01}).get_json()
check("zoom clamps to ZMIN 0.35", pc2.get("zoom") == 0.35, f"{pc2}")
client.post("/viewport", json={"panX": 7.0, "panY": 8.0, "zoom": 1.25})
reloaded = state.load().get("viewport")
check("viewport persisted to disk (survives a fresh load)", reloaded == {"panX": 7.0, "panY": 8.0, "zoom": 1.25}, f"{reloaded}")
pbad = client.post("/viewport", json={"zoom": "nope"})
check("non-numeric viewport -> 400", pbad.status_code == 400, f"{pbad.status_code}")

# ----------------------------------------------------------------------- cleanup
for q in (TMP, TMP.with_suffix(".json.tmp")):
    try:
        q.unlink()
    except OSError:
        pass

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== F-COORD COORDINATE CONTRACT ====================")
print(f"{passed}/{total} checks passed")
print(f"\nF-COORD COORDINATE CONTRACT: {'PASS' if passed == total else 'FAIL'}")
raise SystemExit(0 if passed == total else 1)
