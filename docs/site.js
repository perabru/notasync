'use strict';
// Ajuste estes três valores ao publicar uma nova versão ou trocar o repositório.
const NOTASYNC_RELEASE = Object.freeze({
  repository: 'perabru/notasync',
  version: '2.3.0',
  assets: { exe: 'NotaSync_Setup_2.3.0.exe', zip: 'NotaSync_Instalador.zip' }
});
for (const link of document.querySelectorAll('[data-download]')) {
  const asset = NOTASYNC_RELEASE.assets[link.dataset.download];
  if (asset) link.href = `https://github.com/${NOTASYNC_RELEASE.repository}/releases/download/v${NOTASYNC_RELEASE.version}/${asset}`;
}
for (const link of document.querySelectorAll('[data-repository]')) link.href = `https://github.com/${NOTASYNC_RELEASE.repository}`;
for (const label of document.querySelectorAll('[data-version]')) label.textContent = NOTASYNC_RELEASE.version;
