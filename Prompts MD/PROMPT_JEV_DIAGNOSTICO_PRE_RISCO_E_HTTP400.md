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


---

# ADENDO OBRIGATÓRIO — Erro HTTP 400 do JEv/OpenRouter e estados incorretos na interface (captura de 16/10)

## Evidências observáveis na captura — não inferir além delas

- Tela do JEv Bot com **ciclo 2**, estado **AGUARDANDO**, **0 posições abertas**, funil mostrando `env:2`, `JEv:2`, `resp:0`, `decisões 0`, `paper 0` (as abreviações devem ser confirmadas no código antes de interpretá-las definitivamente).
- Tabela **ANÁLISES ENVIADAS / DECISÕES JEv**: `XNGUSD` e `XBRUSD`, direção `BUY`, aprovação `ERRO`, `Estado API=ERROR_HTTP_400`, latências aproximadamente **497 ms** e **741 ms**, motivo `JEv indisponível (http_error); nenhuma ordem`.
- Tabela de atividade matemática exibe ativos `1000xBONK`, `1000xFLOKI`, `1000xPEPE` como `REJECTED`, com motivos truncados `SEM_ENTRAD...` e `IA_INDISPONI...`. **Não presumir** que todo `REJECTED` seja erro matemático ou que o JEv tenha sido chamado para esses três ativos: verificar IDs, timestamps e estágios.
- Há histórico persistido de um trade paper antigo `XNGUSD SELL`, estado `CLOSED` e motivo `TIME_EXIT_M1`; **não confundir** histórico anterior com abertura de posição nesta sessão.
- Configuração visual mostra risco por trade **5%**, exposição máxima **25%** e drawdown máximo **10%**. **Não alterar** esses parâmetros automaticamente.
- A captura **não exibe** corpo da resposta HTTP 400 nem request payload; portanto a causa do erro **ainda é desconhecida**. Não declarar que é chave inválida, modelo incorreto ou schema sem evidência.

## Prioridade 1 — Investigar e corrigir HTTP 400 sem chamadas pagas

1. Localizar a implementação exata de `avaliar_entrada()` em `jev_bot/jev.py`, sua montagem de payload, transporte HTTP, tratamento de erro e passagem pelo worker em `jev_bot/orquestrador.py`. Localizar logs existentes das duas requisições, correlacionando `run_id`, `request_id`, `candidate_id`, `symbol`, `candle_ref`, `model`, endpoint e horário; **nunca registrar segredo**.
2. Capturar em logs **seguros** o status HTTP, tipo de erro, `error.code`, `error.type`, `error.message` sanitizada, `request_id` remoto se houver, modelo e endpoint sem query secreta. Limitar comprimento e remover tokens, chaves, headers Authorization, dados pessoais. Se o corpo não estiver disponível nos logs anteriores, adicionar instrumentação para futuras ocorrências; não inventar o corpo histórico.
3. Verificar, conforme contrato efetivamente utilizado no projeto e documentação correspondente, o **formato da requisição** ao endpoint OpenRouter: `model`, `messages`, roles, content, `response_format`/JSON schema quando suportados, opções de `temperature`, `max_tokens` ou equivalente, nomes e tipos dos campos, compatibilidade de parâmetros com o modelo selecionado, serialização de `None`/NaN/Infinity/Decimal/datetime/NumPy, headers e URL. Distinguir **HTTP 400 (requisição inválida)** de 401, 402, 403, 404, 429, 5xx, timeout e resposta JSON inválida. Não alterar modelo, endpoint ou credenciais sem evidência e autorização.
4. Validar o payload **localmente antes do POST**, com serialização JSON estrita (`allow_nan=False` ou equivalente), tipos compatíveis, tamanho máximo e schema do contrato binário. Rejeitar dados inválidos com motivo específico e traceback, sem deixar a exceção matar o motor. Nunca preencher preços/risco ausentes com zero para 'fazer passar'.
5. Preservar contrato de resposta **somente** `{"approved": boolean, "reason": string}` e direção imutável `side_proposed`. Se o endpoint/modelo não aceitar algum parâmetro opcional, demonstrar com evidência e implementar o menor ajuste compatível, sem transformar erros em `YES` ou `NO` e sem enviar BUY/SELL/HOLD ao JEv para escolha de direção.
6. **Não executar chamadas reais à OpenRouter nesta tarefa.** Criar fixtures/mocks para HTTP 400 com corpo representativo, incluindo erro de parâmetro não suportado, schema incompatível e payload inválido; provar que o erro é exibido de forma rastreável e que **zero** posições são abertas nesses casos. Para confirmar o erro real, entregar instruções de diagnóstico com **uma chamada real futura somente mediante autorização explícita**.

