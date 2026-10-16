# JEv Bot — Diagnóstico e correção estrita dos vetos de pré-risco com dados MT5

## Missão

Investigue **por que os candidatos direcionais ETHUSD e SOLUSD foram rejeitados por “Spread elevado ou inválido”** antes de consultar o JEv. Determine, com dados e rastreabilidade, se o veto é legítimo ou decorre de erro de unidade, preço, metadados do símbolo, stop ou cálculo. **Corrija somente defeitos comprovados**; não afrouxe limites nem force abertura de posições.

## Contexto confirmado pelo relatório anterior (não presumir acesso ao código)

- Commit local informado: `f14279b`, sem push; 263 testes OK.
- Fluxo desejado e já implementado: **MT5 M1 fechado → Python propõe BUY/SELL → pré-risco → JEv aprova SIM/NÃO → pós-risco com cotação atual → Paper Trading**.
- Demonstração MT5 read-only + JEv mockado: `quant_candidates=4`, `pre_risk_reject=2`, `sem_entrada_python=2`, `chamadas_jev=0`, `paper_opened=0`.
- ETHUSD e SOLUSD: motivo relatado “Spread elevado ou inválido”.
- O JEv real não foi chamado; não inferir que a integração real foi validada.
- `jev_bot/risk.py` contém `pre_check`; `jev_bot/orquestrador.py` chama o pré-risco e registra etapas; GUI em `jev_bot/gui/aba_operacao.py`.

## Escopo obrigatório

### 1. Reproduzir e identificar a causa exata

1. Ler o código efetivo, a configuração e logs existentes. Identificar o ponto exato do veto com `arquivo:linha` e a expressão booleana que o causou.
2. Reproduzir o caso com dados **reais, somente leitura**, de ETHUSD e SOLUSD, quando MT5 estiver disponível. Se indisponível, registrar a limitação e usar fixtures fiéis e explicitamente identificadas como simuladas.
3. Registrar por candidato, sem expor segredos: `run_id`, `candidate_id`, símbolo, candle fechado, direção, bid, ask, last/tick time, `digits`, `point`, `trade_tick_size`, `trade_tick_value`, `trade_contract_size`, `volume_min`, `volume_step`, `volume_max`, `stops_level`, preço de entrada proposto, stop, distância do stop, spread em **preço absoluto, pontos, pips quando aplicável, percentual e razão spread/stop**, limite configurado e decisão final. Informar unidade de cada número.
4. Identificar se há preços defasados, bid/ask ausentes, `ask < bid`, valores `None`/NaN, stop do lado errado, tick value incompatível com moeda da conta, símbolo com sufixo ou especificações diferentes, conversões incorretas, divisão por zero, arredondamento ou confusão entre **spread_pct** e **spread/stop**.
5. Diferenciar explicitamente: **spread alto legítimo**, **dados de mercado inválidos**, **stop curto demais**, **lote mínimo incompatível com orçamento**, **falha técnica**. Não usar a mesma mensagem genérica para causas distintas.

### 2. Corrigir somente o que estiver errado

- Centralizar conversões de unidades e validações numéricas onde já existe a lógica de risco; evitar implementação paralela.
- Aplicar corretamente as especificações retornadas pelo MT5 **por símbolo**, sem pressupor que cripto, metais e forex compartilham `point`, tamanho de contrato, casas decimais ou valor do tick.
- Se os dados forem insuficientes/inconsistentes, **rejeitar com código de motivo preciso**, nunca converter `None` em zero ou assumir spread zero.
- Se o spread/stop realmente exceder o teto vigente, **manter o veto**. Se o problema for stop inválido ou curto, diagnosticar a origem; não alargar stop para passar sem regra preexistente e autorização.
- Não modificar percentuais de risco, teto de spread, stop, capital, lote mínimo, custos ou parâmetros de estratégia para fabricar trades.
- Preservar a direção proposta: JEv apenas aprova/rejeita a direção calculada, jamais a inverte.

### 3. Observabilidade útil, sem poluir a interface

