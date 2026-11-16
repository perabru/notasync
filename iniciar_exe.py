"""Ponto de entrada reservado para a compilacao opcional no Windows."""
import os
from pathlib import Path
import runpy
import sys

if os.name == 'nt' and os.environ.get('LOCALAPPDATA'):
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(Path(os.environ['LOCALAPPDATA']) / 'NotaSync' / 'browsers'))
if '--preparar-navegador' in sys.argv:
    sys.argv = ['playwright', 'install', 'chromium']
    runpy.run_module('playwright', run_name='__main__')
else:
    runpy.run_module('notasync', run_name='__main__')
