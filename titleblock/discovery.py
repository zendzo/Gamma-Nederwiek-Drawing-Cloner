from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import csv, json, os, re, shutil, subprocess, tempfile, time, uuid
from typing import Iterable, List

from runtime.process_tracking import run_console

DISCOVERY_VERSION='0.4A.2-full-replacement'

@dataclass
class DiscoveryField:
    drawing_identity:str; drawing_type:str; source_path:str; drawing:str
    entity_type:str; handle:str; layer:str; tag:str; value:str
    insertion_point:str; owner_handle:str; owner_type:str; block_name:str
    suggested_field:str; confidence:str; confidence_score:int; evidence:str
    proposed_a:str; proposed_00:str; warning:str=''

@dataclass
class DiscoveryResult:
    identity:str; drawing_type:str; source_path:str; status:str; job_folder:str
    tsv_path:str=''; log_path:str=''; fields:List[DiscoveryField]=field(default_factory=list)
    error:str=''; elapsed_seconds:float=0.0

class TitleBlockDiscoveryEngine:
    LOAD_MARKER='GNI_TITLEBLOCK_DISCOVERY_0_4A_LOADED'
    VERSION_MARKER='GNI_TITLEBLOCK_DISCOVERY_0_4A_1_OWNER_FIX'
    SUCCESS_MARKER='GNI_TITLEBLOCK_DISCOVERY_OK:'
    TAG_RULES=(
      ('REVISION',98,('REV','REVISION','REV_NO','REVNO')),
      ('REVISION_DATE',97,('REV_DATE','REVDATE','REVISION_DATE','DATE_REV')),
      ('REVISION_PURPOSE',97,('PURPOSE','STATUS','DESCRIPTION','REV_DESC','ISSUE')),
      ('DRAWN_BY',98,('DRAWN','DRAWN_BY','DRAWNBY','DWN')),
      ('CHECKED_BY',98,('CHECKED','CHECKED_BY','CHECKEDBY','CHK')),
      ('APPROVED_BY',98,('APPROVED','APPROVED_BY','APPROVEDBY','APP')),
      ('PROJECT_NAME',97,('PROJECT','PROJECT_NAME','PROJECTNAME','PROJ_NAME')),
      ('DRAWING_NUMBER',97,('DRAWING_NO','DRAWING_NUMBER','DRAWINGNUMBER','DWG_NO','DWGNO')),
      ('PROJECT_REFERENCE',95,('PROJECT_REF','PROJECT_REFERENCE','REFERENCE','REF_NO','REFNO')),
    )
    def __init__(self,core_console,lisp_source,settings,application_root,timeout=180):
        self.core=Path(core_console).resolve();self.lisp_source=Path(lisp_source).resolve();self.settings=settings;self.application_root=Path(application_root).resolve();self.timeout=int(timeout)
        if not self.core.is_file():raise FileNotFoundError(f'accoreconsole.exe not found: {self.core}')
        if not self.lisp_source.is_file():raise FileNotFoundError(f'Discovery LSP not found: {self.lisp_source}')
        temp=Path(os.environ.get('TEMP') or tempfile.gettempdir()).resolve();self.job_root=temp/'GNI'/'titleblock_discovery';self.runtime_root=temp/'GNI'/'trusted_runtime';self.job_root.mkdir(parents=True,exist_ok=True);self.runtime_root.mkdir(parents=True,exist_ok=True)
        self.runtime_lisp=self.runtime_root/'title_block_discovery.lsp';self._copy(self.lisp_source,self.runtime_lisp)
    @staticmethod
    def _decode(v):
        if v is None:return ''
        if isinstance(v,str):return v
        if v.startswith(b'\xff\xfe') or (len(v)>4 and v[1:2]==b'\x00'):return v.decode('utf-16',errors='replace')
        for enc in ('utf-8','cp1252'):
            try:return v.decode(enc)
            except UnicodeDecodeError:pass
        return v.decode('utf-8',errors='replace')
    @staticmethod
    def _copy(src,dst):
        src=Path(src);dst=Path(dst);dst.parent.mkdir(parents=True,exist_ok=True);tmp=dst.with_suffix(dst.suffix+'.copying')
        if tmp.exists():tmp.unlink()
        with src.open('rb',buffering=0) as a,tmp.open('wb',buffering=0) as b:shutil.copyfileobj(a,b,4*1024*1024);b.flush();os.fsync(b.fileno())
        if tmp.stat().st_size!=src.stat().st_size:raise OSError('Discovery staging size mismatch')
        os.replace(tmp,dst);return dst
    @staticmethod
    def _safe(v):return ''.join(c if c.isalnum() or c in '._-' else '_' for c in str(v))[:80]
    @staticmethod
    def _q(p):return str(Path(p).resolve()).replace('\\','/').replace('"','\\"')
    @staticmethod
    def _tag(v):return re.sub(r'[^A-Z0-9]+','_',str(v or '').upper()).strip('_')
    def _classify(self,row):
        tag=self._tag(row.get('Tag'));value=str(row.get('Value') or '').strip();upper=value.upper();name='UNCLASSIFIED';score=0;e=[]
        for candidate,points,aliases in self.TAG_RULES:
            if tag in aliases:name=candidate;score=points;e.append('TAG='+tag);break
            if any(a in tag for a in aliases if len(a)>=5) and points-8>score:name=candidate;score=points-8;e.append('TAG_CONTAINS='+tag)
        for candidate,needle,points in (("PROJECT_NAME",getattr(self.settings,'project_from',''),92),("DRAWING_NUMBER",getattr(self.settings,'drawing_from',''),92),("PROJECT_REFERENCE",getattr(self.settings,'long_from',''),91),("PROJECT_REFERENCE",getattr(self.settings,'short_from',''),84)):
            if needle and needle.upper() in upper and points>score:name=candidate;score=points;e.append('VALUE_CONTAINS='+needle)
        if name=='UNCLASSIFIED' and re.fullmatch(r'A|\d{1,2}',upper):name='REVISION';score=62;e.append('VALUE_LOOKS_LIKE_REVISION')
        if name=='UNCLASSIFIED' and re.fullmatch(r'\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}',value):name='REVISION_DATE';score=58;e.append('VALUE_LOOKS_LIKE_DATE')
        return name,('HIGH' if score>=90 else 'MEDIUM' if score>=70 else 'LOW'),score,';'.join(e)
    def _proposals(self,name,value):
        value=str(value or '')
        if name=='REVISION':return 'A','00'
        if name=='REVISION_PURPOSE':return 'ISSUED FOR COMMENTS','APPROVED FOR CONSTRUCTION'
        if name=='PROJECT_NAME':return getattr(self.settings,'project_to',''),getattr(self.settings,'project_to','')
        if name=='DRAWING_NUMBER':
            old=getattr(self.settings,'drawing_from','');new=getattr(self.settings,'drawing_to','');v=value.replace(old,new) if old else value;return v,v
        if name=='PROJECT_REFERENCE':
            for old,new in ((getattr(self.settings,'long_from',''),getattr(self.settings,'long_to','')),(getattr(self.settings,'short_from',''),getattr(self.settings,'short_to',''))):
                if old:value=value.replace(old,new)
            return value,value
        return value,value
    def _parse(self,group,source,tsv):
        out=[]
        with tsv.open('r',encoding='utf-8-sig',errors='replace',newline='') as f:
            for row in csv.DictReader(f,delimiter='\t'):
                name,confidence,score,evidence=self._classify(row);a,z=self._proposals(name,row.get('Value'))
                out.append(DiscoveryField(group.identity,group.drawing_type,str(source),row.get('Drawing',''),row.get('Entity Type',''),row.get('Handle',''),row.get('Layer',''),row.get('Tag',''),row.get('Value',''),row.get('Insertion Point',''),row.get('Owner Handle',''),row.get('Owner Type',''),row.get('Block Name',''),name,confidence,score,evidence,a,z,'MANUAL_REVIEW_REQUIRED' if confidence=='LOW' else ''))
        return out
    def discover_group(self,group):
        started=time.time();source=Path(group.primary_source).resolve();job=self.job_root/(datetime.now().strftime('%Y%m%d_%H%M%S_')+group.drawing_type+'_'+self._safe(group.identity)+'_'+uuid.uuid4().hex[:8]);job.mkdir(parents=True,exist_ok=False);log=job/'discovery.log'
        log.write_text(f'MVP={DISCOVERY_VERSION}\nSTATUS=JOB_CREATED\nIDENTITY={group.identity}\nTYPE={group.drawing_type}\nSOURCE={source}\nCORE={self.core}\nLSP={self.runtime_lisp}\n',encoding='utf-8')
        result=DiscoveryResult(group.identity,group.drawing_type,str(source),'PROCESSING',str(job),log_path=str(log))
        try:
            staged=self._copy(source,job/'input.dwg');tsv=job/'title_block_discovery.tsv';script=job/'discover.scr';result.tsv_path=str(tsv)
            script.write_text('\n'.join(['(setvar "FILEDIA" 0)','(setvar "CMDDIA" 0)',f'(setvar "TRUSTEDPATHS" "{self._q(self.runtime_root)}")',f'(load "{self._q(self.runtime_lisp)}")',f'(GNI:DISCOVER "{self._q(tsv)}")','_.QUIT','_Y','']),encoding='ascii',errors='replace')
            cp=run_console([str(self.core),'/i',str(staged),'/s',str(script),'/l','en-US'],capture_output=True,text=False,timeout=self.timeout,cwd=str(job));combined=self._decode(cp.stdout)+'\n'+self._decode(cp.stderr)
            with log.open('a',encoding='utf-8',errors='replace') as f:f.write(f'\nSTATUS=CORE_COMPLETED\nEXIT_CODE={cp.returncode}\n{combined}')
            upper=combined.upper()
            if self.LOAD_MARKER not in combined:raise RuntimeError('DISCOVERY_LISP_LOAD_FAILED')
            if self.VERSION_MARKER not in combined:raise RuntimeError('DISCOVERY_WRONG_LISP_VERSION')
            if '; ERROR:' in upper:raise RuntimeError('DISCOVERY_LISP_RUNTIME_ERROR')
            if self.SUCCESS_MARKER not in combined:raise RuntimeError('DISCOVERY_SUCCESS_MARKER_NOT_FOUND')
            if not tsv.is_file() or tsv.stat().st_size<30:raise RuntimeError('DISCOVERY_TSV_NOT_CREATED')
            result.fields=self._parse(group,source,tsv)
            if not result.fields:raise RuntimeError('DISCOVERY_TSV_HAS_NO_DATA_ROWS')
            result.status='SUCCESS'
        except subprocess.TimeoutExpired as exc:
            result.status='FAILED';result.error=f'CORE_TIMEOUT after {self.timeout} seconds'
            with log.open('a',encoding='utf-8',errors='replace') as f:f.write('\nSTATUS=FAILED\nERROR='+result.error+'\nSTDOUT='+self._decode(exc.stdout)+'\nSTDERR='+self._decode(exc.stderr))
        except Exception as exc:
            result.status='FAILED';result.error=repr(exc)
            with log.open('a',encoding='utf-8',errors='replace') as f:f.write('\nSTATUS=FAILED\nERROR='+result.error+'\n')
        result.elapsed_seconds=round(time.time()-started,3);return result
    def discover_many(self,groups:Iterable):return [self.discover_group(g) for g in groups]
    @staticmethod
    def write_report(bundle_root, results: List[DiscoveryResult], complete=True):
        """Write the discovery handoff report.

        ``complete`` is recorded in the payload so the GUI can tell a finished
        sweep from a cancelled one; both used to produce an identical
        ``title_block_discovery_report.json`` and the pipeline advanced on
        partial data.
        """
        report=Path(bundle_root)/'_GNI_REPORTS';report.mkdir(parents=True,exist_ok=True);jp=report/'title_block_discovery_report.json';cp=report/'title_block_discovery_fields.csv';payload={'schema':'GNI-TitleBlock-Discovery-0.4A.2','generated_at':datetime.now().astimezone().isoformat(),'complete':bool(complete),'summary':{'drawings':len(results),'success':sum(x.status=='SUCCESS' for x in results),'failed':sum(x.status=='FAILED' for x in results),'fields':sum(len(x.fields) for x in results)},'results':[asdict(x) for x in results]};tmp=jp.with_suffix('.json.tmp');tmp.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding='utf-8');os.replace(tmp,jp);names=list(DiscoveryField.__dataclass_fields__)
        with cp.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=names);w.writeheader()
            for r in results:
                for x in r.fields:w.writerow(asdict(x))
        return jp,cp
