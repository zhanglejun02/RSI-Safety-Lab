"""Render the editable RSI Safety Lab architecture figure. No model/API calls."""
from pathlib import Path
from html import escape

OUT = Path(__file__).resolve().parent
W, H = 2160, 1540
C = dict(ink='#192129', muted='#56616D', blue='#7FA1DE', bluebg='#F3F7FD',
         purple='#5BA89F', purplebg='#FAF7FE', green='#80AD64', greenbg='#F3F8EE',
         teal='#5BA89F', tealbg='#F0F8F6', amber='#DEA340', amberbg='#FFF7E7',
         gray='#A7ADB4', graybg='#F7F8FA', white='#FFFFFF')
s = []
s.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
s.append('<title>RSI Safety Lab: one-shot admission, independent audit, and offline evidence</title>')
s.append('<desc>Adapted optimizers and development stress tasks feed a native update workflow. Current admission audits one root update and freezes deployment. Native rejection prevents activation but may still receive diagnostic audit. Platform-managed snapshot references produce an allowed audit view and paired native-continuation probes. Lab admission is distinct from detection. Only train/calibration evidence supports later frozen scorers; continuous external admission is future work.</desc>')
s.append('''<defs>
<filter id="shadow" x="-10%" y="-10%" width="125%" height="130%"><feDropShadow dx="4" dy="5" stdDeviation="1.5" flood-color="#41364E" flood-opacity="0.16"/></filter>
<marker id="arrow" viewBox="0 0 12 12" refX="10.5" refY="6" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M1 1 L11 6 L1 11 Z" fill="#192129"/></marker>
<marker id="trainArrow" viewBox="0 0 12 12" refX="10.5" refY="6" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M1 1 L11 6 L1 11 Z" fill="#5BA89F"/></marker>
<marker id="dataArrow" viewBox="0 0 12 12" refX="10.5" refY="6" markerWidth="8" markerHeight="8" orient="auto"><path d="M1 1 L11 6 L1 11 Z" fill="#5BA89F"/></marker>
<marker id="futureArrow" viewBox="0 0 12 12" refX="10.5" refY="6" markerWidth="8" markerHeight="8" orient="auto"><path d="M1 1 L11 6 L1 11" fill="none" stroke="#87919A" stroke-width="1.7"/></marker>
<style>
text{font-family:Arial,Helvetica,sans-serif;fill:#192129} .head{font-family:"Arial Rounded MT Bold",Arial,Helvetica,sans-serif;font-weight:700} .mono{font-family:Menlo,Consolas,monospace}
</style>
</defs>''')

def rect(x,y,w,h,fill,stroke=None,sw=2.5,r=14,dash=None,shadow=False):
    extra = (f' stroke-dasharray="{dash}"' if dash else '') + (' filter="url(#shadow)"' if shadow else '')
    s.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke or "none"}" stroke-width="{sw}"{extra}/>')

def text(x,y,label,size=20,weight=400,anchor='start',color=None,cls='',rotate=None):
    transform = f' transform="rotate({rotate} {x} {y})"' if rotate else ''
    s.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" class="{cls}" style="fill:{color or C["ink"]}"{transform}>{escape(label)}</text>')

def path(d,color=None,width=3,arrow=True,dash=None):
    marker = 'trainArrow' if color==C['purple'] else 'arrow'
    extra=(f' marker-end="url(#{marker})"' if arrow else '')+(f' stroke-dasharray="{dash}"' if dash else '')
    s.append(f'<path d="{d}" fill="none" stroke="{color or C["ink"]}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"{extra}/>')

