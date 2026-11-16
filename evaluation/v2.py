"""Evaluate the 300 TrafficForge v2 cases using submitted SUMO artifacts."""
import argparse
import collections
import itertools
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(p):return [json.loads(s) for s in p.read_text(encoding='utf8').splitlines() if s.strip()]
def labels():
    sources=['work_synthetic/full_samples.jsonl','work_real_traffic/samples.jsonl','work_edit_pilot/full_samples.jsonl']
    rows=[x for p in sources for x in read(ROOT/'dataset_design_v2'/p)]
    return {x['id']:x for x in rows}


def net_edges(path):
    root=ET.parse(path).getroot()
    result={}
    for e in root.findall('edge'):
        if e.get('function'):continue
        lanes=e.findall('lane')
        result[e.get('id')]={'from':e.get('from'),'to':e.get('to'),'lanes':len(lanes),
                             'speed':tuple(round(float(l.get('speed')),2) for l in lanes)}
    return root,result


def plain(nodes,edges):
    nr=ET.parse(nodes).getroot();er=ET.parse(edges).getroot()
    ns={x.get('id'):{'x':float(x.get('x')),'y':float(x.get('y')),'type':x.get('type','priority')} for x in nr.findall('node')}
    rings={z for r in er.findall('roundabout') for z in r.get('edges','').split()};es=[]
    for x in er.findall('edge'):
        es.append({'id':x.get('id'),'from':x.get('from'),'to':x.get('to'),'lanes':int(x.get('numLanes','1')),
                   'speed':round(float(x.get('speed','13.89')),2),'ring':x.get('id') in rings})
    return ns,es


def iso(reference,actual,label):
    rn,re=reference;an,ae=actual
    if len(rn)!=len(an) or len(re)!=len(ae):return False,'different node/road counts'
    def degree(nodes,edges):
        out={n:[] for n in nodes}; neigh={n:set() for n in nodes}
        for e in edges:
            if e['from'] not in nodes or e['to'] not in nodes:return None,None
            attr=(e['lanes'],e['speed'],e['ring'])
            out[e['from']].append(('out',*attr));out[e['to']].append(('in',*attr))
            neigh[e['from']].add(e['to']);neigh[e['to']].add(e['from'])
        return out,neigh
    ri,rnei=degree(rn,re);ai,anei=degree(an,ae)
    if ri is None or ai is None:return False,'edge endpoint missing'
    rpair=collections.defaultdict(list);apair=collections.defaultdict(list)
    for e in re:rpair[(e['from'],e['to'])].append((e['lanes'],e['speed'],e['ring']))
    for e in ae:apair[(e['from'],e['to'])].append((e['lanes'],e['speed'],e['ring']))
    for d in (rpair,apair):
        for p in d:d[p].sort()
    kind=label['expected']['network_type'];opts={};radial=kind in ('cross','t_junction','y_junction','five_way','six_way')
    explicit_control=radial or kind=='multi_junction' or 'grid_size' in label['expected']
    for u in rn:
        opts[u]=[]
        for v in an:
            if sorted(ri[u])!=sorted(ai[v]):continue
            if len(rnei[u])>=3:
                if explicit_control and (rn[u]['type']=='traffic_light')!=(an[v]['type']=='traffic_light'):continue
                if not explicit_control and rn[u]['type']=='traffic_light' and an[v]['type']!='traffic_light':continue
            opts[u].append(v)
        if not opts[u]:return False,'node degree, lane, speed, ring or control mismatch'
    mapping={};used=set();attempts=0
    def length(n,a,b):return math.hypot(n[a]['x']-n[b]['x'],n[a]['y']-n[b]['y'])
    def consistent(u,v):
        for ru,av in mapping.items():
            if rpair.get((u,ru),[])!=apair.get((v,av),[]) or rpair.get((ru,u),[])!=apair.get((av,v),[]):return False
            constrained=radial or ('grid_size' in label['expected']) or (kind=='multi_junction' and len(rnei[u])>=3 and len(rnei[ru])>=3)
            if constrained and (rpair.get((u,ru)) or rpair.get((ru,u))):
                d=length(rn,u,ru);pred=length(an,v,av)
                if abs(d-pred)>max(5,0.05*d):return False
        return True
    def directions():
        # T/Y explicitly specify absolute arm angles; cross optionally specifies east-side constraints.
        enforce=kind in ('t_junction','y_junction') or (kind=='cross' and (label['expected'].get('one_way_east') or label['expected'].get('east_lanes')!=label['expected'].get('lanes_per_direction_default')))
        if not enforce:return True
        center=max(rn,key=lambda n:len(rnei[n])); mcenter=mapping[center]
        for arm in rnei[center]:
            rangle=math.degrees(math.atan2(rn[arm]['y']-rn[center]['y'],rn[arm]['x']-rn[center]['x']))%360
            if kind=='cross' and min(rangle,360-rangle)>10:continue
            t=mapping[arm];aangle=math.degrees(math.atan2(an[t]['y']-an[mcenter]['y'],an[t]['x']-an[mcenter]['x']))%360
            if abs((rangle-aangle+180)%360-180)>10:return False
        return True
    def search():
        nonlocal attempts
        if len(mapping)==len(rn):return directions()
        if attempts>200000:raise TimeoutError('graph matcher exceeded search bound')
        u=min((x for x in rn if x not in mapping),key=lambda x:sum(y not in used for y in opts[x]))
        for v in opts[u]:
            if v in used or not consistent(u,v):continue
            attempts+=1;mapping[u]=v;used.add(v)
            if search():return True
            used.remove(v);del mapping[u]
        return False
    return (True,'compatible topology and specified geometry') if search() else (False,'no compatible topology/geometry mapping')


