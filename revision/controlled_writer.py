from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import csv, hashlib, json, os, shutil, subprocess, tempfile, time, uuid
from typing import List

WRITER_VERSION='0.4B.2.6.2-shutdown-guard'

@dataclass
class ControlledOutput:
    identity:str; drawing_type:str; output_revision:str; status:str
    source_path:str; working_dwg:str; job_folder:str; log_path:str=''
    result_tsv:str=''; actions_requested:int=0; actions_written:int=0
    actions_verified:int=0; actions_failed:int=0; sha256:str=''
    elapsed_seconds:float=0.0; error:str=''

@dataclass
class ControlledWriterResult:
    identity:str; drawing_type:str; status:str; outputs:List[ControlledOutput]=field(default_factory=list)
    errors:List[str]=field(default_factory=list)

class ControlledRevisionWriter:
    LOAD_MARKER='GNI_REVISION_CONTROLLED_WRITER_0_4B_2_LOADED'
    SUCCESS_MARKER='GNI_CONTROLLED_WRITER_OK:'
    EXIT_MARKER='GNI_WRITER_EXIT_READY'
    VERSION_MARKER='GNI_REVISION_CONTROLLED_WRITER_0_4B_2_3_SHUTDOWN_SAFE'


    FAIL_MARKER='GNI_CONTROLLED_WRITER_FAILED:'
    def __init__(self,core_console,lisp_source,application_root,timeout=180,target_source_layer=('Other','Other object type'),target_layer='NEDERWIEK_DATA'):
        self.core=Path(core_console).resolve();self.lisp_source=Path(lisp_source).resolve();self.application_root=Path(application_root).resolve();self.timeout=int(timeout);self.target_source_layers={str(x).casefold() for x in (target_source_layer if isinstance(target_source_layer,(tuple,list,set)) else (target_source_layer,))};self.target_layer=target_layer
        if not self.core.is_file():raise FileNotFoundError(f'accoreconsole.exe not found: {self.core}')
        if not self.lisp_source.is_file():raise FileNotFoundError(f'Controlled Writer LSP not found: {self.lisp_source}')
        temp=Path(os.environ.get('TEMP') or tempfile.gettempdir()).resolve();self.job_root=temp/'GNI'/'revision_controlled_writer';self.runtime_root=temp/'GNI'/'trusted_runtime';self.job_root.mkdir(parents=True,exist_ok=True);self.runtime_root.mkdir(parents=True,exist_ok=True);self.runtime_lisp=self.runtime_root/'revision_controlled_writer.lsp';self._copy(self.lisp_source,self.runtime_lisp)
        self.assets=self._assets()
        settings_path=self.application_root/'revision'/'writer_settings.json'
        if settings_path.is_file():
            import json as _json
            cfg=_json.loads(settings_path.read_text(encoding='utf-8'))
            self.target_layer=cfg.get('target_layer_name',self.target_layer)
            self.target_layer_color_aci=int(cfg.get('target_layer_color_aci',6))
            configured=cfg.get('target_source_layers',list(self.target_source_layers))
            self.target_source_layers={str(x).casefold() for x in configured}
        else:
            self.target_layer_color_aci=6
    def _assets(self):
        root=self.application_root/'resources'/'reference_images';result=[]
        for name in ('SEAtrium_Ge_.PNG','Tennet LogO New.PNG'):
            p=root/name
            if not p.is_file():raise FileNotFoundError(f'Embedded raster missing: {p}')
            result.append(p)
        return result
    @staticmethod
    def _copy(src,dst):
        src=Path(src);dst=Path(dst);dst.parent.mkdir(parents=True,exist_ok=True);tmp=dst.with_suffix(dst.suffix+'.copying-'+uuid.uuid4().hex[:8])
        with src.open('rb',buffering=0) as a,tmp.open('wb',buffering=0) as b:shutil.copyfileobj(a,b,4*1024*1024);b.flush();os.fsync(b.fileno())
        if tmp.stat().st_size!=src.stat().st_size:tmp.unlink(missing_ok=True);raise OSError('Copy size mismatch')
        os.replace(tmp,dst);return dst
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
    def _q(p):return str(Path(p).resolve()).replace('\\','/').replace('"','\\"')
    @staticmethod
    def _lisp(s):return str(s or '').replace('\\','\\\\').replace('"','\\"')
    @staticmethod
    def _sha(path):
        h=hashlib.sha256()
        with Path(path).open('rb') as f:
            for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
        return h.hexdigest()
    @staticmethod
    def load_ready_plans(path):
        data=json.loads(Path(path).read_text(encoding='utf-8'));return [p for p in data.get('plans',[]) if str(p.get('status','')).startswith('READY')]
    def _action_expr(self,a):
        target_layer=self.target_layer if a.get('action') in ('WRITE','MOVE_ONLY') and str(a.get('layer','')).casefold() in self.target_source_layers else ''
        values=(a.get('handle',''),a.get('entity_type',''),a.get('tag',''),a.get('old_value',''),a.get('new_value',''),a.get('target_field',''),a.get('action',''),a.get('layer',''),target_layer)
        return '('+' '.join('"'+self._lisp(v)+'"' for v in values)+')'
    def _run_output(self,plan,mode,job):
        started=time.time();identity=plan['identity'];source=Path(plan['primary_geometry']).resolve();working=job/f'{identity}_{mode}.dwg';result_tsv=job/f'{mode}_writer_result.tsv';log=job/f'{mode}_writer.log';out=ControlledOutput(identity,plan['drawing_type'],mode,'PROCESSING',str(source),str(working),str(job),str(log),str(result_tsv))
        try:
            self._copy(source,working)
            for asset in self.assets:self._copy(asset,job/asset.name)
            actions=[a for a in plan.get('actions',[]) if a.get('output_revision')==mode];out.actions_requested=len(actions)
            if not actions:raise RuntimeError('NO_ACTIONS_FOR_OUTPUT:'+mode)
            expr="'("+' '.join(self._action_expr(a) for a in actions)+')';script=job/f'{mode}_writer.scr'
            script.write_text('\n'.join(['(setvar "FILEDIA" 0)','(setvar "CMDDIA" 0)','(if (getvar "REPORTERROR") (setvar "REPORTERROR" 0))',f'(setvar "TRUSTEDPATHS" "{self._q(self.runtime_root)}")',f'(load "{self._q(self.runtime_lisp)}")',f'(setq GNI:TARGET-LAYER-COLOR {self.target_layer_color_aci})',f'(setq GNI:WRITER-ACTIONS {expr})',f'(GNI:RUN-WRITER "{mode}" GNI:WRITER-ACTIONS "{self._q(result_tsv)}")','']),encoding='ascii',errors='replace')
            cp=subprocess.run([str(self.core),'/i',str(working),'/s',str(script),'/l','en-US'],capture_output=True,text=False,timeout=self.timeout,cwd=str(job),creationflags=(getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0));combined=self._decode(cp.stdout)+'\n'+self._decode(cp.stderr);log.write_text(f'MVP={WRITER_VERSION}\nEXIT_CODE={cp.returncode}\n{combined}',encoding='utf-8',errors='replace')
            if self.LOAD_MARKER not in combined:raise RuntimeError('WRITER_LISP_LOAD_FAILED')
            if self.VERSION_MARKER not in combined:raise RuntimeError('WRITER_WRONG_LISP_VERSION')
            if '; ERROR:' in combined.upper():raise RuntimeError('WRITER_LISP_RUNTIME_ERROR')
            if self.FAIL_MARKER in combined:raise RuntimeError('WRITER_ACTION_VALIDATION_FAILED')
            if self.SUCCESS_MARKER not in combined:raise RuntimeError('WRITER_SUCCESS_MARKER_NOT_FOUND')
            shutdown_warning=self.EXIT_MARKER not in combined or cp.returncode not in (0,None)
            if not result_tsv.is_file():raise RuntimeError('WRITER_RESULT_TSV_MISSING')
            with result_tsv.open('r',encoding='utf-8-sig',errors='replace',newline='') as f:
                rows=list(csv.DictReader(f,delimiter='\t'))
            out.actions_written=sum(r.get('Status')=='WRITTEN' for r in rows);out.actions_verified=sum(r.get('Status')=='VERIFIED' for r in rows);out.actions_failed=sum(r.get('Status')=='FAILED' for r in rows)
            if out.actions_failed:raise RuntimeError(f'WRITER_ACTIONS_FAILED:{out.actions_failed}')
            out.sha256=self._sha(working);out.status='LOCAL_WRITE_SUCCESS_WITH_SHUTDOWN_WARNING' if shutdown_warning else 'LOCAL_WRITE_SUCCESS';
            for suffix in ('.dwl','.dwl2'):
                lock=working.with_suffix(suffix)
                try:lock.unlink(missing_ok=True)
                except OSError:pass
        except subprocess.TimeoutExpired:out.status='FAILED';out.error=f'CORE_TIMEOUT:{self.timeout}'
        except Exception as exc:out.status='FAILED';out.error=repr(exc)
        out.elapsed_seconds=round(time.time()-started,3);return out
    def run_plan(self,plan):
        job=self.job_root/(datetime.now().strftime('%Y%m%d_%H%M%S_')+plan['drawing_type']+'_'+plan['identity']+'_'+uuid.uuid4().hex[:8]);job.mkdir(parents=True,exist_ok=False);result=ControlledWriterResult(plan['identity'],plan['drawing_type'],'PROCESSING')
        for mode in ('A','00'):result.outputs.append(self._run_output(plan,mode,job))
        failed=[o for o in result.outputs if not o.status.startswith('LOCAL_WRITE_SUCCESS')];result.status='FAILED' if failed else ('LOCAL_PAIR_WRITTEN_WITH_SHUTDOWN_WARNING_NOT_VALIDATED' if any(o.status.endswith('SHUTDOWN_WARNING') for o in result.outputs) else 'LOCAL_PAIR_WRITTEN_NOT_VALIDATED');result.errors=[o.output_revision+':'+o.error for o in failed];return result
    def run_many(self,plans):return [self.run_plan(p) for p in plans]
    @staticmethod
    def write_report(bundle_root,results):
        report=Path(bundle_root)/'_GNI_REPORTS';report.mkdir(parents=True,exist_ok=True);path=report/'revision_controlled_writer_report.json';payload={'schema':'GNI-Controlled-Writer-0.4B.2','version':WRITER_VERSION,'generated_at':datetime.now().astimezone().isoformat(),'summary':{'plans':len(results),'local_pairs':sum(r.status=='LOCAL_PAIR_WRITTEN_NOT_VALIDATED' for r in results),'failed':sum(r.status=='FAILED' for r in results)},'results':[asdict(r) for r in results]};tmp=path.with_suffix('.json.tmp');tmp.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding='utf-8');os.replace(tmp,path);return path