def icon(kind,x,y,size=36,fill='none'):
    shapes={
      'plug':'<path d="M8 3v5M16 3v5M6 8h12v4a6 6 0 0 1-12 0zM12 18v4"/>',
      'bench':'<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V2h6v2M8 10l2 2 4-4M8 17h8"/>',
      'sliders':'<path d="M3 6h18M3 12h18M3 18h18"/><rect x="7" y="3" width="3" height="6" rx="1" fill="white"/><rect x="15" y="9" width="3" height="6" rx="1" fill="white"/><rect x="6" y="15" width="3" height="6" rx="1" fill="white"/>',
      'select':'<path d="M3 4h18l-7 8v7l-4 2v-9z"/>',
      'edit':'<path d="M15 3H4v18h16V10M10 15l1-5 8-8 4 4-8 8zM11 10l4 4"/>',
      'chart':'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 17v-4M12 17V8M17 17v-7M6 6h4"/>',
      'file':'<path d="M5 2h9l5 5v15H5zM14 2v6h5M10 12l-3 3 3 3M14 12l3 3-3 3"/>',
      'shield':'<path d="M12 2l9 4v6c0 5-6 9-9 11-3-2-9-6-9-11V6zM7 12l3 3 7-7"/>',
      'database':'<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 4 18 4 18 0V5M3 12c0 4 18 4 18 0M3 18c0 4 18 4 18 0"/>',
      'eye':'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
      'branch':'<path d="M5 4v16M5 12c8 0 13-2 13-8"/><circle cx="5" cy="3" r="2" fill="white"/><circle cx="5" cy="21" r="2" fill="white"/><circle cx="18" cy="3" r="2" fill="white"/>',
      'search':'<circle cx="10" cy="10" r="7"/><path d="M15 15l7 7M6 11l3-4 3 5 2-3"/>',
      'lock':'<rect x="4" y="10" width="16" height="12" rx="2"/><path d="M7 10V6a5 5 0 0 1 10 0v4M12 15v3"/>',
      'runtime':'<rect x="2" y="3" width="20" height="15" rx="2"/><path d="M8 22h8M12 18v4M6 7l3 3-3 3M12 13h5"/>',
      'learn':'<circle cx="5" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="12" r="3"/><path d="M7 6l9 5M7 18l9-5M7 5h8M7 19h8"/>',
    }
    s.append(f'<g transform="translate({x} {y}) scale({size/24})" fill="{fill}" stroke="{C["ink"]}" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">{shapes[kind]}</g>')

def pill(x,y,w,label,fill,stroke,size=18):
    rect(x,y,w,32,fill,stroke,1.8,7)
    text(x+w/2,y+22,label,size,500,'middle')

def stage(n,x,y,w,h,title,sub,kind,fill,stroke,extra=None):
    rect(x,y,w,h,fill,stroke,2.8,13)
    s.append(f'<circle cx="{x+16}" cy="{y}" r="15" fill="{C["white"]}" stroke="{stroke}" stroke-width="2.5"/>')
    text(x+16,y+6,str(n),18,700,'middle')
    icon(kind,x+17,y+25,34)
    text(x+64,y+40,title,23,700,cls='head')
    text(x+64,y+68,sub,18)
    if extra: text(x+w/2,y+97,extra,17,anchor='middle')

def edge(d, color=None, dash=None, future=False, arrow=True, width=2.7):
    color = color or C['ink']
    marker = 'futureArrow' if future else ('dataArrow' if color == C['teal'] else 'arrow')
    attrs = f' marker-end="url(#{marker})"' if arrow else ''
    if dash: attrs += f' stroke-dasharray="{dash}"'
    s.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"{attrs}/>')

def note(x,y,lines,size=20,gap=29,anchor='start',color=None):
    for i,line in enumerate(lines):
        text(x,y+i*gap,line,size,anchor=anchor,color=color)

# Neutral containers; colors identify the functional components, not decoration.
rect(0,0,W,H,C['white'],sw=0,r=0)
text(40,48,'RSI Safety Lab',39,700,cls='head')
text(2120,47,'Current admission workflow · native continuation · offline evidence',22,anchor='end',color=C['muted'])
for x,w in [(35,355),(435,895),(1375,750)]:
    rect(x,86,w,985,C['graybg'],C['gray'],2.8,23,'12 8')
text(212,132,'Experiment Setup',28,700,'middle',cls='head')
text(882,132,'Native RSI Workflow',30,700,'middle',cls='head')
text(882,166,'Current: one root audit, then frozen deployment',21,600,'middle')
text(1750,132,'Audit & Admission',30,700,'middle',cls='head')
text(1750,164,'Independent audit → lab-controlled admission',21,anchor='middle',color=C['muted'])

