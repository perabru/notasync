# NotaSync — Manual e início rápido

**Versão 2.3.0 · Windows 10/11 de 64 bits**

O NotaSync lê as notas de uma planilha e automatiza o lançamento nas turmas e disciplinas que a conta de professor ou coordenador consegue acessar no portal da Univap. O aplicativo funciona no computador; este site oferece o download e a documentação.

[Voltar ao site](https://perabru.github.io/notasync/) · [Baixar instalador](https://github.com/perabru/notasync/releases/download/v2.3.0/NotaSync_Setup_2.3.0.exe) · [Baixar ZIP](https://github.com/perabru/notasync/releases/download/v2.3.0/NotaSync_Instalador.zip)

## Início rápido

1. **Instale.** Baixe `NotaSync_Setup_2.3.0.exe`, abra o arquivo e clique em **Instalar**. Ao concluir, deixe **Abrir NotaSync agora** marcado. Para abrir depois, use o atalho **NotaSync**.
2. **Carregue as notas.** Clique em **Escolher planilhas** e selecione seus arquivos `.xlsx` ou `.xlsm`. Para usar um endereço de leitura/exportação, abra **Usar um link**, cole o endereço e clique em **Ler planilha**.
3. **Confira a seleção.** O programa identifica as disciplinas e avaliações e seleciona todas as avaliações disponíveis. Desmarque as que não deseja lançar. Confira as pendências e, se precisar, abra **Ver notas identificadas na planilha**.
4. **Escolha como continuar.** Use **Teste resumido** e clique em **Testar resumido**; escolha **Teste completo** e clique em **Testar completo**; ou escolha **Pular teste** e clique em **Continuar sem teste**.
5. **Execute.** Na área de execução, informe o **Login do professor ou coordenador** e a **Senha**. Mantenha **Mostrar o que está fazendo** marcado para acompanhar o navegador, ou desmarque para executar em segundo plano. Clique em **Executar em tempo real**.
6. **Confira o resultado.** Aguarde a conclusão e consulte os lançamentos e pendências. Use **Baixar relatório** ou **Relatório CSV** para guardar o registro da execução.

## 1. Requisitos e instalação

- Windows 10 ou Windows 11 de **64 bits**.
- Conta de professor ou coordenador com acesso às turmas e disciplinas desejadas.
- Internet para acessar o portal e os links de planilha.
- Arquivo Excel atualizado ou link que permita baixar a planilha sem autenticação adicional.

Python e as bibliotecas do programa já acompanham o instalador. Um Microsoft Edge utilizável é aproveitado; se necessário, o instalador prepara o navegador Chromium automaticamente. Pode ser necessário acessar a internet durante essa preparação.

A instalação ocorre no usuário do Windows. Não é preciso instalar Python nem executar comandos manualmente. Se o navegador ou algum componente não puder ser preparado, o instalador informa a etapa e registra o detalhe em `%LOCALAPPDATA%\NotaSync\instalacao.log`.

## 2. Preparar a origem das notas

### Arquivo Excel

Use **Escolher planilhas**. São aceitos arquivos `.xlsx` e `.xlsm`; macros não são executadas. É possível selecionar mais de uma planilha. O botão **×** ao lado de um arquivo o retira da execução, sem apagar o original.

O programa reconhece as avaliações pelos cabeçalhos e utiliza os metadados presentes na origem, como disciplina, turma, ano e período. Exportações completas do portal, como arquivos chamados `consulta.xlsx`, também são reconhecidas. O nome do arquivo, sozinho, não determina qual avaliação deve receber a nota.

### Google Sheets e outros links

Em **Usar um link**, informe um endereço HTTPS de download ou um Google Sheets que permita leitura/exportação sem outro login. Clique em **Ler planilha**.

Se o documento for privado ou o link abrir apenas uma página de login, entre na conta autorizada, baixe uma cópia em Excel e use **Escolher planilhas**. O programa não utiliza a senha do portal para entrar no Google Sheets.

A execução usa a cópia carregada. Se a origem online mudar, carregue novamente e escolha como continuar. Alterar arquivos, link, avaliações ou configuração invalida a escolha de teste anterior.

## 3. Como o programa distingue as matérias

A associação usa o **RA/código de matrícula exato** e a disciplina. Turma, ano e período são conferidos quando estão explícitos na origem. Siglas só são aceitas quando identificam uma única disciplina no acesso atual; não há associação aproximada pelo nome do aluno.

Uma nota identificada como Redes permanece vinculada a Redes. Uma nota de Banco de Dados não é usada como nota de outra matéria. Quando a associação é ambígua, conflitante ou não é encontrada no acesso da conta, o registro fica como **pendência**.

| Bimestre | Avaliação | Projeto |
| --- | --- | --- |
| 1º | A11 | AT1 |
| 2º | A12 | AT2 |
| 3º | A13 | AT3 |
| 4º | A14 | AT4 |

**Projetos com disciplina explícita:** AT1 a AT4 ficam restritos à matéria indicada na planilha ou na exportação.

**Projetos genéricos sem disciplina específica:** o fluxo compartilhado de projetos pode aplicar a mesma nota pelo RA nas disciplinas acessíveis correspondentes. Esse comportamento atende às planilhas comuns de projetos usadas pela coordenação. Confira a origem antes de executar; uma planilha que identifica uma matéria não é tratada como um projeto genérico.

Somente as avaliações selecionadas podem ser alteradas. Colunas de conceito `C-*`, recuperação `AR*`, médias `MB*`/`MBF`, exame `EF` e `NC` não são usadas como substitutas das oito avaliações.

## 4. Escolher entre resumido, completo e pular

| Opção | O que verifica | Resultado |
| --- | --- | --- |
| **Teste resumido** | Abre a réplica local e testa até três notas por avaliação em cada turma/disciplina, com matrículas do início, meio e fim da lista. | Informa se passou e quantas notas foram conferidas em relação ao total apto. Uma amostra não é apresentada como teste completo. |
| **Teste completo** | Testa todas as notas aptas selecionadas na mesma réplica local, sem amostragem. | Informa a aprovação ou a etapa que falhou, com a quantidade de notas verificadas. |
| **Pular teste** | Não abre o navegador de teste e não executa um teste oculto. | Mostra **Teste pulado** e libera a área de execução. |

Nos dois modos de teste, a automação usa uma cópia em memória das notas importadas e uma conta fictícia. Ela preenche as células, confirma **Salvar**, recarrega a página e compara os valores persistidos. Também confere a preservação dos campos fora do lote, médias, recuperações, conceitos e alunos cancelados, além dos bloqueios de alterações indevidas.

**Nenhuma nota é enviada à Univap durante o teste e o arquivo original não é modificado.** O login e a senha reais não são necessários para testar. A réplica local é identificada como ambiente de teste.

Após um teste aprovado ou a escolha de pular, aparece **Executar em tempo real**. O atalho **Testar completo antes de executar** continua disponível após resumir ou pular. Se um teste falhar, leia a etapa e o detalhe mostrado antes de decidir como continuar.

## 5. Lançar com acesso de professor ou coordenador

Use o mesmo acesso que você utiliza no portal. Não é necessário ter perfil de coordenação: o programa utiliza somente as ofertas que a conta consegue abrir. Ele não concede novas permissões.

1. Confira a origem e as avaliações selecionadas.
2. Conclua o teste escolhido ou use **Pular teste** → **Continuar sem teste**.
3. Preencha **Login do professor ou coordenador** e **Senha**.
4. Escolha se deseja acompanhar o navegador em **Mostrar o que está fazendo**.
5. Clique em **Executar em tempo real** e aguarde.

O programa localiza as turmas e disciplinas acessíveis, compara os dados e altera somente as diferenças. Antes de editar, relê a nota atual. Depois de salvar, recarrega e confirma a persistência. As mesmas conferências de lançamento continuam ativas quando o teste local foi pulado.

Alunos cancelados ou com situação não confirmada, RAs ausentes, notas inválidas, valores conflitantes e associações ambíguas ficam bloqueados. A ausência de uma linha na origem não é um comando para apagar a nota no portal.

Se a conta exigir autenticação adicional ou as turmas não aparecerem, execute com o navegador visível e confira o acesso. Mudanças de layout ou permissões no portal podem exigir revisão da configuração.

## 6. Entender a barra de progresso

- **Preenchidas:** notas digitadas e confirmadas no campo do navegador. Essa contagem ainda não comprova que foram gravadas no portal.
- **Conferidas:** notas relidas na etapa de verificação após salvar e recarregar.
- **Percentual:** acompanha o trabalho observado; chega a 100% somente ao concluir a operação.
- **Total ainda desconhecido:** durante o acesso e a descoberta de turmas, a barra indica andamento sem inventar um percentual.

O resumido informa a cobertura da amostra. Por exemplo, **24 de 328 notas aptas testadas** significa que 24 notas foram verificadas; não significa que as demais 304 foram testadas. Pendências de alunos cancelados não são confundidas com notas que ficaram fora da amostra.

Ao desmarcar **Mostrar o que está fazendo**, a execução ocorre em segundo plano. No Windows, o programa emite um aviso e um sinal sonoro ao terminar. O resultado continua disponível no painel.

## 7. Resultado, pendências e relatório

Abra **Histórico, conferência e opções avançadas** para consultar:

- **Ver notas identificadas na planilha:** alunos, turmas, disciplinas, avaliações e pendências da origem.
- **Resultados no portal real:** valores anteriores, valores novos e resultado de cada lançamento.
- **Atividade desta sessão:** andamento e mensagens das operações.
- **Relatório CSV:** histórico exportável, também acessível pelo botão **Baixar relatório** após uma execução.

Uma pendência por avaliação não significa necessariamente um aluno diferente. Um único aluno cancelado pode produzir oito registros bloqueados quando as oito avaliações foram importadas.

Se uma gravação ficar **não confirmada** ou o resultado for incerto, o envio é interrompido. Confira o histórico e a turma diretamente no portal antes de repetir: a falta de confirmação não comprova que o envio falhou.

## 8. Resolver problemas

| Situação | O que conferir |
| --- | --- |
| O download do `.exe` não inicia | Use o download alternativo em ZIP, extraia o arquivo e abra o instalador. Confira se a versão é 2.3.0. |
| A instalação não termina | Leia a etapa informada. Confira a conexão e o navegador disponível. Consulte `%LOCALAPPDATA%\NotaSync\instalacao.log` e execute o instalador novamente após encerrar o programa. |
| O teste não abre o navegador | Use **Encerrar programa** e execute o instalador para conferir os componentes e o navegador. |
| Nenhuma avaliação é identificada | Confira os cabeçalhos exatos e o formato `.xlsx`/`.xlsm`. Um nome de arquivo ou uma coluna genérica de média não determina a avaliação. |
| Uma matéria ou aluno não é localizado | Confira RA, disciplina, turma, ano, período e o acesso da conta. Corrija ambiguidades na origem; não substitua RA por nome aproximado. |
| O link pede login | Baixe a planilha como Excel na conta autorizada e selecione o arquivo local. |
| As turmas reais não aparecem | Execute com **Mostrar o que está fazendo** marcado. Confira a autenticação, o ano configurado e as permissões no portal. |
| A nota mudou após a comparação | Confira a pendência e a turma no portal. Carregue a origem atualizada e faça uma nova execução após a conferência. |
| Salvar falhou ou a gravação ficou incerta | Confira o relatório e o portal antes de repetir. O programa pode ter enviado parte do lote, mesmo sem conseguir confirmar o resultado. |
| A tela do portal mudou | A opção **Configuração do portal** contém seletores e ano letivo. Uma alteração invalida a escolha anterior. Não inclua senha, cookies ou códigos de autenticação nessa configuração. |

## 9. Atualizar, reparar e remover

Antes de instalar uma atualização, use **Encerrar programa** no painel. O instalador não interrompe um lançamento em andamento.

Execute o instalador novamente para atualizar ou reparar os componentes. As configurações e o histórico ficam preservados em `%LOCALAPPDATA%\NotaSync`. Para remover, use **Desinstalar NotaSync** no menu Iniciar ou a lista de aplicativos do Windows; o histórico e a configuração permanecem na pasta de dados do usuário.

## 10. Dados e privacidade

A planilha é processada no computador. A senha de acesso é usada na sessão e não é salva na configuração nem nos relatórios. Os testes utilizam uma cópia local; na execução real, as notas são enviadas ao portal acadêmico autorizado.

Este site não solicita credenciais, não recebe planilhas e não lança notas. Ele apenas distribui o programa e a documentação. Nenhum dado real de alunos acompanha o instalador ou o código publicado.

## 11. Versão e validação

A versão 2.3.0 passou por **199 testes automatizados** em ambiente local. A integração verificou os três modos, o teste resumido com **24 de 328** notas aptas, o teste completo com **328 de 328** e uma conta de professor simulada com acesso a uma única disciplina.

Essas verificações não certificam o comportamento de todas as contas ou de futuras alterações no portal. A instalação em Windows e a gravação de notas no portal real ainda não foram validadas nesta entrega.

**SHA-256 do instalador 2.3.0:**

```text
a582658940492e45121e1e35235512bf1cf528418f3b27555a7c4e8cc1ce7fa6
```

[Arquivos da versão no GitHub](https://github.com/perabru/notasync/releases/tag/v2.3.0) · [Repositório](https://github.com/perabru/notasync)

---

**Bruno Michel Pera · NotaSync 2.3.0**  
Aplicativo independente para automação local. Não é um serviço oficial da Univap.