def synthetic(label,folder,net):
    ref=plain(ROOT/label['artifacts']['nodes'],ROOT/label['artifacts']['edges'])
    actual=plain(folder/'model.nod.xml',folder/'model.edg.xml')
    _,net_actual=net_edges(net)
    if set(net_actual)!={x['id'] for x in actual[1]}:return 'unverified','netconvert changed model graph edge IDs'
    for e in actual[1]:
        a=net_actual[e['id']]
        if a['lanes']!=e['lanes'] or any(abs(v-e['speed'])>0.02 for v in a['speed']):return 'fail','compiled road lane or speed differs from plan'
    kind=label['expected']['network_type']
    def compress(graph):
        # A one-in/one-out point may merely split a single carriageway.
        nodes,edges=graph;nodes={k:dict(v) for k,v in nodes.items()};edges=[dict(x) for x in edges]
        while True:
            changed=False
            for n in list(nodes):
                incoming=[e for e in edges if e['to']==n];outgoing=[e for e in edges if e['from']==n]
                if len(incoming)!=1 or len(outgoing)!=1 or nodes[n]['type']=='traffic_light':continue
                a,b=incoming[0],outgoing[0]
                if a['from']==b['to'] or (a['lanes'],a['speed'],a['ring'])!=(b['lanes'],b['speed'],b['ring']):continue
                edges.remove(a);edges.remove(b);edges.append({**a,'to':b['to']});del nodes[n];changed=True;break
            if not changed:return nodes,edges
    compared=compress(actual) if kind in ('merge','diverge') else actual
    ok,why=iso(ref,compared,label)
    if ok:return 'pass',why
    # A topology mismatch may be a different but valid layout; reject only
    # obvious failures that can be read directly from the instruction.
    nodes,edges=actual
    neighbors={n:set() for n in nodes}
    for e in edges:
        neighbors[e['from']].add(e['to']);neighbors[e['to']].add(e['from'])
    if kind=='multi_junction' and sum(len(v)>=4 for v in neighbors.values())<label['expected']['junctions']:
        return 'fail','fewer four-arm intersections than requested'
    if label['expected'].get('attached_intersection') and not any(nodes[n]['type']=='traffic_light' and len(v)>=3 for n,v in neighbors.items()):
        return 'fail','no adjacent signal-controlled intersection with at least three approaches'
    return 'unverified',why+'; alternate layout or ambiguous requirement needs review'