## Prioridade 2 — Diagnóstico integrado: candidato → pré-risco → JEv → pós-risco → paper

- Para cada ativo/candle, produzir **um único registro terminal de estágio**: `SEM_ENTRADA_PYTHON`, `RISK_REJECTED_PRE_JEV`, `JEV_REQUEST_ERROR`, `JEV_REJECTED`, `RISK_REJECTED_POST_JEV`, `PAPER_OPENED`, `STALE` ou `DUPLICATE`, conforme o fluxo real. Distinguir status de cálculo, status de elegibilidade, transporte da API e status de ordem. Evitar `REJECTED` genérico como sinônimo de indisponibilidade de IA.
- Mostrar contadores **por sessão** coerentes, com transições atômicas e sem contagem duplicada: `quant_candidates`, `side_proposed`, `pre_risk_pass`, `pre_risk_reject`, `jev_enqueued`, `jev_http_400`, `jev_yes`, `jev_no`, `jev_error`, `post_risk_reject`, `paper_opened`. Comparar com os contadores antigos `env/JEv/resp/decisões/paper` e esclarecer o significado de cada um; evitar que uma requisição com erro conte como aprovação/decisão.
- Verificar por que `IA_INDISPONI...` aparece em tabela matemática de ativos diferentes daqueles com HTTP 400; correlacionar eventos por ID, não apenas proximidade visual. Conferir se linhas antigas/persistidas estão sendo misturadas com a sessão atual.
- Exibir motivo completo em tooltip ou painel de detalhe para textos truncados (`IA_INDISPONI...`, `SEM_ENTRAD...`), sem quebrar o layout; status `ERRO_API` deve aparecer distinto de `REJECTED_PRE_RISK`, `NO` e `SEM_ENTRADA`.
- Investigar separadamente spread/lote/stop para ETHUSD e SOLUSD e os ativos atuais XNGUSD/XBRUSD, quando houver dados. **Não presumir que o HTTP 400 decorra do spread**, pois são etapas distintas.
- Revalidar cotação, stop, lote, exposição, candle e TTL após um `YES` válido e antes do fill paper; nunca abrir ordem quando erro HTTP ou risco reprovado. Não usar `mt5.order_send`.

## Testes adicionais obrigatórios J–Q

J. HTTP 400 com mensagem sanitizada → `ERROR_HTTP_400` e `JEV_REQUEST_ERROR`, sem `YES`, sem fill, motor segue.
K. HTTP 400 por opção incompatível / schema de resposta não suportado → reproduzir com mock, provar causa e ajuste mínimo.
L. Payload contendo NaN, Infinity, `None` essencial, NumPy ou datetime não serializável → falha local específica, sem POST, sem crash, sem inventar zero.
M. 401/402/403/404/429/500/timeout/JSON inválido → classificação distinta, nenhuma aprovação implícita, sem fill.
N. `BUY` matemático + pré-risco aprovado + `approved=true` + pós-risco aprovado → exatamente 1 fill BUY paper; equivalente SELL; `approved=false` → 0.
O. Tabela matemática vs tabela JEv: IDs e estágios corretos, mensagens completas acessíveis, sem falso `PENDING`, sem confundir `REJECTED` com `ERRO_API`.
P. Contadores de sessão coerentes para 2 requisições que retornam HTTP 400: 2 tentativas, 2 erros, 0 aprovações, 0 paper; nenhuma duplicata.
Q. Persistência do histórico: trade paper antigo não conta como posição aberta no ciclo atual; reabrir GUI não duplica histórico.

## Entrega adicional obrigatória

- **Causa comprovada do HTTP 400**, com arquivo:linha e evidência de corpo sanitizado, **ou** declarar explicitamente que falta o corpo real e listar hipóteses verificadas sem afirmar causa não demonstrada.
- Tabela correlacionada XNGUSD/XBRUSD com request IDs, payload sanitizado, status, mensagem do servidor quando disponível, estágio final e motivo de não execução; comparar com ETHUSD/SOLUSD do diagnóstico pré-risco.
- Explicar a divergência aparente entre os motivos `IA_INDISPONI...` dos criptoativos e o HTTP 400 dos símbolos de energia, com evidência de IDs/timestamps, ou declarar indeterminado.
- Testes A–Q, sem chamadas pagas, sem ordens reais, sem alterações em credenciais/limites e **sem push**. Commit local e relatório de mudanças mínimas.

