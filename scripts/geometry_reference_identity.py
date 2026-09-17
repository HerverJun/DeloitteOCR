"""Independent text/structure-only identity audit for the real-result track.

No image or predicted/GT geometry is accepted. An edge is retained only when it
is the sole possible match at its rank across ALL optimal LCS alignments. Two
nonrepeated exact anchors establish table identity; alternative tables, numeric
substitutions and ambiguous repeated runs remain unassociated outputs.
"""
from collections import Counter,defaultdict
import unicodedata

IDENTITY_RULE='unique-optimal-lcs-ranks-v1'


def normalized(value):return ''.join(unicodedata.normalize('NFC',value).split())


def associate(reference_targets,actual_edit):
    if len(actual_edit.get('tables',[]))!=1:return {}
    actual=sorted(actual_edit['tables'][0]['cells'],key=lambda c:(c['row'],c['column']))
    reference=sorted(reference_targets,key=lambda c:(c.get('table',0),c['row'],c['column']))
    if len({t.get('table',0) for t in reference})!=1:return {}
    a=[normalized(c['text']) for c in actual];b=[normalized(c['text']) for c in reference]
    ac,bc=Counter(a),Counter(b)
    if sum(bool(v) and ac[v]==bc[v]==1 for v in ac)<2:return {}
    n,m=len(a),len(b)
    if n>2000 or m>2000:return {}
    prefix=[[0]*(m+1) for _ in range(n+1)]
    suffix=[[0]*(m+1) for _ in range(n+1)]
    for i in range(n):
        for j in range(m):prefix[i+1][j+1]=prefix[i][j]+1 if a[i]==b[j] else max(prefix[i][j+1],prefix[i+1][j])
    for i in range(n-1,-1,-1):
        for j in range(m-1,-1,-1):suffix[i][j]=suffix[i+1][j+1]+1 if a[i]==b[j] else max(suffix[i+1][j],suffix[i][j+1])
    ranks=defaultdict(list)
    for i in range(n):
        for j in range(m):
            if a[i]==b[j] and prefix[i][j]+1+suffix[i+1][j+1]==prefix[n][m]:ranks[prefix[i][j]].append((i,j))
    result={}
    for options in ranks.values():
        if len(options)!=1:continue
        i,j=options[0];source,target=actual[i],reference[j]
        if (source['row_span'],source['column_span'])!=(target['row_span'],target['column_span']):continue
        result[(0,source['row'],source['column'])]=(target.get('table',0),target['row'],target['column'])
    return result
