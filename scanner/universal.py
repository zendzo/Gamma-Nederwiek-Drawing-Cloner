from pathlib import Path
from datetime import datetime,timezone
from collections import defaultdict
import re,hashlib
from models import SourceFile,DrawingGroup
REV=re.compile(r'_(A|\d{2}|\d|)$',re.I); SUP={'.dwg':'DWG','.xls':'PART_LIST','.xlsx':'PART_LIST','.nc1':'NC1','.dxf':'DXF'}
class Scanner:
 def __init__(self,root):self.root=Path(root).resolve()
 @staticmethod
 def parse(stem):
  m=REV.search(stem)
  if not m:return re.sub(r'\s+-\s+1$','',stem).strip(),'',''
  raw=m.group(1).upper(); ident=re.sub(r'\s+-\s+1$','',stem[:m.start()]).strip(); warn=''
  if raw=='A':r='A'
  elif len(raw)==2 and raw.isdigit():r=raw
  elif raw.isdigit():r=raw.zfill(2);warn='NONSTANDARD_FILENAME_REVISION'
  else:r='';warn='NONSTANDARD_FILENAME_REVISION'
  return ident,r,warn
 @staticmethod
 def dtype(name):
  u=name.upper()
  return 'SP' if u.startswith('SP-') else ('AS' if u.startswith('AS-') else ('GA' if '-GA-' in u else 'UNKNOWN'))
 def scan(self):
  if not self.root.is_dir():raise ValueError('Invalid source folder')
  out=[]
  for p in self.root.rglob('*'):
   ext=p.suffix.lower()
   if not p.is_file() or ext not in SUP:continue
   role=SUP[ext];ident=rev=warn='';dt='OTHER';sel=True
   if role=='DWG':ident,rev,warn=self.parse(p.stem);dt=self.dtype(p.name)
   elif role=='PART_LIST' and not any(x in (str(p).upper()) for x in ('PART LIST','PARTLIST','BOM','MATERIAL LIST','PLATE LIST')):warn='PART_LIST_REVIEW';sel=False
   s=p.stat();out.append(SourceFile(str(p),p.relative_to(self.root).as_posix(),p.name,ext[1:].upper(),s.st_size,datetime.fromtimestamp(s.st_mtime,timezone.utc).isoformat(),role,dt,ident,rev,warn,sel,''))
  return out
 def groups(self,fs):
  d=defaultdict(list)
  for f in fs:
   if f.role=='DWG':d[f.identity].append(f)
  out=[]
  for ident,cs in sorted(d.items()):
   dt=next((x.drawing_type for x in cs if x.drawing_type!='UNKNOWN'),'UNKNOWN');nums=sorted([x for x in cs if x.revision.isdigit()],key=lambda x:(int(x.revision),x.modified_iso),reverse=True);aa=sorted([x for x in cs if x.revision=='A'],key=lambda x:x.modified_iso,reverse=True);primary=(nums or aa or sorted(cs,key=lambda x:x.modified_iso,reverse=True))[0].path;donor=aa[0].path if aa else None;w=';'.join(sorted({x.warning for x in cs if x.warning}));out.append(DrawingGroup(ident,dt,cs,True,primary,donor,w))
  return out
 @staticmethod
 def supporting(fs):
  out=[]
  for f in fs:
   if f.role=='PART_LIST':f.destination_relative='4. PART LIST/'+f.filename;out.append(f)
   elif f.role in ('NC1','DXF'):f.destination_relative='5. NC1 & DXF/'+f.filename;out.append(f)
  return out
