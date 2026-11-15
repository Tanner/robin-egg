"""Robin Egg enclosure, parametric CadQuery model.

Run:  python3 robin_egg.py [outdir]
Writes foot.stl, body.stl, chassis.stl, crown.stl (and .step) plus an assembly STEP.

Coordinates: z is up, in mm, measured from the tip of the unsliced egg (the
table is at z = Z_FLOOR). +y is the back of the egg (USB-C side), +x is the
mute side. Board numbers come from Seeed's STEP model
respeaker_mic_array_xvf3800_1_with-xiao-0820.stp, re-centred on the board.
"""
import math, os, sys
import cadquery as cq

# ---------------------------------------------------------------- parameters
EGG_L, EGG_B, EGG_W = 150.0, 120.0, 10.0   # length, breadth, egg asymmetry (Hugelschaffer)
Z_FLOOR = 13.0          # slice the blunt end flat here (foot about 76 mm across)
WALL = 3.5              # foot and body wall
CROWN_WALL = 2.0        # thinner so the LEDs glow through and holes stay short

# foot / speaker
Z_BAFFLE = 37.0         # underside of the baffle = top of the foot
BAFFLE_T = 3.0
DRIVER_HOLE_D = 66.0    # DMA70-4 basket is 64 mm
DRIVER_SCREW = 59.46 / 2  # 4 x M3 on a 59.5 mm square (84.1 mm circle)
DRIVER_PILOT_D = 2.8
FOOT_FLOOR_T = 3.0
FOOT_POST_R = 43.0      # 4 posts at 0/90/180/270, clear of the 70 mm square frame
FOOT_SCREW_D = 3.4
INSERT_D, INSERT_DEPTH = 4.0, 5.0   # M3 heat-set inserts in the baffle
SLOTS = 20

# chamber / divider (chassis)
Z_LEDGE_TOP = 76.0
LEDGE_IN_R = 48.0
CHASSIS_T = 3.0
CHASSIS_R = 53.2        # must pass the bayonet lip (ID 2*LIP_IN_R)
WIRE_HOLE_D = 4.0
CHASSIS_SCREW_R, CHASSIS_SCREW_ANGLES = 50.6, (45, 165, 285)

# board (Seeed XVF3800 + XIAO)
Z_PCB = 93.0            # underside of the PCB
PCB_T, PCB_R = 1.11, 50.0
BOARD_HOLES = [(21.4, 24.5), (-21.4, 24.5), (0.0, -34.0)]   # M2 slots
STANDOFF_D, STANDOFF_PILOT = 5.5, 1.7
BTN_X, BTN_Z = 49.7, -1.5   # side-push buttons at x = +-49.7, centre 1.5 mm under PCB
XIAO_USB_Y, XIAO_USB_Z = 49.5, -2.4   # port mouth and centre height
MUTE_SIDE = +1          # +1 if mute is the +x button, -1 if it is the -x one. Check on the bench.

# split and bayonet
Z_SPLIT = 86.0
BORE_R = 55.0           # body inner bore near the top
LIP_IN_R, LIP_Z0, LIP_Z1 = 53.7, 82.0, 84.0
SKIRT_R0, SKIRT_R1, SKIRT_Z0 = 51.9, 53.4, 79.5
LUG_R1, LUG_W, LUG_Z0, LUG_Z1 = 54.6, 10.0, 80.0, 81.8
NOTCH_W = 12.5
NOTCH_ANGLES = (0, 120, 240)
LOCK_TWIST = -30        # locked lugs sit 30 deg clockwise (seen from above) of the notches

# crown details
PERF = os.environ.get("PERF", "1") == "1"
PERF_D = 2.6
PERF_Z0, PERF_Z1 = 99.0, 141.0
PERF_N = 380
APEX_HOLE_D = 5.0
USB_POCKET_W, USB_POCKET_Z1 = 13.5, 96.0

# ---------------------------------------------------------------- egg profile
def egg_r(z):
    x = z - EGG_L / 2
    v = (EGG_L**2 - 4 * x * x) / (EGG_L**2 + 8 * EGG_W * x + 4 * EGG_W**2)
    return 0.0 if v <= 0 else EGG_B / 2 * math.sqrt(v)

