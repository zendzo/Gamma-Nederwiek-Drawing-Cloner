from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
import csv, json, math, os, re
from typing import Dict, Iterable, List, Optional, Tuple

PLAN_VERSION='0.4B.1a-spatial-classifier'

HEADER_ALIASES={
 'REVISION':('REV','REV.','REVISION','REV NO','REV. NO'),
 'REVISION_DATE':('REV DATE','REV. DATE','REVISION DATE','DATE'),
 'DRAWN_BY':('PREPARED BY','PREPARED BY.','DRAWN BY','DRAWN'),
 'REVISION_PURPOSE':('PURPOSE OF ISSUE','PURPOSE','ISSUE PURPOSE'),
 'REVISION_CHANGE_LOG':('COMMENT / CHANGE LOG','COMMENT/CHANGE LOG','CHANGE LOG','DESCRIPTION'),
 'CHECKED_BY':('CHECKED BY','CHECKED'),
 'APPROVED_BY':('APPROVED BY','APPROVED'),
}
STRUCTURAL_FIELDS=('REVISION','REVISION_DATE','DRAWN_BY','REVISION_PURPOSE','REVISION_CHANGE_LOG','CHECKED_BY','APPROVED_BY')
CONTROLLED_FIELDS=('PROJECT_NAME','DRAWING_NUMBER','PROJECT_REFERENCE')

@dataclass
class SpatialRevisionRow:
 identity:str; drawing_type:str; row_kind:str; row_y:float; header_y:float
 revision_handle:str=''; revision_value:str=''; date_handle:str=''; date_value:str=''
 purpose_handle:str=''; purpose_value:str=''; change_log_handle:str=''; change_log_value:str=''
 drawn_handle:str=''; drawn_value:str=''; checked_handle:str=''; checked_value:str=''
 approved_handle:str=''; approved_value:str=''; confidence:str='LOW'; confidence_score:int=0
 evidence:str=''; warning:str=''

@dataclass
class PlanAction:
 identity:str; drawing_type:str; output_revision:str; source_role:str; source_path:str
 handle:str; entity_type:str; layer:str; block_name:str; tag:str; old_value:str
 target_field:str; new_value:str; confidence:str; confidence_score:int; evidence:str
 action:str; reason:str

@dataclass
class RevisionPlan:
 identity:str; drawing_type:str; status:str; primary_geometry:str; revision_a_donor:str
 migration_date:str; revision_a_date:str; revision_00_date:str
 actions:List[PlanAction]=field(default_factory=list)
 spatial_rows:List[SpatialRevisionRow]=field(default_factory=list)
 warnings:List[str]=field(default_factory=list); errors:List[str]=field(default_factory=list)

