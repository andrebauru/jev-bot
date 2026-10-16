# JEv Bot — Painéis lado a lado e envio de TODAS as análises matemáticas ao JEv

## Missão

Inspecione o código atual e implemente a menor alteração segura para:

1. Mostrar o trabalho matemático Python e o acompanhamento do JEv **lado a lado** no dashboard PySide6, com quatro ativos simultâneos visíveis.
2. Enviar ao JEv **cada análise matemática concluída e tecnicamente válida**, mesmo quando o resultado quantitativo for desfavorável, negativo, neutro ou não elegível para trade.
3. Manter o **gate de execução de paper trading exclusivamente no Python**, sem permitir que a opinião do JEv contorne matemática, custos, risco ou validade temporal.
4. Preservar pool matemático de quatro símbolos, MT5, UI responsiva e DeepSeek operacionalmente desativado.

## Evidência do vídeo fornecido

A gravação mostra uma tabela horizontal muito estreita chamada “Atividade matemática em paralelo (até 4 ativos)”, com estados `REJECTED` e motivos truncados `NO_MATHEM...`. O diagnóstico de ciclo mostra análises matemáticas e rejeições, mas `eligible=0`, `enfileirados=0`, `chamadas JEv=0`, `respostas=0`. A área de oportunidades e a de cálculos estão empilhadas verticalmente e difíceis de acompanhar. Não concluir, apenas pelo vídeo, que existe chamada oculta ao DeepSeek.

## Mudança fundamental de contrato: análise != oportunidade != ordem

Hoje o despacho parece condicionado a `eligible_for_jev` ou `quant_validated`. **Eliminar esse filtro como condição para ENVIAR UMA ANÁLISE AO JEv**.

Criar dois conceitos distintos e tipados:

- `MathematicalAnalysis`: resultado matemático final válido, com `symbol`, `run_id`, `analysis_id`, `candle_time`, `completed_at`, `buy_metrics`, `sell_metrics`, `spread`, `cost_status`, `quant_status` (`POSITIVE`, `NEGATIVE`, `NEUTRAL`, `INSUFFICIENT_EDGE`), `rejection_reasons`, `execution_eligible` e qualidade dos dados.
- `TradeOpportunity`: derivada de uma análise **somente quando** critérios de edge, OOS, custo e risco permitirem. Nunca converter uma análise ruim em oportunidade.

**Regra de despacho:** `CALCULATION_COMPLETED + DATA_VALID → JEV_QUEUE`, sem exigir `execution_eligible=true`.

Uma análise `NEGATIVE`, `NO_MATHEMATICAL_EDGE`, `IN_SAMPLE_RATE`, `OOS_RATE` ou `COST_EXCEEDS_GROSS_EDGE` **deve chegar ao JEv com essas métricas e razões**, desde que os cálculos tenham terminado com dados tecnicamente válidos.

Exceções que NÃO devem ser enviadas como análise concluída: ausência de histórico mínimo, OHLC inválido, preço/tick indispensável ausente, cálculo interrompido ou exceção de worker. Registrar como `TECHNICAL_FAILURE` no painel, sem fingir que houve avaliação econômica.

### Papel do JEv

Enviar um resumo quantitativo estruturado e compacto, não 43.200 candles, incluindo os dois lados BUY/SELL, expectativa, amostras, OOS, spread, horizonte, motivos e status. Solicitar ao JEv parecer `BUY`, `SELL` ou `HOLD` e justificativa; para análises negativas, JEv pode comentar por que não operar. **O JEv não pode aprovar execução quando `execution_eligible=false`.**

A resposta JEv deve ser exibida inclusive para análises negativas, mas a operação paper somente poderá ocorrer quando:

- `execution_eligible=true` no Python;
- JEv indicar BUY/SELL coerente com lado matematicamente elegível;
- candle/mercado forem revalidados;
- gate de risco Python aprovar.

Se JEv disser BUY para análise matematicamente negativa, mostrar `JEv BUY | EXECUÇÃO BLOQUEADA: NEGATIVE_EXPECTANCY`; não abrir paper.

## Pipeline assíncrono

`MT5 → histórico suficiente → pool Python (até 4) → resultado matemático válido → fila de ANÁLISES JEv → JEv → parecer exibido → gate de paper Python (somente oportunidades elegíveis)`.

- Despachar imediatamente quando cada cálculo terminar, sem esperar os demais do lote.
- `run_id`, `analysis_id`, `request_id`, `symbol`, `candle_time` para correlação e deduplicação.
- Fila limitada, backpressure, retry com política explícita e limites, timeouts e estados `QUEUED`, `SENT`, `REPLIED`, `API_ERROR`, `EXPIRED`, `QUEUE_FULL`, `CANCELLED`.
- Não converter erros de API em HOLD.
- Não bloquear GUI, fetch MT5, pool matemático ou próximo ciclo enquanto espera JEv.
- Garantir que os 90 símbolos continuem recebendo turnos de análise ao longo dos ciclos. Mudança de ciclo não cancela automaticamente cálculos já iniciados; resultados com candle vencido podem ser exibidos como análise histórica, mas nunca executados.
- **Não descartar silenciosamente** análises negativas por dedup, fila cheia, timeout ou mudança de ciclo; contabilizar e mostrar destino final. Não prometer envio efetivo quando a fila estiver cheia.

## Controle de custos e autorização