def edit(label,case,net):
    before,b=net_edges(ROOT/case['inputs']['input_net']);_,reference=net_edges(ROOT/label['artifacts']['reference_net']);out,a=net_edges(net)
    if set(a)!=set(reference):return 'fail','road additions/deletions differ from requested reference'
    for edge in a:
        if a[edge]!=reference[edge]:return 'fail','road endpoint, lane or speed mismatch: '+edge
    target=label['expected']
    if 'node' in target:
        j=next((x for x in out.findall('junction') if x.get('id')==target['node']),None)
        if j is None or (j.get('type')=='traffic_light')!=(target['control']=='traffic_light'):return 'fail','junction control mismatch'
    def external(root,edges):
        return {(x.get('from'),x.get('to'),x.get('fromLane'),x.get('toLane')) for x in root.findall('connection')
                if x.get('from') in edges and x.get('to') in edges}
    c=external(out,a);ref=ET.parse(ROOT/label['artifacts']['reference_net']).getroot();rc=external(ref,reference)
    if target.get('prohibited') and any(x[:2]==(target['from'],target['to']) for x in c):return 'fail','requested turn remains available'
    if c!=rc:
        return 'unverified',f'external lane connections differ from reference: {len(c-rc)} extra and {len(rc-c)} missing; needs semantic review'
    return 'pass','target edit and road attributes matched'


def real(label,execution,net):
    e=label['expected'];d=execution['details'];lat,lon=d['center']
    dist=111320*math.hypot(lat-e['center_lat'],(lon-e['center_lon'])*math.cos(math.radians(lat)))
    if dist>10 or d['shape']!=e['shape'] or abs(d['size_m']-e['size_m'])>0.5:return 'fail','location, shape or requested range mismatch'
    _,ref=net_edges(ROOT/label['artifacts']['reference_net']);_,out=net_edges(net)
    if ref!=out:return 'unverified','same requested region but reference road edges or attributes differ; needs spatial review'
    return 'pass','location, shape, range and road signatures matched'


def flows(label,case,execution,net):
    e=label['expected'];required=e.get('traffic')
    if label['task']=='edit' and label.get('edit_type')=='traffic':required=e
    if not required:return 'not_requested','no explicit traffic requirement'
    if execution.get('flow_execution')!='loaded' or not execution.get('routes'):return 'fail','required demand missing or SUMO rejected the routes'
    rr=ET.parse(execution['routes']).getroot(); routes={r.get('id'):r.get('edges','').split() for r in rr.findall('route')}
    actual=[];_,edge=net_edges(net)
    for f in rr.findall('flow'):
        path=routes.get(f.get('route'),[])
        if not path or path[0] not in edge or path[-1] not in edge:continue
        rate=round(float(f.get('vehsPerHour','-1')),4);begin=round(float(f.get('begin','0')),4);end=round(float(f.get('end','-1')),4)
        if case['task']=='real_map':
            middle=tuple(edge[p]['to'] for p in path if p in edge)
            actual.append((edge[path[0]]['from'],middle,edge[path[-1]]['to'],rate,begin,end))
        else:actual.append((path[0],path[-1],rate,begin,end))
    if case['task']=='synthetic':
        wanted=[(x['from_edge'],x['to_edge'],float(x['vehicles_per_hour']),float(x['begin_s']),float(x['end_s'])) for x in required['flows']]
    elif case['task']=='edit':wanted=[(required['from'],required['to'],float(required['vehicles_per_hour']),float(required['begin_s']),float(required['end_s']))]
    else:
        wanted=[(required['osm_start_node'],required['node'],required['osm_end_node'],float(required['vehicles_per_hour']),float(required['begin_s']),float(required['end_s']))]
        found=[]
        for start,mids,end,rate,begin,finish in actual:
            for w in wanted:
                if (start,end,rate,begin,finish)==(w[0],w[2],w[3],w[4],w[5]) and w[1] in mids:found.append(w)
        return ('pass','requested OD, rate and duration match') if collections.Counter(found)>=collections.Counter(wanted) else ('fail','OD, rate or duration mismatch')
    return ('pass','requested OD, rate and duration match') if collections.Counter(actual)>=collections.Counter(wanted) else ('fail','OD, rate or duration mismatch')


