# JEv Bot — Correção objetiva: Python quantitativo local + JEv imediato + interface PySide6 legível

## Missão
Corrigir o projeto existente com **alterações mínimas e verificáveis**. Não criar arquitetura nova, não reescrever módulos estáveis, não adicionar treinamento de IA. Inspecionar o código real antes de alterar. A implementação anterior ainda relata DeepSeek em filas e contadores; isso **não atende** à solicitação atual.

## Requisitos inegociáveis
1. **DeepSeek completamente desativado do fluxo operacional**: não chamar API, não enfileirar, não aguardar, não fazer descoberta/treinamento/revalidação com DeepSeek. Remover do scheduler todas as referências executáveis e timers associados. Preservar arquivos antigos apenas se necessário à compatibilidade; sem execução. A interface não deve exibir painel DeepSeek como atividade atual. Se houver chave/configuração antiga, não apagar segredos nem quebrar configurações existentes; simplesmente não utilizá-los. Registrar de modo verificável `deepseek_calls=0`.
2. **Python local realiza toda a análise**: ler candles M1 fechados do MetaTrader 5, manter janela deslizante de até 30 dias em cache incremental por ativo, calcular padrões estatísticos/quantitativos, amostra efetiva, testes fora da amostra, custos, spread, expectativa líquida e validade temporal. Não usar RSI/MACD ou indicadores tradicionais. Não usar candle em formação, lookahead ou dados futuros. Nenhuma IA externa para análise ou aprendizado.
3. **Despacho imediato ao JEv**: ao encontrar uma oportunidade matematicamente elegível, enviar ao JEv **na mesma passagem pelo ativo**, sem esperar varredura dos demais símbolos, conclusão de ciclo, fila de aprendizado ou download de outros históricos. Payload compacto, tipado e com `symbol`, `run_id`, `candle_ref`, `pattern_id`, evidências e horário. Registrar latência de detecção até despacho. Se houver conexão de rede com JEv, ela deve ter timeout explícito e não bloquear o scheduler ou a GUI; resultados tardios ou referentes a candle vencido são descartados.
4. **JEv só decide** BUY / SELL / HOLD (ou SIM/NÃO/HOLD conforme contrato atual), com ativo e decisão exibidos na GUI. JEv não calcula tamanho da posição, não gerencia exposição e não executa ordens. Python mantém gates de risco, máximo de posições, limites de perdas, spread, tamanho e veto absoluto. Somente paper trading; jamais enviar ordens reais.
5. **Sem obrigar trades**: se a oportunidade não passa pelos critérios, registrar motivo por ativo e seguir imediatamente para o próximo. Não afrouxar filtros para forçar entradas. Se JEv devolver HOLD, registrar, liberar o fluxo e continuar analisando os demais ativos; não ficar em loop de HOLD no mesmo candle.
6. **Ciclos M1 previsíveis**: um símbolo lento ou sem cache não pode impedir o progresso de todos. Pré-carregamento progressivo de histórico com prioridades para mercados marcados Operar; processamento incremental com prazo e cancelamento real. Ciclos com `run_id`, início/fim, duração, CONCLUIDO/PARCIAL/CANCELADO/ERRO, contador visível. Não iniciar tarefas concorrentes que alterem estado sem sincronização. O ciclo seguinte não depende de chamadas DeepSeek (inexistentes).

## Interface gráfica — corrigir listas espremidas verticalmente
Manter **PySide6 desktop**, visual escuro e responsivo. Inspecionar layout real (widgets, `QSplitter`, `QVBoxLayout`, `QHBoxLayout`, stretch factors, `sizePolicy`, `minimumHeight`, `maximumHeight`, margens e cabeçalhos) e corrigir a causa das listas com altura insuficiente.

