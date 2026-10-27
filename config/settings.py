from dataclasses import dataclass,asdict,fields
from pathlib import Path
import json
@dataclass
class Settings:
    source_folder:str=''; destination_root:str=''; destination_bundle_name:str='Nederwiek_Bundle'; core_console:str=''
    project_from:str='IJMUIDEN VER GAMMA'; project_to:str='NEDERWIEK BETA'; drawing_from:str='2013J310015'; drawing_to:str='I301-J30019'
    long_from:str='OEPA230146'; long_to:str='OEPA240101'; short_from:str='230146'; short_to:str='240101'; target_layer:str='NEDERWIEK_DATA'
    contractor_doc_from:str='2GWG'; contractor_doc_to:str='2GWN'
    workers:int=4
    def __post_init__(self):
        try:self.workers=min(16,max(1,int(self.workers)))
        except (TypeError,ValueError):self.workers=4
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
        p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(asdict(self),indent=2),encoding='utf-8')
