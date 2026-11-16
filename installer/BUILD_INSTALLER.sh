#!/usr/bin/env bash
set -euo pipefail
ns_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$ns_script_dir"
# Compilador NSIS 3 instalado pelo sistema, ou caminho informado por MAKENSIS.
ns_compiler="${MAKENSIS:-makensis}"
python3 "$ns_script_dir/prepare_vendor.py"
python3 "$ns_script_dir/bundle_runtime.py"
if [[ $# -gt 0 ]]; then
  "$ns_compiler" -V3 "-DOUTPUT_FILE=$1" NotaSync.nsi
else
  "$ns_compiler" -V3 NotaSync.nsi
fi
