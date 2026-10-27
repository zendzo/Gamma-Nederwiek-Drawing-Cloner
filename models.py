from dataclasses import dataclass, field
from typing import List, Optional
@dataclass
class SourceFile:
    path:str; relative_path:str; filename:str; extension:str; size_bytes:int; modified_iso:str
    role:str='OTHER'; drawing_type:str='OTHER'; identity:str=''; revision:str=''; warning:str=''; selected:bool=True; destination_relative:str=''
@dataclass
class DrawingGroup:
    identity:str; drawing_type:str; candidates:List[SourceFile]=field(default_factory=list); selected:bool=True
    primary_source:Optional[str]=None; revision_a_donor:Optional[str]=None; warning:str=''
@dataclass
class JobResult:
    identity:str; drawing_type:str; source_path:str; mode:str; status:str='PENDING'; failure_stage:str=''
    attempt_count:int=0; elapsed_seconds:float=0.0; job_folder:str=''; output_a_dwg:str=''; output_00_dwg:str=''
    log_path:str=''; message:str=''
