"""Fit check for robin_cylinder.py: Seeed board model and a DMA70-4 envelope against the parts, plus chamber volume."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from robin_cylinder import *

base, body, chassis, top = make_base(), make_body(), make_chassis(), make_top()
top_holes = make_top(led_holes=True, crush_ribs=True)
here = os.path.dirname(os.path.abspath(__file__))
board = cq.importers.importStep(os.path.join(here, "..", "..", "reference", "datasheets", "3d", "respeaker_mic_array_xvf3800_1_with-xiao-0820.stp"))
bsol = [s.translate(cq.Vector(-150, 80, Z_PCB)) for s in board.solids().vals()]

def clashes(name, part, solids):
    pv = part.val(); pbb = pv.BoundingBox(); hits = 0
    for s in solids:
        sb = s.BoundingBox()
        if sb.xmax < pbb.xmin or sb.xmin > pbb.xmax or sb.ymax < pbb.ymin or sb.ymin > pbb.ymax or sb.zmax < pbb.zmin or sb.zmin > pbb.zmax:
            continue
        v = pv.intersect(s).Volume()
        if v > 0.01:
            hits += 1; c = sb.center
            print("  Clash between %s and a part at (%.1f, %.1f, %.1f): %.2f mm3" % (name, c.x, c.y, c.z, v))
    print("Board against %s: %d clashes" % (name, hits), flush=True)

for n, p in (("body", body), ("chassis", chassis), ("top", top), ("top_holes", top_holes)):
    clashes(n, p, bsol)

# every LED should sit under open air in the holed top
leds = [s for s in bsol if abs(math.hypot(s.BoundingBox().center.x, s.BoundingBox().center.y) - LED_R) < 0.5 and s.BoundingBox().zmax > Z_PCB + 1.5]
covered = 0
for s in leds:
    c = s.BoundingBox().center
    if top_holes.val().intersect(cyl(1.0, Z_PCB, H + 1, c.x, c.y).val()).Volume() > 0.001:
        covered += 1
        print("  LED at (%.1f, %.1f) has no hole over it" % (c.x, c.y))
print("LEDs under a hole in top_holes: %d of %d" % (len(leds) - covered, len(leds)), flush=True)

# the plain top should not touch the body; the holed top should touch it only at the crush ribs
print("Top against body: %.2f mm3 overlap" % top.val().intersect(body.val()).Volume())
ribs = top_holes.val().intersect(body.val())
print("Holed top against body: %.2f mm3 overlap in %d crush ribs" % (ribs.Volume(), len(ribs.Solids())), flush=True)

# Dayton drawing: 3.0 mm flange, 38.6 mm behind it, 64 mm basket
driver = cyl(64, Z_BAFFLE, Z_BAFFLE + 38.6).union(cq.Workplane("XY").box(70, 70, 3, centered=(True, True, False)).translate((0, 0, Z_BAFFLE - 3)))
for n, p in (("base", base), ("body", body), ("chassis", chassis)):
    v = p.val().intersect(driver.val()).Volume()
    print("Speaker envelope against %s: %.2f mm3 overlap" % (n, v), flush=True)

air = cyl(2 * R_IN, Z_BAFFLE + BAFFLE_T, Z_CHASSIS).cut(body)
gross = air.val().Volume() / 1e6
print("Chamber air %.3f L gross, about %.3f L net after the speaker" % (gross, gross - 0.025))
gap = cyl(2 * R, Z_BAND, Z_BAFFLE).cut(cyl(2 * R_IN, Z_BAND, Z_BAFFLE))
print("Sound gap %.1f mm tall" % (Z_BAFFLE - Z_BAND))
