# JEv Bot — Correção prioritária do scalping M1 operacional

## Missão
Audite e corrija o repositório existente do JEv Bot sem reescrevê-lo. O produto é um **robô de scalping M1 em paper trading**, não um sistema cuja atividade principal seja aprender padrões. O usuário observou cerca de duas horas sem decisões individuais, com a lista JEv exibindo apenas `RESUMO`, ativos ausentes e possíveis limites de chamadas DeepSeek. Identifique a causa exata nos códigos, estados, logs e testes antes de corrigir.

## Contrato funcional e responsabilidades
1. **Python / motor matemático**: usar apenas candles M1 fechados e histórico móvel de 30 dias; procurar continuamente oportunidades por ativo, com métricas reproduzíveis de movimentos, reversões, continuação, probabilidades e retorno líquido estimado, sem indicadores tradicionais. Priorizar oportunidades atuais, não varredura exploratória interminável.
2. **DeepSeek / OpenRouter**: receber dados matemáticos compactos, interpretar e propor hipóteses/opções operacionais. Consultar padrões previamente validados em JSON, mas **não bloquear a busca operacional para treinar/aprender**. Em rejeição de hipótese, avaliar rapidamente outra candidata disponível ou próximo símbolo. Aplicar cache, deduplicação sem impedir novas oportunidades e limites configuráveis de chamadas/tokens; em rate limit, registrar o motivo e continuar cálculos/triagem local, sem chamadas repetidas em loop.
3. **Python / validação independente**: conferir matematicamente toda hipótese e custos (spread, comissões, slippage plausível); dados insuficientes ou inconsistentes não viram trade. Usar avaliação temporal fora da amostra, evitar lookahead, eventos sobrepostos e reuso indevido de resultados antigos. Não diminuir silenciosamente critérios de segurança para aumentar a quantidade de operações.
4. **Motor de risco, fora do JEv**: decidir elegibilidade, dimensionamento, exposição total, limite de posições abertas, drawdown, perda diária e emergência; supervisionar posições abertas. O JEv não calcula nem administra risco.
5. **JEv / decisão**: para cada oportunidade validada e elegível, emitir exatamente `SIM`, `NÃO` ou `HOLD`, com `symbol`, `analysis_id`, `pattern_id`, timestamp, justificativa curta e validade temporal. `HOLD` deve ter condição objetiva e prazo; ao expirar, reavaliar ou descartar a candidatura, sem loop infinito. `NÃO` encerra apenas aquela candidata, não a busca por outras oportunidades.
6. **Executor paper**: executar somente `SIM` com autorização atual do motor de risco; nenhuma ordem real, mesmo com credenciais MT5 cadastradas. Revalidar risco e preço imediatamente antes da execução.

## Cadência e prioridade
- Sem posição aberta: processamento por candle M1 fechado, com busca operacional prioritária; não esperar TTL de 1 hora para reavaliar uma **nova** oportunidade.
- Com posição aberta: preservar a política configurada de análise a cada 15 minutos, mas manter supervisão local contínua das posições, riscos e comandos de emergência, sem esperar 15 minutos para proteção.
- Distinguir TTL de descoberta de padrões de validade de oportunidades M1; um resumo estatístico repetido não significa que não haja novo preço/candle/condição de entrada.
- Com muitos símbolos, usar escalonamento justo, limite de trabalho por ciclo, cache incremental e prioridade para mercados habilitados em Operar. Nunca bloquear GUI ou comandos de segurança.
- Não exigir número mínimo de trades. **Exigir diagnósticos e decisões rastreáveis** quando há candidatos elegíveis. Quando não houver candidatos, apresentar contagem e motivos por ativo/ciclo.

## Corrigir o dashboard
- Na lista JEv, exibir apenas eventos individuais com **ativo real**, horário, `SIM/NÃO/HOLD`, razão, status e referência de análise. `RESUMO` fica em painel separado de diagnóstico, nunca como se fosse uma decisão do JEv.
- Exibir funil por ciclo: símbolos selecionados → com M1 válido → candidatos gerados → enviados ao DeepSeek → hipóteses recebidas → validados → elegíveis → enviados ao JEv → SIM/NÃO/HOLD → paper executados.
- Mostrar razões explícitas para cada queda no funil (sem candidato, dados incompletos, spread/custo, amostra insuficiente, rate limit, timeout, risco, resposta inválida, expiração). Nunca apresentar `HOLD` como substituto silencioso de erro.
- Mostrar tempo desde a última decisão real, última análise DeepSeek, última oportunidade elegível e último trade, com IDs correlacionáveis nos logs.
- Manter listas redimensionáveis, coalescência de eventos, interface responsiva e logs rotacionados/mascarados.

## Auditoria obrigatória
1. Traçar ponta a ponta a origem das candidatas, a lógica de filtros, as chamadas ao DeepSeek, o contrato validado, a passagem ao JEv e o executor. Identificar onde o fluxo se esgota.
2. Investigar especificamente: limite de chamadas/hora, deduplicação por hash, TTL de 1h, cache M1, filtros OOS/Wilson, seleção Monitorar/Operar, `RESUMO` indevido na lista JEv, e possibilidade de o JEv nunca receber candidatos.
3. Medir com logs e testes a latência por etapa, quantos símbolos são visitados por minuto e por que nenhuma decisão individual ocorreu em duas horas. Não atribuir a causa sem evidência.
4. Implementar o menor conjunto de mudanças necessário. Preservar o histórico de operações, memória de padrões e todas as travas de segurança.
5. Não enviar requisições pagas em testes automatizados: mocks obrigatórios; qualquer smoke test real da OpenRouter precisa de autorização explícita.

## Testes de aceitação
- Candidata válida passa Python → DeepSeek → validação → risco → JEv e gera decisão com símbolo real; `SIM` executa apenas no paper se risco ainda permitir.
- Rejeição de uma hipótese faz a busca continuar para outra candidata/ativo, sem loop de `HOLD` e sem perder a cadência M1.
- Limite de API e timeout geram estado de falha explícito e backoff, sem travar o restante do scanner nem fingir uma decisão.
- Nenhuma candidata válida produz diagnóstico verificável, sem criar trades artificiais.
- `RESUMO` não ocupa linhas da lista de decisões individuais.
- Mudança em candle fechado é percebida mesmo quando hash do resumo histórico não mudou.
- Pausa, emergência, drawdown e exposição impedem novas entradas; posições abertas permanecem supervisionadas.
- GUI continua responsiva com 90 mercados e histórico incremental; nenhum envio de ordem real.
- Arquivos `test*.py` e scripts auxiliares de teste ficam em `testes/`; executar descoberta unittest, compileall e smoke offscreen; relatar contagens exatas sem somar suítes duplicadas.

## Entrega
Apresentar: (a) causa-raiz com arquivo/função/evidência; (b) fluxograma real antes/depois; (c) arquivos alterados; (d) métricas do funil e latência; (e) testes e resultados reais; (f) pendências de validação Windows/MT5/OpenRouter; (g) confirmação explícita de paper trading. Não fazer push sem autorização explícita nesta execução. Não afirmar que o bot será lucrativo ou que precisa operar a cada minuto.
