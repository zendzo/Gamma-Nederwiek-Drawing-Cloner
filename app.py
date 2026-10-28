import sys
from pathlib import Path
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import *
from config.settings import Settings
from destination.builder import DestinationBuilder
from revision.stable_external_writer import StableExternalActionWriter,StableWriterError
from revision.reopen_validator import StableReopenValidator,ReopenValidationError
from revision.atomic_publish_dry_run import AtomicPublisherDryRun,AtomicPublishDryRunError
from revision.atomic_publisher import AtomicPublisher,AtomicPublishError
from revision.pdf_worker import PdfPublishWorker
from runtime.production_workers import DiscoveryWorker, ProductionPlanWorker
from runtime.background_controller import BackgroundOperationController
BASE=Path(__file__).resolve().parent;CFG=BASE/'config'/'settings.json'
PIPELINE=(('scan','Scan source'),('destination','Build destination'),('clone','Clone supporting files'),('discovery','Discover title blocks'),('plan','Build revision plan'),('writer','Stable external writer'),('validator','Reopen validator'),('dryrun','Atomic publish dry run'),('publish','Atomic publisher'),('pdf','Publish PDFs'))
STAGE_REPORTS={'destination':'destination_manifest.json','clone':'supporting_file_clone_report.json','discovery':'title_block_discovery_report.json','plan':'revision_pilot_write_plan.json','writer':'stable_external_writer_report.json','validator':'reopen_validation_report.json','dryrun':'atomic_publish_dry_run_report.json','publish':'atomic_publish_report.json','pdf':'pdf_publish_report.json'}
AUTO_STAGES=('scan','destination','clone','discovery','plan','writer','validator','dryrun')
class Main(QMainWindow):
 def __init__(self):
  super().__init__();self.setWindowIcon(QIcon(str(BASE/'resources'/'app_icon.ico')));self.bg=BackgroundOperationController(self);self.bg.progress.connect(self._bg_progress);self.bg.status.connect(self.status_update_safe);self.bg.finished.connect(self._bg_finished);self.bg.failed.connect(self._bg_failed);self.resize(1500,850);self.setWindowTitle('Gamma to Nederwiek - Revision Migration (0.4C.15 - new job reset)');self.s=Settings.load(CFG);self._finished_root=None;self.files=[];self.groups=[];self.support=[];self._chain=False;self._skipped=set();self._running_stage=None;self._busy=False
  tabs=QTabWidget();self.setCentralWidget(tabs);w=QWidget();tabs.addTab(w,'Setup');l=QVBoxLayout(w);f=QFormLayout();self.src=QLineEdit(self.s.source_folder);self.dst=QLineEdit(self.s.destination_root);self.bundle=QLineEdit(self.s.destination_bundle_name);self.exe=QLineEdit(self.s.core_console)
  for label,e,folder in [('Source',self.src,True),('Destination root',self.dst,True),('Bundle name',self.bundle,None),('accoreconsole.exe',self.exe,False)]:f.addRow(label,self.browse(e,folder) if folder is not None else e)
  l.addLayout(f);self.maped=[]
  m=QGroupBox('Controlled replacement mapping');mf=QFormLayout(m)
  for label,attr,default in [('Project from','project_from','IJMUIDEN VER GAMMA'),('Project to','project_to','NEDERWIEK BETA'),('Drawing from','drawing_from','2013J310015'),('Drawing to','drawing_to','I301-J30019'),('Long ref from','long_from','OEPA230146'),('Long ref to','long_to','OEPA240101'),('Short ref from','short_from','230146'),('Short ref to','short_to','240101'),('Contractor doc-id from','contractor_doc_from','2GWG'),('Contractor doc-id to','contractor_doc_to','2GWN')]:e=QLineEdit(getattr(self.s,attr,default));self.maped.append((attr,e));mf.addRow(label,e)
  l.addWidget(m);pg=QGroupBox('Migration pipeline');pv=QVBoxLayout(pg);self.stage_tbl=self.table(['Step','Stage','Status','Report']);self.stage_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers);self.stage_tbl.setSelectionMode(QAbstractItemView.SingleSelection);self.stage_tbl.setSelectionBehavior(QAbstractItemView.SelectRows);self.stage_tbl.setMaximumHeight(230);self.stage_tbl.cellDoubleClicked.connect(self.open_stage_report);pv.addWidget(self.stage_tbl)
  self.primary=QPushButton('Run: Scan source');self.primary.setMinimumHeight(38);self.primary.clicked.connect(self.run_next_stage);self.primary.setDefault(True);pv.addWidget(self.primary)
  # Only reachable once the PDF stage is complete and nothing is running; the
  # pipeline derives every row from report files, so this clears session state
  # instead of deleting evidence on disk.
  self.newjobb=QPushButton('New Job (clear inputs)');self.newjobb.setEnabled(False);self.newjobb.setToolTip('Available after the PDF stage is complete. Clears every result, table and input field so the next job starts from Scan.');self.newjobb.clicked.connect(self.new_job);pv.addWidget(self.newjobb)
  prow=QHBoxLayout();self.cancelb=QPushButton('Cancel Safely');self.cancelb.setEnabled(False);self.cancelb.clicked.connect(self.cancel_unified_operation);self.workers=QSpinBox();self.workers.setRange(1,16);self.workers.setValue(max(1,min(16,int(getattr(self.s,'workers',4)))));self.workers.setToolTip('Concurrent Core Console processes for the writer, validator and PDF stages. 4-6 is a good starting point.');prow.addWidget(self.cancelb);prow.addStretch();prow.addWidget(QLabel('Parallel workers'));prow.addWidget(self.workers);pv.addLayout(prow)
  self.prog=QProgressBar();pv.addWidget(self.prog);self.status=QLabel('Ready. The button runs every remaining step up to the publish gate.');self.status.setWordWrap(True);pv.addWidget(self.status)
  self.adv=QGroupBox('Advanced - run or re-run one stage');self.adv.setCheckable(True);self.adv.setChecked(False);ag=QGridLayout(self.adv);self.stage_buttons={}
  for index,(key,label) in enumerate(PIPELINE):button=QPushButton(label);button.clicked.connect(lambda checked=False,stage=key:self.run_stage(stage));button.setEnabled(False);ag.addWidget(button,index//3,index%3);self.stage_buttons[key]=button
  self.adv.toggled.connect(self._sync_advanced_buttons);pv.addWidget(self.adv);l.addWidget(pg);l.addStretch();self._sync_advanced_buttons(False)
  w2=QWidget();tabs.addTab(w2,'Drawing Candidates');ll=QVBoxLayout(w2);self.tbl=self.table(['Type','Identity','Primary source','A donor','Candidates','Warning']);ll.addWidget(self.tbl)
  ws=QWidget();tabs.addTab(ws,'Supporting File Clone');sl=QVBoxLayout(ws);self.stbl=self.table(['Select','Role','Extension','Source Path','Destination','Status','Action','Backup','Error']);sl.addWidget(self.stbl);sbar=QHBoxLayout();self.cloneb=QPushButton('Clone Selected Supporting Files');self.cloneb.clicked.connect(self.clone_supporting);self.policy=QComboBox();self.policy.addItems(['BACKUP_OVERWRITE','SKIP_DIFFERENT','KEEP_BOTH']);self.retryb=QPushButton('Retry Failed Copies');self.retryb.clicked.connect(self.retry_supporting);self.openb=QPushButton('Open Supporting Destination');self.openb.clicked.connect(self.open_supporting_destination);sbar.addWidget(self.cloneb);sbar.addWidget(QLabel('Collision:'));sbar.addWidget(self.policy);sbar.addWidget(self.retryb);sbar.addWidget(self.openb);sbar.addStretch();sl.addLayout(sbar);self.support_results=[]
  wd=QWidget();tabs.addTab(wd,'Title Block Discovery');dl=QVBoxLayout(wd);self.dtbl=self.table(['Identity','Type','Source','Entity','Handle','Layer','Block','Tag','Value','Suggested Field','Confidence','Score','Evidence','Proposed A','Proposed 00','Warning']);dl.addWidget(self.dtbl);self.discovery_results=[]
  wp=QWidget();tabs.addTab(wp,'Revision Plan');pl=QVBoxLayout(wp);self.ptbl=self.table(['Identity','Type','Status','Primary Geometry','A Source Policy','Migration Date','A Date','00 Date','Actions','Warnings','Errors']);pl.addWidget(self.ptbl);self.revision_plans=[]
  w3=QWidget();tabs.addTab(w3,'Reports');lll=QVBoxLayout(w3);self.rtbl=self.table(['Report','Modified','Size (KB)']);self.rtbl.cellDoubleClicked.connect(self.open_report);lll.addWidget(self.rtbl);self._report_paths=[]
  rbar=QHBoxLayout();self.refreshb=QPushButton('Refresh Reports');self.refreshb.clicked.connect(self.refresh_reports);self.folderb=QPushButton('Open Reports Folder');self.folderb.clicked.connect(self.open_reports_folder);rbar.addWidget(self.refreshb);rbar.addWidget(self.folderb);rbar.addStretch();lll.addLayout(rbar);note=QLabel('Reports are the pipeline handoff files in <destination>/<bundle>/_GNI_REPORTS. Double-click a row to open it. Revision-cloud deletion and DXF content migration are not implemented yet.');note.setWordWrap(True);lll.addWidget(note);tabs.currentChanged.connect(self._tab_changed);self.refresh_pipeline()
 def table(self,h):t=QTableWidget(0,len(h));t.setHorizontalHeaderLabels(h);t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents);t.horizontalHeader().setStretchLastSection(True);return t
 def _sync_advanced_buttons(self,on):
  for button in self.stage_buttons.values():button.setEnabled(bool(on) and not self._busy)
 def stage_report_path(self,key):
  name=STAGE_REPORTS.get(key);return self.reports_root()/name if name else None
 def _report_state(self,key,root=None):
  """Return 'complete', 'incomplete' or 'missing' for a stage report.

  `root` overrides the reports folder, so a reset can inspect the bundle the
  finished job actually used instead of whatever the fields now point at."""
  name=STAGE_REPORTS.get(key)
  path=(Path(root)/name) if root and name else self.stage_report_path(key)
  if not path or not path.is_file():return 'missing'
  import json
  try:payload=json.loads(path.read_text(encoding='utf-8'))
  except (OSError,ValueError):return 'missing'
  if not isinstance(payload,dict):return 'missing'
  # A cancelled sweep still writes its report; without this flag the pipeline
  # advanced on partial data. Applies to every stage now: the PDF writer marks
  # its report partial on cancel too. Reports written before 0.4C.7 have no flag
  # and keep the old behaviour so upgrades do not re-run everything.
  if payload.get('complete') is False:return 'incomplete'
  return 'complete'
 def _stage_complete(self,key,root=None):
  if key=='scan':return bool(self.files)
  if key in self._skipped:return True
  state=self._report_state(key,root)
  if state!='complete':return False
  if key=='discovery':return ((Path(root) if root else self.reports_root())/'title_block_discovery_fields.csv').is_file()
  return True
 def next_stage(self):
  if not self.files:return ('scan','Scan source')
  for key,label in PIPELINE:
   if key=='scan' or key in self._skipped:continue
   if not self._stage_complete(key):return (key,label)
  return None
 def refresh_pipeline(self):
  nxt=self.next_stage();nxt_key=nxt[0] if nxt else ''
  self.stage_tbl.setRowCount(len(PIPELINE))
  for row,(key,label) in enumerate(PIPELINE):
   path=self.stage_report_path(key)
   if key in self._skipped:state='SKIPPED'
   elif key==self._running_stage:state='RUNNING'
   elif self._stage_complete(key):state='DONE'
   elif self._report_state(key)=='incomplete':state='INCOMPLETE (rerun)'
   else:state='PENDING'
   for column,value in enumerate([str(row+1),label,state,path.name if path else '(this session)']):self.stage_tbl.setItem(row,column,QTableWidgetItem(value))
   if key==nxt_key:self.stage_tbl.selectRow(row)
  # Remember the bundle of the finished job. The operator may edit Destination
  # root / Bundle name *before* pressing New Job (to avoid replacing the old
  # bundle), and the reset must still find the reports it is about to delete.
  # The PDF report is the terminal stage: reaching it proves every earlier one
  # ran, and `clone` may legitimately have been skipped with no report at all.
  if self._report_state('pdf')=='complete':self._finished_root=str(self.reports_root())
  self.refresh_primary()
  if hasattr(self,'newjobb'):self.newjobb.setEnabled(self.job_finished())
 def job_finished(self):
  """True when the last job finished and nothing is running.

  Deliberately not `self.files`/`next_stage()`: those follow the current input
  fields, so a session restored from disk (no scan in memory) or one where the
  operator already typed a new bundle would look unfinished forever."""
  if self._busy or self._chain or self._running_stage:return False
  root=self._finished_root
  return bool(root) and self._report_state('pdf',root)=='complete'
 def _new_job_defaults(self):
  """Persisted values the inputs are restored to by new_job().

  Read on demand instead of cached: settings.json is machine-local state and a
  stale in-memory copy would restore values the operator had already changed."""
  return Settings.load(CFG)
 def new_job(self):
  """Clear every result, table, input and report so the next job starts at Scan."""
  # Workers release their QThread after _bg_finished, so a live thread here
  # means a late signal; clearing UI under it would race the next stage.
  workers=[w for w in (getattr(self,'discovery_worker',None),getattr(self,'plan_worker',None),getattr(self,'pdf_worker',None)) if w is not None and w.isRunning()]
  if workers or (self.bg.worker and self.bg.worker.isRunning()):QMessageBox.information(self,'New Job','A stage is still shutting down. Wait for it to finish, then press New Job again.');return False
  if not self.job_finished():QMessageBox.information(self,'New Job','This job is not finished yet. New Job is only available after the PDF stage is complete. Finish the pipeline first, then press New Job again.');return False
  # The finished job may not be the bundle the fields point at any more: the
  # operator is allowed to type a new bundle name first. Delete the reports of
  # the job that actually finished, and warn about where the next one lands.
  root=Path(self._finished_root or self.reports_root())
  clearing=f'The reports and checkpoints of the finished job will be deleted, and every table and input field is cleared.\n\nReports folder:\n{root}'
  existing=self._existing_bundle()
  if existing:
   path,count=existing
   answer=QMessageBox.warning(self,'Bundle already exists - replace it?','A bundle with this name already exists in the destination folder:\n\n'+f'{path}\n({count} file(s) inside)\n\n'+'Starting this new job reuses that same bundle. When the pipeline reaches publishing, files that already exist there are REPLACED (the previous DWG/PDF is copied to _GNI_BACKUP first).\n\nTo keep the existing bundle, change Bundle name or Destination root before starting.\n\n'+clearing+'\n\nStart a new job anyway?',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
  else:
   answer=QMessageBox.question(self,'Start a new job?',clearing+'\n\nThe published DWGs and PDFs in the bundle are not touched.\n\nStart a new job?',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
  if answer!=QMessageBox.Yes:return False
  # Save first: the fields still hold the job's own values, so the defaults
  # restored below are the ones the operator last used.
  self.save()
  # The pipeline is derived from these files, so they are the real reset. Old
  # checkpoints would otherwise make the writer/PDF stages skip matching pairs
  # on the next run; gate_rejected/failures/*.stale are per-run too.
  removed,left=self._clear_reports(root)
  self.files=[];self.groups=[];self.support=[];self.support_results=[];self.discovery_results=[];self.revision_plans=[];self._skipped=set();self._chain=False;self._running_stage=None
  for name in ('tbl','stbl','dtbl','ptbl','rtbl'):
   table=getattr(self,name,None)
   if table is not None:table.setRowCount(0)
  self._report_paths=[];self._finished_root=None
  # Inputs go back to the persisted defaults, not to blank: an empty Source
  # would make Scan impossible until the operator retyped it.
  defaults=self._new_job_defaults()
  self.src.setText(defaults.source_folder);self.dst.setText(defaults.destination_root);self.bundle.setText(defaults.destination_bundle_name);self.exe.setText(defaults.core_console)
  for attr,edit in self.maped:edit.setText(getattr(defaults,attr,''))
  self.workers.setValue(max(1,min(16,int(getattr(defaults,'workers',4)))))
  self.prog.setRange(0,100);self.prog.setValue(0)
  message=f'New job ready. {removed} report/checkpoint file(s) cleared; all tables and inputs reset. The button runs every step up to the publish gate.'
  if left:message+=f' {left} file(s) could not be deleted (in use?) - close AutoCAD/Core Console windows and retry.'
  self.status.setText(message);self.refresh_pipeline();return True
 def _existing_bundle(self):
  """(path, file count) of the bundle the current fields point at, or None.

  New Job keeps the destination inputs, so the usual case is a second run into
  the same bundle - and publishing replaces whatever is already in it."""
  name=self.bundle.text().strip()
  if not name:return None
  try:path=Path(self.dst.text()).expanduser()/name
  except (OSError,ValueError):return None
  if not path.is_dir():return None
  count=0
  try:
   for item in path.rglob('*'):
    if item.is_file():count+=1
  except OSError:pass
  return path,count
 def _clear_reports(self,root=None):
  """Delete this job's handoff files; returns (removed, left). Never raises.

  Only the per-run evidence goes: published DWGs, PDFs and backups live outside
  _GNI_REPORTS and are left alone."""
  root=Path(root or self.reports_root())
  removed=left=0
  if not root.is_dir():return removed,left
  for path in sorted(root.iterdir()):
   if not path.is_file():continue
   try:path.unlink();removed+=1
   except OSError:left+=1
  return removed,left
 def refresh_primary(self):
  nxt=self.next_stage()
  # Nothing pending: the button is a shortcut to the evidence, not a dead end.
  if not nxt:self.primary.setText('Open Reports');self.primary.setToolTip('All pipeline stages are complete. Opens the Reports tab.');return
  self.primary.setToolTip('')
  key,label=nxt;root=Path(self.dst.text())/self.bundle.text()
  if key=='publish':
   try:count=len(AtomicPublisher.load_ready_transactions(root))
   except Exception:count=0
   self.primary.setText(f'Publish {count} validated pair(s)')
  elif key=='pdf':self.primary.setText('Publish PDFs')
  else:self.primary.setText('Run migration to publish gate: '+label)
 def run_next_stage(self):
  nxt=self.next_stage()
  if not nxt:self.show_reports_tab();self.status.setText('All pipeline stages are complete. Reports are listed below.');return
  self._chain=nxt[0] in AUTO_STAGES;self.run_stage(nxt[0])
 def show_reports_tab(self):
  for index in range(self.centralWidget().count()):
   if self.centralWidget().tabText(index)=='Reports':self.centralWidget().setCurrentIndex(index);self.refresh_reports();return
 def run_stage(self,key):
  self.save();handler={'scan':self.do_scan,'destination':self.build_destination,'clone':self.clone_supporting,'discovery':self.discover_title_blocks,'plan':self.build_revision_plan,'writer':self.run_controlled_writer,'validator':self.run_reopen_validator,'dryrun':self.run_atomic_publish_dry_run,'publish':self.run_atomic_publisher,'pdf':self.run_pdf_publisher}[key]
  self._running_stage=key;self.refresh_pipeline();started=handler()
  if not started:
   self._running_stage=None
   if self._chain and key=='clone':
    self._skipped.add('clone');self.refresh_pipeline();QTimer.singleShot(0,self.run_next_stage)
   else:
    self._chain=False;self.refresh_pipeline()
 def _advance_pipeline(self,completed):
  self._running_stage=None;self.refresh_pipeline()
  if not self._chain:return
  if completed not in AUTO_STAGES:self._chain=False;return
  if not self._stage_complete(completed):
   self._chain=False;self.refresh_pipeline();QMessageBox.warning(self,'Pipeline stalled',f'{completed} finished, but its required report was not found.\n\nAutomatic execution was stopped to prevent an infinite loop. Check the Reports tab, then re-run this stage from Advanced.');return
  completed_index=AUTO_STAGES.index(completed)
  next_key=AUTO_STAGES[completed_index+1] if completed_index+1<len(AUTO_STAGES) else None
  if next_key:
   QTimer.singleShot(30,lambda stage=next_key:self.run_stage(stage))
  else:
   self._chain=False;self.refresh_pipeline()
   # Read next_stage() only after refresh_pipeline() so the label is current;
   # it used to be computed from rows rendered before this stage was marked DONE,
   # which left the primary button one stage behind.
   nxt=self.next_stage()
   if nxt:
    self.status.setText('Non-destructive stages complete. Review the dry-run report, then publish explicitly.');QMessageBox.information(self,'Ready to publish',f'All non-destructive stages are complete.\n\nNext action: {self.primary.text()}\n\nReview the dry-run report in the Reports tab before publishing.')
 def _tab_changed(self,index):
  if self.centralWidget().tabText(index)=='Reports':self.refresh_reports()
 def reports_root(self):return Path(self.dst.text())/self.bundle.text()/'_GNI_REPORTS'
 def refresh_reports(self):
  root=self.reports_root();self._report_paths=[];self.rtbl.setRowCount(0)
  if not root.is_dir():self.status.setText('Reports: no _GNI_REPORTS folder yet for this bundle');return
  files=sorted((p for p in root.iterdir() if p.is_file()),key=lambda p:p.stat().st_mtime,reverse=True);stamp=__import__('datetime').datetime.fromtimestamp;self.rtbl.setRowCount(len(files))
  for row,path in enumerate(files):
   stat=path.stat();self._report_paths.append(path)
   for column,value in enumerate([path.name,stamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),f'{stat.st_size/1024:.1f}']):self.rtbl.setItem(row,column,QTableWidgetItem(value))
  self.status.setText(f'Reports: {len(files)} file(s) in {root}')
 def open_report(self,row,column):
  if 0<=row<len(self._report_paths):self._open_path(self._report_paths[row])
 def _open_path(self,path):
  """Open a file/folder with the OS handler; never raises."""
  import os,subprocess,sys
  try:
   if hasattr(os,'startfile'):os.startfile(str(path))
   elif sys.platform=='darwin':subprocess.Popen(['open',str(path)])
   else:subprocess.Popen(['xdg-open',str(path)])
  except Exception as error:QMessageBox.critical(self,'Open failed',str(error))
 def open_stage_report(self,row,column=None):
  """Open the handoff report for the double-clicked pipeline row."""
  if not 0<=row<len(PIPELINE):return
  key,_=PIPELINE[row]
  if key=='scan':self.show_reports_tab();return
  path=self.stage_report_path(key);stale=Path(str(path)+'.stale') if path else None
  if path and not path.is_file():path=stale if stale and stale.is_file() else None
  if not path or not path.is_file():
   QMessageBox.information(self,'Open stage report',f'No report yet for the {PIPELINE[row][1]} stage.\n\nIt is written when the stage completes.');return
  self._open_path(path)
 def open_reports_folder(self):
  root=self.reports_root()
  if root.is_dir():self._open_path(root)
  else:QMessageBox.information(self,'Reports','No reports folder yet. Run a pipeline step first.')
 def browse(self,e,folder):
  x=QWidget();q=QHBoxLayout(x);q.setContentsMargins(0,0,0,0);q.addWidget(e);b=QPushButton('Browse');b.clicked.connect(lambda:self.pick(e,folder));q.addWidget(b);return x
 def pick(self,e,folder):
  p=QFileDialog.getExistingDirectory(self,'Folder',e.text()) if folder else QFileDialog.getOpenFileName(self,'Core Console',e.text(),'Executable (*.exe)')[0]
  if p:e.setText(p)
 def save(self):
  self.s.source_folder=self.src.text();self.s.destination_root=self.dst.text();self.s.destination_bundle_name=self.bundle.text();self.s.core_console=self.exe.text()
  if hasattr(self,'workers'):self.s.workers=int(self.workers.value())
  for a,e in self.maped:setattr(self.s,a,e.text())
  self.s.save(CFG)
 def run_controlled_writer(self):
  root=Path(self.dst.text())/self.bundle.text();report=root/'_GNI_REPORTS'/'stable_external_writer_report.json'
  try:
   self.save();plan=root/'_GNI_REPORTS'/'revision_pilot_write_plan.json'
   if not plan.is_file():QMessageBox.critical(self,'Stable Writer','Build Revision Plan first.');return False
   plans,rejected=StableExternalActionWriter.load_ready_plans(plan,return_rejected=True)
   if rejected:self._report_rejected('Stable Writer',rejected)
   self._start_bg('writer',{'plans':plans,'core':self.exe.text(),'lisp':BASE/'lisp'/'stable_external_writer.lsp','app':BASE,'bundle_root':root,'workers':int(self.workers.value())});return True
  except StableWriterError as e:return self._gate_blocked('Stable Writer',report,e)
  except Exception as e:QMessageBox.critical(self,'Stable Writer failed',str(e));return False
 def run_reopen_validator(self):
  root=Path(self.dst.text())/self.bundle.text();report=root/'_GNI_REPORTS'/'reopen_validation_report.json'
  try:
   self.save();pairs,rejected=StableReopenValidator.load_writer_results(root/'_GNI_REPORTS'/'stable_external_writer_report.json',return_rejected=True)
   if rejected:self._report_rejected('Reopen Validator',rejected)
   self._start_bg('validator',{'pairs':pairs,'core':self.exe.text(),'lisp':BASE/'lisp'/'reopen_validator.lsp','app':BASE,'bundle_root':root,'workers':int(self.workers.value())});return True
  except ReopenValidationError as e:return self._gate_blocked('Reopen Validator',report,e)
  except Exception as e:QMessageBox.critical(self,'Reopen Validator failed',str(e));return False
 def run_atomic_publish_dry_run(self):
  root=Path(self.dst.text())/self.bundle.text();report=root/'_GNI_REPORTS'/'atomic_publish_dry_run_report.json'
  try:
   self.save();pairs,rejected=AtomicPublisherDryRun.load_validated_pairs(root,return_rejected=True)
   if rejected:self._report_rejected('Atomic Dry Run',rejected)
   self._start_bg('dryrun',{'pairs':pairs,'app':BASE,'bundle_root':root,'workers':int(self.workers.value())});return True
  except AtomicPublishDryRunError as e:return self._gate_blocked('Atomic Dry Run',report,e)
  except Exception as e:QMessageBox.critical(self,'Atomic Dry Run failed',str(e));return False
 def _report_rejected(self,stage,rejected):
  """Record plans the gate dropped so the batch can continue without them."""
  root=self.reports_root();root.mkdir(parents=True,exist_ok=True);path=root/'gate_rejected.json'
  if not rejected:
   path.unlink(missing_ok=True);return
  payload={'schema':'GNI-Gate-Rejected-0.4C.7','generated_at':__import__('datetime').datetime.now().astimezone().isoformat(),'stage':stage,'count':len(rejected),'rejected':rejected}
  path.write_text(__import__('json').dumps(payload,indent=2,ensure_ascii=False),encoding='utf-8')
  self.status.setText(f'{stage}: {len(rejected)} drawing(s) skipped by the gate - see gate_rejected.json')
 def _quarantine_report(self,report_path):
  """Rename a stale handoff report so the pipeline cannot advance onto it.

  When a gate rejects every item the stage did not run, but its previous report
  is still on disk and still looks current. Leaving it there meant the next
  stage ran against stale data and the failing stage never reached a terminal
  state, so every resume re-raised the same error.
  """
  try:
   stale=Path(str(report_path)+'.stale');report_path.replace(stale);return stale
  except OSError:
   report_path.unlink(missing_ok=True);return None
 def _gate_blocked(self,stage,report_path,error,rejected=None):
  """Report a gate that accepted nothing, quarantine the stale report, return False."""
  message=str(error);rejected=list(rejected or [])
  _,_,detail=message.partition(':')
  detail=detail.strip()
  # Detail is "IDENTITY=ERR1,ERR2; IDENTITY2=...". A leading "IDENTITY=..." form
  # is used by the writer, whose code prefix is followed by a space.
  # Entries without '=' carry no errors but are still reported.
  if detail and not rejected:
   for chunk in detail.split(';'):
    chunk=chunk.strip()
    if not chunk:continue
    identity,_,errors=chunk.partition('=')
    rejected.append({'identity':identity.strip() or '?','errors':[e.strip() for e in errors.split(',') if e.strip()]})
  if rejected:self._report_rejected(stage,rejected)
  self._quarantine_report(report_path) if report_path else None
  self.refresh_pipeline()
  if not rejected:self.status.setText(f'{stage}: nothing to process - {message}');return False
  summary=f'{len(rejected)} drawing(s) rejected, 0 accepted.'
  self.status.setText(f'{stage}: {summary} Nothing was written.')
  # `self` is a QMainWindow, so QMessageBox.warning is the QWidget overload
  # (parent, title, text) - unlike the static convenience call.
  box=QMessageBox(QMessageBox.Warning,f'{stage} - nothing to process',f'{summary}\n\n'+'\n'.join(f'{item.get("identity","?")}: {", ".join(item.get("errors",[]))}' for item in rejected[:10])+'\n\nThe previous handoff report was moved aside so the pipeline cannot advance on stale data. Fix the listed drawings, then run this stage again.',QMessageBox.Ok,self)
  box.setObjectName('gateBlockedDialog');box.show();box.raise_()
  return False
 def run_atomic_publisher(self):
  root=Path(self.dst.text())/self.bundle.text();report=root/'_GNI_REPORTS'/'atomic_publish_report.json'
  try:
   self.save();packages,rejected=AtomicPublisher.load_ready_transactions(root,return_rejected=True)
   if rejected:self._report_rejected('Atomic Publisher',rejected)
   overwrites=sum(f.get('destination_exists',False) for p in packages for f in p['dry_run'].get('files',[]));answer=QMessageBox.question(self,'Confirm Atomic Publishing',f'Publish {len(packages)} validated pair(s)?\nSkipped by gate: {len(rejected)}\nExisting files: {overwrites}\nBundle: {root}',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
   if answer!=QMessageBox.Yes:return False
   self._start_bg('publish',{'packages':packages,'app':BASE,'bundle_root':root,'workers':int(self.workers.value())});return True
  except AtomicPublishError as e:return self._gate_blocked('Atomic Publisher',report,e)
  except Exception as e:QMessageBox.critical(self,'Atomic Publisher failed',str(e));return False
 def run_pdf_publisher(self):
  try:
   self.save();builder=DestinationBuilder(self.dst.text(),self.bundle.text(),BASE);report=builder.bundle_root/'_GNI_REPORTS'/'atomic_publish_report.json'
   if not report.is_file():QMessageBox.critical(self,'PDF Publisher','Run Atomic Publisher first.');return False
   answer=QMessageBox.question(self,'Confirm PDF Publishing',f'Generate and publish PDFs from PUBLISHED destination DWGs?\n\nExisting PDFs are backed up first; unchanged pairs are skipped.\n\nBundle:\n{builder.bundle_root}',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
   if answer!=QMessageBox.Yes:return False
   self._set_pipeline_busy(True);self.prog.setRange(0,0);self.status.setText('Preparing PDF Publisher...');self.pdf_worker=PdfPublishWorker(self.exe.text(),builder.bundle_root,BASE,int(self.workers.value()));self.pdf_worker.progress.connect(self._on_pdf_progress);self.pdf_worker.completed.connect(self._on_pdf_complete);self.pdf_worker.failed.connect(self._on_pdf_failed);self.pdf_worker.start();return True
  except Exception as error:self._on_pdf_failed(str(error));return False
 def _on_pdf_progress(self,current,total,identity,stage):
  self.prog.setRange(0,total);self.prog.setValue(max(0,current-1));self.status.setText(f'PDF Publisher ({int(self.workers.value())} workers): {current} of {total} | {identity} | {stage}')
 def _on_pdf_complete(self,results,report):
  self._running_stage=None;self._set_pipeline_busy(False);self.prog.setRange(0,100);self.prog.setValue(100);ok=sum(r.status=='PDF_PAIR_PUBLISHED' for r in results);failed=len(results)-ok;unchanged=sum(o.status=='PDF_UNCHANGED' for r in results for o in r.outputs);failed_pairs=[r for r in results if r.status!='PDF_PAIR_PUBLISHED'];self.status.setText(f'PDF Publisher: {ok} pairs published ({unchanged} unchanged), {failed} failed');self.refresh_pipeline()
  # A long batch can have many failures; show the headline in the dialog and keep
  # the complete list on disk so nothing has to be copied out of a message box.
  failure_file=self._write_failures('PDF Publisher',failed_pairs,report)
  details='\n'.join(f'{r.identity}: {r.status} '+('; '.join(r.errors) if r.errors else '') for r in results)
  if failed:
   headline='\n'.join(f'{r.identity}: {r.status}' for r in failed_pairs[:5])
   if len(failed_pairs)>5:headline+=f'\n... {len(failed_pairs)-5} more (see {failure_file.name})'
   self.status.setText(self.status.text()+f' | first failures: {headline.replace(chr(10),"; ")}')
   QMessageBox.warning(self,'PDF Publishing finished with failures',f'{self.status.text()}\n\n{headline}\n\nFull report:\n{report}\n\nFailure list:\n{failure_file}')
  else:
   QMessageBox.information(self,'PDF Publishing complete',self.status.text()+'\n\n'+details+'\n\nReport:\n'+report)
 def _write_failures(self,stage,failed_pairs,report):
  """Persist the full failure list next to the reports; never raises."""
  import json,datetime
  root=self.reports_root()
  try:
   root.mkdir(parents=True,exist_ok=True)
   path=root/'failures.json'
   payload={'schema':'GNI-Failures-0.4C.9','generated_at':datetime.datetime.now().astimezone().isoformat(),'stage':stage,'report':str(report),'count':len(failed_pairs),'failures':[{'identity':getattr(r,'identity',''),'status':getattr(r,'status',''),'errors':list(getattr(r,'errors',[]) or [])} for r in failed_pairs]}
   path.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding='utf-8')
   return path
  except OSError as error:
   self.status.setText(self.status.text()+f' | failure list could not be written: {error}')
   return root/'failures.json'
 def _on_pdf_failed(self,message):
  self._running_stage=None;self._chain=False;self._set_pipeline_busy(False);self.prog.setRange(0,100);self.prog.setValue(0);self.refresh_pipeline();QMessageBox.critical(self,'PDF Publisher failed',message)
 def status_update_safe(self,message):
  self.status.setText(message)
 def closeEvent(self,event):
  workers=[w for w in (getattr(self,'discovery_worker',None),getattr(self,'plan_worker',None),getattr(self,'pdf_worker',None)) if w is not None and w.isRunning()]
  busy=bool(workers) or bool(self.bg.worker and self.bg.worker.isRunning())
  if busy:
   answer=QMessageBox.question(self,'Stop and exit?','A migration stage is still running.\n\nClosing now cancels it: the current drawing finishes its safe unit, then accoreconsole processes are stopped. Partial results stay resumable via the checkpoints.\n\nStop and exit?',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
   if answer!=QMessageBox.Yes:event.ignore();return
  timer=getattr(self,'_heartbeat',None)
  if timer:timer.stop()
  for worker in workers:
   if hasattr(worker,'cancel'):worker.cancel()
  from runtime.process_tracking import child_registry
  child_registry.begin_shutdown()
  for worker in workers:
   if not worker.wait(5000):QMessageBox.warning(self,'Background operation',f'{worker.__class__.__name__} did not stop in 5 seconds and will be abandoned.')
  if not self.bg.shutdown(5000):QMessageBox.warning(self,'Background operation','The background operation did not stop in 5 seconds and will be abandoned.')
  child_registry.terminate_all()
  # The app is no longer exiting (an earlier close may have been cancelled, or
  # Close() was only called on a window that is still alive). Without this the
  # registry refused every child spawned by a later run_console and returned
  # returncode -15 with empty output.
  child_registry.cancel_shutdown()
  event.accept()
 def _set_pipeline_busy(self,busy):
  self._busy=busy;self.primary.setEnabled(not busy);self.workers.setEnabled(not busy);self.cloneb.setEnabled(not busy);self._sync_advanced_buttons(self.adv.isChecked());self.cancelb.setEnabled(busy);self.newjobb.setEnabled(not busy and self.job_finished())
 def _start_bg(self,operation,payload):
  self._set_pipeline_busy(True);self.prog.setRange(0,0);self.status.setText('Starting '+operation+'...');self.bg.start(operation,payload)
 def _bg_progress(self,current,total,identity,output,stage):
  self.prog.setRange(0,max(total,1));self.prog.setValue(current);self.status.setText(f'{self.bg.operation} ({int(self.workers.value())} workers): {current} of {total} | {identity} | {output} | {stage}')
 def cancel_unified_operation(self):
  worker=getattr(self,'discovery_worker',None) or getattr(self,'plan_worker',None) or getattr(self,'pdf_worker',None)
  if worker and worker.isRunning() and hasattr(worker,'cancel'):worker.cancel();self.status.setText('Cancellation requested. Current safe unit will finish first.')
  elif self.bg.worker and self.bg.worker.isRunning():self.bg.cancel()
 def _bg_finished(self,operation,result,report,cancelled,skipped):
  self._running_stage=None;self._set_pipeline_busy(False);self.prog.setRange(0,100);self.prog.setValue(100);state='cancelled safely' if cancelled else 'complete';ok=True
  if operation=='scan':
   self.files=result['files'];self.groups=result['groups'];self.support=result['supporting'];self.populate_supporting();self.tbl.setRowCount(len(self.groups))
   for r,g in enumerate(self.groups):
    for c,v in enumerate([g.drawing_type,g.identity,g.primary_source or '',g.revision_a_donor or '',str(len(g.candidates)),g.warning]):self.tbl.setItem(r,c,QTableWidgetItem(v))
   message=f'Scan {state}: {len(self.groups)} identities';ok=bool(self.groups)
  elif operation=='destination':message=result.message
  elif operation=='clone':
   self.support_results=result;self.refresh_support_results();message=f'Supporting clone {state}: {len(result)} processed';ok=all(x.status!='FAILED' for x in result)
  elif operation=='writer':message=f'Stable Writer {state}: {sum(x.status.startswith("LOCAL_PAIR_WRITTEN") for x in result)} complete, {sum(x.status=="FAILED" for x in result)} failed, {skipped} skipped';ok=all(x.status!='FAILED' for x in result)
  elif operation=='validator':message=f'Reopen Validator {state}: {sum(x.status=="VALIDATED" for x in result)} validated, {sum(x.status=="FAILED" for x in result)} failed, {skipped} skipped';ok=all(x.status=='VALIDATED' for x in result)
  elif operation=='dryrun':message=f'Atomic Dry Run {state}: {sum(x.status=="DRY_RUN_READY_TO_PUBLISH" for x in result)} ready, {sum(x.status=="DRY_RUN_BLOCKED" for x in result)} blocked';ok=all(x.status=='DRY_RUN_READY_TO_PUBLISH' for x in result)
  elif operation=='publish':message=f'Atomic Publisher {state}: {sum(x.status=="PUBLISHED" for x in result)} published, {sum(x.status!="PUBLISHED" for x in result)} failed';ok=all(x.status=='PUBLISHED' for x in result)
  else:message=operation+' '+state
  self.status.setText(message);self.refresh_pipeline()
  if cancelled:self._chain=False
  if not self._chain:QMessageBox.information(self,'Background operation complete',message+('\n\nReport:\n'+report if report else ''))
  elif ok:
   # Mid-cascade success needs no modal: the status line and progress bar carry
   # it. One summary dialog is shown when the cascade reaches the publish gate.
   self.status.setText(message+' | continuing...');self._advance_pipeline(operation)
  else:
   self._chain=False;self.refresh_pipeline();QMessageBox.warning(self,'Pipeline stopped',message+'\n\nFix the reported issue, then run the next step again. Completed stages are skipped automatically.')
 def _bg_failed(self,operation,message):
  self._running_stage=None;self._chain=False;self._set_pipeline_busy(False);self.prog.setRange(0,100);self.prog.setValue(0);self.refresh_pipeline();QMessageBox.critical(self,operation+' failed',message)
 def build_revision_plan(self):
  try:
   self.save();builder=DestinationBuilder(self.dst.text(),self.bundle.text(),BASE);csv_path=builder.bundle_root/'_GNI_REPORTS'/'title_block_discovery_fields.csv'
   if not csv_path.is_file():QMessageBox.critical(self,'Revision Write Plan','Run Discover Title Blocks first. Discovery CSV was not found.');return False
   groups=[group for group in self.groups if group.selected and group.drawing_type in ('SP','AS','GA')]
   if not groups:QMessageBox.information(self,'Revision Write Plan','No SP, AS, or GA groups are selected.');return False
   self.revision_plans=[];self._plan_started=__import__('time').monotonic();self._set_pipeline_busy(True);self.prog.setRange(0,len(groups));self.prog.setValue(0)
   self.plan_worker=ProductionPlanWorker(groups,csv_path,self.dst.text(),self.bundle.text(),self.src.text(),self.s,BASE);self.plan_worker.progress.connect(self._on_plan_progress);self.plan_worker.completed.connect(self._on_plan_complete);self.plan_worker.failed.connect(self._on_background_failed);self.plan_worker.start();return True
  except Exception as error:self._on_background_failed(str(error));return False
 def _on_plan_progress(self,current,total,identity,stage):
  self.prog.setRange(0,total);self.prog.setValue(current);elapsed=int(__import__('time').monotonic()-self._plan_started);self.status.setText(f'Building Revision Plans ({total} drawings): {current} of {total} | {identity} | {stage} | elapsed {elapsed//60:02d}:{elapsed%60:02d}')
 def _on_plan_complete(self,plans,json_report,csv_report,spatial_report,cleanup_report,cancelled):
  self.revision_plans=plans;self.refresh_revision_plan();blocked=sum(p.status=='BLOCKED' for p in plans);ready=sum(p.status.startswith('READY') for p in plans);self._finish_background_ui();state='cancelled safely' if cancelled else 'complete';self.status.setText(f'Revision Plan {state}: {ready} ready, {blocked} blocked');self.refresh_pipeline();detail=self.status.text()+'\n\nJSON:\n'+json_report+'\n\nCSV:\n'+csv_report+'\n\nSpatial rows:\n'+spatial_report+'\n\nCleanup plan:\n'+cleanup_report
  if self._chain and not cancelled and blocked==0:self._advance_pipeline('plan')
  elif self._chain:self._chain=False;QMessageBox.warning(self,'Pipeline stopped',self.status.text()+'\n\nFix the blocked plans, then run the next step again.')
  else:QMessageBox.information(self,'Revision Plan',detail)
 def refresh_revision_plan(self):
  self.ptbl.setRowCount(len(self.revision_plans))
  for row,p in enumerate(self.revision_plans):
   values=[p.identity,p.drawing_type,p.status,p.primary_geometry,'Highest numeric revision',p.migration_date,p.revision_a_date,p.revision_00_date,str(len(p.actions)),'; '.join(p.warnings),'; '.join(p.errors)]
   for column,value in enumerate(values):self.ptbl.setItem(row,column,QTableWidgetItem(str(value)))
 def discover_title_blocks(self):
  try:
   self.save();groups=[group for group in self.groups if group.selected]
   if not groups:QMessageBox.information(self,'Title Block Discovery','No drawing groups are selected.');return False
   self.discovery_results=[];self._discovery_started=__import__('time').monotonic();self._discovery_last=(0,len(groups),'','Preparing discovery job');self._set_pipeline_busy(True);self.prog.setRange(0,len(groups));self.prog.setValue(0)
   self._heartbeat=QTimer(self);self._heartbeat.timeout.connect(self._update_activity_heartbeat);self._heartbeat.start(1000)
   self.discovery_worker=DiscoveryWorker(groups,self.exe.text(),self.dst.text(),self.bundle.text(),self.src.text(),self.s,BASE);self.discovery_worker.progress.connect(self._on_discovery_progress);self.discovery_worker.activity.connect(lambda identity,stage:self._set_activity(identity,stage));self.discovery_worker.completed.connect(self._on_discovery_complete);self.discovery_worker.failed.connect(self._on_background_failed);self.discovery_worker.start();return True
  except Exception as error:self._on_background_failed(str(error));return False
 def _on_discovery_progress(self,current,total,identity,stage):
  self._discovery_last=(current,total,identity,stage);self.prog.setRange(0,total);self.prog.setValue(max(0,current-1 if stage!='Drawing completed' else current));self._update_activity_heartbeat()
 def _set_activity(self,identity,stage):
  current,total,_,_=getattr(self,'_discovery_last',(0,0,'',''));self._discovery_last=(current,total,identity,stage);self._update_activity_heartbeat()
 def _update_activity_heartbeat(self):
  current,total,identity,stage=getattr(self,'_discovery_last',(0,0,'','Working'));elapsed=int(__import__('time').monotonic()-getattr(self,'_discovery_started',__import__('time').monotonic()));dots='.'*((elapsed%3)+1);position=min(max(current,1),total) if total else 0;self.status.setText(f'Discovering Total ({total} drawings): {position} of {total} | {identity} | {stage}{dots} | elapsed {elapsed//60:02d}:{elapsed%60:02d}')
 def _on_discovery_complete(self,results,json_report,csv_report,cancelled):
  self.discovery_results=results;self.refresh_discovery();failed=sum(item.status=='FAILED' for item in results);fields=sum(len(item.fields) for item in results);self._finish_background_ui();state='cancelled safely' if cancelled else 'complete';self.status.setText(f'Title Block Discovery {state}: {len(results)-failed} success, {failed} failed, {fields} fields');self.refresh_pipeline();detail=self.status.text()+'\n\nJSON:\n'+json_report+'\n\nCSV:\n'+csv_report
  if self._chain and not cancelled and failed==0:self._advance_pipeline('discovery')
  elif self._chain:self._chain=False;QMessageBox.warning(self,'Pipeline stopped',self.status.text()+'\n\nReview the failed drawings, then run the next step again.')
  else:QMessageBox.information(self,'Title Block Discovery',detail)
 def _finish_background_ui(self):
  timer=getattr(self,'_heartbeat',None)
  if timer:timer.stop()
  self._running_stage=None;self.prog.setRange(0,100);self.prog.setValue(100);self._set_pipeline_busy(False)
 def _on_background_failed(self,message):
  self._finish_background_ui();self._chain=False;self.refresh_pipeline();QMessageBox.critical(self,'Background operation failed',message)
 def refresh_discovery(self):
  fields=[field for result in self.discovery_results for field in result.fields]
  self.dtbl.setRowCount(len(fields))
  for row,item in enumerate(fields):
   values=[item.drawing_identity,item.drawing_type,item.source_path,item.entity_type,item.handle,item.layer,item.block_name,item.tag,item.value,item.suggested_field,item.confidence,str(item.confidence_score),item.evidence,item.proposed_a,item.proposed_00,item.warning]
   for column,value in enumerate(values):self.dtbl.setItem(row,column,QTableWidgetItem(str(value)))
 def populate_supporting(self):
  self.stbl.setRowCount(len(self.support))
  for row,item in enumerate(self.support):
   checkbox=QCheckBox();checkbox.setChecked(bool(item.selected));checkbox.stateChanged.connect(lambda state,index=row:setattr(self.support[index],'selected',state==Qt.Checked.value));self.stbl.setCellWidget(row,0,checkbox)
   for column,value in enumerate([item.role,item.extension,item.path,item.destination_relative,'PENDING','','',''],1):self.stbl.setItem(row,column,QTableWidgetItem(str(value)))
 def selected_supporting_paths(self):
  return [item.path for item in self.support if item.selected]
 def clone_supporting(self,only_failed=False):
  try:
   self.save();paths=[item.source_path for item in self.support_results if item.status=='FAILED'] if only_failed else self.selected_supporting_paths()
   if not paths:
    if not only_failed:return False
    QMessageBox.information(self,'Supporting clone','No failed copies to retry.');return False
   self._start_bg('clone',{'source':self.src.text(),'destination':self.dst.text(),'bundle':self.bundle.text(),'app':BASE,'policy':self.policy.currentText(),'paths':paths});return True
  except Exception as e:QMessageBox.critical(self,'Supporting clone failed',str(e));return False
 def refresh_support_results(self):
  by_source={item.source_path:item for item in self.support_results}
  for row,item in enumerate(self.support):
   result=by_source.get(str(Path(item.path).resolve()))
   if result:
    for offset,value in enumerate([result.destination_path,result.status,result.action,result.backup_path,result.error],4):self.stbl.setItem(row,offset,QTableWidgetItem(str(value)))
 def retry_supporting(self):
  self.clone_supporting(only_failed=True)
 def open_supporting_destination(self):
  root=Path(self.dst.text())/self.bundle.text();target=root/'4. PART LIST'
  if target.exists():self._open_path(target)
 def build_destination(self):
  try:self.save();self._start_bg('destination',{'source':self.src.text(),'destination':self.dst.text(),'bundle':self.bundle.text(),'app':BASE});return True
  except Exception as e:QMessageBox.critical(self,'Destination build failed',str(e));return False
 def do_scan(self):
  try:self.save();self._start_bg('scan',{'source':self.src.text()});return True
  except Exception as e:QMessageBox.critical(self,'Scan failed',str(e));return False
 def prune_temp_jobs(self):
  """Drop Core Console job folders older than a week; never fatal on startup."""
  try:
   from runtime.process_tracking import gni_temp_root,prune_job_root
   removed,failed=prune_job_root(gni_temp_root(),keep_days=7)
   if removed or failed:self.status.setText(f'Temporary jobs: {removed} stale job folder(s) removed'+(f', {failed} could not be removed' if failed else ''))
  except Exception:pass
if __name__=='__main__':app=QApplication(sys.argv);app.setStyle('Fusion');w=Main();app.setWindowIcon(w.windowIcon());w.prune_temp_jobs();w.show();sys.exit(app.exec())