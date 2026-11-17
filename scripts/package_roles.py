"""Create complete, hash-verified source deliveries; never include user config/data."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
ROLES=('data_collector','model_trainer','sorting_operator')


def allowed(path):
    if path.parts[:2]==('models','current_act'):
        return path.name in ('config.json','model.safetensors','model_reference.json') or path.name.startswith(('policy_preprocessor','policy_postprocessor'))
    return ('__pycache__' not in path.parts and path.suffix not in ('.pyc','.log','.mp4','.safetensors')
            and path.name not in ('bundle_manifest.json','sorting.json','config.json')
            and not any(part.startswith('.') for part in path.parts))


def main():
    version=(ROOT/'VERSION').read_text().strip()
    output=ROOT/'dist/role_bundles';output.mkdir(parents=True,exist_ok=True)
    for role in ROLES:
        folder=ROOT/role
        files=sorted(p for p in folder.rglob('*') if p.is_file() and allowed(p.relative_to(folder)))
        manifest={'role':role,'version':version,'entrypoint':{'data_collector':'collect.py','model_trainer':'train.py','sorting_operator':'run_sorting.py'}[role],'files':{p.relative_to(folder).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
        checksum=folder/'bundle_manifest.json';checksum.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        target=output/(role+'_'+version.replace('+','_')+'.zip')
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for p in [*files,checksum]:archive.write(p,role+'/'+p.relative_to(folder).as_posix(),compress_type=zipfile.ZIP_STORED if p.suffix=='.safetensors' else zipfile.ZIP_DEFLATED)
        print(str(target)+' · '+str(len(files))+' files · '+str(target.stat().st_size)+' bytes')


if __name__=='__main__':main()