A nova regra pode produzir até dezenas de chamadas por minuto com 90 ativos, aumentando despesas de API. Implementar configuração `JEV_SEND_ALL_ANALYSES` e `JEV_MAX_REQUESTS_PER_MINUTE`, além de teto de gasto/chamadas por sessão quando houver metadados de custo. O modo **envio de todas as análises** deve ser claramente identificado na UI e habilitado mediante consentimento do usuário antes de realizar chamadas pagas. Não ativar chamadas reais no teste automatizado nem usar credenciais para teste pago sem autorização explícita. Se limite de custo/taxa impedir envio, marcar `RATE_LIMITED` ou `BUDGET_LIMITED`, mostrar contagem e manter política de fila definida, sem alegar que foi enviado.

## Novo layout PySide6 — lado a lado

Na aba Operação, depois dos controles compactos e do status, usar um **QSplitter horizontal** com painéis de largura ajustável:

**Esquerda (aprox. 55%) — Python: análises matemáticas**
- Tabela principal: `Ativo | Candle | Estado | BUY edge | SELL edge | OOS | Spread | Resultado | Envio JEv`.
- Quatro símbolos em `CALCULATING` simultaneamente, atualizados em tempo real.
- Análises finalizadas aparecem sem desaparecer imediatamente; cores e textos acessíveis, tooltip para motivos completos, clique abre métricas.
- Filtros `Todos`, `Calculando`, `Concluídos`, `Falhas técnicas`.

**Direita (aprox. 45%) — JEv: fila, análise e decisão**
- Tabela: `Ativo | Análise | Quant | Status API | Parecer JEv | Latência | Execução`.
- Mostrar também resultados negativos: `NEGATIVE → SENT → HOLD → PAPER BLOQUEADO`.
- Status técnico distinto de decisão de mercado.
- Clique abre detalhes completos, inclusive motivos quantitativos e resposta JEv.

Abaixo dos dois painéis, em área de altura ajustável, manter histórico paper e diagnóstico compacto. Usar QSplitter vertical para essa região se necessário. Persistir tamanhos com QSettings. Independência de rolagem, atualização via signals/slots na thread GUI, sem travamentos. Boa visualização em 1366×768 e 1920×1080; colunas podem ter scroll horizontal e tooltips, não cortar motivo sem acesso ao texto completo. Não recriar a GUI inteira.

### Resumo visível

`Matemáticas concluídas | Enviadas JEv | Aguardando | Respostas | Erros API | Bloqueadas pelo Python | Paper abertas`.

Não misturar `análise enviada` com `oportunidade elegível`. Manter contadores separados:

- `math_completed_valid`
- `math_negative`
- `math_positive`
- `jev_queued_all`
- `jev_sent_all`
- `jev_replied_all`
- `jev_failed`
- `jev_rate_or_budget_limited`
- `execution_eligible`
- `paper_blocked_by_python`
- `paper_opened`

## Auditoria de código antes da mudança

Identificar e citar arquivo/função/linha dos gates atuais entre `_matematica_pura`, `_aplicar_matematica`, `_drenar_analises`, `_oportunidade`, `eligible_for_jev`, worker JEv e GUI. Remover apenas o gate de **envio ao JEv**; preservar integralmente os gates de **execução**. Verificar compatibilidade do contrato atual JEv e se a API aceita uma análise com direção `NONE`/`HOLD`; se não, adaptar payload e parsing com testes. Não inventar resultado BUY/SELL para análise negativa.

## Testes obrigatórios sem chamada paga

1. Análise positiva válida → enviada JEv mock → resposta BUY → gate Python → paper quando permitido.
2. Análise negativa válida → enviada JEv mock → resposta HOLD → nenhuma ordem.
3. Análise negativa válida → enviada JEv mock → resposta BUY → execução bloqueada pelo Python.
4. Sem edge matemático, mas cálculo válido → enviada JEv mock.
5. Histórico insuficiente / tick essencial inválido / exceção → falha técnica visível, não enviada como análise válida.
6. Quatro ativos terminam em ordem diferente → cada um enviado ao terminar, sem esperar lote.
7. 90 símbolos, múltiplos ciclos, fairness, candle vencido, fila cheia, dedup, API 401/429/500, timeout, parsing malformado.
8. Rate limit/orçamento → nenhum gasto ou chamada acima do limite, sem perder a rastreabilidade.
9. GUI lado a lado, resize, QSettings, rolagem, texto completo dos motivos e eventos assíncronos.
10. DeepSeek sem chamada; nenhuma ordem real; 100% paper.

## Critérios de aceitação e relatório

Entregar evidência de **três análises válidas (positiva, negativa e sem edge)** chegando ao worker JEv mockado, com `analysis_id` correlacionado e respostas mostradas no painel direito. Demonstrar que `eligible_for_jev=0` **não implica** `jev_sent_all=0`. Provar que `paper_opened=0` quando análise não é elegível, mesmo com JEv BUY. Mostrar screenshot real da GUI (não apenas offscreen, se Windows estiver disponível) e métricas dos 4 workers. Rodar suite completa e reportar falhas, alterações, benchmark, limitações e hash do commit. Não fazer push sem autorização explícita.

**Não** reintroduzir DeepSeek, não afrouxar critérios matemáticos, não alterar credenciais, não fazer trades reais, não usar chamadas pagas sem autorização e não confundir análise com oportunidade de trade.
