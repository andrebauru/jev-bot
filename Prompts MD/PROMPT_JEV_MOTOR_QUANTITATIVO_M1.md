# JEv Bot — Melhoria assertiva do motor quantitativo M1 (sem DeepSeek)

## Missão
Corrigir o gargalo **antes do despacho ao JEv**: candidatos locais são gerados, mas não passam pela validação quantitativa em mercados reais. Não reestruture o sistema inteiro, não altere credenciais e não afrouxe arbitrariamente critérios de qualidade para fabricar trades. Trabalhe sobre o código atual, verificando os commits locais e o estado do repositório antes de qualquer alteração.

## Contexto comprovado no relatório anterior
- 90 símbolos monitorados e operáveis.
- Ciclos reais 33, 34 e 35: respectivamente 34/34, 38/38 e 12/12 candidatos rejeitados; zero oportunidades enviadas ao JEv.
- BTCUSD: 43.200 candles M1 fechados, nenhum candidato; ETHUSD: 2 candidatos rejeitados (OOS 12,42% e 13,01%, expectativa líquida após spread -0,1086% e -0,1095%); 1INCHUSD: nenhum setup no snapshot.
- 184 registros locais: 182 rejeitados e dois validados apenas para `TESTE`.
- Caminho mockado Python → JEv → paper comprovado, mas nenhuma chamada JEv real foi realizada.
- DeepSeek está desativado. Não reintroduzir DeepSeek, treinamento externo, indicadores tradicionais nem modelos adicionais.

## Arquitetura obrigatória
`MT5 M1 fechado → cache incremental → Python quantitativo (descoberta e validação) → despacho JEv imediato de candidato elegível → resposta tipada → Python (política, risco e revalidação de candle) → paper executor`.

JEv **somente decide** BUY/SELL/HOLD (ou SIM/NÃO/HOLD conforme contrato atual). Python calcula matemática, custos, posição, exposição, tamanho, gestão e execução. Uma oportunidade aprovada não espera o término da varredura dos outros símbolos. DeepSeek: zero chamadas e nenhuma dependência.

## Tarefa 1 — Auditoria causal, antes de modificar
1. Inspecionar `jev_bot/candidatos.py`, `jev_bot/padroes.py`, `jev_bot/orquestrador.py`, `jev_bot/markets.py`, cache, contratos e testes atuais; identificar thresholds e suas unidades, janelas, amostras e horizontes.
2. Rastrear por símbolo e por família: `candles_validos → setup_detectado → hipotese_gerada → treino → OOS → custos → elegivel → enfileirado → JEv`.
3. Reproduzir BTCUSD, ETHUSD, 1INCHUSD com MT5 read-only quando disponível, além de amostra diversificada de símbolos Forex e cripto. Separar ausência de sinal, falta de amostra, edge negativo e falha técnica.
4. Investigar bugs de escala de preços, casas decimais, `_Point`, `digits`, bid/ask, spread, timezone, timestamp, candle em formação, eventos sobrepostos, labels de horizonte, unidades de percentuais e retorno BUY/SELL.
5. Registrar evidências e o primeiro ponto de perda do funil. Não assumir que a ausência de trades é bug: confirmar.

## Tarefa 2 — Ampliar a descoberta matemática, sem indicadores clássicos
Implementar **famílias pequenas, testáveis e independentes** de setups M1, aproveitando a estrutura existente:
- **Aceleração/desaceleração** de retornos normalizados, com direção e horizonte explícitos.
- **Reversão à média empírica** após deslocamentos anormais em relação à distribuição recente.
- **Continuação** após impulso e pausa curta, com confirmação apenas por candles fechados.
- **Rompimento** de extremos de janela e teste de continuação/falso rompimento.
- **Recuperação/retração** após queda ou alta, usando magnitude e duração observadas.
- **Compressão/expansão de amplitude** quando a evidência suportar.

Usar aritmética de preços/retornos, quantis, distribuição empírica, duração, dispersão e probabilidades condicionais. **Não introduzir RSI, MACD, médias móveis como sinais clássicos nem dezenas de hiperparâmetros sem controle.** Cada hipótese precisa de `strategy_id`, `symbol`, `direction`, parâmetros, horizonte, candle de referência e identidade estável. Evitar duplicatas e explosão combinatória.

## Tarefa 3 — Adaptação por símbolo
- Substituir limites absolutos globais de movimento por medidas dimensionais coerentes: retornos percentuais/log, quantis e amplitude/volatilidade empírica recente, usando apenas passado disponível.
- Calibrar **por símbolo** e, quando houver amostra suficiente, por regime observado, sem misturar preço de BTCUSD com EURUSD.
- Usar spread **atual** relativo ao mid-price atual e as especificações MT5 do símbolo; detectar preço/tick ausente e bloquear, nunca substituir custo desconhecido por zero.
- Estimar comissão e slippage apenas quando houver fonte/configuração verificável; caso contrário, sinalizar `CUSTO_INCOMPLETO` e adotar política conservadora explícita (não afirmar expectativa líquida completa). Registrar análise de sensibilidade a custos adicionais plausíveis, sem inventar cobrança da corretora.
- Tratar símbolos com spreads elevados, iliquidez, sessões fechadas, gaps e especificações distintas.