# Inputs: adapted systems, development tasks, and the shared host contract.
rect(55,200,315,160,C['white'],C['gray'],2.3,14)
icon('plug',72,215,31)
text(116,242,'Systems & optimizers',21,700,cls='head')
text(212,275,'GEPA · OpenEvolve (adapted)',19,anchor='middle')
text(212,303,'Custom RSI via a standard adapter',18,anchor='middle')
pill(81,319,263,'Capabilities · state · task API',C['graybg'],C['gray'],17)

rect(55,390,315,335,C['white'],C['gray'],2.3,14)
icon('bench',73,406,30)
text(116,432,'Stress task suite (dev)',21,700,cls='head')
text(77,466,'PAIRED NORMAL / STRESS TASKS',15,700,color=C['muted'])
pill(76,484,136,'Authorization',C['graybg'],C['gray'],17)
pill(224,484,126,'Finance',C['graybg'],C['gray'],18)
pill(76,526,136,'Retail',C['graybg'],C['gray'],18)
pill(224,526,126,'Privacy',C['graybg'],C['gray'],18)
text(77,586,'EXTENSION TASKS',15,700,color=C['muted'])
note(77,617,['Tool feedback · project quota','Dynamic constraints · RL'],18,28)
edge('M76 668 H350',C['gray'],arrow=False,width=1.3)
text(212,700,'Policies · tools · effect oracles',18,anchor='middle')

rect(55,755,315,290,C['white'],C['gray'],2.3,14)
icon('sliders',73,775,30)
text(116,802,'Protocol & host',23,700,cls='head')
note(78,843,['Splits · seeds · budgets','Tool and adapter contracts'],20,31)
edge('M77 897 H349',C['gray'],arrow=False,width=1.3)
note(78,930,['Snapshot & fork service','Bounded execution','Trusted logs · provenance'],20,36)

# Stages of the root update. Candidate is on the proposal/evaluation path.
stage(1,480,240,310,110,'Select parent','Base + full inherited state','select',C['greenbg'],C['green'])
stage(2,930,240,340,110,'Propose update','Native optimizer','edit',C['greenbg'],C['green'])
rect(950,395,300,75,C['greenbg'],C['green'],2.6,13)
icon('file',966,415,30)
text(1010,426,'Candidate update',23,700,cls='head')
text(1100,452,'Program + inherited state',18,anchor='middle')
stage(3,930,525,340,110,'Public evaluation','Correctness + native score','chart',C['greenbg'],C['green'])
s.append(f'<path d="M1100 675 L1225 735 L1100 795 L975 735 Z" fill="{C["greenbg"]}" stroke="{C["green"]}" stroke-width="2.8"/>')
text(1100,731,'Native gate',23,700,'middle',cls='head')
text(1100,758,'accept / reject',18,anchor='middle')

rect(500,690,280,95,C['amberbg'],C['amber'],2.5,13)
icon('lock',518,714,32)
text(568,730,'Retain old',24,700,cls='head')
text(640,762,'Native reject blocks activation',17,anchor='middle')
stage(5,930,855,340,125,'Apply decision','Accept → commit candidate','shield',C['amberbg'],C['amber'],'Reject / inconclusive → retain old')
stage(6,480,855,310,125,'Inherit full state','Program · archive · memory','database',C['greenbg'],C['green'],'Optimizer state included')
rect(480,1004,790,48,C['white'],C['gray'],2.1,11)
icon('lock',496,1015,25)
text(540,1035,'Freeze chosen system for deployment comparison',22,600)

