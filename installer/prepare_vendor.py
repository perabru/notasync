"""Obtém somente os dois componentes oficiais fixados por SHA-256."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlretrieve

base = Path(__file__).resolve().parent / 'vendor'
manifest = json.loads((base / 'SOURCES.json').read_text(encoding='utf-8'))
for item in manifest['files']:
    name = item['file']
    if Path(name).name != name:
        raise ValueError('Nome de arquivo inválido no manifesto.')
    source = urlparse(item['source'])
    if source.scheme != 'https' or source.hostname not in {'www.python.org', 'files.pythonhosted.org'}:
        raise ValueError('Origem de componente inválida.')
    target = base / name
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == item['sha256']:
        continue
    partial = base / (name + '.download')
    print('Baixando componente oficial:', name, flush=True)
    urlretrieve(item['source'], partial)
    if hashlib.sha256(partial.read_bytes()).hexdigest() != item['sha256']:
        partial.unlink(missing_ok=True)
        raise ValueError('Falha de integridade no componente baixado.')
    partial.replace(target)
print('Componentes oficiais verificados.')
