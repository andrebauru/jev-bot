# JEv Bot — Correção assertiva: Python encontra oportunidades, mas nada chega ao JEv

## Missão e prioridade

Investigue **o código real e os logs reais** do projeto JEv Bot no estado atual (commits locais `cc495b9`, `cf6c07e`, `029f17e`, se presentes). A queixa observada é: **o scanner M1 roda, mas não aparecem solicitações/decisões por ativo no painel JEv durante uso real**. Testes sintéticos passando não comprovam funcionamento com MT5/OpenRouter reais. **Encontre a primeira etapa onde as oportunidades desaparecem, prove com evidência e corrija a menor causa-raiz possível.** Não faça reestruturação geral, não reintroduza DeepSeek, não ative trading real, não faça push sem autorização.

## Arquitetura obrigatória

MT5 (candles M1 fechados) → Python local (coleta incremental, geração de candidatos, cálculos quantitativos, validação matemática e custos) → **despacho imediato e não bloqueante ao JEv de cada oportunidade elegível** → JEv devolve decisão tipada BUY/SELL/HOLD ou SIM/NÃO/HOLD conforme contrato atual → thread do orquestrador revalida candle, preços, risco e posições → executor exclusivamente paper.

O Python gerencia toda a exposição, quantidade de posições, dimensionamento, drawdown, stops, limites e veto. JEv **não** calcula risco, gerencia posições ou descobre padrões. DeepSeek: **zero chamadas, zero filas, zero dependências operacionais**. Preserve GUI PySide6 e configurações existentes.

## ETAPA 1 — Auditoria instrumentada do funil real (antes de mudar filtros)

Localize as funções reais, cite arquivo/função/linha e registre contadores separados, por ciclo e por ativo, para:

1. `symbol_selected` / `symbol_operable` / `symbol_available_mt5`;
2. `fetch_attempt`, `fetch_ok`, `fetch_error`, `closed_m1_count`, `cache_hit`, `history_sufficient`, `history_insufficient`;
3. `candidate_generated`, `candidate_rejected` com código de motivo;
4. `quant_validated`, `quant_rejected` com motivo e métricas: amostra, OOS, expectativa líquida, custo/spread, validade temporal;
5. `eligible_for_jev`, `ineligible_for_jev` (por quê);
6. `queue_enqueue_attempt`, `queue_enqueued`, `queue_full`, `queue_deduped`, `queue_expired`, `worker_dequeued`;
7. `jev_request_started`, `jev_http_success`, `jev_http_error`, `jev_parse_error`, `jev_timeout`, `jev_response_received`;
8. `response_accepted`, `response_discarded_stale`, `response_discarded_run_mismatch`, `response_discarded_duplicate`;
9. `decision_buy`, `decision_sell`, `decision_hold`, `risk_veto`, `paper_opened`, `paper_not_opened`.

Cada item deve ter `run_id`, `symbol`, `candle_ref` (timestamp de fechamento M1), `candidate_id`, `request_id`, duração e motivo estruturado quando pertinente. IDs devem sobreviver à passagem entre threads. Não logue chaves, tokens, senhas nem payloads sensíveis. **Nunca publique `RESUMO` como se fosse decisão do JEv.**

Crie um **diagnóstico compacto na GUI**: `ativos selecionados → dados válidos → candidatos → aprovados → enfileirados → chamadas JEv → respostas → decisões → operações paper`, com os **3 motivos de rejeição mais frequentes**, último envio JEv, idade da fila e idade da última resposta. Exiba `0` explicitamente quando não houver atividade. Diferencie `SEM_SETUP`, `SEM_DADOS`, `SEM_ELEGIBILIDADE`, `FILA_BLOQUEADA`, `IA_INDISPONIVEL`, `SEM_DECISAO` e `VETO_RISCO`.

## ETAPA 2 — Provar o primeiro bloqueio com dados reais

Reproduza em modo seguro e somente leitura no MT5, usando pelo menos 3 ativos operáveis e depois a seleção real (se disponível). Não faça ordens nem chamadas pagas automaticamente em testes. Com uma execução real da API, **somente mediante autorização explícita e configuração válida**, verifique se o JEv é de fato alcançado.

Inspecione especialmente:

- O scanner está usando **candle M1 fechado**, não o candle em formação? Timestamp e timezone corretos? Histórico de 30 dias e cache coerentes? Quantos candles efetivamente disponíveis?
- `Operar=true` chega até o filtro? Símbolos com sufixos MT5 e mercados fechados são tratados corretamente?
- Candidatos são produzidos em dados reais? A regra exige um padrão DeepSeek antigo, JSON de aprendizado, `pattern_id` inexistente, `validation_status` antigo, amostra inviável ou contrato incompatível? **Eliminar dependências residuais de DeepSeek** sem desativar validação quantitativa.
- Existe validação dupla ou gate contraditório que rejeita tudo? `expected_net_return` usa unidade percentual versus fração corretamente? Spread, comissão e slippage estão na mesma unidade e escala?
- O scanner está descartando candidatos por `candle_ja_avaliado` antes de despachar? A chave de deduplicação inclui `symbol + candle_ref + candidate_id` e não bloqueia candidatos diferentes?
- Há starvation pela ordem dos 90 símbolos, budget de 50s, inicialização do cache, lock, espera de worker, backpressure ou fila cheia?
- A fila é efetivamente consumida? Worker inicia, permanece vivo, captura exceções, respeita timeout e devolve resultado ao orquestrador? `run_id` anterior está invalidando respostas legítimas do mesmo candle?
- JEv configurado? Endpoint/modelo/provedor realmente suportam o contrato de decisão atual? Falhas HTTP/parse estão sendo mostradas ou ocultadas como HOLD?
- UI recebe eventos de decisão do worker? Existe filtro de canal, limite de lista, coalescing ou thread Qt que oculta decisões já retornadas?

