# PROMPT — JEv Bot: logs, dashboard responsivo e organização de testes

## Missão
Trabalhe **sobre o projeto JEv Bot existente**, preservando funcionalidades, arquitetura útil, configurações, paper trading e o fluxo **cálculos locais em Python → análise DeepSeek → decisão final JEv**. Antes de alterar, inspecione o repositório, identifique o framework de interface e a implementação atual do download do histórico. **Não reescreva o projeto do zero.** Faça alterações pequenas, verificáveis e compatíveis.

## 1. Corrigir travamentos ao baixar histórico
- Investigue e reproduza o congelamento da interface ao iniciar o download de histórico M1 (janela móvel dos últimos 30 dias). Identifique chamadas bloqueantes, loops pesados, atualização excessiva de widgets e acesso indevido à interface por threads secundárias.
- Execute download, leitura, processamento matemático e tarefas de I/O fora da thread principal da GUI. Se a interface for PySide6, use `QThread`/workers com sinais e slots (ou mecanismo equivalente já adotado), sem manipular widgets diretamente no worker.
- Baixe e processe dados em **lotes limitados**, com paginação, controle de memória, timeout, tentativas com backoff, cancelamento cooperativo e progresso. Evite carregar cópias desnecessárias de todo o histórico e evite disparar análises simultâneas duplicadas para o mesmo ativo.
- Faça atualizações da GUI com frequência controlada (throttle/coalescing), não a cada candle. Exiba ativo, progresso, quantidade de candles, status e erros sem travar a janela.
- Preserve a frequência acordada: sem posição aberta, análise a cada 1 minuto; com posição aberta, a cada 15 minutos. Não permita sobreposição de ciclos: se um ciclo ainda estiver rodando, registre e adie/ignore o próximo conforme regra explícita.
- Respeite limites de risco e drawdown: ao atingir um limite, interrompa **novas análises e novas entradas**, sem interromper a supervisão de posições abertas nem os controles de segurança. A ação **EMERGÊNCIA** deve continuar disponível e independente do download.
- Não introduza dependências pesadas sem justificativa. Priorize eficiência, baixo uso de RAM/CPU, fluidez e tempos de inicialização semelhantes aos da versão anterior.

## 2. Logs em pasta própria, com rotação e limpeza
- Crie/organize a pasta `logs/` na raiz do projeto, preferencialmente usando o módulo padrão `logging` do Python e `RotatingFileHandler` ou solução equivalente compatível com o ambiente.
- Registre eventos importantes: inicialização, encerramento, download (início, lotes, conclusão), análise matemática, chamada/resposta da IA (somente metadados seguros), validação de padrões, decisão do JEv, alterações de limites de risco, bloqueios, ordens simuladas, erros e exceções.
- Use registros com timestamp, nível, módulo, ativo e `analysis_id`/correlation ID quando aplicável. Evite despejar todos os candles ou payloads completos no log.
- Configure limites iniciais ajustáveis: **10 MB por arquivo, até 5 arquivos rotacionados**, e limpeza de arquivos antigos com **mais de 14 dias**; garanta que a política não exclua logs ativos e funcione no Windows. Documente os parâmetros.
- Não grave senhas, tokens, chaves OpenRouter, credenciais MetaTrader 5, nem dados sensíveis. Masque segredos inclusive em mensagens de erro e tracebacks.
- Evite handlers duplicados e escrita excessiva. Se houver múltiplos processos gravando no mesmo arquivo, escolha uma estratégia segura para concorrência.
- Erros graves devem aparecer também no painel de status, sem depender de abrir arquivos manualmente.

## 3. Melhorar layout e responsividade do dashboard
- Preserve o aplicativo desktop Python, **sem migrar para dashboard web**.
- Reorganize o dashboard para ter hierarquia visual clara: (1) status geral e controles Iniciar/Pausar/Emergência; (2) limites de risco, exposição e drawdown; (3) fluxo de trabalho em três painéis identificáveis: **Cálculos Python → DeepSeek → JEv**; (4) histórico de trades com rolagem.
- Mantenha as abas existentes de gráficos/desempenho e configurações (OpenRouter e MetaTrader 5). Não remova recursos atuais.
- Use layouts responsivos do framework existente, divisores redimensionáveis (`QSplitter`, se PySide6), tamanhos mínimos razoáveis e barras de rolagem. Permita maximizar e redimensionar a janela sem sobreposição, cortes ou controles inacessíveis.
- Evite redesenhos completos de tabelas/gráficos em cada evento; use atualização incremental, limites de linhas visíveis e filas de eventos com descarte/agrupamento controlado quando necessário.
- Mantenha botões funcionais durante download e análise. Exiba progresso não bloqueante e possibilidade de cancelar a operação com segurança.
- Preserve a função de copiar o gráfico como imagem para a área de transferência via clique direito.

