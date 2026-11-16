# NotaSync 2.3.0

Automação local de lançamento de notas para professores e coordenadores com acesso ao portal da Univap.

## Downloads

- `NotaSync_Setup_2.3.0.exe`: instalador para Windows 10/11 de 64 bits.
- `NotaSync_Instalador.zip`: o mesmo instalador compactado, como alternativa de download.
- `SHA256SUMS.txt`: hashes dos dois arquivos para conferência de integridade.

O instalador inclui Python e as bibliotecas do programa. Um Microsoft Edge utilizável é aproveitado; Chromium é preparado automaticamente se necessário. Não é preciso instalar Python manualmente.

## O que esta versão oferece

- Identificação dos campos A11, AT1, A12, AT2, A13, AT3, A14 e AT4 presentes na origem.
- Distinção de disciplinas, matrículas, turmas, anos e períodos informados pela planilha.
- Acesso limitado às ofertas que a conta do professor ou coordenador consegue abrir.
- Notas ambíguas, inválidas, conflitantes e de alunos cancelados ficam como pendência.
- Teste resumido, teste completo ou opção explícita de pular o teste.
- Barra com etapa, notas preenchidas e notas conferidas após salvar e recarregar.
- Execução visível ou em segundo plano, aviso de conclusão e relatório CSV.

Quando a origem identifica uma disciplina, inclusive nos projetos AT, as notas ficam vinculadas a ela. Planilhas genéricas de projetos sem disciplina específica mantêm o compartilhamento entre as disciplinas acessíveis.

## Validação realizada

199 testes passaram no ambiente local. A integração com a cópia local de uma planilha verificou 24 de 328 notas no modo resumido e 328 de 328 no completo. A opção de pular foi conferida sem executar um teste oculto. O escopo de uma conta de professor foi testado com um adaptador sintético.

O executável foi compilado, extraído e comparado aos fontes; os 889 hashes do runtime conferiram. Nenhuma planilha ou dado real de aluno acompanha o instalador.

Essa validação não constitui teste de execução do instalador em Windows nem de gravação no portal real. Na execução, a conta, as permissões, as colunas e a persistência das notas são conferidas novamente.

## Começar

1. Encerre uma versão anterior do NotaSync antes de atualizar.
2. Instale e abra pelo atalho.
3. Carregue a planilha, confira as avaliações e escolha como continuar.
4. Informe seu acesso e execute o lançamento.

Leia o [manual](https://perabru.github.io/notasync/manual.html) e o [início rápido](https://perabru.github.io/notasync/#inicio-rapido).
