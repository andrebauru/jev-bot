# PROMPT DE IMPLEMENTAÇÃO — JEv Bot: scalping M1 operacional, decisão estruturada e aprendizado paralelo

## Missão

Audite o código REAL do repositório JEv Bot no estado atual e implemente, com alterações mínimas e testes, um fluxo de scalping **M1 prioritariamente operacional**. Corrija o cenário observado em que a GUI mostra apenas `RESUMO`, o JEv não apresenta símbolo nem decisão individual e o DeepSeek consome ciclos aprendendo sem alimentar oportunidades. Não assuma que os relatórios anteriores correspondem exatamente ao código atual: comprove cada causa por arquivo, função, linha, logs e testes. **Não invente operações, não afrouxe limiares estatísticos só para gerar trades e não habilite ordens reais.**

## Princípios obrigatórios

1. **Python = dados, cálculos, elegibilidade, política, gerenciamento de risco, posições e execução paper.** Ele controla exposição total, número e tamanho de posições, drawdown, spread, custos, slippage, latência, validade dos dados, emergências e bloqueios. Verifique risco antes de consultar a IA quando possível e novamente imediatamente antes de executar.
2. **JEv = decisão probabilística curta e tipada sobre um setup elegível**, não gerencia risco nem quantidade de posições. A saída de decisão operacional é `BUY`, `SELL` ou `HOLD` (ou SIM/NÃO/HOLD por direção proposta, se o contrato existente exigir; documente uma única convenção sem ambiguidade). A decisão deve ter símbolo, candle de referência, identificadores, probabilidades/confiança disponíveis, versão do modelo, duração da chamada e motivo codificado. Não peça justificativas em prosa se a API nativa não oferece texto.
3. **DeepSeek = descoberta, revisão e atualização de hipóteses em paralelo**, nunca dependência síncrona de cada decisão JEv. Não é necessário descobrir padrão novo para cada candle. Reutilize padrões existentes matematicamente validados e candidatos operacionais independentes do aprendizado, desde que tenham comprovação estatística suficiente.
4. **Somente candles M1 fechados**, janela móvel de 30 dias, sem indicadores tradicionais (RSI/MACD etc.). Dados de preço, retornos, sequências, volatilidade estatística, spreads e custos podem ser calculados pelo Python. Sem lookahead ou uso de candle em formação.
5. **Exclusivamente paper trading**. Proibido habilitar negociação real, enviar ordens reais ou incluir atalhos que contornem essa restrição. Preservar pausa, cancelamento, emergência e persistência dos bloqueios.
6. **Nunca interpretar ausência de trade como defeito por si só**; o defeito é ausência de decisões/diagnóstico ou pipeline bloqueado. A meta é avaliação regular e auditável de oportunidades, não frequência artificial de entradas.

## Arquitetura-alvo (sem gargalo de aprendizado)

```text
MT5 M1 fechado + cache incremental (30 dias)
                 |
        Python: snapshot numérico
                 |
      Scanner de setups M1 rápidos
                 |
    +------------+--------------------------+
    |                                       |
    v                                       v
Padrões já validados                DeepSeek assíncrono
+ hipóteses determinísticas         descoberta/revisão
    |                                       |
    v                                       v
Validação Python local              Revalidação Python
    |                                       |
    +--------------> Memória versionada <--+
    |
    v
Elegibilidade e pré-gates de risco/custos
    |
    v
JEv: perguntas atômicas / decisão tipada
    |
    v
Python: composição de política + gates finais de risco
    |
    +-- BUY/SELL aprovado --> execução PAPER + acompanhamento
    +-- HOLD/NO/VETO -----> registro por ativo + próxima oportunidade
```

## Prioridade 1 — localizar e remover a causa do JEv silencioso

- Rastreie seleção `Monitorar/Operar` → cache MT5 → scanner → critérios de elegibilidade → DeepSeek → memória de padrões → chamada JEv → parser → risco → executor → publicação PySide6.
- Em cada retorno antecipado, produza motivo tipado e contadores: `NO_CLOSED_CANDLE`, `NO_SETUP`, `INSUFFICIENT_SAMPLE`, `OOS_REJECTED`, `COST_NEGATIVE`, `SPREAD_TOO_HIGH`, `RISK_BLOCK`, `AI_RATE_LIMIT`, `AI_TIMEOUT`, `AI_INVALID_RESPONSE`, `STALE_SNAPSHOT`, `DECISION_HOLD`, `DECISION_NO`, `PAPER_EXECUTED` etc.
- Prove se o JEv não é chamado por ausência de setups, filtros, seleção de mercado, erro de integração, limite de chamadas, fila ocupada, timeout, ou erro na GUI. Nunca chamar `RESUMO` de decisão JEv; resumos vão a painel próprio.
- O painel JEv deve mostrar **ativo**, horário/candle, decisão, confiança quando fornecida, resultado/veto e tempo de resposta. Exibir `sem decisão: motivo` por ativo quando a chamada não ocorrer. Limitar tamanho da lista, preservar rolagem, não inundar a GUI.
- Uma rejeição de hipótese **não paralisa o ativo**: testar próxima hipótese válida dentro de orçamento finito, senão avançar para próximo ativo. Sem loops infinitos de retry nem chamadas pagas em cascata.