- Dar **prioridade vertical ao conteúdo útil**: cálculos/oportunidades, decisões JEv e histórico de trades devem ocupar a maior parte da janela. Reduzir cabeçalhos, chips, resumos e espaços vazios excessivos.
- Usar **QSplitter redimensionável** para painéis de operação, com tamanhos iniciais razoáveis e `setStretchFactor`, sem `setFixedHeight` ou `maximumHeight` restritivos nas listas. Persistir tamanhos do splitter (`QSettings` ou mecanismo existente) ao reiniciar.
- Cada lista/tabela tem **rolagem vertical independente**, seleção legível, cabeçalho compacto, fonte confortável, truncamento horizontal com tooltip ou colunas ajustáveis. Não empilhar linhas infinitas sem limite.
- Se três painéis lado a lado causarem compressão, reorganizar em **abas internas ou layout adaptativo**, priorizando Decisões JEv e Oportunidades. Em janela pequena, nenhum painel essencial deve desaparecer; preservar possibilidade de maximizar a janela.
- Separar RESUMO do feed de decisões: RESUMO compacto em barra/status ou aba apropriada. Lista JEv deve conter **somente decisões por ativo**, horário, BUY/SELL/HOLD, motivo e latência. Lista de oportunidades mostra ativo, estado matemático e motivo de rejeição.
- Eliminar o painel operacional DeepSeek ou substituí-lo por painel **Python Quantitativo**, sem prometer treinamento.
- Testar visualmente nas resoluções 1366×768, 1920×1080 e janela reduzida, inclusive escalonamento de fonte/DPI; capturar screenshots locais no relatório, quando possível.
- Nunca atualizar widgets diretamente a partir de worker threads; usar sinais/slots e limitar taxa de atualizações.

## Ordem de trabalho
1. Ler `orquestrador.py`, `candidatos.py`, `padroes.py`, `jev.py`, módulos MT5, risco/paper, GUI e testes existentes; mapear o fluxo real e identificar todas as chamadas/fila DeepSeek e as restrições de altura da GUI.
2. Criar testes de regressão **antes** da correção para: DeepSeek nunca chamado; oportunidade válida despachada ao JEv antes do próximo símbolo; HOLD não trava; erro/timeout JEv não paralisa ciclos; ciclo 1→2→3; risco veto; GUI redimensionável.
3. Aplicar o menor patch possível, sem duplicar scanners ou introduzir nova camada de orquestração.
4. Executar testes automatizados e smoke test GUI offscreen; quando possível, validar MT5 somente leitura e execução manual Windows. Mockar JEv em testes para não gastar tokens. Não modificar thresholds estatísticos sem justificativa quantitativa e teste.
5. Atualizar README, explicar como iniciar e quais painéis observar. **Não fazer push sem autorização explícita**.

## Evidências obrigatórias no relatório
- Antes/depois com arquivos, funções e linhas reais; distinguir hipótese de causa comprovada.
- Prova de que nenhuma chamada DeepSeek ocorre durante ciclos e que não há fila/TTL ativo.
- Log sintético de 3 ciclos consecutivos: para cada ativo elegível, `python_validated_at` → `jev_dispatched_at` → decisão → gate de risco → paper; com latência medida. Demonstrar despacho antes do processamento do próximo símbolo.
- Resultado de HOLD e timeout sem bloquear o próximo ativo/ciclo.
- Contagem por ciclo: símbolos analisados, dados insuficientes, setups encontrados, validados, enviados ao JEv, BUY/SELL/HOLD, vetos de risco, operações paper.
- Screenshots e/ou testes geométricos comprovando que as listas têm altura utilizável e splitter redimensionável; informar limitações se não houver Windows/MT5 disponível.
- `python -X utf8 -m unittest discover -s testes`, `python -m compileall -q jev_bot testes` e testes GUI offscreen, com números reais, não inventados.
- Commit local e status do push; não declarar sucesso sem evidência.

## Critério final de aceite
**Python local encontra e valida oportunidades M1 e despacha imediatamente ao JEv. DeepSeek tem zero participação. A GUI mostra decisões por ativo em painéis legíveis, com altura ajustável. Ciclos seguem avançando, sem bloquear por API ou histórico, e todas as operações permanecem em paper trading.**
