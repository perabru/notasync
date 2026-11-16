# Gerar o instalador NotaSync 2.3.0

O instalador Windows extrai um ambiente completo já montado e verificado. Não depende de PowerShell nem pip durante a instalação no computador de destino.

## Preparar e compilar

- Windows: instale Python 3.11+ e NSIS 3 para desenvolvimento, então execute `BUILD_INSTALLER.cmd`.
- Linux: instale NSIS 3 e execute `BUILD_INSTALLER.sh` (ou forneça o caminho do compilador em `MAKENSIS`).
- Os scripts obtêm os arquivos oficiais, conferem os hashes, montam o runtime Windows CPython 3.13 AMD64 e compilam o instalador. A montagem exige internet; os componentes são fixados em `requirements-windows.lock`.

`bundle_runtime.py` monta `vendor/runtime` com todas as dependências e cria `runtime_integrity.json`. O compilador inclui essa pasta no executável. `verify_install.py` confere a integridade, os imports e um teste local de navegador no computador de destino.

## Navegador e relatório

Um Edge utilizável é preferido. Na ausência de um navegador compatível, Playwright instala Chromium automaticamente. Os resultados de cada etapa são registrados em `%LOCALAPPDATA%\NotaSync\instalacao.log`.

Não inclua planilhas, dados de alunos, senhas ou perfis de navegador no payload. A compilação em Linux não constitui teste de execução em Windows.

## Validação do modo de teste

O programa oferece teste resumido, teste completo e a opção de pular o teste. Os testes usam uma cópia local da origem selecionada e a mesma implementação de leitura, edição e confirmação do adaptador real. A demonstração usa dados fictícios. O teste resumido verifica uma amostra de até três notas por turma/disciplina e avaliação; o teste completo verifica todas as notas aptas. Os dois testes exigem os 11 checks e informam a cobertura realizada. Pular o teste não abre o navegador de treinamento nem é apresentado como um teste aprovado.

A escolha e o resultado ficam vinculados à planilha, às avaliações e à configuração. Alterações exigem uma nova escolha. O lançamento real continua conferindo o acesso, as colunas, a situação dos alunos, o valor atual e a persistência após salvar, mesmo quando o teste foi pulado.

Execute `python -m unittest discover -s tests -v` na raiz do projeto. Para incluir os testes de navegador locais, defina `NOTASYNC_BROWSER_TESTS=1` e `NOTASYNC_TRAINING_BROWSER_TESTS=1`. Opcionalmente, `NOTASYNC_TEST_BROWSER` informa o caminho de um navegador Chromium compatível para QA. Essa variável é usada exclusivamente pelos testes, não altera o modo visível do botão Testar instalado. Nenhum teste automatizado acessa ou grava o portal real.
