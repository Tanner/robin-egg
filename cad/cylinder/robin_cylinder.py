"""Robin Egg enclosure v2: squat cylinder, parametric CadQuery model.

Run:  python3 robin_cylinder.py [outdir]
Writes stl/cylinder_base.stl, cylinder_body.stl, cylinder_chassis.stl, cylinder_top.stl
and the same in step/. outdir defaults to this folder.

Coordinates: z is up, in mm, from the table. +y is the back (USB-C side), +x is the
mute side. Board numbers come from Seeed's STEP model, re-centred on the board with the
PCB underside at z = Z_PCB.

First print: the top is a keyed push-on fit, not the twist lock in the design doc.
"""
import math, os, sys
import cadquery as cq

# ---------------------------------------------------------------- parameters
OD = 115.0
R = OD / 2
WALL = 2.5
R_IN = R - WALL

# base
BASE_T = 4.0            # floor plate
Z_BAND = 13.0           # top of the dark base band; the sound gap runs from here to Z_BAFFLE
CONE_R, CONE_TIP_Z = 30.0, 14.0
POST_R, POST_D = 43.0, 8.0          # 4 posts at 0/90/180/270, clear of the 70 mm speaker frame
POST_ANGLES = (0, 90, 180, 270)
SCREW_CLEAR_D, HEAD_D, HEAD_DEPTH = 3.4, 6.5, 3.2
FOOT_D, FOOT_DEPTH, FOOT_R, FOOT_ANGLES = 10.5, 1.0, 40.0, (45, 165, 285)
BASE_CLEAR = 0.3        # radial clearance between base band and body

# body
Z_BAFFLE = 20.0         # baffle underside; the body sleeve starts here
BAFFLE_T = 3.0
DRIVER_HOLE_D = 66.0
DRIVER_SCREW = 59.35 / 2
DRIVER_PILOT_D = 2.8
INSERT_D, INSERT_DEPTH = 4.0, 5.5
BOSS_D, BOSS_TOP = 8.0, 33.0        # insert bosses rise into the chamber; blind so the box stays sealed
PILOT_TOP = 31.5
Z_CHASSIS = 62.0        # chassis underside (chamber is 39 mm tall inside)
LEDGE_W = 5.0
CHASSIS_T = 3.0
CHASSIS_SCREW_R, CHASSIS_SCREW_ANGLES = 52.5, (45, 165, 285)
CHASSIS_PILOT_D, CHASSIS_CLEAR_D = 2.1, 2.8
WIRE_HOLE = (-38.0, -14.0, 4.0)

# board (Seeed XVF3800 + XIAO)
Z_PCB = 75.0            # PCB underside; 10 mm bay above the chassis
PCB_T = 1.11
BOARD_HOLES = [(21.4, 24.5), (-21.4, 24.5), (0.0, -34.0)]
STANDOFF_D, STANDOFF_PILOT = 5.5, 1.7
BTN_Z = Z_PCB - 1.5     # side-push buttons at x = +-49.7
BTN_TIP_R = 51.6
XIAO_USB_Z = Z_PCB - 2.4
MUTE_SIDE = +1          # +1 if mute is the +x button, -1 if the -x one. Check on the bench.
MICS = [(33, 33), (-33, 33), (33, -33), (-33, -33)]

# top and joint
Z_SEAM = 69.0           # visible seam between body and top
LAP_H = 3.0             # top's wall overlaps the body's thinned lip by this much
LAP_T = 1.2             # body lip keeps the inner R_IN..R_IN+LAP_T
LAP_CLEAR = 0.15
KEY_W, KEY_H = 4.0, 2.0  # tab on the body lip at the front, slot in the top
TOP_GAP, LID_T = 1.5, 2.5
Z_LID = Z_PCB + 2.3 + TOP_GAP       # lid underside
H = Z_LID + LID_T
TOP_BEVEL = 3.0
MIC_HOLE_D, MIC_BOSS_D, MIC_BOSS_GAP = 1.2, 6.0, 0.6
GLOW_R0, GLOW_R1, GLOW_T = 37.0, 43.0, 1.2
USB_POCKET_W, USB_POCKET_Z0, USB_POCKET_Z1 = 13.5, XIAO_USB_Z - 4.5, XIAO_USB_Z + 5.0   # room for the plug's overmould
FLEX_W, FLEX_Z0, FLEX_Z1, FLEX_SLOT = 9.0, Z_SEAM + LAP_H + 0.5, Z_LID - 0.5, 1.0
RESET_HOLE_D = 1.8

