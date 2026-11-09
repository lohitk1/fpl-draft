"""Rolling backtest of the opponent points-allowed model for defenders.
Usage: python3 backtest.py [first_test_gw] [last_test_gw] [window]  (defaults 6 9 5). Rerun later in the season with longer windows."""
import sys,math,os; sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from concurrent.futures import ThreadPoolExecutor
from fpl_draft import analysis, api
bs=api.bootstrap(); ps=analysis.players()
defs=[p for p in ps.values() if p['pos']=='DEF' and p['minutes']>0]
with ThreadPoolExecutor(8) as ex: data=list(ex.map(lambda p:(p,api.element_history(p['id'])),defs))
H={p['id']:(p,{x['event']:x for x in h}) for p,h in data}
def pear(a,b):
    ma,mb=sum(a)/len(a),sum(b)/len(b); d=math.sqrt(sum((x-ma)**2 for x in a)*sum((y-mb)**2 for y in b)); return sum((x-ma)*(y-mb) for x,y in zip(a,b))/d if d else 0
def run(T,W=5):
    train=range(T-W,T)
    g={pid:[x for k,x in by.items() if k<T and x['minutes']>=85] for pid,(p,by) in H.items()}
    eff={}
    for pid,(p,by) in H.items():
        for x in g[pid]:
            if x['event'] in train:
                o=[y['total_points'] for y in g[pid] if y['event']!=x['event']]
                if o: eff.setdefault(x['opponent_team'],[]).append(x['total_points']-sum(o)/len(o))
    E={o:sum(v)/len(v) for o,v in eff.items()}
    D=[]
    for pid,(p,by) in H.items():
        tr=[by[k] for k in train if k in by]
        if not tr or sum(x['minutes'] for x in tr)/W<70 or not g[pid]: continue
        t=by.get(T)
        if not t or t['minutes']<60: continue
        base=sum(x['total_points'] for x in g[pid])/len(g[pid]); e=E.get(t['opponent_team'],0)
        D.append((base,e,base+e,t['total_points']))
    return D
allD=[]
print(f"{'test GW':8}{'n':>4}{'A: baseline':>13}{'B: opp only':>13}{'C: base+opp':>13}   MAE A -> C   easy-vs-hard gap (actual-normal)")
a=sys.argv[1:]
first,last,W=(int(a[0]) if a else 6),(int(a[1]) if len(a)>1 else api.current_gw()),(int(a[2]) if len(a)>2 else 5)
for T in range(first,last+1):
    D=run(T,W); allD+=D; act=[d[3] for d in D]
    mae=lambda i: sum(abs(d[i]-d[3]) for d in D)/len(D)
    S=sorted(D,key=lambda d:-d[1]); k=len(S)//3
    gap=sum(d[3]-d[0] for d in S[:k])/k - sum(d[3]-d[0] for d in S[2*k:])/(len(S)-2*k)
    print(f"GW{T:<6}{len(D):>4}{pear([d[0] for d in D],act):>13.2f}{pear([d[1] for d in D],act):>13.2f}{pear([d[2] for d in D],act):>13.2f}   {mae(0):.2f} -> {mae(2):.2f}   {gap:+.2f}")
act=[d[3] for d in allD]; mae=lambda i: sum(abs(d[i]-d[3]) for d in allD)/len(allD)
S=sorted(allD,key=lambda d:-d[1]); k=len(S)//3
gap=sum(d[3]-d[0] for d in S[:k])/k - sum(d[3]-d[0] for d in S[2*k:])/(len(S)-2*k)
print(f"{'POOLED':8}{len(allD):>4}{pear([d[0] for d in allD],act):>13.2f}{pear([d[1] for d in allD],act):>13.2f}{pear([d[2] for d in allD],act):>13.2f}   {mae(0):.2f} -> {mae(2):.2f}   {gap:+.2f}")
