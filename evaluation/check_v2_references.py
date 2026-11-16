"""Self-check candidate comparator on references and targeted negative controls."""
import copy
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from v2 import ROOT, public_cases, labels, plain, iso, synthetic, edit, real, flows, necessary


def check(selfroot):
    lab=labels();results=[]
    for case in public_cases():
        label=lab[case['id']];net=ROOT/label['artifacts']['reference_net'];folder=selfroot/case['id'];folder.mkdir(exist_ok=True)
        try:
            if case['task']=='synthetic':
                shutil.copyfile(ROOT/label['artifacts']['nodes'],folder/'model.nod.xml')
                shutil.copyfile(ROOT/label['artifacts']['edges'],folder/'model.edg.xml')
                m,why=synthetic(label,folder,net)
                problem=necessary(label,folder/'model.nod.xml',folder/'model.edg.xml',net)
                if problem: m,why='fail',problem
            elif case['task']=='edit':m,why=edit(label,case,net)
            else:
                x=label['expected'];m,why=real(label,{'details':{'center':[x['center_lat'],x['center_lon']],'shape':x['shape'],'size_m':x['size_m']}},net)
            demand=label['artifacts'].get('reference_routes')
            ex={'flow_execution':'loaded','routes':str(ROOT/demand)} if demand else {'flow_execution':'not_provided'}
            f,fwhy=flows(label,case,ex,net)
            if m!='pass' or f not in ('pass','not_requested'):results.append({'id':case['id'],'map':m,'map_reason':why,'flow':f,'flow_reason':fwhy})
        except Exception as exc:results.append({'id':case['id'],'error':f'{type(exc).__name__}: {exc}'})
    first=lab['v2-synthetic-000'];ref=plain(ROOT/first['artifacts']['nodes'],ROOT/first['artifacts']['edges'])
    modified=copy.deepcopy(ref);modified[1][0]['speed']+=1
    neg_speed=iso(ref,modified,first)[0] is False
    case=next(x for x in public_cases() if x['id']=='v2-synthetic-000');net=ROOT/first['artifacts']['reference_net']
    r=ET.parse(ROOT/first['artifacts']['reference_routes']);r.getroot().find('flow').set('vehsPerHour','9999')
    fake=selfroot/'wrong_flow.rou.xml';r.write(fake,encoding='utf8',xml_declaration=True)
    neg_flow=flows(first,case,{'flow_execution':'loaded','routes':str(fake)},net)[0]=='fail'
    report={'reference_cases':300,'reference_failures':results,'negative_speed_detected':neg_speed,'negative_flow_detected':neg_flow}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if results or not (neg_speed and neg_flow):raise SystemExit(1)


if __name__=='__main__':
    with TemporaryDirectory() as folder:
        check(Path(folder))