## 4. Organização obrigatória de testes
- Crie uma pasta **`testes/`** na raiz, se não existir.
- **Todo novo arquivo de teste Python deve ser criado diretamente em `testes/`**, com nome descritivo como `test_download_historico.py`, `test_rotacao_logs.py`, `test_dashboard_responsivo.py`.
- Localize arquivos de teste `.py` soltos na raiz ou em locais improvisados e **mova-os para `testes/`**, atualizando imports, caminhos e configuração do executor de testes. Não mova arquivos de produção apenas por terminarem em `.py`.
- Remova somente testes temporários comprovadamente descartáveis, depois de confirmar que não são usados; prefira mover a apagar. Preserve testes existentes relevantes.
- Não deixe scripts temporários, arquivos de teste ou artefatos de debug espalhados pela raiz. Configure `.gitignore` para caches de teste e logs gerados, sem apagar arquivos necessários.

## 5. Testes e critérios de aceite
- Crie/atualize testes para: download paginado, timeout/retry, cancelamento, não sobreposição de ciclos, limites de memória razoáveis, GUI responsiva durante I/O, rotação/retenção de logs, mascaramento de segredos e organização de testes.
- Teste em **paper trading**, com dados simulados ou históricos; não envie ordens reais nem ative trading real.
- Execute os testes existentes e novos; corrija regressões. Para testes visuais, valide redimensionamento, maximização e interação dos botões durante download.
- Comprove que a janela continua responsiva ao baixar histórico e que o processo não cresce indefinidamente em RAM, arquivos de log ou número de tarefas.
- Caso alguma verificação não possa ser executada no ambiente, informe explicitamente, sem alegar sucesso não testado.

## Entrega obrigatória
Ao concluir, apresente: **(a)** causa identificada do travamento, **(b)** arquivos alterados/criados, **(c)** política de rotação e limpeza dos logs, **(d)** melhorias de layout, **(e)** testes executados e resultados, **(f)** arquivos de teste movidos para `testes/`, **(g)** eventuais pendências. Não altere estratégias de trade nem a lógica de aprovação do JEv além do necessário para corrigir concorrência, desempenho e segurança. **Prioridade máxima: aplicação leve, responsiva e estável.**

## 6. Seleção de mercados na aba Configurações
- Adicione na aba **Configurações** uma seção **Mercados para Monitoramento e Operação**, com caixas de seleção (ticks) claras e independentes para **Monitorar** e **Operar** por mercado. Não crie uma nova interface web.
- Organize por categorias que existirem na corretora/conexão, por exemplo Forex, ações, índices, metais/commodities e criptomoedas. **Não presuma disponibilidade**: descubra e apresente somente categorias e símbolos realmente disponíveis no MetaTrader 5 conectado; quando offline, use as últimas opções válidas salvas, identificando-as como cache, sem inventar ativos.
- Permita expandir categorias, marcar/desmarcar todos os símbolos visíveis de uma categoria e filtrar por nome ou símbolo. Mantenha a janela fluida mesmo com muitos instrumentos, usando carregamento sob demanda e atualização incremental.
- Regra de consistência: **Operar exige Monitorar**. Se o usuário desmarcar Monitorar, desmarque Operar para aquele mercado/símbolo; explique isso na interface. Desabilite a operação de instrumentos indisponíveis ou não habilitados pela corretora.
- Persistência: salve as seleções em configuração local estruturada (JSON ou mecanismo já usado pelo projeto), de forma atômica, com valores padrão conservadores. Recarregue-as ao iniciar; não grave credenciais junto com essa configuração.
- Desempenho: baixe histórico M1 de até 30 dias e agende análise somente para símbolos selecionados para **Monitorar**; não processe mercados desmarcados nem dispare chamadas ao DeepSeek para eles. Reaproveite cache válido e limite tarefas concorrentes para não travar o dashboard.
- O DeepSeek pode analisar ativos monitorados, mas **somente** os marcados para Operar podem ser enviados ao fluxo de aprovação de novas operações pelo JEv. Ainda assim, os limites de risco, exposição e drawdown prevalecem. **Continuar exclusivamente em paper trading**: o tick Operar não habilita operações reais.
- Ao mudar seleções durante uma execução, aplique a alteração com segurança: cancele/ignore análises pendentes de símbolos desmarcados, sem interromper a supervisão de posições já abertas. Mostre quantidade de mercados monitorados e habilitados para operar, e registre mudanças no log sem informações sensíveis.
- Crie os testes correspondentes diretamente em `testes/`, incluindo persistência, dependência Operar→Monitorar, filtro de histórico e DeepSeek, mudança durante processamento, interface responsiva e garantia de não enviar ordens reais.

## Entrega complementar
Ao concluir, liste os arquivos modificados, descreva o funcionamento dos ticks, mostre evidências dos testes realizados e confirme que nenhuma função existente foi removida e que a GUI continua responsiva durante o download de histórico.