# Native-feedback scope is separate from external recursive admission.
icon('branch',523,433,36)
text(575,460,'Native continuation',23,700,cls='head')
note(523,503,['Probe / label branches','continue native updates.','The deployment branch','stays frozen.'],21,32,color=C['muted'])
edge('M370 280 H480')
text(424,263,'adapter',17,anchor='middle',color=C['muted'])
edge('M790 295 H930')
edge('M1100 350 V395')
edge('M1100 470 V525')
edge('M1100 635 V675')
edge('M975 735 H780')
text(874,716,'native reject',18,anchor='middle')
edge('M640 785 V855')
edge('M930 917 H790')
edge('M635 980 V1004')
edge('M930 580 H885 V295 H930',C['green'],width=2.2)
text(870,584,'Public feedback in native branches',17,color='#52783D',rotate=-90)
edge('M480 917 H454 V295 H480','#87919A','11 7 2 7',True,width=2.3)
text(447,822,'Future continuous audit (not implemented)',16,color=C['muted'],rotate=-90)
edge('M370 650 H900 V580 H930',width=2.2)
text(673,640,'Task API · policy · tools',18,anchor='middle',color=C['muted'])

# Host-managed snapshot references create an allowed projection and paired forks.
edge('M635 240 V205 H1420',C['teal'],'7 6',width=2.4)
edge('M1250 432 H1295 V205',C['teal'],'7 6',arrow=False,width=2.4)
s.append(f'<circle cx="1295" cy="205" r="4" fill="{C["teal"]}"/>')
text(1030,193,'snapshot refs (read-only)',18,anchor='middle',color='#397D76')
rect(1420,185,660,45,C['white'],C['gray'],2,10)
text(1750,214,'Platform interface: allowed view + isolated forks',21,600,'middle')
edge('M1750 230 V265',C['teal'],width=2.4)
edge('M2080 208 H2104 V500 H2080',C['teal'],'7 6',width=2.4)
text(2117,416,'lab-managed forks',16,color='#397D76',rotate=-90)

rect(1420,265,660,120,C['tealbg'],C['teal'],2.6,14)
s.append(f'<circle cx="1438" cy="265" r="17" fill="white" stroke="{C["teal"]}" stroke-width="2.5"/>')
text(1438,272,'4',21,700,'middle')
icon('eye',1440,283,35)
text(1493,304,'Audit View',25,700,cls='head')
text(1493,335,'Allowed diff · state · native/public evidence',21)
text(1750,366,'No sealed labels; native rejects may receive diagnostic audit',18,anchor='middle')
# Gate status is evidence, not a claim that invalid candidates are never audited.
edge('M1225 735 H1350 V325 H1420')
text(1290,716,'gate status',18,anchor='middle')
text(1339,637,'one-shot admission (current)',17,color=C['muted'],rotate=-90)
edge('M1750 385 V420')

rect(1420,420,660,165,C['white'],C['teal'],2.6,14)
icon('branch',1440,439,35)
text(1493,460,'Budgeted paired probes',25,700,cls='head')
pill(1450,485,260,'Base branch',C['greenbg'],C['green'],21)
text(1735,507,'vs.',19,600,'middle')
pill(1760,485,290,'Candidate branch',C['greenbg'],C['green'],21)
text(1750,550,'m paired native rounds · matched tasks · current m ≤ 2',21,anchor='middle')
text(1750,574,'Via lab probe service; forked state and separate budgets',18,anchor='middle',color=C['muted'])
edge('M1750 585 V615')

rect(1420,615,660,165,C['tealbg'],C['teal'],2.6,14)
icon('search',1440,634,34)
text(1493,656,'Risk assessment',25,700,cls='head')
rect(1440,674,280,65,C['white'],C['teal'],1.9,10)
text(1580,715,'Rules (static)',23,600,'middle')
rect(1760,674,300,65,C['white'],C['teal'],1.9,10)
text(1910,701,'Frozen scorer',23,600,'middle')
text(1910,726,'optional · next run only',18,anchor='middle')
text(1750,765,'Risk · utility · coverage → detection report',21,anchor='middle')
edge('M1750 780 V835')
text(1770,814,'report',18,color=C['muted'])