def one(case,label,run_dir):
    folder=run_dir/'cases'/case['id'];v2=folder/'adapter_v2'/'execution.json';v3=folder/'adapter_v3'/'execution.json'
    p=v3 if case['task']=='edit' and v3.exists() else v2 if case['task']=='edit' and v2.exists() else folder/'execution.json';r={'id':case['id'],'task':case['task']}
    if not p.exists():return {**r,'status':'pending','map_check':'unverified','flow_check':'unverified','full_pass':None}
    try:
        ex=json.loads(p.read_text(encoding='utf8'));r['status']=ex['status']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**r,'status':'invalid_receipt','map_check':'unverified','flow_check':'unverified','full_pass':None,'reason':str(exc)}
    if ex['status'] in ('timeout','infrastructure_error','output_truncated'):
        return {**r,'map_check':'unverified','flow_check':'unverified','full_pass':None,'reason':ex.get('reason')}
    if ex['status']!='map_delivered':return {**r,'map_check':'fail','flow_check':'unverified','full_pass':False,'reason':ex.get('detail')}
    try:
        net=p.parent / ex['net']
        if ex.get('routes'):
            ex['routes'] = str(p.parent / ex['routes'])
        if case['task']=='synthetic':m,reason=synthetic(label,folder,net)
        elif case['task']=='edit':m,reason=edit(label,case,net)
        else:m,reason=real(label,ex,net)
        if m == 'unverified' and case['task'] == 'synthetic':
            problem = necessary(label, folder / 'model.nod.xml', folder / 'model.edg.xml', net)
            if problem:
                m, reason = 'fail', problem
        f,fwhy=flows(label,case,ex,net)
        full=False if m=='fail' or f=='fail' else None if m=='unverified' else True
        return {**r,'map_check':m,'map_reason':reason,'flow_check':f,'flow_reason':fwhy,'full_pass':full}
    except (OSError,KeyError,ValueError,TypeError,ET.ParseError,TimeoutError) as exc:
        return {**r,'map_check':'unverified','flow_check':'unverified','full_pass':None,'reason':f'audit_error: {type(exc).__name__}: {exc}'}


def summary(rows):
    statuses=collections.Counter(x['status'] for x in rows);assess=[x for x in rows if x.get('full_pass') is not None]
    return {'total':len(rows),'execution_status':dict(statuses),'map_check':dict(collections.Counter(x['map_check'] for x in rows)),
            'flow_check':dict(collections.Counter(x['flow_check'] for x in rows)),'assessable':len(assess),
            'full_pass':sum(x['full_pass'] is True for x in rows),'full_pass_rate_among_assessable':sum(x['full_pass'] is True for x in rows)/len(assess) if assess else None,
            'unassessed':len(rows)-len(assess)}


RADIAL={'cross','t_junction','y_junction','five_way','six_way'}


def angle_diff(a,b):return abs((a-b+180)%360-180)