def _profile_pts(offset):
    """(r, z) points of the outer surface offset inward by `offset` along the normal."""
    zs = [Z_FLOOR - 1 + i * 0.5 for i in range(int((EGG_L - Z_FLOOR + 1) / 0.5))]
    raw = [(egg_r(z), z) for z in zs]
    pts = []
    for i in range(1, len(raw) - 1):
        (r0, z0), (r1, z1), (r2, z2) = raw[i - 1], raw[i], raw[i + 1]
        tr, tz = r2 - r0, z2 - z0
        n = math.hypot(tr, tz)
        nr, nz = tz / n, -tr / n          # outward normal
        pts.append((r1 - offset * nr, z1 - offset * nz))
    pts = [p for p in pts if p[0] > 0.8]
    keep = pts[::4]
    if keep[-1] != pts[-1]:
        keep.append(pts[-1])
    return keep

def egg_solid(offset=0.0):
    pts = _profile_pts(offset)
    top_z = EGG_L - offset                    # apex on the axis
    z_bot = pts[0][1]
    w = (cq.Workplane("XZ").moveTo(0, z_bot).lineTo(*pts[0]).spline(pts[1:], includeCurrent=True)
         .lineTo(0, top_z).close())
    return w.revolve(360, (0, 0, 0), (0, 1, 0))

def slab(z0, z1, r=80):
    return cq.Workplane("XY").workplane(offset=z0).circle(r).extrude(z1 - z0)

def ring(r0, r1, z0, z1):
    return cq.Workplane("XY").workplane(offset=z0).circle(r1).circle(r0).extrude(z1 - z0)

def radial_box(angle, r_mid, dr, w, z0, z1):
    b = cq.Workplane("XY").box(dr, w, z1 - z0, centered=(True, True, False)).translate((r_mid, 0, z0))
    return b.rotate((0, 0, 0), (0, 0, 1), angle)