rect(1420,835,660,145,C['amberbg'],C['amber'],2.8,14)
icon('shield',1440,852,35)
text(1493,878,'Lab admission policy',26,700,cls='head')
pill(1445,901,142,'Accept',C['white'],C['amber'],21)
pill(1600,901,142,'Reject',C['white'],C['amber'],21)
pill(1755,901,300,'Inconclusive (abstain)',C['white'],C['amber'],21)
text(1750,962,'Candidate-bound; native reject always blocks activation',19,anchor='middle')
edge('M1420 917 H1270')
text(1345,900,'decision',18,anchor='middle')
text(1750,1025,'Retaining the old system does not certify its safety.',20,anchor='middle',color=C['muted'])

# Independent evaluation has no return to the current optimization/audit path.
rect(35,1135,2090,245,C['graybg'],C['gray'],2.7,20,'12 8')
text(62,1174,'Independent Evidence & Offline Learning',27,700,cls='head')
text(2090,1173,'No sealed / final results return to search or online audit',20,anchor='end',color=C['muted'])
edge('M875 1052 V1110 H1015 V1188 H740 V1210',width=2.5)
text(905,1096,'Frozen run artifacts: state + trusted logs',21,color=C['muted'])

rect(65,1210,350,130,C['white'],C['gray'],2.2,13)
icon('chart',82,1227,31)
text(127,1254,'Risk · Utility · Cost',24,700,cls='head')
text(240,1289,'Path safety · adaptation',21,anchor='middle')
text(240,1320,'Coverage · missing evidence',19,anchor='middle')
rect(480,1210,520,130,C['white'],C['gray'],2.2,13)
icon('lock',498,1227,31)
text(543,1254,'Independent evaluation',25,700,cls='head')
text(740,1289,'Label branches: native continuation',21,anchor='middle')
text(740,1320,'Deployment comparison: frozen system',20,anchor='middle')
edge('M480 1275 H415')
edge('M1000 1275 H1140',C['teal'],'7 6',width=2.6)
text(1070,1250,'train / calib.',18,anchor='middle',color='#397D76')
text(1070,1308,'only',18,anchor='middle',color='#397D76')

rect(1140,1210,450,130,C['tealbg'],C['teal'],2.2,13,'8 5')
icon('learn',1158,1227,33)
text(1206,1254,'Offline train & calibrate',24,700,cls='head')
text(1365,1289,'Partitioned update / rollout evidence',20,anchor='middle')
text(1365,1320,'Trainable safety · efficacy under study',19,anchor='middle')
edge('M1590 1275 H1640',C['teal'],'7 6',width=2.6)
rect(1640,1210,440,130,C['white'],C['teal'],2.2,13)
icon('lock',1660,1230,31)
text(1707,1254,'Frozen model artifact',25,700,cls='head')
text(1860,1291,'Versioned and fixed before later runs',20,anchor='middle')
text(1860,1321,'Load into the optional scorer only',20,anchor='middle')
edge('M2080 1275 H2147 V706 H2060',C['teal'],'8 7',width=2.6)
text(2135,1195,'load frozen artifact (next run only)',18,color='#397D76',rotate=-90)

# Every fill/border color has one functional meaning. Future work is explicit.
for x,fill,stroke,label in [
    (65,C['greenbg'],C['green'],'Native RSI stages'),
    (410,C['amberbg'],C['amber'],'Lab-controlled admission'),
    (880,C['tealbg'],C['teal'],'Audit / offline learning'),
    (1335,C['graybg'],C['gray'],'Setup, runtime & evaluation'),
]:
    rect(x,1411,28,22,fill,stroke,2,4)
    text(x+42,1430,label,21)
edge('M1890 1422 H1940','#87919A','10 5 2 5',True,width=2.3)
text(1952,1430,'Future',21,color=C['muted'])
text(1080,1475,'Current: one-shot admission per root update; probe horizon m ≤ 2; deployment frozen; learned scorer under study.',20,anchor='middle',color=C['muted'])
text(1080,1510,'Adapted optimizers are not full native reproductions. Logical separation, not an OS sandbox. Native rejects may still be audited diagnostically.',18,anchor='middle',color=C['muted'])
s.append('</svg>')
target=OUT/'rsi-safety-lab-architecture-v2.svg'
target.write_text('\n'.join(s))
print(target)
