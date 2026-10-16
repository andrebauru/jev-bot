# JEv Bot — alteração cirúrgica: risco antes do JEv e aprovação binária

## Missão
Inspecione o código atual do projeto JEv Bot e implemente a menor alteração segura para que a análise matemática Python determine **exclusivamente** a direção BUY/SELL; o gerenciamento de risco avalie o candidato **antes** de qualquer chamada ao JEv; e o JEv responda somente **YES** ou **NO** à proposta de entrada, sem poder alterar direção, lote, stop, take-profit ou risco. Preserve a execução **exclusivamente PAPER**.

## Contexto e problema
Há relatos de posições não abertas e erros estranhos. A implementação anterior permitia que JEv respondesse BUY/SELL/HOLD, inclusive contradizendo a direção quantitativa. A auditoria anterior identificou crash de formatação `NoneType.__format__`, vetos reais por lote mínimo BTCUSD e spread ETHUSD, e ausência de prova ponta a ponta com JEv real. Não presuma que todos os problemas foram resolvidos; examine logs atuais e fluxo real.

## Contrato obrigatório
1. MT5: consumir apenas candles M1 **fechados**, com timestamps e identificação de símbolo corretos.
2. Python quantitativo: calcular hipótese, evidência estatística e direção fixa `BUY` ou `SELL`, com preço de referência, stop, alvo quando aplicável, validade temporal, custo e identificação `candidate_id`, `candle_ref`, `run_id`.
3. Python risk management **pré-JEv**: verificar dados válidos, spread, stop, distância mínima, volume mínimo/step, risco monetário, exposição, drawdown, limites e capacidade de execução PAPER. Calcular lote viável conforme regras existentes. **Se houver veto, não chamar JEv**; publicar `RISK_REJECTED_PRE_JEV` com razão precisa.
4. Só após aprovação de risco: enfileirar para JEv, em worker não bloqueante, payload imutável com `symbol`, `side_proposed`, `entry_reference`, `stop_loss`, `take_profit` (se houver), `volume`, `risk_amount`, `risk_pct`, `spread`, evidências quantitativas, `candidate_id`, `request_id`, `candle_ref`, `expires_at`. Não incluir credenciais em logs.
5. Prompt ao JEv: 'Avalie exclusivamente se esta entrada proposta merece aprovação. Direção fixa: BUY/SELL. Você NÃO pode propor a direção oposta nem alterar parâmetros. Responda estritamente JSON {"approved":true|false,"reason":"..."}. Se insuficiência de dados, responda false.' Validar esquema estritamente; respostas ambíguas, BUY/SELL/HOLD legado, JSON inválido, timeout, 401/429/5xx são **ERRO_API/REJECTED**, nunca aprovação implícita. `NO` é rejeição normal, não erro de API.
6. Python recebe `YES`: conferir request/candidate/candle/run correspondentes, deduplicação e TTL; **revalidar** risco com cotação atual, volume e limites de exposição antes de executar. Não reutilizar aprovação antiga nem operar se o risco mudou. Executar exclusivamente PAPER, com preço de fill coerente com bid/ask. `NO`: registrar `JEV_REJECTED`, sem ordem. Falhas técnicas: `JEV_ERROR`, sem ordem.
7. JEv não pode inverter BUY↔SELL, nem decidir risco, nem executar ordens. Nunca converter resposta JEv em direção; direção executada deve ser exatamente `side_proposed` do Python.

## Auditoria obrigatória dos erros atuais
- Localizar no código e logs reais o primeiro estágio de perda: `quant_candidate -> pre_risk_pass/reject -> jev_enqueue -> jev_request -> jev_response_yes/no/error -> post_risk_pass/reject -> paper_opened`.
- Correlacionar cada etapa com `run_id`, `candidate_id`, `request_id`, `symbol`, `side_proposed`, `candle_ref`, latência e motivo tipado; traceback sanitizado em exceções.
- Conferir por que paper permanece 0: ausência de candidatos, veto pré-risco, fila, erro API, NO, veto pós-risco, execução paper ou exibição GUI. Não alterar thresholds para forçar fills.
- Examinar `None`/NaN/inf, unidades percentuais, stop/spread, volume mínimo, timezone e candle fechado, validade M1, fila cheia, duplicatas, persistência, falhas de rede e processamento de callbacks.
- Se o risco depender de preço de execução futuro, use pré-checagem com cotação atual e rechecagem final; nunca declarar pré-aprovação como garantia.

## GUI e observabilidade
- Renomear decisão JEv para **Aprovação JEv: SIM / NÃO / ERRO / EXPIRADO**, mostrando símbolo, direção proposta pelo Python, razão, estado API e latência.
- Mostrar contadores por sessão: `quant_candidates`, `pre_risk_pass`, `pre_risk_reject`, `jev_enqueued`, `jev_yes`, `jev_no`, `jev_error`, `post_risk_reject`, `paper_opened` e top 3 motivos de não execução.
- Distinguir claramente `RISK_REJECTED_PRE_JEV`, `JEV_REJECTED`, `JEV_ERROR`, `STALE`, `RISK_REJECTED_POST_JEV`, `PAPER_OPENED`. Não apresentar PENDING como decisão final.
- Preservar responsividade da GUI, splitters, cache e operação com muitos símbolos.

## Testes sem custo e sem ordens reais
A. BUY + risco pré-aprovado + JEv YES + risco pós-aprovado -> uma posição PAPER BUY.
B. SELL equivalente -> uma posição PAPER SELL.
C. BUY/SELL + JEv NO -> zero posições.
D. Risco pré-vetado (lote mínimo, spread, drawdown, stop inválido) -> **zero chamadas ao JEv** e zero posições.
E. JEv tenta responder lado oposto ou formato legado -> rejeição de protocolo, zero posições.
F. 401/429/500/timeout/JSON inválido/None -> erro explícito, zero posições, motor continua.
G. YES tardio, candle expirado, resposta duplicada ou IDs inconsistentes -> zero posições.
H. YES + cotação/risco alterados -> veto pós-JEv, zero posições.
I. Duas oportunidades simultâneas e 90+ símbolos -> filas justas, sem travamento e sem execução duplicada.
J. GUI exibe SIM/NÃO/ERRO e motivo de veto, sem falsos PENDING.
K. Teste de regressão do crash OOS `None`, log de traceback e isolamento do ciclo.
L. Busca estática e teste que garantam ausência de caminho `mt5.order_send` e qualquer ordem real.

## Restrições
- Não reativar DeepSeek nem aprendizado online.
- Não refatorar todo o projeto; alterar apenas os pontos comprovadamente necessários.
- Não alterar chaves, `.env`, credenciais, limites de risco ou parâmetros financeiros sem autorização.
- Não usar chamadas pagas JEv em testes automatizados; integração real somente com autorização explícita.
- Não fazer push; commit local somente depois de testes.

## Entrega exigida
1. Diagnóstico dos erros observados com arquivo:linha e trechos de log sanitizados.
2. Fluxo antes/depois e arquivos alterados.
3. Evidência de que **risco pré-JEv impede chamadas desnecessárias** e de que **JEv não muda direção**.
4. Resultado dos testes e contadores reais por estágio; separar testes mock de validação MT5 real somente leitura.
5. Limitações ainda abertas, inclusive se JEv real não foi testado.
6. Hash do commit local e confirmação de ausência de push.
