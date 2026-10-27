from __future__ import annotations
from pathlib import Path
import os, shutil, subprocess, tempfile, time, uuid
from models import JobResult

class BackgroundEngine:
    """Core Console-only engine using pre-copied target DWG + QSAVE.

    This avoids SAVEAS prompts/hangs. Each mode opens its own already-named
    local target copy, updates it, runs QSAVE, validates markers, then publishes
    it atomically to the destination.
    """
    LOAD_MARKER='GNI_BACKGROUND_ENGINE_0_3A_LOADED'
    VERSION_MARKER='GNI_BACKGROUND_ENGINE_0_3A_7_QSAVE'
    SUCCESS_MARKER='GNI_MIGRATION_OK:'
    REQUIRED_RASTERS=('SEAtrium_Ge_.PNG','Tennet LogO New.PNG')
    def __init__(self,exe,lisp,settings,timeout=180):
        self.exe=Path(exe).resolve(); self.lisp_source=Path(lisp).resolve(); self.s=settings; self.timeout=int(timeout)
        if not self.exe.is_file(): raise FileNotFoundError(f'accoreconsole.exe not found: {self.exe}')
        if not self.lisp_source.is_file(): raise FileNotFoundError(f'LSP not found: {self.lisp_source}')
        temp=Path(os.environ.get('TEMP') or tempfile.gettempdir()).resolve()
        self.root=temp/'GNI'/'migration_jobs'; self.runtime=temp/'GNI'/'trusted_runtime'
        self.root.mkdir(parents=True,exist_ok=True); self.runtime.mkdir(parents=True,exist_ok=True)
        self.runtime_lisp=self.runtime/'background_engine.lsp'; self.copy(self.lisp_source,self.runtime_lisp)
    @staticmethod
    def q(p): return str(Path(p).resolve()).replace('\\','/').replace('"','\\"')
    @staticmethod
    def safe(v): return ''.join(c if c.isalnum() or c in '._-' else '_' for c in v).strip('._')[:80]
    def job(self,g):
        p=self.root/(time.strftime('%Y%m%d_%H%M%S')+'_'+g.drawing_type+'_'+self.safe(g.identity)+'_'+uuid.uuid4().hex[:8]); p.mkdir(); return p
    @staticmethod
    def copy(src,dst):
        src=Path(src); dst=Path(dst); dst.parent.mkdir(parents=True,exist_ok=True); tmp=dst.with_suffix(dst.suffix+'.copying')
        if tmp.exists(): tmp.unlink()
        with src.open('rb',buffering=0) as a,tmp.open('wb',buffering=0) as b:
            shutil.copyfileobj(a,b,4*1024*1024); b.flush(); os.fsync(b.fileno())
        if tmp.stat().st_size!=src.stat().st_size: raise OSError('COPY_SIZE_MISMATCH')
        os.replace(tmp,dst)
        if not dst.is_file() or dst.stat().st_size!=src.stat().st_size: raise OSError('COPY_VERIFY_FAILED')
        return dst.resolve()
    def find_required_rasters(self):
        asset_root=(Path(__file__).resolve().parent.parent/'resources'/'reference_images').resolve()
        rasters=[]
        missing=[]
        for name in self.REQUIRED_RASTERS:
            asset=asset_root/name
            if asset.is_file() and asset.stat().st_size>0:
                rasters.append(asset)
            else:
                missing.append(name)
        if missing:
            raise FileNotFoundError(
                'EMBEDDED_RASTER_NOT_FOUND: '+', '.join(missing)+
                '; expected folder='+str(asset_root)
            )
        return rasters

    def copy_rasters(self,rasters,folder):
        folder=Path(folder)
        folder.mkdir(parents=True,exist_ok=True)
        for raster in rasters:
            self.copy(raster,folder/raster.name)

    def maps(self):
        vals=[(self.s.project_from,self.s.project_to),(self.s.drawing_from,self.s.drawing_to),(self.s.long_from,self.s.long_to),(self.s.short_from,self.s.short_to)]
        esc=lambda x:str(x).replace('\\','\\\\').replace('"','\\"')
        return "'("+' '.join(f'(\"{esc(a)}\" \"{esc(b)}\")' for a,b in vals if str(a))+')'
    def make_script(self,job,mode):
        sc=job/f'run_{mode}.scr'; trusted=self.q(self.runtime)
        sc.write_text('\n'.join(['(setvar "FILEDIA" 0)','(setvar "CMDDIA" 0)','(setvar "PROXYNOTICE" 0)',f'(setvar "TRUSTEDPATHS" "{trusted}")',f'(load "{self.q(self.runtime_lisp)}")',f'(setq GNI:MAPS {self.maps()})',f'(GNI:PROCESS "{mode}")','_.QUIT','_Y','']),encoding='ascii',errors='replace')
        return sc
    def run_mode(self,job,source_copy,mode):
        # Open the final local filename directly. LISP only QSAVE, never SAVEAS.
        local=job/f'{source_copy.stem}_{mode}.dwg'; self.copy(source_copy,local)
        marker=Path(str(local)+'.gni.txt'); sc=self.make_script(job,mode); log=job/f'{mode}.log'
        cmd=[str(self.exe),'/i',str(local),'/s',str(sc),'/l','en-US']
        started=time.time()
        def decode_console(value):
            if value is None:
                return ''
            if isinstance(value, str):
                return value
            # AutoCAD 2018 Core Console commonly emits UTF-16LE without BOM.
            if value.startswith(b'\xff\xfe') or (len(value) > 4 and value[1:2] == b'\x00'):
                return value.decode('utf-16', errors='replace')
            for encoding in ('utf-8', 'cp1252'):
                try:
                    return value.decode(encoding)
                except UnicodeDecodeError:
                    pass
            return value.decode('utf-8', errors='replace')
        try:
            cp=subprocess.run(cmd,capture_output=True,text=False,timeout=self.timeout,cwd=str(job))
        except subprocess.TimeoutExpired as e:
            timeout_text='TIMEOUT after %ss\nstdout=%s\nstderr=%s' % (self.timeout, decode_console(e.stdout), decode_console(e.stderr))
            log.write_text(timeout_text,encoding='utf-8',errors='replace')
            raise RuntimeError(f'CORE_TIMEOUT; log={log}')
        stdout=decode_console(cp.stdout)
        stderr=decode_console(cp.stderr)
        combined=stdout+'\n'+stderr
        log.write_text(combined,encoding='utf-8',errors='replace')
        upper=combined.upper()
        if self.LOAD_MARKER not in combined: raise RuntimeError(f'LISP_LOAD_MARKER_NOT_FOUND; exit={cp.returncode}; log={log}')
        if self.VERSION_MARKER not in combined: raise RuntimeError(f'WRONG_LISP_VERSION; log={log}')
        if 'GNI_STAGE_ERROR:QSAVE' in upper: raise RuntimeError(f'QSAVE_FAILED; log={log}')
        if self.SUCCESS_MARKER not in combined: raise RuntimeError(f'MIGRATION_MARKER_NOT_FOUND; exit={cp.returncode}; log={log}')
        if not local.is_file() or local.stat().st_size<1000: raise RuntimeError(f'MIGRATION_OUTPUT_INVALID; log={log}')
        if not marker.is_file(): raise RuntimeError(f'MIGRATION_RESULT_MARKER_NOT_CREATED; log={log}')
        return local,log,round(time.time()-started,2)
    def run_one(self,g,destination_root,pilot=True):
        start=time.time(); job=self.job(g); r=JobResult(g.identity,g.drawing_type,g.primary_source,'PILOT' if pilot else 'BACKGROUND',job_folder=str(job)); staged=job/'source_input.dwg'
        try:
            rasters=self.find_required_rasters()
            self.copy(g.primary_source,staged);
            self.copy_rasters(rasters,job); folder={'SP':'1. SP','AS':'2. AS','GA':'3. GA'}[g.drawing_type]
            outA,logA,secA=self.run_mode(job,staged,'A'); r.attempt_count=1
            out00,log00,sec00=self.run_mode(job,staged,'00'); r.attempt_count=2
            finalA=Path(destination_root)/'A'/folder/'CAD'/f'{g.identity}_A.dwg'; final00=Path(destination_root)/'00'/folder/'CAD'/f'{g.identity}_00.dwg'
            self.copy(outA,finalA); self.copy(out00,final00); self.copy_rasters(rasters,finalA.parent); self.copy_rasters(rasters,final00.parent); r.output_a_dwg=str(finalA); r.output_00_dwg=str(final00); r.status='SUCCESS'; r.log_path=str(job); r.message=f'QSAVE background success; A={logA}; 00={log00}'
        except Exception as e:
            text=str(e); r.status='FAILED'; r.log_path=str(job); stages=('EMBEDDED_RASTER_NOT_FOUND','CORE_TIMEOUT','LISP_LOAD_MARKER_NOT_FOUND','WRONG_LISP_VERSION','QSAVE_FAILED','MIGRATION_MARKER_NOT_FOUND','MIGRATION_OUTPUT_INVALID','MIGRATION_RESULT_MARKER_NOT_CREATED')
            r.failure_stage=next((x for x in stages if x in text),'BACKGROUND_MIGRATION_FAILED'); r.message=repr(e)
        r.elapsed_seconds=round(time.time()-start,2); return r
