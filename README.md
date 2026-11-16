# NotaSync

Automação local para ler planilhas de notas e lançar as avaliações nas disciplinas e turmas acessíveis à conta de professor ou coordenador no portal da Univap.

**Versão 2.3.0 · Windows 10/11 de 64 bits**

- [Site e início rápido](https://perabru.github.io/notasync/)
- [Manual online](https://perabru.github.io/notasync/manual.html)
- [Instalador Windows](https://github.com/perabru/notasync/releases/download/v2.3.0/NotaSync_Setup_2.3.0.exe)
- [Download alternativo em ZIP](https://github.com/perabru/notasync/releases/download/v2.3.0/NotaSync_Instalador.zip)
- [Manual neste repositório](MANUAL.md)

## Início rápido

1. Baixe e abra o instalador. Não é necessário instalar Python manualmente.
2. Abra o NotaSync e use **Escolher planilhas** ou **Usar um link** → **Ler planilha**.
3. Confira as disciplinas e avaliações identificadas automaticamente e desmarque as que não deseja lançar.
4. Escolha **Teste resumido**, **Teste completo** ou **Pular teste**. Os testes usam uma réplica local e não alteram o portal real.
5. Informe seu login de professor ou coordenador e clique em **Executar em tempo real**.
6. Acompanhe as notas preenchidas e conferidas; ao terminar, consulte as pendências e baixe o relatório.

## Recursos

- Reconhece `.xlsx` e `.xlsm`, incluindo exportações completas do portal.
- Identifica A11/AT1, A12/AT2, A13/AT3 e A14/AT4.
- Associa RA exato e disciplina, com turma, ano e período quando disponíveis.
- Mantém disciplinas explícitas separadas, inclusive para projetos AT.
- Utiliza apenas as turmas e disciplinas que a conta já pode acessar.
- Oferece teste resumido por amostra, teste completo ou execução sem teste local.
- Mostra progresso de preenchimento e conferência após salvar.
- Bloqueia ambiguidades, conflitos, alunos cancelados e resultados não confirmados.

Projetos genéricos sem uma disciplina específica podem compartilhar a nota por RA entre as disciplinas acessíveis, conforme o fluxo de origem. Projetos com disciplina explícita permanecem restritos a ela.

## Privacidade

O site é apenas para download e documentação: não recebe planilhas nem credenciais. O aplicativo processa a origem localmente; a senha é usada na sessão e não é salva na configuração nem nos relatórios. Não publique dados reais de alunos, senhas, perfis de navegador ou históricos de execução neste repositório.

## Validação

199 testes automatizados passaram em ambiente local. A integração incluiu resumido **24/328**, completo **328/328** e acesso de professor simulado limitado a uma disciplina. A instalação em Windows e a gravação no portal real ainda não foram validadas nesta entrega.

SHA-256 de `NotaSync_Setup_2.3.0.exe`:

```text
a582658940492e45121e1e35235512bf1cf528418f3b27555a7c4e8cc1ce7fa6
```

## Projeto

O site estático fica em `docs/` e pode ser publicado no GitHub Pages a partir da branch principal. Os instaladores são distribuídos na página de versões do GitHub.

**Autor:** Bruno Michel Pera. Aplicativo independente; não é um serviço oficial da Univap.