def sector(r0, r1, a0, a1, z0, z1):
    n = max(4, int(abs(a1 - a0) / 3))
    outer = [(r1 * math.cos(math.radians(a0 + (a1 - a0) * i / n)), r1 * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]
    inner = [(r0 * math.cos(math.radians(a1 - (a1 - a0) * i / n)), r0 * math.sin(math.radians(a1 - (a1 - a0) * i / n))) for i in range(n + 1)]
    return cq.Workplane("XY").workplane(offset=z0).polyline(outer + inner).close().extrude(z1 - z0)

def cyl(d, z0, z1, x=0, y=0):
    return cq.Workplane("XY").workplane(offset=z0).center(x, y).circle(d / 2).extrude(z1 - z0)

OUT = egg_solid(0)
IN_BODY = egg_solid(WALL)
IN_CROWN = egg_solid(CROWN_WALL)

# ---------------------------------------------------------------- foot
def make_foot():
    f = OUT.intersect(slab(Z_FLOOR, Z_BAFFLE)).cut(IN_BODY.intersect(slab(Z_FLOOR + FOOT_FLOOR_T, Z_BAFFLE + 1)))
    # deflector cone under the driver
    cone = cq.Solid.makeCone(28, 3, 13, cq.Vector(0, 0, Z_FLOOR + FOOT_FLOOR_T))
    f = f.union(cq.Workplane().add(cone))
    # screw posts up to the baffle, M3 x 25 from below
    for a in (0, 90, 180, 270):
        x, y = FOOT_POST_R * math.cos(math.radians(a)), FOOT_POST_R * math.sin(math.radians(a))
        f = f.union(cyl(8, Z_FLOOR, Z_BAFFLE, x, y).intersect(OUT))
        f = f.cut(cyl(FOOT_SCREW_D, Z_FLOOR - 1, Z_BAFFLE + 1, x, y))
        f = f.cut(cyl(6.2, Z_FLOOR - 1, Z_FLOOR + 3, x, y))          # head counterbore
    # sound slots round the wall, fabric glued behind
    for i in range(SLOTS):
        a = 360 / SLOTS * (i + 0.5)
        f = f.cut(radial_box(a, 50, 20, 3.5, Z_FLOOR + 6, Z_BAFFLE - 3))
    # rubber feet recesses
    for a in (90, 210, 330):
        x, y = 27 * math.cos(math.radians(a)), 27 * math.sin(math.radians(a))
        f = f.cut(cyl(10.5, Z_FLOOR - 1, Z_FLOOR + 1.0, x, y))
    return f

# ---------------------------------------------------------------- body
def make_body():
    b = OUT.intersect(slab(Z_BAFFLE, Z_SPLIT))
    b = b.cut(IN_BODY.intersect(slab(Z_BAFFLE + BAFFLE_T, 76)))
    b = b.cut(cyl(2 * BORE_R, 76, Z_SPLIT + 1))
    # driver hole and pilot holes
    b = b.cut(cyl(DRIVER_HOLE_D, Z_BAFFLE - 1, Z_BAFFLE + BAFFLE_T + 1))
    for sx in (-1, 1):
        for sy in (-1, 1):
            b = b.cut(cyl(DRIVER_PILOT_D, Z_BAFFLE - 1, Z_BAFFLE + BAFFLE_T + 1, sx * DRIVER_SCREW, sy * DRIVER_SCREW))
    # heat-set inserts for the foot screws
    for a in (0, 90, 180, 270):
        x, y = FOOT_POST_R * math.cos(math.radians(a)), FOOT_POST_R * math.sin(math.radians(a))
        b = b.cut(cyl(INSERT_D, Z_BAFFLE - 1, Z_BAFFLE + INSERT_DEPTH, x, y))
    # ledge for the chassis (45 deg underside, prints without support)
    led = (cq.Workplane("XZ").polyline([(LEDGE_IN_R, Z_LEDGE_TOP), (59, Z_LEDGE_TOP), (59, Z_LEDGE_TOP - 11),
                                         (LEDGE_IN_R, Z_LEDGE_TOP)]).close().revolve(360, (0, 0, 0), (0, 1, 0)))
    b = b.union(led.intersect(OUT))
    for a in CHASSIS_SCREW_ANGLES:   # chassis screws, M2.5 self-tap into wall columns
        x, y = CHASSIS_SCREW_R * math.cos(math.radians(a)), CHASSIS_SCREW_R * math.sin(math.radians(a))
        b = b.union(cyl(7, Z_BAFFLE + BAFFLE_T, Z_LEDGE_TOP, x, y).intersect(OUT))
        b = b.cut(cyl(2.2, Z_LEDGE_TOP - 9, Z_LEDGE_TOP + 1, x, y))
    # bayonet lip with three entry notches
    lip = ring(LIP_IN_R, BORE_R + 0.5, LIP_Z0, LIP_Z1)
    for a in NOTCH_ANGLES:
        lip = lip.cut(radial_box(a, (LIP_IN_R + BORE_R) / 2, 4, NOTCH_W, LIP_Z0 - 1, LIP_Z1 + 1))
    b = b.union(lip)
    return b

# ---------------------------------------------------------------- chassis (divider + board standoffs)
def make_chassis():
    z0 = Z_LEDGE_TOP
    c = cyl(2 * CHASSIS_R, z0, z0 + CHASSIS_T)
    for (x, y) in BOARD_HOLES:
        c = c.union(cyl(STANDOFF_D, z0, Z_PCB, x, y))
        c = c.cut(cyl(STANDOFF_PILOT, z0 + 1, Z_PCB + 1, x, y))
    for a in CHASSIS_SCREW_ANGLES:
        x, y = CHASSIS_SCREW_R * math.cos(math.radians(a)), CHASSIS_SCREW_R * math.sin(math.radians(a))
        c = c.cut(cyl(2.8, z0 - 1, z0 + CHASSIS_T + 1, x, y))
    # wire grommet holes: speaker pigtail near the JST, spare for sensors
    for (x, y) in ((-38.0, -14.0), (-20.0, -40.0)):
        c = c.cut(cyl(WIRE_HOLE_D, z0 - 1, z0 + CHASSIS_T + 1, x, y))
    return c

# ---------------------------------------------------------------- crown
def fib_points(n):
    """Points spread evenly over the crown band, as (x, y, z, nx, ny, nz)."""
    pts, golden = [], math.pi * (3 - math.sqrt(5))
    # area weight by z: sample z by cumulative band area
    zs = [PERF_Z0 + (PERF_Z1 - PERF_Z0) * i / 400 for i in range(401)]
    w = []
    for i in range(400):
        za, zb = zs[i], zs[i + 1]
        ra, rb = egg_r(za), egg_r(zb)
        w.append(math.pi * (ra + rb) * math.hypot(rb - ra, zb - za))
    tot, cum = sum(w), [0]
    for x in w: cum.append(cum[-1] + x)
    for k in range(n):
        t = (k + 0.5) / n * tot
        j = max(i for i in range(401) if cum[i] <= t)
        z = zs[min(j, 399)] + (t - cum[j]) / w[min(j, 399)] * (zs[1] - zs[0])
        a = k * golden
        r = egg_r(z)
        dr = (egg_r(z + 0.2) - egg_r(z - 0.2)) / 0.4
        nr, nz = 1 / math.hypot(1, dr), -dr / math.hypot(1, dr)
        pts.append((r * math.cos(a), r * math.sin(a), z, nr * math.cos(a), nr * math.sin(a), nz))
    return pts

def make_crown():
    c = OUT.intersect(slab(Z_SPLIT, EGG_L + 5)).cut(IN_CROWN.intersect(slab(Z_SPLIT - 1, EGG_L + 5)))
    # skirt and the ring that ties it to the crown wall
    c = c.union(ring(SKIRT_R0, SKIRT_R1, SKIRT_Z0, Z_SPLIT))
    c = c.union(ring(SKIRT_R0, 57.0, Z_SPLIT, Z_SPLIT + 1.5).intersect(OUT))
    # The 3.5 mm jack and the XMOS USB-C stick out past the board edge at the front.
    # Gap in the skirt so they pass while the crown goes down (at the unlocked angle),
    # and a groove in the wall so the jack clears while it twists to lock.
    c = c.cut(sector(SKIRT_R0 - 1, 55.4, -127, -93, SKIRT_Z0 - 1, Z_SPLIT + 1.6))
    c = c.cut(sector(SKIRT_R0 - 1, 55.6, -112, -62, Z_SPLIT + 1.4, Z_PCB + 2.5))
    for a in NOTCH_ANGLES:
        c = c.union(radial_box(a + LOCK_TWIST, (SKIRT_R1 + LUG_R1) / 2 - 0.2, LUG_R1 - SKIRT_R1 + 0.4, LUG_W, LUG_Z0, LUG_Z1))
    # USB-C pocket at the back, lined up with the XIAO port
    c = c.cut(radial_box(90, 56, 14, USB_POCKET_W, SKIRT_Z0 + 6.4, USB_POCKET_Z1)
              .intersect(slab(Z_SPLIT - 0.01, USB_POCKET_Z1)))
    c = c.cut(radial_box(90, 52.6, 3.2, USB_POCKET_W, Z_SPLIT - 0.01, Z_SPLIT + 1.51))
    # mute flexure: U-slot tab hinged at the top, nub presses the board's side button
    zb = Z_PCB + BTN_Z
    ang = 0 if MUTE_SIDE > 0 else 180
    tab_w, tab_z0, tab_z1, gap = 11.0, zb - 3.0, zb + 11.0, 0.8
    slots = (radial_box(0, 56, 8, gap, tab_z0, tab_z1).translate((0, tab_w / 2, 0))
             .union(radial_box(0, 56, 8, gap, tab_z0, tab_z1).translate((0, -tab_w / 2, 0)))
             .union(radial_box(0, 56, 8, tab_w + gap, tab_z0 - gap, tab_z0)))
    c = c.cut(slots.rotate((0, 0, 0), (0, 0, 1), ang))
    nub_in = BTN_X + 1.85 + 0.45     # 0.45 mm clear of the button cap at rest
    nub = radial_box(0, (nub_in + 55.5) / 2, 55.5 - nub_in, 5, zb - 1.0, zb + 1.0)
    c = c.union(nub.rotate((0, 0, 0), (0, 0, 1), ang))
    # paperclip hole for the other (reset) button
    pin = cq.Workplane("YZ").workplane(offset=50).center(0, zb).circle(0.9).extrude(12)
    c = c.cut(pin.rotate((0, 0, 0), (0, 0, 1), 180 - ang))
    # light sensor window at the apex
    c = c.cut(cyl(APEX_HOLE_D, EGG_L - 25, EGG_L + 5))
    # speckle perforations
    if PERF:
        holes = []
        for (x, y, z, nx, ny, nz) in fib_points(PERF_N):
            if math.hypot(x, y) < 6:     # leave the apex window alone
                continue
            d = cq.Vector(nx, ny, nz)
            h = cq.Solid.makeCylinder(PERF_D / 2, 10, cq.Vector(x, y, z) - d * 6, d)
            holes.append(h)
        comp = cq.Compound.makeCompound(holes)
        c = c.cut(cq.Workplane().add(comp))
    return c

def export(part, name, outdir):
    cq.exporters.export(part, os.path.join(outdir, name + ".stl"), tolerance=0.03, angularTolerance=0.08)
    cq.exporters.export(part, os.path.join(outdir, name + ".step"))

if __name__ == "__main__":
    outdir = sys.argv[1] if len(sys.argv) > 1 else "out"
    os.makedirs(outdir, exist_ok=True)
    parts = {"foot": make_foot(), "body": make_body(), "chassis": make_chassis(), "crown": make_crown()}
    for n, p in parts.items():
        export(p, n, outdir)
        print(n, "volume cm3 %.1f" % (p.val().Volume() / 1000))
    asm = cq.Assembly()
    for n, p in parts.items():
        asm.add(p, name=n)
    asm.save(os.path.join(outdir, "robin_egg_assembly.step"))
