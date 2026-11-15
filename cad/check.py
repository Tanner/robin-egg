"""Fit check: Seeed board model and a DMA70-4 envelope against the printed parts, plus the crown twist-on path. Slow (10 to 20 minutes)."""
import sys; sys.argv=['x']
import os; os.environ['PERF']='0'
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'robin_egg.py')).read().split('if __name__')[0])
body=make_body(); chassis=make_chassis(); crown=make_crown(); foot=make_foot()
air = IN_BODY.intersect(slab(Z_BAFFLE+BAFFLE_T, Z_LEDGE_TOP)).cut(body)
import sys; print("chamber air gross cm3 %.0f" % (air.val().Volume()/1000), flush=True)
board = cq.importers.importStep(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'reference', 'datasheets', '3d', 'respeaker_mic_array_xvf3800_1_with-xiao-0820.stp'))
bsol = [s.translate(cq.Vector(-150,80,Z_PCB)) for s in board.solids().vals()]
bb=cq.Compound.makeCompound(bsol).BoundingBox(); print("board bbox", round(bb.xmin,1),round(bb.xmax,1),round(bb.ymin,1),round(bb.ymax,1),round(bb.zmin,2),round(bb.zmax,2))
for name,part in (("body",body),("chassis",chassis),("crown",crown)):
    pv=part.val(); pbb=pv.BoundingBox(); hits=0
    for s in bsol:
        sb=s.BoundingBox()
        if sb.xmax<pbb.xmin or sb.xmin>pbb.xmax or sb.ymax<pbb.ymin or sb.ymin>pbb.ymax or sb.zmax<pbb.zmin or sb.zmin>pbb.zmax: continue
        v=pv.intersect(s).Volume()
        if v>0.01: hits+=1; c=s.BoundingBox().center; print("  CLASH",name,round(v,2),"at",round(c.x,1),round(c.y,1),round(c.z,1))
    print(name,"clashes",hits)
# driver envelope
drv = cyl(64, Z_BAFFLE, Z_BAFFLE+35.6).union(cq.Workplane("XY").box(70,70,3,centered=(True,True,False)).translate((0,0,Z_BAFFLE-3)))
for name,part in (("body",body),("chassis",chassis),("foot",foot)):
    print("driver vs",name,"%.2f"%part.val().intersect(drv.val()).Volume())
# crown sweep: rotate crown -30..0 deg and check against board (twist-on), and lift crown 0..10mm
import math
for ang in (30,20,10,0):
  for dz in ((0,2,4,7) if ang==30 else (0,)):
    cr=crown.val().rotate(cq.Vector(0,0,0),cq.Vector(0,0,1),ang).translate(cq.Vector(0,0,dz))
    hit=0; cb=cr.BoundingBox()
    for s in bsol:
        sb=s.BoundingBox()
        if sb.zmax<cb.zmin or sb.zmin>cb.zmax: continue
        if math.hypot(sb.center.x,sb.center.y)+max(sb.xlen,sb.ylen)/2 < 48: continue
        if cr.intersect(s).Volume()>0.01: hit+=1
    lip_hit = cr.intersect(body.val()).Volume()
    print("crown rot",ang,"lift",dz,"board hits",hit,"body overlap %.2f"%lip_hit)