def necessary(label,nod,edg,net):
    nodes,edges=plain(nod,edg);kind=label['expected']['network_type'];exp=label['expected']
    if any(e['from'] not in nodes or e['to'] not in nodes for e in edges):return None
    if kind in RADIAL:
        neighbors={n:set() for n in nodes}
        for e in edges:neighbors[e['from']].add(e['to']);neighbors[e['to']].add(e['from'])
        center=max(neighbors,key=lambda n:len(neighbors[n]))
        # Only decide when this is a simple star. Intermediate road splits or
        # alternative arrangements remain unverified.
        if any(len(v)!=1 for k,v in neighbors.items() if k!=center):return None
        cx,cy=nodes[center]['x'],nodes[center]['y'];groups=[]
        for e in edges:
            if e['from']==center:other=e['to'];direction='out'
            elif e['to']==center:other=e['from'];direction='in'
            else:return None
            dx=nodes[other]['x']-cx;dy=nodes[other]['y']-cy;length=math.hypot(dx,dy)
            if length<0.1:return '支路长度为零，无法形成要求的路口'
            angle=math.degrees(math.atan2(dy,dx))%360
            g=next((x for x in groups if angle_diff(x['angle'],angle)<=8),None)
            if g is None:g={'angle':angle,'directions':set(),'lengths':[]};groups.append(g)
            g['directions'].add(direction);g['lengths'].append(length)
        if len(groups)!=exp['arms']:return f'支路方向数量为 {len(groups)}，题目要求 {exp["arms"]} 条'
        if exp.get('one_way_east'):
            for g in groups:
                needed={'in'} if angle_diff(g['angle'],0)<=10 else {'in','out'}
                if g['directions']!=needed:return '东侧单向或其余支路双向的方向不满足要求'
        elif any(g['directions']!={'in','out'} for g in groups):return '至少一条要求双向通行的支路只有单向道路'
        wanted=exp.get('arm_length_m')
        if wanted and any(abs(d-wanted)>max(5,0.05*wanted) for g in groups for d in g['lengths']):
            return f'至少一条支路的长度不符合 {wanted} 米要求'
        if kind in ('t_junction','y_junction'):
            target=exp['arm_angles_degrees'];actual=[g['angle'] for g in groups]
            if not any(all(angle_diff(a,b)<=10 for a,b in zip(target,perm)) for perm in itertools.permutations(actual)):
                return '支路方向角与题面明确给出的角度不符'
        return None
    if kind=='roundabout':
        root=ET.parse(net).getroot();external={e.get('id'):e for e in root.findall('edge') if not e.get('function')}
        rings=root.findall('roundabout')
        if not rings:return None
        # Any detected ring may satisfy the request; reject only if every ring
        # has insufficient entry or exit connections to the outside.
        counts=[]
        for ring in rings:
            ids=set(ring.get('edges','').split());redges=[external[k] for k in ids if k in external]
            if not redges:continue
            ring_nodes={e.get('from') for e in redges}|{e.get('to') for e in redges}
            enter={e.get('to') for k,e in external.items() if k not in ids and e.get('from') not in ring_nodes and e.get('to') in ring_nodes}
            leave={e.get('from') for k,e in external.items() if k not in ids and e.get('from') in ring_nodes and e.get('to') not in ring_nodes}
            counts.append((len(enter),len(leave)))
        if counts and all(a<exp['ring_entries'] or b<exp['ring_entries'] for a,b in counts):
            return f'环岛外部入口/出口数量 {counts} 不满足 {exp["ring_entries"]} 个入口及相应出口要求'
        return None
    if kind in ('merge','diverge'):
        incoming=collections.defaultdict(set);outgoing=collections.defaultdict(set)
        for e in edges:incoming[e['to']].add(e['from']);outgoing[e['from']].add(e['to'])
        if kind=='merge':valid=any(len(incoming[n])>=2 and len(outgoing[n])>=1 for n in nodes)
        else:valid=any(len(incoming[n])>=1 and len(outgoing[n])>=2 for n in nodes)
        if not valid:return '缺少主路与匝道实际汇入或分流所必需的不同支路'
    return None


def public_cases():
    data = ROOT / 'dataset_design_v2' / 'pilot_views'
    rows = [row for task in ('synthetic', 'real', 'edit')
            for row in read(data / f'{task}_public.jsonl')]
    if len(rows) != 300 or len({row['id'] for row in rows}) != 300:
        raise ValueError('Expected 300 distinct public case IDs')
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True, help='Submission directory containing cases/<id>/')
    parser.add_argument('--out', type=Path, required=True, help='Directory for results.jsonl and summary.json')
    args = parser.parse_args()
    if not (args.run / 'cases').is_dir():
        parser.error('Submission directory must contain a cases/ directory')
    cases = public_cases()
    references = labels()
    if set(references) != {case['id'] for case in cases}:
        raise ValueError('Public case IDs and reference IDs differ')
    artifacts = {ROOT / path for label in references.values() for path in label['artifacts'].values()}
    missing = next((p for p in sorted(artifacts) if not p.is_file()), None)
    if missing is not None:
        parser.error(f'Missing dataset artifact {missing}. Extract the dataset archives before scoring.')
    rows = [one(case, references[case['id']], args.run.resolve()) for case in cases]
    report = {
        'overall': summary(rows),
        'by_task': {task: summary([r for r in rows if r['task'] == task])
                    for task in ('synthetic', 'real_map', 'edit')},
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'results.jsonl').write_text(
        ''.join(json.dumps(r, ensure_ascii=False) + chr(10) for r in rows), encoding='utf8')
    result = json.dumps(report, ensure_ascii=False, indent=2)
    (args.out / 'summary.json').write_text(result + chr(10), encoding='utf8')
    print(result)


if __name__ == '__main__':
    main()
