"""Verify the source delivery without importing robot, camera or ML packages."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parent
manifest=json.loads((root/'bundle_manifest.json').read_text())
wrong=[]
for name,expected in manifest['files'].items():
 p=root/name
 if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:wrong.append(name)
if wrong:raise SystemExit('交付文件缺失或已修改：'+', '.join(wrong))
print('交付完整：'+manifest['role']+' · '+str(len(manifest['files']))+' 个文件 · '+manifest['version'])
