from dataclasses import dataclass,asdict,field,fields
from pathlib import Path
import json
@dataclass
class Settings:
    source_folder:str=''; destination_root:str=''; destination_bundle_name:str='Nederwiek_Bundle'; core_console:str=''
    project_from:str='IJMUIDEN VER GAMMA'; project_to:str='NEDERWIEK BETA'; drawing_from:str='2013J310015'; drawing_to:str='I301-J30019'
    long_from:str='OEPA230146'; long_to:str='OEPA240101'; short_from:str='230146'; short_to:str='240101'; target_layer:str='NEDERWIEK_DATA'
    contractor_doc_from:str='2GWG'; contractor_doc_to:str='2GWN'
    workers:int=4
    # Drawings opened per Core Console process. Core Console startup (~4.7 s)
    # dominates any per-drawing work (0.5-2 s), so >1 amortises it. The field
    # keeps its historical name but every drawing stage (discovery, writer,
    # validator, PDF) reads it through runtime/batching.py.
    discovery_batch_size:int=4
    # Bundle roots the operator has opened, newest first (capped by _remember_job).
    # Machine-local convenience only; the job itself lives in _GNI_REPORTS.
    recent_jobs:list=field(default_factory=list)
    def __post_init__(self):
        try:self.workers=min(16,max(1,int(self.workers)))
        except (TypeError,ValueError):self.workers=4
        try:self.discovery_batch_size=min(16,max(1,int(self.discovery_batch_size)))
        except (TypeError,ValueError):self.discovery_batch_size=4
    @classmethod
    def load(cls,p):
        p=Path(p)
        if not p.exists():return cls()
        try:data=json.loads(p.read_text(encoding='utf-8'))
        except (OSError,ValueError):return cls()
        if not isinstance(data,dict):return cls()
        known={field.name for field in fields(cls)}
        return cls(**{key:value for key,value in data.items() if key in known})
    def save(self,p):
        """Write settings, falling back to the writable root instead of raising.

        A portable copy can sit in a folder the user cannot write (``Program
        Files``, a read-only share). ``save`` is called at the start of nearly
        every button handler, so an unhandled ``PermissionError`` here would
        look like a broken app. The fallback keeps the session working and the
        next ``Settings.load`` picks the same file up.
        """
        p=Path(p)
        try:p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(asdict(self),indent=2),encoding='utf-8');return p
        except OSError:
            from runtime.paths import fallback_settings_path
            fallback=fallback_settings_path();fallback.parent.mkdir(parents=True,exist_ok=True);fallback.write_text(json.dumps(asdict(self),indent=2),encoding='utf-8');return fallback
