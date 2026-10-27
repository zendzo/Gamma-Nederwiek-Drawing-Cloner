from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import threading
import time
import traceback

from PySide6.QtCore import QObject, QThread, QTimer, Signal


class PipelineWorker(QThread):
    progress = Signal(int, int, str, str, str)
    completed = Signal(str, object, str, bool, int)
    failed = Signal(str, str)

    def __init__(self, operation, payload):
        super().__init__()
        self.operation = operation
        self.payload = payload
        self.cancel_requested = False
        self._stop = threading.Event()

    def cancel(self):
        self.cancel_requested = True
        self._stop.set()

    @property
    def stop_event(self):
        return self._stop

    @staticmethod
    def _sha(path):
        h = hashlib.sha256()
        with Path(path).open('rb') as f:
            for block in iter(lambda: f.read(4 * 1024 * 1024), b''): h.update(block)
        return h.hexdigest()

    def run(self):
        try:
            method = getattr(self, '_run_' + self.operation)
            results, report, cancelled, skipped = method()
            self.completed.emit(self.operation, results, str(report or ''), cancelled, skipped)
        except Exception:
            self.failed.emit(self.operation, traceback.format_exc())

    def _run_scan(self):
        from scanner.universal import Scanner
        src = self.payload['source']
        self.progress.emit(0, 1, '', '', 'Scanning source folders')
        scanner = Scanner(src)
        files = scanner.scan()
        self.progress.emit(0, 1, '', '', 'Grouping drawing identities')
        groups = scanner.groups(files)
        supporting = scanner.supporting(files)
        return {'files': files, 'groups': groups, 'supporting': supporting}, '', False, 0

    def _run_destination(self):
        from destination.builder import DestinationBuilder
        self.progress.emit(0, 1, '', '', 'Building destination structure')
        builder = DestinationBuilder(self.payload['destination'], self.payload['bundle'], self.payload['app'])
        result = builder.build(self.payload['source'], copy_assets_to_all_cad=True)
        return result, result.manifest_path, False, 0

    def _run_clone(self):
        from destination.builder import DestinationBuilder
        from supporting.clone_engine import SupportingCloneEngine
        builder = DestinationBuilder(self.payload['destination'], self.payload['bundle'], self.payload['app'])
        builder.build(self.payload['source'], copy_assets_to_all_cad=True)
        engine = SupportingCloneEngine(builder.bundle_root, self.payload['policy'])
        paths = self.payload['paths']; results=[]; total=len(paths)
        for i,path in enumerate(paths,1):
            if self.cancel_requested: break
            self.progress.emit(i,total,Path(path).name,'','Copying supporting file')
            results.extend(engine.clone_many([path]))
        report = engine.write_report(results)
        return results, report, self.cancel_requested and len(results)<total, 0

    @property
    def _writer_checkpoint(self):
        return Path(self.payload['bundle_root'])/'_GNI_REPORTS'/'stable_writer_checkpoint.json'

    def _load_writer_checkpoint(self):
        try:
            data=json.loads(self._writer_checkpoint.read_text(encoding='utf-8'))
            return {x['identity']:x for x in data.get('results',[]) if x.get('status')=='LOCAL_PAIR_WRITTEN_NOT_VALIDATED'}
        except Exception:return {}

    @staticmethod
    def _plan_sha(plan):
        """Hash the executable action set so plan edits invalidate the checkpoint.

        Source and output hashes alone are not enough: editing the plan (adding a
        cleaned handle, changing a target value) leaves both untouched, and the
        checkpoint would then hand back a DWG that no longer matches the plan.
        """
        actions = [
            {key: action.get(key, "") for key in (
                "output_revision", "handle", "action", "old_value", "new_value",
                "target_field", "reason",
            )}
            for action in plan.get("actions", [])
        ]
        actions.sort(key=lambda item: (item["output_revision"], item["handle"], item["target_field"]))
        payload = json.dumps(actions, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _writer_reusable(self, record, plan, source_sha=None, plan_sha=None):
        if not record:return False
        # Checkpoints written before 0.4C.7 have no plan hash and are never reused:
        # recomputing one pair is cheap compared to publishing a stale drawing.
        if not plan_sha or record.get('plan_sha256')!=plan_sha:return False
        results={x.get('output_revision'):x for x in record.get('outputs',[])}
        if set(results)!={'A','00'}:return False
        source=Path(plan['primary_geometry'])
        if not source.is_file() or record.get('source_sha256')!=(source_sha or self._sha(source)):return False
        for x in results.values():
            p=Path(x.get('working_dwg',''))
            if not p.is_file() or self._sha(p)!=x.get('sha256'):return False
        return True

    def _write_writer_checkpoint(self, results, total, cancelled, skipped, plans, source_hash=None, plan_hash=None):
        source_hash=source_hash or {p['identity']:self._sha(p['primary_geometry']) for p in plans if Path(p['primary_geometry']).is_file()}
        plan_hash=plan_hash or {}
        rows=[]
        for result in results:
            row=asdict(result);row['source_sha256']=source_hash.get(result.identity,'');row['plan_sha256']=plan_hash.get(result.identity,'');rows.append(row)
        payload={'schema':'GNI-Stable-Writer-Checkpoint-0.4C.7','updated_at':datetime.now().astimezone().isoformat(),'total_pairs':total,'completed_pairs':len(results),'skipped':skipped,'cancelled':cancelled,'results':rows}
        path=self._writer_checkpoint;path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.json.tmp');tmp.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding='utf-8');os.replace(tmp,path)

    def _run_writer(self):
        from revision.stable_external_writer import StableExternalActionWriter, StablePairResult, StableOutput
        plans=self.payload['plans'];total=len(plans);previous=self._load_writer_checkpoint();results=[];skipped=0
        writer=StableExternalActionWriter(self.payload['core'],self.payload['lisp'],self.payload['app'])
        workers=max(1,int(self.payload.get('workers') or writer.max_workers))
        source_hash={p['identity']:self._sha(p['primary_geometry']) for p in plans if Path(p['primary_geometry']).is_file()}
        plan_hash={p['identity']:self._plan_sha(p) for p in plans}
        todo=[]
        for plan in plans:
            old=previous.get(plan['identity'])
            if self._writer_reusable(old,plan,source_hash.get(plan['identity']),plan_hash.get(plan['identity'])):
                results.append(StablePairResult(old['identity'],old['drawing_type'],old['status'],[StableOutput(**x) for x in old['outputs']],list(old.get('errors',[]))))
                skipped+=1
            else:todo.append(plan)
        if skipped:self.progress.emit(skipped,total,'','PAIR',f'Reused {skipped} valid checkpoint(s)')
        last=[0.0];fatal=threading.Event()
        def save(force=False):
            now=time.monotonic()
            if force or now-last[0]>=5:
                self._write_writer_checkpoint(results,total,False,skipped,plans,source_hash,plan_hash);last[0]=now
        def on_result(result):
            # Runs on pool threads: a raise here would kill a worker mid-pool and
            # leave run_many's wait() spin forever.
            if fatal.is_set():return
            try:
                results.append(result);self.progress.emit(len(results),total,result.identity,'PAIR',result.status);save()
            except Exception:
                fatal.set()
                raise
        try:
            writer.run_many(todo,workers=workers,on_result=on_result,should_cancel=lambda:self.cancel_requested)
        finally:
            order={p['identity']:index for index,p in enumerate(plans)}
            results.sort(key=lambda item:order.get(item.identity,10**9))
            cancelled=self.cancel_requested and len(results)<total
            report=writer.write_report(self.payload['bundle_root'],results)
            self._write_writer_checkpoint(results,total,cancelled,skipped,plans,source_hash,plan_hash)
        if fatal.is_set():raise RuntimeError('WRITER_CHECKPOINT_WRITE_FAILED: see the log for the underlying error')
        return results,report,cancelled,skipped

    def _run_validator(self):
        from revision.reopen_validator import StableReopenValidator, ValidationPair, ValidationOutput
        pairs=self.payload['pairs'];total=len(pairs);results=[];skipped=0
        validator=StableReopenValidator(self.payload['core'],self.payload['lisp'],self.payload['app'])
        workers=max(1,int(self.payload.get('workers') or 4))
        checkpoint=Path(self.payload['bundle_root'])/'_GNI_REPORTS'/'reopen_validation_checkpoint.json'
        try:
            previous={x['identity']:x for x in json.loads(checkpoint.read_text(encoding='utf-8')).get('results',[]) if x.get('status')=='VALIDATED'}
        except Exception:previous={}
        todo=[]
        for pair in pairs:
            identity=pair['identity'];record=previous.get(identity)
            current={x['output_revision']:x.get('sha256','') for x in pair['outputs']}
            recorded={x['output_revision']:x.get('sha256_before','') for x in record.get('outputs',[])} if record else {}
            if record and current==recorded and set(current)=={'A','00'}:
                results.append(ValidationPair(record['identity'],record['drawing_type'],record['status'],[ValidationOutput(**x) for x in record['outputs']],list(record.get('errors',[]))));skipped+=1
            else:todo.append(pair)
        if skipped:self.progress.emit(skipped,total,'','PAIR',f'Reused {skipped} validated checkpoint(s)')
        last=[0.0];fatal=threading.Event()
        def save(force=False):
            now=time.monotonic()
            if force or now-last[0]>=5:
                payload={'schema':'GNI-Reopen-Checkpoint-0.4C.7','total_pairs':total,'completed_pairs':len(results),'skipped':skipped,'cancelled':False,'results':[asdict(x) for x in results]};tmp=checkpoint.with_suffix('.tmp');tmp.write_text(json.dumps(payload,indent=2),encoding='utf-8');os.replace(tmp,checkpoint);last[0]=now
        def on_result(result):
            if fatal.is_set():return
            try:
                results.append(result);self.progress.emit(len(results),total,result.identity,'PAIR',result.status);save()
            except Exception:
                fatal.set()
                raise
        try:
            validator.validate_many(todo,workers=workers,on_result=on_result,should_cancel=lambda:self.cancel_requested)
        finally:
            order={pair['identity']:index for index,pair in enumerate(pairs)};results.sort(key=lambda item:order.get(item.identity,10**9))
            report=validator.write_report(self.payload['bundle_root'],results)
        if fatal.is_set():raise RuntimeError('VALIDATOR_CHECKPOINT_WRITE_FAILED: see the log for the underlying error')
        return results,report,self.cancel_requested and len(results)<total,skipped

    def _run_dryrun(self):
        from revision.atomic_publish_dry_run import AtomicPublisherDryRun
        pairs=self.payload['pairs'];publisher=AtomicPublisherDryRun(self.payload['bundle_root'],self.payload['app']);results=[];total=len(pairs)
        for i,pair in enumerate(pairs,1):
            if self.cancel_requested:break
            self.progress.emit(i,total,pair['identity'],'PAIR','Checking hashes, locks, paths and assets');results.append(publisher.inspect_pair(pair))
        report=publisher.write_report(self.payload['bundle_root'],results);return results,report,self.cancel_requested and len(results)<total,0

    def _run_publish(self):
        from revision.atomic_publisher import AtomicPublisher
        packages=self.payload['packages'];publisher=AtomicPublisher(self.payload['bundle_root'],self.payload['app']);results=[];total=len(packages)
        for i,package in enumerate(packages,1):
            if self.cancel_requested:break
            identity=package['dry_run']['identity'];self.progress.emit(i,total,identity,'TRANSACTION','Staging, backup, commit and verification');results.append(publisher.publish_package(package))
        report=publisher.write_report(self.payload['bundle_root'],results);return results,report,self.cancel_requested and len(results)<total,0


class BackgroundOperationController(QObject):
    progress = Signal(int,int,str,str,str)
    finished = Signal(str,object,str,bool,int)
    failed = Signal(str,str)
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent);self.worker=None;self.operation='';self.started=0.0
        self.timer=QTimer(self);self.timer.timeout.connect(self._heartbeat)
        self.last=(0,0,'','','Preparing')

    def start(self, operation, payload):
        if self.worker and self.worker.isRunning():raise RuntimeError('Another background operation is already running: '+self.operation)
        self.operation=operation;self.started=time.monotonic();self.last=(0,0,'','','Preparing');self.worker=PipelineWorker(operation,payload);self.worker.progress.connect(self._progress);self.worker.completed.connect(self._completed);self.worker.failed.connect(self._failed);self.timer.start(1000);self.worker.start()

    def cancel(self):
        if self.worker and self.worker.isRunning():self.worker.cancel();self.status.emit('Cancellation requested. Current safe unit will finish first.')

    def _progress(self,*args):self.last=args;self.progress.emit(*args)
    def _heartbeat(self):
        a,b,c,d,e=self.last;elapsed=int(time.monotonic()-self.started);dots='.'*((elapsed%3)+1);self.status.emit(f'{self.operation}: {a} of {b} | {c} | {d} | {e}{dots} | elapsed {elapsed//60:02d}:{elapsed%60:02d}')
    def _completed(self,*args):self.timer.stop();self.finished.emit(*args);self.worker=None
    def _failed(self,*args):self.timer.stop();self.failed.emit(*args);self.worker=None

    def shutdown(self, timeout_ms=5000):
        """Stop the worker thread so Qt does not destroy a running QThread."""
        self.timer.stop()
        worker = self.worker
        if not worker or not worker.isRunning():
            self.worker = None
            return True
        worker.cancel()
        if worker.wait(timeout_ms):
            self.worker = None
            return True
        return False