**Critério adicional de sucesso:** erro HTTP 400 não é tratado como veto matemático nem como resposta NÃO do JEv; o aplicativo informa sua causa quando observável, continua estável e só abre paper após aprovação binária válida e dois checks de risco aprovados.

---

# ADENDO OBRIGATÓRIO — Reativar botão de teste de conexão/status OpenRouter

Além de todas as correções anteriores, **reative o botão existente para testar o status da OpenRouter na interface do JEv Bot**. Procure primeiro o controle já implementado (inclusive sinais/slots, serviço e estado habilitado); **não crie botão duplicado** nem redesenhe a GUI. Se foi desabilitado ou perdeu sua conexão, corrija a causa e reabilite-o na área de Configurações/Integração em que já estava.

## Comportamento obrigatório

1. O botão deve ter rótulo claro, como **“Testar OpenRouter”**, estar habilitado quando a configuração permitir teste e responder ao clique sem congelar a GUI (worker assíncrono, timeout e cancelamento/limpeza adequados). Durante o teste, mostrar “Testando...” e impedir cliques duplicados.
2. **Separar diagnóstico local de teste real:** ao clicar, primeiro validar localmente presença da chave (sem exibi-la), URL/base, modelo selecionado e montagem do payload. Informar falhas locais sem enviar requisição. Disponibilizar teste remoto **somente por ação consciente do usuário**, com aviso de que uma chamada de inferência pode consumir créditos. Nunca executar chamadas remotas automaticamente na inicialização, em testes automatizados ou apenas por abrir a tela.
3. Para verificar o serviço remoto, usar preferencialmente um endpoint de diagnóstico/documentado que não gere inferência nem custo, caso seja adequado para verificar autenticação e disponibilidade. **Não apresentar um simples HTTP 200 de listagem como prova de que o modelo JEv aceita o payload de aprovação binária.** Para validar especificamente o contrato `avaliar_entrada()` e reproduzir o HTTP 400, oferecer ação separada **“Testar requisição JEv (pode consumir créditos)”**, mediante confirmação explícita, com payload mínimo válido e **sem criar qualquer operação Paper ou real**.
4. Mostrar resultado compreensível e auditável: status **OK / FALHA / NÃO TESTADO**, endpoint/host, modelo configurado, horário, latência, código HTTP, tipo e mensagem de erro sanitizada. Diferenciar 400, 401, 402, 403, 404, 429, 5xx, timeout, DNS/TLS e erro de parsing. Exibir detalhes técnicos em tooltip ou área expansível, sem revelar API key, token, Authorization ou dados confidenciais.
5. Se o teste remoto retornar **HTTP 400**, apresentar o motivo real devolvido pelo servidor, quando disponível, e correlacionar com o formato do payload do protocolo binário `{ "approved": bool, "reason": str }`. Se a resposta não trouxer detalhe, informar **“causa não informada pela API”**, sem adivinhar.
6. **Isolar totalmente o teste de conectividade do motor de trading:** clicar no botão não altera contadores de operações, não enfileira candidatos, não muda aprovação SIM/NÃO, não abre posições, não dispara `mt5.order_send`, não troca modelo, endpoint, credenciais ou limites de risco.
7. Preservar o status da conexão como **diagnóstico independente** dos estados de decisões por candidato. “OpenRouter OK” não significa que um trade foi aprovado; “OpenRouter falhou” não é `JEv NO`.

## Testes obrigatórios do botão

- Botão reativado e conectado ao handler; teste local válido/inválido; estado visual inicial/testando/concluído; GUI responsiva.
- Mock de resposta OK, HTTP 400 com corpo, 401, 402, 429, 5xx, timeout, DNS/TLS e JSON inválido; mensagens e sanitização corretas.
- Teste de payload binário válido e inválido com mocks; nenhuma aprovação implícita.
- Nenhuma chamada real paga nos testes automatizados; chamada de inferência real apenas com clique e confirmação explícitos do usuário.
- Confirmar **zero ordens reais, zero operações Paper e zero alterações de contadores do motor** durante todos os testes do botão.

## Entrega adicional

Informar **arquivo:linha** do botão, handler e serviço de teste; por que estava inativo; o que foi corrigido; capturas/descrição do estado antes/depois; comandos e resultados dos testes. Preservar todas as restrições anteriores: **sem push**, sem alterações de credenciais e sem ordens reais.