## Prioridade 2 — scanner de scalping prático

- A cada fechamento de candle M1, analisar incrementalmente apenas ativos selecionados; dar prioridade aos ativos com configuração `Operar` e dados válidos, com justiça para evitar starvation dos demais.
- Criar candidatos operacionais leves a partir de **padrões já validados** e configurações estatísticas verificáveis (sequências de queda/alta, reversões, continuação, amplitude/retorno, tempo de resolução). Não exigir uma nova resposta DeepSeek para cada candidato.
- Cada candidato tem: `symbol`, `timeframe=M1`, `candle_ref`, `direction`, `setup_id`, `pattern_id` se houver, tamanho de amostra, OOS, retorno líquido estimado, custos/spread, prazo de validade e hash de evidência. Não transformar hipóteses não validadas em setups elegíveis.
- Usar deduplicação por `(symbol, candle_ref, setup_id, direction)` e impedir decisões/ordens repetidas para o mesmo sinal. Descartar snapshot desatualizado, mas continuar imediatamente no próximo candle.
- Separar claramente **ciclo de monitoramento** de **ciclo de execução**: varredura e decisão não aguardam treinamento/descoberta. Definir orçamento de tempo e fila limitada por ativo, telemetria de throughput, backlog, tempo por estágio e p95 de latência.
- Caso nenhum setup seja elegível, mostrar contagem de ativos varridos e razões concretas; não fabricar chamada ao JEv sem informação suficiente só para preencher a tela.

## Prioridade 3 — DeepSeek paralelo, por evento

- Remover dependência síncrona do DeepSeek na rota de decisão; usar worker/fila assíncrona com limite de concorrência, timeout, backoff, orçamento de tokens e cancelamento seguro.
- Acionar descoberta/revisão quando Python detectar **mudança estatisticamente relevante** nos dados fechados (comparação recente vs referência, tamanho de amostra mínimo, teste/intervalo adequado e controle de múltiplas comparações). Evitar disparo por ruído de um candle.
- Manter revisão periódica como fallback, sem travar o trading. Dedupe por hash de snapshot/resumo e cooldown de chamadas; um rate limit do DeepSeek não bloqueia scanner nem JEv.
- DeepSeek sugere hipóteses; Python recalcula tudo com amostras não sobrepostas, validação temporal OOS, custos e estabilidade, mantendo os critérios existentes. Persistir padrões por ativo com versão, status, validade, proveniência e escrita atômica. Não substituir automaticamente padrão válido por hipótese nova.
- Se o orçamento DeepSeek esgotar, sinalizar na GUI e continuar a operação com dados e padrões já validados.

## Prioridade 4 — API JEv real e perguntas atômicas

- **Inspecione a integração existente e a documentação oficial atual antes de alterar o cliente.** Não presuma que o Jev via OpenRouter tem o mesmo contrato/latência do SDK direto TypeSafe. Se a integração atual for um LLM que devolve texto, documentar essa diferença e implementar adaptador isolado, sem alegar probabilidades calibradas que não existem.
- Se a API nativa TypeSafe estiver disponível e configurada, considerar perguntas tipadas independentes em uma chamada (por exemplo: direção `up/down/neutral`, qualidade do setup e ambiente de execução), com saída estruturada e probabilidades. Perguntas devem ser atômicas; dependências e regras de composição ficam no Python. Não enviar dados privados desnecessários nem payload grande.
- **Não substituir automaticamente provedor, modelo ou chave**. Configuração da GUI deve permitir escolher integração existente ou nova somente após verificação e consentimento. Guardar chaves mascaradas, nunca em logs; usar `.env` conforme projeto.
- Registrar versão do modelo, latência, confiança/probabilidades efetivamente retornadas e erros. Aplicar timeout compatível com validade do candle e fallback seguro (`HOLD`/sem operação), sem reusar decisões antigas. Não prometer 70–500ms em rede real sem medir.
- Não usar probabilidade da API como probabilidade de lucro sem **calibração empírica local**. Testar confiabilidade por ativo/condição, Brier score/ECE quando houver amostra suficiente; caso contrário, marcar como não calibrada.

## Prioridade 5 — risco e execução exclusivamente Python