class RevisionWritePlanBuilder:
 def __init__(self,settings,migration_date=None,a_offset=-7,revision_00_offset=1):
  self.settings=settings;self.migration_date=self._date(migration_date);self.a_offset=int(a_offset);self.revision_00_offset=int(revision_00_offset)
 @staticmethod
 def _date(value):
  if value in (None,''):return date.today()
  if isinstance(value,date):return value
  return datetime.strptime(str(value),'%Y-%m-%d').date()
 @staticmethod
 def _norm(value):return re.sub(r'\s+',' ',str(value or '').replace('\t',' ').replace('\r',' ').replace('\n',' ')).strip().upper()
 @staticmethod
 def _xy(row):
  try:
   values=str(row.get('insertion_point','')).split(',');return float(values[0]),float(values[1])
  except:return None
 @staticmethod
 def _load_fields(path):
  with Path(path).open('r',encoding='utf-8-sig',errors='replace',newline='') as f:return list(csv.DictReader(f))
 @staticmethod
 def _selected_groups(groups):
  # Production scale: unlimited selected drawing groups. Memory remains bounded
  # because the GUI worker invokes the planner one drawing at a time.
  return [g for g in groups if getattr(g,'selected',True) and g.drawing_type in ('SP','AS','GA')]
 @classmethod
 def _header_field(cls,value):
  value=cls._norm(value)
  for field,aliases in HEADER_ALIASES.items():
   if value in aliases:return field
  return None
 @staticmethod
 def _cluster_y(items,tolerance=0.35):
  groups=[]
  for item in sorted(items,key=lambda z:z['_y']):
   if not groups or abs(item['_y']-sum(x['_y'] for x in groups[-1])/len(groups[-1]))>tolerance:groups.append([item])
   else:groups[-1].append(item)
  return groups
 @classmethod
 def _find_header(cls,rows):
  candidates=[]
  for row in rows:
   point=cls._xy(row);field=cls._header_field(row.get('value'))
   if point and field:
    item=dict(row);item['_x'],item['_y']=point;item['_header_field']=field;candidates.append(item)
  best=None
  for cluster in cls._cluster_y(candidates,0.65):
   unique={r['_header_field'] for r in cluster}
   required=len(unique & {'REVISION','REVISION_DATE','REVISION_PURPOSE'})
   score=required*100+len(unique)
   if required>=3 and (best is None or score>best[0]):best=(score,cluster)
  return best[1] if best else []
 @staticmethod
 def _column_bounds(header):
  ordered=sorted(header,key=lambda r:r['_x']);centers=[]
  seen=set()
  for row in ordered:
   if row['_header_field'] not in seen:centers.append((row['_header_field'],row['_x'],row));seen.add(row['_header_field'])
  bounds=[]
  for i,(field,x,row) in enumerate(centers):
   left=-math.inf if i==0 else (centers[i-1][1]+x)/2
   right=math.inf if i==len(centers)-1 else (x+centers[i+1][1])/2
   bounds.append((field,left,right,x,row))
  return bounds
 @classmethod
 def _detect_spatial_rows(cls,identity,drawing_type,rows):
  header=cls._find_header(rows)
  if not header:return [],['REVISION_HEADER_NOT_FOUND']
  bounds=cls._column_bounds(header);header_y=sum(r['_y'] for r in header)/len(header)
  min_x=min(x for _,_,_,x,_ in bounds);max_x=max(x for _,_,_,x,_ in bounds)
  # Revision rows are below the header and limited to the detected table width.
  candidates=[]
  for row in rows:
   point=cls._xy(row)
   if not point:continue
   x,y=point
   if y>=header_y-0.5 or x<min_x-2.5 or x>max_x+2.5:continue
   item=dict(row);item['_x']=x;item['_y']=y
   for field,left,right,center,hrow in bounds:
    if left<=x<right:item['_column']=field;item['_header_x']=center;item['_header_handle']=hrow.get('handle','');candidates.append(item);break
  detected=[]
  for cluster in cls._cluster_y(candidates,0.35):
   cells={}
   for item in cluster:
    field=item['_column'];current=cells.get(field)
    if current is None or abs(item['_x']-item['_header_x'])<abs(current['_x']-current['_header_x']):cells[field]=item
   rev=cls._norm(cells.get('REVISION',{}).get('value',''))
   date_value=str(cells.get('REVISION_DATE',{}).get('value','')).strip()
   purpose=cls._norm(cells.get('REVISION_PURPOSE',{}).get('value',''))
   change=cls._norm(cells.get('REVISION_CHANGE_LOG',{}).get('value',''))
   if rev not in ('A','00'):continue
   row_kind=rev
   score=30
   evidence=['HEADER_ANCHORED','Y_CLUSTERED','REVISION_VALUE_MATCH']
   if re.fullmatch(r'\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}[./-]\d{1,2}[./-]\d{1,2}',date_value):score+=25;evidence.append('DATE_VALUE_MATCH')
   expected='ISSUED FOR COMMENTS' if rev=='A' else 'APPROVED FOR CONSTRUCTION'
   if expected in purpose:score+=25;evidence.append('PURPOSE_VALUE_MATCH')
   if expected in change:score+=10;evidence.append('CHANGE_LOG_VALUE_MATCH')
   if any(k in cells for k in ('DRAWN_BY','CHECKED_BY','APPROVED_BY')):score+=10;evidence.append('PERSONNEL_CELL_PRESENT')
   confidence='HIGH' if score>=80 else 'MEDIUM' if score>=65 else 'LOW'
   def pair(field):
    item=cells.get(field,{})
    return item.get('handle',''),item.get('value','')
   rh,rv=pair('REVISION');dh,dv=pair('REVISION_DATE');ph,pv=pair('REVISION_PURPOSE');ch,cv=pair('REVISION_CHANGE_LOG');drh,drv=pair('DRAWN_BY');ckh,ckv=pair('CHECKED_BY');aph,apv=pair('APPROVED_BY')
   detected.append(SpatialRevisionRow(identity,drawing_type,row_kind,sum(x['_y'] for x in cluster)/len(cluster),header_y,rh,rv,dh,dv,ph,pv,ch,cv,drh,drv,ckh,ckv,aph,apv,confidence,score,';'.join(evidence),'' if confidence=='HIGH' else 'MANUAL_REVIEW_REQUIRED'))
  warnings=[]
  for kind in ('A','00'):
   matches=[r for r in detected if r.row_kind==kind]
   if not matches:warnings.append('SPATIAL_ROW_NOT_FOUND:'+kind)
   elif len(matches)>1:warnings.append('SPATIAL_ROW_AMBIGUOUS:'+kind+':'+str(len(matches)))
  return detected,warnings
 @staticmethod
 def _format_date(template,value):
  text=str(template or '').strip()
  if re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}',text):return value.strftime('%Y-%m-%d')
  if '/' in text:return value.strftime('%d/%m/%Y')
  if '.' in text:return value.strftime('%d.%m.%Y')
  return value.strftime('%d-%m-%Y')
 def _action(self,group,output,row,field,new,evidence,score=95,reason='SPATIAL_HEADER_ANCHORED'):
  old=row.get('value','');return PlanAction(group.identity,group.drawing_type,output,'PRIMARY_GEOMETRY',group.primary_source or '',row.get('handle',''),row.get('entity_type',''),row.get('layer',''),row.get('block_name',''),row.get('tag',''),old,field,new,'HIGH',score,evidence,'WRITE' if new!=old else 'VERIFY_ONLY',reason)
 def _row_by_handle(self,rows,handle):return next((r for r in rows if r.get('handle')==handle),None)
 def _spatial_actions(self,group,rows,spatial,a_date,z_date):
  actions=[]
  for detected in spatial:
   output=detected.row_kind;when=a_date if output=='A' else z_date;purpose='ISSUED FOR COMMENTS' if output=='A' else 'APPROVED FOR CONSTRUCTION'
   for handle,field,new in ((detected.revision_handle,'REVISION',output),(detected.date_handle,'REVISION_DATE',self._format_date(detected.date_value,when)),(detected.purpose_handle,'REVISION_PURPOSE',purpose),(detected.change_log_handle,'REVISION_CHANGE_LOG',purpose)):
    row=self._row_by_handle(rows,handle) if handle else None
    if row:actions.append(self._action(group,output,row,field,new,detected.evidence,detected.confidence_score))
   for handle,field in ((detected.drawn_handle,'DRAWN_BY'),(detected.checked_handle,'CHECKED_BY'),(detected.approved_handle,'APPROVED_BY')):
    row=self._row_by_handle(rows,handle) if handle else None
    if row:
     action=self._action(group,output,row,field,row.get('value',''),detected.evidence,detected.confidence_score,'PERSONNEL_PRESERVED');action.action='VERIFY_ONLY';actions.append(action)
  return actions
 def _controlled_actions(self,group,rows):
  actions=[]
  mappings=[('PROJECT_NAME',getattr(self.settings,'project_from',''),getattr(self.settings,'project_to',''),95),('DRAWING_NUMBER',getattr(self.settings,'drawing_from',''),getattr(self.settings,'drawing_to',''),95),('PROJECT_REFERENCE',getattr(self.settings,'long_from',''),getattr(self.settings,'long_to',''),95),('PROJECT_REFERENCE',getattr(self.settings,'short_from',''),getattr(self.settings,'short_to',''),90)]
  seen=set()
  for field,old,new,score in mappings:
   if not old:continue
   for row in rows:
    value=row.get('value','')
    if old.lower() not in value.lower():continue
    key=(row.get('handle'),field)
    if key in seen:continue
    seen.add(key);target=re.sub(re.escape(old),lambda _:new,value,flags=re.I)
    for output in ('A','00'):actions.append(self._action(group,output,row,field,target,'EXACT_CONFIGURED_SUBSTRING',score,'CONTROLLED_MULTI_OCCURRENCE'))
  return actions
 def build(self,groups,discovery_csv):
  all_rows=self._load_fields(discovery_csv);grouped={}
  for row in all_rows:grouped.setdefault(row.get('drawing_identity',''),[]).append(row)
  plans=[]
  for group in self._selected_groups(groups):
   rows=grouped.get(group.identity,[]);a_date=self.migration_date+timedelta(days=self.a_offset);z_date=self.migration_date+timedelta(days=self.revision_00_offset);plan=RevisionPlan(group.identity,group.drawing_type,'PLANNING',group.primary_source or '',group.revision_a_donor or '',self.migration_date.isoformat(),a_date.isoformat(),z_date.isoformat())
   if not group.primary_source:plan.errors.append('PRIMARY_GEOMETRY_MISSING')
   if not group.revision_a_donor:plan.errors.append('REVISION_A_DONOR_MISSING')
   if not rows:plan.errors.append('DISCOVERY_ROWS_MISSING')
   spatial,warnings=self._detect_spatial_rows(group.identity,group.drawing_type,rows);plan.spatial_rows=spatial;plan.warnings.extend(warnings)
   high={r.row_kind:r for r in spatial if r.confidence=='HIGH'}
   for kind in ('A','00'):
    if kind not in high:plan.errors.append('HIGH_CONFIDENCE_SPATIAL_ROW_MISSING:'+kind)
   plan.actions.extend(self._spatial_actions(group,rows,spatial,a_date,z_date));plan.actions.extend(self._controlled_actions(group,rows))
   if not any(a.target_field=='PROJECT_REFERENCE' for a in plan.actions):plan.warnings.append('FIELD_NOT_FOUND:PROJECT_REFERENCE')
   plan.status='BLOCKED' if plan.errors else ('READY_WITH_WARNINGS' if plan.warnings else 'READY');plans.append(plan)
  return plans
 @staticmethod
 def write_reports(bundle_root,plans):
  report=Path(bundle_root)/'_GNI_REPORTS';report.mkdir(parents=True,exist_ok=True);jp=report/'revision_pilot_write_plan.json';cp=report/'revision_pilot_write_plan.csv';sp=report/'revision_spatial_rows.csv'
  payload={'schema':'GNI-Revision-Spatial-Plan-0.4B.1a','version':PLAN_VERSION,'generated_at':datetime.now().astimezone().isoformat(),'summary':{'plans':len(plans),'ready':sum(p.status.startswith('READY') for p in plans),'blocked':sum(p.status=='BLOCKED' for p in plans),'actions':sum(len(p.actions) for p in plans),'spatial_rows':sum(len(p.spatial_rows) for p in plans)},'plans':[asdict(p) for p in plans]};tmp=jp.with_suffix('.json.tmp');tmp.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding='utf-8');os.replace(tmp,jp)
  with cp.open('w',encoding='utf-8-sig',newline='') as f:
   names=list(PlanAction.__dataclass_fields__);w=csv.DictWriter(f,fieldnames=names);w.writeheader()
   for p in plans:
    for a in p.actions:w.writerow(asdict(a))
  with sp.open('w',encoding='utf-8-sig',newline='') as f:
   names=list(SpatialRevisionRow.__dataclass_fields__);w=csv.DictWriter(f,fieldnames=names);w.writeheader()
   for p in plans:
    for r in p.spatial_rows:w.writerow(asdict(r))
  return jp,cp,sp
