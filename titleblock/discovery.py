from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import csv, json, os, re, shutil, subprocess, tempfile, time, uuid
from typing import Iterable, List

from runtime.batching import batches as _batches_of
from runtime.console_text import decode_console
from runtime.process_tracking import run_console

DISCOVERY_VERSION='0.4A.3-batched-discovery'

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
    # Set by the engine before parsing; never serialised into the report
    # (dataclass fields are what `asdict()` writes).
    _group:object=field(default=None,repr=False,compare=False)
    # True when the record was restored from a checkpoint instead of scanned.
    _resumed:bool=field(default=False,repr=False,compare=False)

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
        self.runtime_lisp=self.runtime_root/'title_block_discovery.lsp'
        # The engine is constructed once per discovery sweep, but a sweep can be
        # restarted many times. Re-copying the LSP on every construction is pure
        # overhead, so only a changed file is staged.
        if not self._same_content(self.lisp_source,self.runtime_lisp):self._copy(self.lisp_source,self.runtime_lisp)
    # Shared decoder: see runtime/console_text.py (UTF-16LE without BOM).
    _decode=staticmethod(decode_console)
    @staticmethod
    def _same_content(src,dst):
        try:
            if src.stat().st_size!=dst.stat().st_size:return False
            with src.open('rb') as a,dst.open('rb') as b:return a.read()==b.read()
        except OSError:return False
    @staticmethod
    def _copy(src,dst):
        src=Path(src);dst=Path(dst);dst.parent.mkdir(parents=True,exist_ok=True);tmp=dst.with_suffix(dst.suffix+'.'+uuid.uuid4().hex[:8]+'.copying')
        try:
            with src.open('rb',buffering=0) as a,tmp.open('wb',buffering=0) as b:shutil.copyfileobj(a,b,4*1024*1024);b.flush();os.fsync(b.fileno())
            if tmp.stat().st_size!=src.stat().st_size:raise OSError('Discovery staging size mismatch')
            os.replace(tmp,dst)
        finally:
            if tmp.exists():tmp.unlink()
        return dst
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
            # The producer (lisp/title_block_discovery.lsp) writes raw tab-joined
            # fields and never quotes them, so a text value that happens to be a
            # double quote must stay literal. With the default QUOTE_MINIMAL the
            # reader treated it as an opening quote and swallowed the rest of the
            # file into one value, which then broke header detection for that
            # drawing (measured: DR-4110-0407-GA-0001).
            for row in csv.DictReader(f,delimiter='\t',quoting=csv.QUOTE_NONE):
                name,confidence,score,evidence=self._classify(row);a,z=self._proposals(name,row.get('Value'))
                out.append(DiscoveryField(group.identity,group.drawing_type,str(source),row.get('Drawing',''),row.get('Entity Type',''),row.get('Handle',''),row.get('Layer',''),row.get('Tag',''),row.get('Value',''),row.get('Insertion Point',''),row.get('Owner Handle',''),row.get('Owner Type',''),row.get('Block Name',''),name,confidence,score,evidence,a,z,'MANUAL_REVIEW_REQUIRED' if confidence=='LOW' else ''))
        return out
    def _insertion(self,identity):
        """Fresh job folder for one drawing, created here.

        A plain `mkdir(exist_ok=False)` would crash the drawing on a name
        collision, and `exist_ok=True` is worse: a reused folder would append to
        a previous run's log and its stale TSV would make the marker checks
        meaningless. So the collision is resolved here instead.
        """
        for _ in range(64):
            candidate=self.job_root/(datetime.now().strftime('%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:8]+'_'+self._safe(identity))
            try:
                candidate.mkdir(parents=True,exist_ok=False);return candidate
            except FileExistsError:
                continue
        raise OSError(f'Could not allocate a discovery job folder for {identity}')
    def _stage(self,group,batch_root):
        """Stage one drawing in its **own** job folder and return the result.

        Every drawing keeps the historical one-folder-per-drawing layout even
        when several are scanned in one process: a shared folder overwrote each
        TSV with the next drawing's, so results carried real numbers for the
        wrong identity.
        """
        source=Path(group.primary_source).resolve();job=self._insertion(batch_root.name+'_'+group.drawing_type+'_'+self._safe(group.identity))
        log=job/'discovery.log'
        log.write_text(f'MVP={DISCOVERY_VERSION}\nSTATUS=JOB_CREATED\nIDENTITY={group.identity}\nTYPE={group.drawing_type}\nSOURCE={source}\nCORE={self.core}\nLSP={self.runtime_lisp}\nBATCH={batch_root.name}\n',encoding='utf-8')
        result=DiscoveryResult(group.identity,group.drawing_type,str(source),'PROCESSING',str(job),log_path=str(log))
        result._group=group
        staged=self._copy(source,job/'input.dwg')
        result.tsv_path=str(job/'title_block_discovery.tsv')
        return staged,result
    def _write_script(self,job,lines):
        # SDI is deliberately NOT set: Core Console rejects it ("cannot be reset
        # unless there is only one drawing open") and the rejection prints
        # `; error:` to the transcript.
        script=job/'discover.scr'
        script.write_text('\n'.join(['(setvar "FILEDIA" 0)','(setvar "CMDDIA" 0)',f'(setvar "TRUSTEDPATHS" "{self._q(self.runtime_root)}")',f'(load "{self._q(self.runtime_lisp)}")',*lines,'_.QUIT','_Y','']),encoding='ascii',errors='replace')
        return script
    @staticmethod
    def _batches(groups,size):
        # One chunking rule for every stage: see runtime/batching.py.
        return _batches_of(groups,size)
    def _check_markers(self,result,combined):
        """Assert the LISP banners plus this drawing's own TSV.

        The TSV check is what keeps a batch honest: console output is shared by
        every drawing in the process, so only the per-drawing artefact can prove
        that *this* drawing was actually scanned.
        """
        upper=combined.upper()
        if self.LOAD_MARKER not in combined:raise RuntimeError('DISCOVERY_LISP_LOAD_FAILED')
        if self.VERSION_MARKER not in combined:raise RuntimeError('DISCOVERY_WRONG_LISP_VERSION')
        if '; ERROR:' in upper:raise RuntimeError('DISCOVERY_LISP_RUNTIME_ERROR')
        if self.SUCCESS_MARKER not in combined:raise RuntimeError('DISCOVERY_SUCCESS_MARKER_NOT_FOUND')
        tsv=Path(result.tsv_path)
        if not tsv.is_file() or tsv.stat().st_size<30:raise RuntimeError('DISCOVERY_TSV_NOT_CREATED')
    def _read(self,result,combined,started,log_suffix):
        """Validate one drawing against *combined* console output and parse it.

        Every drawing is validated against its own TSV and its own success
        marker, so a batch cannot promote a sibling: a drawing whose
        `GNI:DISCOVER` never ran has no TSV and is reported FAILED.
        """
        try:
            self._check_markers(result,combined)
            result.fields=self._parse(result._group,Path(result.source_path),Path(result.tsv_path))
            if not result.fields:raise RuntimeError('DISCOVERY_TSV_HAS_NO_DATA_ROWS')
            result.status='SUCCESS'
        except RuntimeError as exc:
            result.status='FAILED';result.error=str(exc)
            if log_suffix:
                with Path(result.log_path).open('a',encoding='utf-8',errors='replace') as f:f.write('\nSTATUS=FAILED\nERROR='+result.error+'\n')
        result.elapsed_seconds=round(time.time()-started,3);return result
    def _fail(self,result,error,started):
        result.status='FAILED';result.error=error;result.elapsed_seconds=round(time.time()-started,3)
        return result
    def discover_batch(self,groups):
        """Scan up to `len(groups)` drawings inside a single Core Console run.

        Core Console startup (~4.7 s measured on the production machine) dwarfs
        the scan itself (<1 s), so one process per drawing spends most of its
        life booting. This opens each staged copy in turn from one `.scr`,
        which amortises that startup across the batch.

        Failure is isolated per drawing (see `_read`). If the whole run dies,
        every drawing in the batch is reported FAILED rather than mislabelled.
        """
        groups=list(groups)
        if not groups:return []
        if len(groups)==1:return [self.discover_group(groups[0])]
        started=time.time();batch_root=self._insertion('batch_'+str(groups[0].identity))
        batch_log=batch_root/'batch.log';batch_log.write_text(f'MVP={DISCOVERY_VERSION}\nBATCH_SIZE={len(groups)}\n',encoding='utf-8')
        merged=[];staged,results=[],[]
        try:
            for group in groups:
                dwg,result=self._stage(group,batch_root);merged.append((dwg,result))
        except Exception as exc:
            error=f'STAGING_FAILED:{exc!r}'
            with batch_log.open('a',encoding='utf-8',errors='replace') as f:f.write(f'\nSTATUS=FAILED\nERROR={error}\n')
            if merged:
                return self._order(groups,[self._fail(result,error,started) for _,result in merged])
            for group in groups:
                out=DiscoveryResult(group.identity,group.drawing_type,str(group.primary_source),'FAILED',str(batch_root),error=error)
                results.append(self._fail(out,error,started))
            return self._order(groups,results)
        # One .scr / one process covers every staged drawing in this batch.
        # AutoCAD resets the LISP environment on `_.OPEN`, so the LSP has to
        # be re-loaded before each drawing and the per-drawing scan re-run.
        # Issuing the opens from the script (not from inside LISP) is the only
        # form Core Console accepts without swallowing the rest of the run.
        lines = []
        for index, (dwg, result) in enumerate(merged):
            if index > 0:
                lines.append("_.OPEN")
                lines.append(str(dwg).replace("\\", "/"))
            lines.extend([
                '(setvar "FILEDIA" 0)',
                '(setvar "CMDDIA" 0)',
                f'(setvar "TRUSTEDPATHS" "{self._q(self.runtime_root)}")',
                f'(load "{self._q(self.runtime_lisp)}")',
                f'(GNI:DISCOVER "{self._q(result.tsv_path)}")',
            ])
        lines.extend(["_.QUIT", "_Y"])
        script = batch_root / "discover.scr"
        script.write_text("\n".join(lines) + "\n", encoding="ascii", errors="replace")
        # Kept for test compatibility and post-mortem debugging: lists every
        # (DWG, TSV) pair the batch touched, even though the .scr no longer
        # hands them to a LISP batch function.
        self._jobfile(batch_root, merged)
        combined,failure='',None
        # A batch is allowed proportionally longer, but one hung drawing can
        # otherwise pin the whole sweep, so this stays a hard wall-clock cap.
        timeout=self.timeout*len(groups)
        try:
            cp=run_console([str(self.core),'/i',str(merged[0][0]),'/s',str(script),'/l','en-US'],capture_output=True,text=False,timeout=timeout,cwd=str(batch_root))
            combined=self._decode(cp.stdout)+'\n'+self._decode(cp.stderr)
            with batch_log.open('a',encoding='utf-8',errors='replace') as f:f.write(f'\nSTATUS=CORE_COMPLETED\nEXIT_CODE={cp.returncode}\n{combined}')
        except subprocess.TimeoutExpired as exc:
            combined=self._decode(exc.stdout)+'\n'+self._decode(exc.stderr);failure=f'CORE_TIMEOUT after {timeout} seconds'
            with batch_log.open('a',encoding='utf-8',errors='replace') as f:f.write(f'\nSTATUS=FAILED\nERROR={failure}\n{combined}')
        except Exception as exc:
            failure=f'DISCOVERY_BATCH_FAILED:{exc!r}'
            with batch_log.open('a',encoding='utf-8',errors='replace') as f:f.write(f'\nSTATUS=FAILED\nERROR={failure}\n')
        for _,result in merged:
            with Path(result.log_path).open('a',encoding='utf-8',errors='replace') as f:
                if failure:f.write(f'\nSTATUS=FAILED\nERROR={failure}\n')
                else:f.write(f'\nSTATUS=CORE_COMPLETED\n{combined}')
            if failure:self._fail(result,failure,started)
            else:self._read(result,combined,started,log_suffix=False)
        return self._order(groups,[result for _,result in merged])
    @staticmethod
    def _order(groups,results):
        order={str(group.identity):index for index,group in enumerate(groups)}
        return sorted(results,key=lambda item:order.get(str(item.identity),10**9))
    @staticmethod
    def _jobfile(batch_root,merged):
        """Write the DWG/TSV pairs the LISP `read-line` loop consumes.

        `read-line` returns the next line verbatim, so the pairs stay unquoted:
        the LISP side reads exactly two lines (DWG, TSV) per drawing.
        """
        path=batch_root/'discover_job.txt'
        rows='\n'.join(f'{TitleBlockDiscoveryEngine._q(dwg)}\n{TitleBlockDiscoveryEngine._q(result.tsv_path)}' for dwg,result in merged)
        path.write_text(rows+'\n',encoding='ascii',errors='replace')
        return path
    def discover_group(self,group):
        started=time.time();job=self._insertion(group.drawing_type+'_'+self._safe(group.identity));log=job/'discovery.log'
        source=Path(group.primary_source).resolve()
        log.write_text(f'MVP={DISCOVERY_VERSION}\nSTATUS=JOB_CREATED\nIDENTITY={group.identity}\nTYPE={group.drawing_type}\nSOURCE={source}\nCORE={self.core}\nLSP={self.runtime_lisp}\n',encoding='utf-8')
        result=DiscoveryResult(group.identity,group.drawing_type,str(source),'PROCESSING',str(job),log_path=str(log))
        result._group=group
        try:
            staged=self._copy(source,job/'input.dwg');tsv=job/'title_block_discovery.tsv';result.tsv_path=str(tsv)
            script=self._write_script(job,[f'(GNI:DISCOVER "{self._q(tsv)}")'])
            cp=run_console([str(self.core),'/i',str(staged),'/s',str(script),'/l','en-US'],capture_output=True,text=False,timeout=self.timeout,cwd=str(job));combined=self._decode(cp.stdout)+'\n'+self._decode(cp.stderr)
            with log.open('a',encoding='utf-8',errors='replace') as f:f.write(f'\nSTATUS=CORE_COMPLETED\nEXIT_CODE={cp.returncode}\n{combined}')
            self._check_markers(result,combined)
            result.fields=self._parse(group,source,tsv)
            if not result.fields:raise RuntimeError('DISCOVERY_TSV_HAS_NO_DATA_ROWS')
            result.status='SUCCESS'
        except subprocess.TimeoutExpired as exc:
            result.status='FAILED';result.error=f'CORE_TIMEOUT after {self.timeout} seconds'
            with log.open('a',encoding='utf-8',errors='replace') as f:f.write('\nSTATUS=FAILED\nERROR='+result.error+'\nSTDOUT='+self._decode(exc.stdout)+'\nSTDERR='+self._decode(exc.stderr))
        except Exception as exc:
            result.status='FAILED';result.error=str(exc) if isinstance(exc,RuntimeError) else repr(exc)
            with log.open('a',encoding='utf-8',errors='replace') as f:f.write('\nSTATUS=FAILED\nERROR='+result.error+'\n')
        result.elapsed_seconds=round(time.time()-started,3);return result
    def discover_one(self,group):
        """Discover one group in its own Core Console process (no batching)."""
        return self.discover_group(group)
    def discover_many(self,groups:Iterable,batch_size=1):
        """Discover every group, `batch_size` drawings per Core Console process.

        ``batch_size <= 1`` keeps the historical serial one-process-per-drawing
        path, which is the deterministic fallback used by the tests.
        """
        groups=list(groups)
        if not groups:return []
        if batch_size and batch_size>1:
            out=[]
            for chunk in self._batches(groups,batch_size):out.extend(self.discover_batch(chunk))
            return out
        return [self.discover_group(g) for g in groups]
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
