"""Conservative spatial pruning: every intersecting box remains a candidate.

The index is immutable before any assignment is published. A completed token
has been checked against every spatial candidate, including overlapping cells.
"""
from collections import defaultdict
from math import floor, sqrt
import time

from ocr_workbench.coordinates import bounds


class SpatialBudget(ValueError):
    pass


class BoxIndex:
    def __init__(self, cells, maximum, deadline=None):
        self.cells = {c.id:c for c in cells}
        self.boxes = {c.id:bounds(c.cell_polygon) for c in cells}
        if len(self.cells) != len(cells):
            raise SpatialBudget('duplicate_candidate_id')
        self.buckets = defaultdict(list)
        if not cells:
            self.origin=(0,0);self.step=(1,1)
            return
        boxes=list(self.boxes.values())
        x0,y0=min(b[0] for b in boxes),min(b[1] for b in boxes)
        x1,y1=max(b[2] for b in boxes),max(b[3] for b in boxes)
        n=max(1,int(sqrt(len(cells))))
        self.origin=(x0,y0);self.step=(max((x1-x0)/n,1),max((y1-y0)/n,1))
        inserted=0
        for ident,box in self.boxes.items():
            if deadline is not None and time.perf_counter()>deadline:
                raise SpatialBudget('timeout')
            xs,ys=self.keys(box)
            count=len(xs)*len(ys)
            if count+inserted>maximum:
                raise SpatialBudget('candidate_budget_exceeded')
            for x in xs:
                for y in ys:self.buckets[x,y].append(ident)
            inserted+=count

    def keys(self, box):
        x0,y0=self.origin;dx,dy=self.step
        left,right=floor((box[0]-x0)/dx),floor((box[2]-x0)/dx)
        top,bottom=floor((box[1]-y0)/dy),floor((box[3]-y0)/dy)
        # Querying a far-away giant token must not allocate an unbounded grid.
        return range(left,right+1),range(top,bottom+1)

    def query(self, box):
        xs,ys=self.keys(box)
        if len(xs)*len(ys)>len(self.buckets)*4:
            ids=self.cells.keys()
        else:
            ids={ident for x in xs for y in ys for ident in self.buckets.get((x,y),())}
        return [self.cells[i] for i in sorted(ids) if
            self.boxes[i][0]<box[2] and box[0]<self.boxes[i][2] and
            self.boxes[i][1]<box[3] and box[1]<self.boxes[i][3]]
