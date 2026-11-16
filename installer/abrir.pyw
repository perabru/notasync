"""Inicializador sem console do aplicativo instalado."""
import ctypes
import os
from pathlib import Path
import runpy
import sys
import traceback

ns_data = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'NotaSync'
ns_data.mkdir(parents=True, exist_ok=True)
ns_log = (ns_data / 'aplicativo.log').open('a', encoding='utf-8', buffering=1)
sys.stdout = ns_log
sys.stderr = ns_log
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ns_data / 'browsers')
os.environ.setdefault('NOTASYNC_DATA_DIR', str(ns_data))
os.environ.setdefault('PYTHONUTF8', '1')
try:
    runpy.run_module('notasync', run_name='__main__')
except Exception:
    (ns_data / 'inicializacao.log').write_text(traceback.format_exc(), encoding='utf-8')
    ctypes.windll.user32.MessageBoxW(None, 'Não foi possível iniciar o NotaSync. Execute o instalador novamente para reparar.\n\nDetalhes em: ' + str(ns_data / 'inicializacao.log'), 'NotaSync', 0x10)