# ---------------------------------------------------------------- helpers
def cyl(d, z0, z1, x=0, y=0):
    return cq.Workplane("XY").workplane(offset=z0).center(x, y).circle(d / 2).extrude(z1 - z0)

def tube(r0, r1, z0, z1):
    return cyl(2 * r1, z0, z1).cut(cyl(2 * r0, z0, z1))

def polar(r, a):
    return r * math.cos(math.radians(a)), r * math.sin(math.radians(a))

def radial_box(angle, r_mid, dr, w, z0, z1):
    return cq.Workplane("XY").box(dr, w, z1 - z0).translate((r_mid, 0, (z0 + z1) / 2)).rotate((0, 0, 0), (0, 0, 1), angle)

# ---------------------------------------------------------------- parts
def make_base():
    b = cyl(2 * (R - BASE_CLEAR), 0, BASE_T)
    b = b.union(tube(R - BASE_CLEAR - WALL, R - BASE_CLEAR, 0, Z_BAND))
    cone = cq.Workplane("XY").workplane(offset=BASE_T).circle(CONE_R).workplane(offset=CONE_TIP_Z - BASE_T).circle(1.5).loft()
    b = b.union(cone)
    for a in POST_ANGLES:
        x, y = polar(POST_R, a)
        b = b.union(cyl(POST_D, 0, Z_BAFFLE, x, y))
        b = b.cut(cyl(SCREW_CLEAR_D, -1, Z_BAFFLE + 1, x, y)).cut(cyl(HEAD_D, -1, HEAD_DEPTH, x, y))
    for a in FOOT_ANGLES:
        x, y = polar(FOOT_R, a)
        b = b.cut(cyl(FOOT_D, -1, FOOT_DEPTH, x, y))
    return b

def make_body():
    b = tube(R_IN, R, Z_BAFFLE, Z_SEAM)
    b = b.union(tube(R_IN, R_IN + LAP_T, Z_SEAM, Z_SEAM + LAP_H))
    b = b.union(radial_box(-90, R_IN + LAP_T / 2, LAP_T, KEY_W, Z_SEAM + LAP_H, Z_SEAM + LAP_H + KEY_H))
    b = b.cut(radial_box(90, R - 1, 8, USB_POCKET_W, USB_POCKET_Z0, Z_SEAM + LAP_H + 1))   # USB-C plug notch at the back
    b = b.union(cyl(2 * R_IN + 0.2, Z_BAFFLE, Z_BAFFLE + BAFFLE_T))
    b = b.cut(cyl(DRIVER_HOLE_D, Z_BAFFLE - 1, Z_BAFFLE + BAFFLE_T + 1))
    for sx in (-1, 1):
        for sy in (-1, 1):
            b = b.cut(cyl(DRIVER_PILOT_D, Z_BAFFLE - 1, Z_BAFFLE + BAFFLE_T + 1, sx * DRIVER_SCREW, sy * DRIVER_SCREW))
    for a in POST_ANGLES:
        x, y = polar(POST_R, a)
        b = b.union(cyl(BOSS_D, Z_BAFFLE, BOSS_TOP, x, y))
        b = b.cut(cyl(INSERT_D, Z_BAFFLE - 1, Z_BAFFLE + INSERT_DEPTH, x, y)).cut(cyl(2.5, Z_BAFFLE, PILOT_TOP, x, y))
    # chassis ledge with a 45 degree chamfer underneath so it prints without supports
    ledge = tube(R_IN - LEDGE_W, R_IN + 0.1, Z_CHASSIS - 1.0, Z_CHASSIS)
    chamfer = cq.Workplane("XZ").polyline([(R_IN + 0.1, Z_CHASSIS - 1.0 - LEDGE_W), (R_IN + 0.1, Z_CHASSIS - 1.0), (R_IN - LEDGE_W, Z_CHASSIS - 1.0)]).close().revolve(360, (0, 0, 0), (0, 1, 0))
    b = b.union(ledge).union(chamfer)
    for a in CHASSIS_SCREW_ANGLES:
        x, y = polar(CHASSIS_SCREW_R, a)
        b = b.cut(cyl(CHASSIS_PILOT_D, Z_CHASSIS - 5.5, Z_CHASSIS + 0.1, x, y))
    return b