## Tarefa 4 — Separar descoberta de validação
- **Descoberta:** gerar múltiplas hipóteses candidatas com baixo custo computacional, a partir de candles M1 fechados.
- **Validação:** separar treino e OOS cronologicamente (preservar OOS ≥30% vigente, se aplicável), purgar eventos/horizontes sobrepostos entre treino e teste, evitar vazamento temporal e reutilização indevida do mesmo OOS para escolher hiperparâmetros.
- Exigir amostra independente suficiente, estabilidade por subperíodos, expectativa após custos conhecidos, incerteza estatística e comparação com baseline simples; preservar os gates de segurança existentes salvo evidência documentada de bug.
- Controlar múltiplas hipóteses e seleção oportunista: holdout final intocado ou validação walk-forward com limites de pesquisa. Não otimizar indefinidamente no mesmo mês de dados.
- Uma hipótese rejeitada não impede avaliar outras famílias para o mesmo ativo; limitar orçamento por ativo e por ciclo para manter a cadência M1.
- Padrões validados expiram e são revalidados com janela móvel; não reutilizar validação sintética `TESTE` para ativos reais.

## Tarefa 5 — Funil e priorização justos
- Mostrar por família/ativo: gerados, amostra insuficiente, OOS reprovado, custo maior que edge, instabilidade, elegíveis, enviados ao JEv, decisões, veto de risco, paper aberto.
- Exibir **motivo tipado e números** em rejeições (ex.: `OOS_EDGE_NEGATIVE`, `INSUFFICIENT_EVENTS`, `COST_UNKNOWN`, `SPREAD_TOO_HIGH`, `UNSTABLE_PATTERN`, `NO_SETUP`). RESUMO não entra na lista de decisões JEv.
- Priorizar símbolos com **expectativa OOS líquida historicamente robusta**, ajustada por incerteza e recência, e oportunidades atuais; reservar cota de varredura para demais símbolos, evitando starvation e viés de seleção.
- Não confundir `90 ativos examinados` com `90 oportunidades`; nenhum envio ao JEv sem candidato válido. Não fabricar decisões ou trades.
- Atualizar GUI PySide6 com listas legíveis, espaço vertical, QSplitter persistente e atualização coalescida; evitar inundar GUI com logs por candle.

## Tarefa 6 — Latência e confiabilidade
- Ao validar um candidato, despachar imediatamente ao worker JEv limitado; não esperar os demais símbolos nem tarefas de aprendizado.
- Respostas atrasadas, candle vencido, fila cheia, HTTP/timeout/parse e configuração inválida recebem códigos próprios; nunca transformar falha em HOLD legítimo.
- Revalidar preços, candle e risco **antes** de paper OPENED; impedir duplicação de ordens e respostas fora de ordem.
- Carregar histórico de 30 dias incrementalmente, sem bloquear o primeiro ciclo por 90 downloads completos; permitir avaliação somente com amostra estatisticamente suficiente.
- Manter execução exclusivamente **paper**, sem qualquer ordem real ou alteração de credenciais. Nenhuma chamada paga em testes por padrão; uma chamada real requer autorização explícita.

## Tarefa 7 — Testes obrigatórios e evidências
1. Testes unitários de cada família com séries sintéticas positivas, negativas, ruído, preços muito distintos, spreads altos, amostras insuficientes, candle em formação e BUY/SELL.
2. Testes anti-lookahead, eventos não sobrepostos, OOS cronológico, controle de múltiplos testes, estabilidade e custo em unidade correta.
3. Teste E2E mockado: pelo menos uma oportunidade quantitativamente válida chega ao JEv imediatamente e uma inválida não chega; verificar decisão, veto, expiração e execução paper.
4. Regressão com amostra **real read-only MT5** (quando disponível): apresentar tabelas agregadas por família/ativo; comparar motor antigo vs novo com mesmo período e mesmos custos. Não afirmar que aumentou oportunidades sem evidência.
5. Benchmark: tempo de varredura, uso de CPU/RAM, cache hit/miss, idade do candle no despacho e ciclos 1→2→3 com 90 símbolos.
6. Testar falhas futuras: dados incompletos, bid/ask ausentes, símbolos fechados, ticks atrasados, MT5 desconectado, fila saturada, JEv offline, resposta tardia, duplicatas, erro de disco e JSON corrompido.
7. Executar `python -X utf8 -m unittest discover -s testes`, `python -m compileall -q jev_bot testes` e smoke GUI offscreen. Não alegar teste real de JEv se foi mockado.

## Critérios de aceite
- DeepSeek = 0 chamadas e 0 dependências no fluxo.
- Python gera e valida hipóteses M1 de múltiplas famílias com controles de sobreajuste, custos e OOS; resultados reproduzíveis.
- Candidato elegível → despacho JEv imediato e rastreável por `run_id`, `candidate_id`, `request_id`, `symbol`.
- Sem candidato elegível → motivos quantificados por família/ativo; sem falsos BUY/SELL/HOLD.
- GUI permanece responsiva, listas legíveis e ciclos avançam; paper e risco preservados.
- Sem relaxar limites só para aumentar a quantidade de operações; sem reestruturação ampla.

## Relatório final exigido
Entregar: (a) causas-raiz com arquivo/função/linha e evidência; (b) famílias implementadas e critérios; (c) comparação quantitativa antes/depois, por símbolo e por família, incluindo OOS e custos; (d) funil até JEv; (e) benchmarks e testes executados; (f) limitações e pendências; (g) arquivos modificados e commit local. **Não fazer push sem autorização explícita.** Não alterar `.env`, tokens, IDs ou credenciais. Não abrir ordens reais.