Para cada hipótese, marque **CONFIRMADA / DESCARTADA / NÃO TESTADA**, com evidência (log, teste ou código). Não chamar ausência de trades de bug sem localizar o estágio. Não declarar integração real validada se apenas mocks foram usados.

## ETAPA 3 — Corrigir com mínimo de alterações

- Se o problema for um gate residual do DeepSeek, remova-o e adapte a elegibilidade aos **candidatos quantitativos locais**, mantendo validação de dados, OOS e custos.
- Se filtros forem excessivamente restritivos, apresente distribuição quantitativa das rejeições e proponha ajustes **justificados e testados fora da amostra**; não reduza limiares às cegas para fabricar trades.
- Despache candidato aprovado ao JEv **assim que for validado**, sem aguardar o restante dos símbolos ou aprendizado; limite fila e concorrência de forma explícita, com métricas e política de descarte de candidatos obsoletos.
- Se não houver candidato válido, exiba motivo e métricas por ativo; **não invente chamadas JEv nem decisões** para preencher a lista.
- Se houver erro de API, exiba `ERRO_API` e reintento controlado; **não converta falha técnica em HOLD de mercado**.
- Se resposta chegar após o vencimento do candle/snapshot, descarte, registre motivo e siga o ciclo seguinte. Nunca execute ordem com dado vencido.
- Evite busy loop, chamadas repetidas no mesmo candle e vazamento de threads. Mantenha cancelamento, pausa e emergência funcionais.
- Sem mudanças em `.env`, credenciais, IDs, tokens ou configurações do usuário; não publique nada nem ative ordens reais.

## ETAPA 4 — Testes de regressão e falhas futuras (obrigatórios)

Acrescente testes em `testes/`, sem rede paga por padrão:

1. MT5 com candles M1 fechados válidos → candidato quantitativo válido → **enfileirado e consumido pelo worker** → JEv mock BUY → revalidação → paper aberto.
2. Mesmo fluxo JEv SELL e HOLD; cada decisão com símbolo, timestamp, latência e motivo visível.
3. 90 ativos, maioria sem setup, alguns com setup: os válidos chegam ao JEv no mesmo ciclo sem starvation.
4. Zero candidatos reais: razões de rejeição e contadores corretos; nenhuma decisão fabricada.
5. API indisponível, 401, 429, 500, timeout, JSON malformado, resposta vazia, modelo incompatível: erro explícito, sem operação, worker sobrevive.
6. Fila cheia, dedup por candle, múltiplos candidatos do mesmo ativo, resposta fora de ordem, `run_id` alterado, candle vencido: sem perdas silenciosas e sem execução stale.
7. Primeira inicialização sem cache e 90 históricos: ciclos avançam e processamento é incremental; não bloquear GUI.
8. Mercados fechados, símbolo com sufixo, histórico curto, candles duplicados, gaps, timezone, preço/spread inválido: razões explícitas.
9. Erros de unidade `%` vs fração, spread vs retorno, slippage, OOS, lookahead: bloquear falsos positivos.
10. Risco máximo, posições máximas, drawdown, pausa, emergência e cancelamento: nenhum paper trade indevido.
11. GUI Windows/Qt: lista JEv exibe **ativo + horário + BUY/SELL/HOLD + motivo + latência**; resumos ficam fora da lista; splitters verticais utilizáveis e persistidos.
12. Proibir chamadas DeepSeek e qualquer ordem real em toda a suíte; chamadas externas Jev mockadas por padrão.

Rodar `python -X utf8 -m unittest discover -s testes`, `python -m compileall -q jev_bot testes`, GUI offscreen e, se disponível, smoke de leitura MT5. Não afirmar teste manual Windows/MT5 realizado se não foi.

## ETAPA 5 — Critérios objetivos de aceite

- Demonstração com dados sintéticos de ponta a ponta: candidato → worker → JEv → decisão por ativo → risco → paper.
- Demonstração de diagnóstico com dados reais MT5 em modo leitura: para cada símbolo, saber **exatamente** em qual etapa parou e por quê.
- Se houver oportunidade matematicamente válida, a fila registra despacho imediato e o worker inicia a chamada; se não houver, diagnóstico comprova o motivo.
- Ciclos 1→2→3 avançam sem depender de DeepSeek, mesmo com API JEv lenta ou fora do ar.
- `deepseek_calls=0` verificável; nenhuma ordem real; nenhuma chamada paga em testes.
- GUI não mostra somente `RESUMO` como decisão; motivo da ausência de decisões fica visível.

## Entrega exigida

Relatório curto e factual: **(a)** causa-raiz comprovada com arquivo/linha; **(b)** primeiro contador do funil que zera; **(c)** patch mínimo e arquivos; **(d)** logs sanitizados de uma oportunidade chegando ao JEv e de uma rejeição real; **(e)** testes e limitações; **(f)** confirmação de zero DeepSeek, paper only; **(g)** hash do commit local. **Não fazer push sem autorização explícita.** Se não conseguir reproduzir no MT5 real, dizer claramente o que falta e não declarar o problema resolvido.