- Python decide quantas posições podem ficar abertas, tamanho por operação, risco agregado, limites diários, exposição por ativo, correlação se disponível, margem simulada, drawdown, spread e slippage. O JEv não recebe autoridade para alterar esses limites.
- Fazer pré-veto barato antes da chamada paga quando um ativo não puder operar; revalidar o estado e os limites imediatamente antes de qualquer ordem paper para evitar race conditions.
- Registrar separadamente: `JEv=BUY/SELL/HOLD`, `policy=ALLOW/VETO`, `paper=OPENED/NOT_OPENED` e causa. Um `BUY` vetado não é `HOLD` do JEv.
- Definir política para posições já abertas sem depender de novo aprendizado; stops, saídas e emergência determinísticos. Não fechar automaticamente posições apenas por timeout de IA sem regra explícita de risco.

## Interface PySide6 — observabilidade útil

- Painel `Oportunidades M1`: ativo, candle, setup, direção proposta, amostra/OOS, retorno líquido esperado, elegibilidade e motivo.
- Painel `Decisões JEv`: ativo, BUY/SELL/HOLD, probabilidades/confiança se disponíveis, latência, ID, decisão de política e status paper. `RESUMO` não pode aparecer como linha de decisão.
- Painel `Aprendizado DeepSeek`: hipóteses geradas/validadas/rejeitadas, última consulta, motivo de trigger, rate limit e economia de tokens; deixar claro que é paralelo.
- Resumo do ciclo em área própria: monitorados, operáveis, M1 novos, candidatos, enviados ao JEv, BUY/SELL/HOLD, vetados, paper executados, razões de não envio, fila e tempos. Sem renderização excessiva.
- Preservar visual dark, redimensionamento, abas, coalescing de eventos, listas limitadas, gráfico de equity/drawdown e copiar gráfico como imagem.

## Testes obrigatórios (sem gasto real de API)

1. Com padrão válido já persistido e DeepSeek indisponível, candidato M1 chega ao JEv e retorna decisão por ativo.
2. `BUY` e `SELL` passam pelo risco e executam apenas paper se permitidos; veto por risco registra decisão e motivo sem executar.
3. `HOLD`/`NO` não trava ciclo; próximo candidato/ativo é analisado.
4. Sem padrão/candidato, resumo informa motivo por ativo e scanner continua; nenhuma decisão fictícia.
5. Mudança estatística relevante aciona DeepSeek assíncrono; ruído não aciona; rate limit/timeout não bloqueia JEv.
6. Teste de candles fechados, 30 dias móveis, dedupe por candle/setup, ausência de lookahead e cache incremental.
7. Teste de 90+ ativos com orçamento finito, fairness, GUI responsiva, sem explosão de chamadas.
8. JSON malformado, respostas sem probabilidades, atraso e mudança de versão do modelo resultam em tratamento seguro.
9. Guardas anti-API nos testes: chamadas reais a DeepSeek/JEv falham imediatamente se não forem mockadas; nenhum token pago acidentalmente.
10. Nenhuma ordem real possível, mesmo em falha, emergência, restart ou race condition.
11. Testes E2E simulados com sequência de candles mostrando contagens por estágio e pelo menos uma decisão JEv exibida com símbolo, sem exigir trade artificial.
12. Executar `python -X utf8 -m unittest discover -s testes`, `python -m compileall -q jev_bot testes` e teste GUI offscreen. Medir tempo de varredura, latência e decisões por ciclo com mocks; não apresentar dados sintéticos como desempenho financeiro real.

## Critérios de aceite

- A aplicação **não fica duas horas apenas em aprendizado** enquanto a rota operacional aguarda DeepSeek; os ciclos M1 de scanner e decisão seguem independentes.
- Cada candle fechado de ativo operável recebe avaliação de elegibilidade rastreável. Quando há candidato válido, há tentativa JEv ou motivo explícito de bloqueio.
- Cada decisão JEv exibida contém ativo e BUY/SELL/HOLD (ou contrato equivalente claramente mapeado). `RESUMO` é somente resumo.
- Rejeições não causam loop de indecisão nem repetição paga sem limite. O sistema continua procurando oportunidades em outros setups/ativos.
- Risco e gestão de posições pertencem exclusivamente ao Python. A IA não contorna veto.
- O sistema permanece paper-only, leve, estável, auditável, com limites de API respeitados.

## Entrega exigida do agente

1. Causas-raiz comprovadas com caminhos, funções, linhas e evidências antes/depois.
2. Diagrama do fluxo real corrigido e contrato de entrada/saída de cada estágio.
3. Lista de arquivos alterados, decisões de compatibilidade da API Jev e eventuais migrações.
4. Logs sintéticos ou sanitizados de um ciclo demonstrando ativo → candidato → JEv → risco → paper/veto e ciclo sem setup.
5. Resultados reais de testes, medições, limitações e passos de validação manual no Windows/MT5.
6. Relatar explicitamente quaisquer chamadas pagas realizadas e custo conhecido. **Não executar chamadas pagas nem fazer push sem autorização explícita.**

**Prioridade final: fazer trading paper com decisões rastreáveis no M1; aprendizado é suporte assíncrono, não o produto principal.**