- Cada candidato direcional deve registrar um funil inequívoco: `PYTHON_PROPOSED → PRE_RISK_APPROVED` ou `PRE_RISK_REJECTED(reason_code, details)`; somente aprovados seguem a `JEV_ENQUEUED → JEV_YES/NO/ERROR → POST_RISK_* → PAPER_*`.
- Exibir no tooltip/detalhes do veto os **valores medidos, suas unidades e o limite aplicado**. Texto principal compacto.
- Separar contadores por sessão: propostas direcionais, vetos por causa, aprovações pré-risco, chamadas JEv, aprovações JEv, vetos pós-risco e posições paper.
- Não tratar veto legítimo como crash, nem erro de API como NÃO.

### 4. Provar o fluxo completo em Paper Trading

Executar testes determinísticos, sem custo de API:

A. Spread legítimo acima do limite → pré-veto, **zero** chamadas ao JEv, zero posições.
B. Spread dentro do limite + stop/lote válidos → pré-risco aprovado, **uma** chamada ao JEv mockado.
C. Caso B + JEv `approved=true` + pós-risco aprovado → **uma** posição paper BUY ou SELL, direção preservada, preço de fill correto (BUY ask / SELL bid), IDs e registro no histórico.
D. Caso B + JEv `approved=false` → zero posições.
E. Caso B + cotação piorada antes do fill → veto pós-risco, zero posições.
F. Casos com `point`/`tick_size`/`digits` diferentes, símbolos com sufixo, valores `None`, NaN, bid/ask invertidos, stop inválido e distância zero → rejeição precisa, **sem crash**.
G. Orçamento insuficiente para `volume_min` → veto legítimo com motivo próprio; nunca reduzir risco artificialmente.
H. Resposta duplicada / candle expirado → nenhuma posição duplicada ou obsoleta.
I. Verificar que `mt5.order_send` **não é chamado**, inclusive em erro; testes e demonstrações são exclusivamente paper.

Se possível, executar demonstração com dados MT5 reais **somente leitura** e JEv mockado. Distinguir rigorosamente os resultados de fixtures dos resultados de mercado real. **Não exigir que a demonstração real abra posição** quando o risco corretamente bloquear.

## Restrições inegociáveis

- **Nenhuma ordem real.** MT5 é fonte de dados somente leitura; execução apenas no Paper Trading.
- **Nenhuma chamada paga ao JEv/OpenRouter** nesta tarefa. Usar mocks.
- Não alterar `.env`, chaves, tokens, IDs, modelos, permissões, credenciais ou contas.
- Não remover nem contornar risk management, nem alterar a lógica quantitativa de direção sem prova de bug diretamente relacionado.
- Não reativar DeepSeek nem o protocolo antigo BUY/SELL/HOLD.
- Não refatorar módulos não relacionados, não adicionar recursos extras, **não fazer push**.
- Preservar histórico e persistência das posições paper existentes.

## Entrega exigida

1. **Causa raiz com arquivo:linha**, expressão exata, dados de entrada, unidades e limite violado; classificar como bug ou veto correto.
2. Tabela por ETHUSD/SOLUSD: spread absoluto, spread em pontos, spread/stop, stop, limites, metadados MT5 e motivo preciso. **Não inventar números**: usar `N/D` quando não houver dados.
3. Diff e arquivos alterados, justificando cada mudança mínima.
4. Resultado dos testes A–I e comandos usados; contagem total de testes, `compileall` e GUI offscreen se disponíveis.
5. Evidência de ao menos um caminho **pré-risco aprovado → JEv SIM mockado → posição paper** com IDs rastreáveis, sem fingir que isso aconteceu no mercado real.
6. Contadores reais de uma sessão MT5 read-only, se possível, com os motivos de cada bloqueio.
7. Limitações e pendências; indicar claramente se JEv real permanece não testado.
8. Commit **local somente**, informar hash e status Git; **não executar push**.

**Critério de sucesso:** explicar objetivamente os vetos reais e corrigir somente eventuais defeitos, além de demonstrar que uma proposta válida, aprovada pelo risco e pelo JEv mockado, chega ao executor Paper Trading sem alterar os controles de segurança.
