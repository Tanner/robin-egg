"""Fit check for robin_cylinder.py: Seeed board model and a DMA70-4 envelope against the parts, plus chamber volume."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from robin_cylinder import *

base, body, chassis, top = make_base(), make_body(), make_chassis(), make_top()
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

for n, p in (("body", body), ("chassis", chassis), ("top", top)):
    clashes(n, p, bsol)

driver = cyl(64, Z_BAFFLE, Z_BAFFLE + 36.5).union(cq.Workplane("XY").box(70, 70, 3, centered=(True, True, False)).translate((0, 0, Z_BAFFLE - 3)))
for n, p in (("base", base), ("body", body), ("chassis", chassis)):
    v = p.val().intersect(driver.val()).Volume()
    print("Speaker envelope against %s: %.2f mm3 overlap" % (n, v), flush=True)

air = cyl(2 * R_IN, Z_BAFFLE + BAFFLE_T, Z_CHASSIS).cut(body)
gross = air.val().Volume() / 1e6
print("Chamber air %.3f L gross, about %.3f L net after the speaker" % (gross, gross - 0.025))
gap = cyl(2 * R, Z_BAND, Z_BAFFLE).cut(cyl(2 * R_IN, Z_BAND, Z_BAFFLE))
print("Sound gap %.1f mm tall" % (Z_BAFFLE - Z_BAND))