def make_chassis():
    c = cyl(2 * (R_IN - 0.4), Z_CHASSIS, Z_CHASSIS + CHASSIS_T)
    for x, y in BOARD_HOLES:
        c = c.union(cyl(STANDOFF_D, Z_CHASSIS + CHASSIS_T, Z_PCB, x, y)).cut(cyl(STANDOFF_PILOT, Z_CHASSIS + 1, Z_PCB + 1, x, y))
    for a in CHASSIS_SCREW_ANGLES:
        x, y = polar(CHASSIS_SCREW_R, a)
        c = c.cut(cyl(CHASSIS_CLEAR_D, Z_CHASSIS - 1, Z_CHASSIS + CHASSIS_T + 1, x, y))
    c = c.cut(cyl(WIRE_HOLE[2], Z_CHASSIS - 1, Z_CHASSIS + CHASSIS_T + 1, WIRE_HOLE[0], WIRE_HOLE[1]))
    return c

def make_top():
    outer = cq.Workplane("XY").workplane(offset=Z_SEAM).circle(R).extrude(H - Z_SEAM).faces(">Z").edges().chamfer(TOP_BEVEL)
    t = outer.cut(cyl(2 * R_IN, Z_SEAM - 1, Z_LID))
    # the top's wall is thinned at the bottom to sit over the body's lip
    t = t.cut(cyl(2 * (R_IN + LAP_T + LAP_CLEAR), Z_SEAM - 1, Z_SEAM + LAP_H + LAP_CLEAR))
    # slot for the body's key tab at the front, so the mic holes land over the mics
    t = t.cut(radial_box(-90, R_IN + LAP_T / 2, LAP_T + 0.6, KEY_W + 0.4, Z_SEAM, Z_SEAM + LAP_H + KEY_H + 0.3))
    # mic bosses and holes
    for x, y in MICS:
        t = t.union(cyl(MIC_BOSS_D, Z_PCB + PCB_T + MIC_BOSS_GAP, Z_LID + 0.1, x, y))
        t = t.cut(cyl(MIC_HOLE_D, Z_PCB, H + 1, x, y))
    # thin the lid over the LED ring so it glows as a ring
    t = t.cut(tube(GLOW_R0, GLOW_R1, Z_LID - 1, H - GLOW_T))
    # USB-C pocket at the back, open at the bottom so the top lifts off over the plug area
    t = t.cut(radial_box(90, R - 1, 8, USB_POCKET_W, Z_SEAM - 1, USB_POCKET_Z1))
    # mute flexure: U-slot tab hinged at the top, nub presses the side button
    ang = 0 if MUTE_SIDE > 0 else 180
    slot = radial_box(0, R - 1, 6, FLEX_SLOT, FLEX_Z0, FLEX_Z1).translate((0, FLEX_W / 2, 0)) \
        .union(radial_box(0, R - 1, 6, FLEX_SLOT, FLEX_Z0, FLEX_Z1).translate((0, -FLEX_W / 2, 0))) \
        .union(radial_box(0, R - 1, 6, FLEX_W + FLEX_SLOT, FLEX_Z0 - FLEX_SLOT, FLEX_Z0))
    nub = radial_box(0, (R_IN + BTN_TIP_R + 0.4) / 2, R_IN - BTN_TIP_R - 0.4 + 0.2, 4.0, FLEX_Z0, BTN_Z + 2)
    t = t.cut(slot.rotate((0, 0, 0), (0, 0, 1), ang)).union(nub.rotate((0, 0, 0), (0, 0, 1), ang))
    # paperclip hole for the other (reset) button
    t = t.cut(cq.Workplane("YZ").circle(RESET_HOLE_D / 2).extrude(20).translate((R - 10, 0, BTN_Z)).rotate((0, 0, 0), (0, 0, 1), ang + 180))
    return t

def export(part, name, outdir):
    cq.exporters.export(part, os.path.join(outdir, "stl", "cylinder_" + name + ".stl"), tolerance=0.05, angularTolerance=0.1)
    cq.exporters.export(part, os.path.join(outdir, "step", "cylinder_" + name + ".step"))

if __name__ == "__main__":
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    os.makedirs(os.path.join(outdir, "stl"), exist_ok=True)
    os.makedirs(os.path.join(outdir, "step"), exist_ok=True)
    parts = {"base": make_base(), "body": make_body(), "chassis": make_chassis(), "top": make_top()}
    for n, p in parts.items():
        print("Exporting %s: valid %s, volume %.1f cm3" % (n, p.val().isValid(), p.val().Volume() / 1000), flush=True)
        export(p, n, outdir)
    print("Overall size: %.1f mm across, %.1f mm tall" % (OD, H))
