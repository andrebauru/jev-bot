# JEv Bot — Auditoria quantitativa de rentabilidade e execução M1

## Missão

Investigue, meça e corrija **o menor conjunto de causas comprovadas** que impede o motor Python de produzir oportunidades economicamente justificadas e enviá-las ao JEv no scalping M1. **Não force operações**, não relaxe limiares sem evidência e não reestruture o projeto inteiro. Trabalhe a partir do estado real do repositório, atualmente reportado em `7c1da35` (`main`/`origin/main`); confirme o HEAD antes de começar.

**Arquitetura obrigatória:** MT5 somente leitura → Python quantitativo local → validação cronológica e econômica → envio imediato de oportunidade elegível ao JEv → resposta tipada BUY/SELL/HOLD → Python revalida preço, candle, exposição e risco → executor exclusivamente **paper trading**. DeepSeek permanece totalmente desativado (`deepseek_calls=0`). O JEv não descobre padrões, não calcula risco, não define tamanho de posição e não gerencia ordens.

## Evidências conhecidas a confirmar, não presumir

- Seleção reportada: 90 símbolos marcados para operar.
- Snapshot real MT5 anterior: BTCUSD sem setup; ETHUSD com candidato rejeitado (OOS ~13,14%; expectativa após spread ~−0,1102%); 1INCHUSD com candidato rejeitado (OOS ~0,15%; spread ~0,9293%). Nenhum elegível nos três; ciclos 33–35 tiveram 34/34, 38/38, 12/12 rejeições.
- `214` testes reportados como aprovados; chamadas reais ao JEv não verificadas; comissão e slippage desconhecidos.
- Benchmark reportado: cache JSON hit ~0,407 s vs miss ~0,08 s; comparar caminhos equivalentes antes de concluir.
- O último commit reportado é `7c1da35`; não assumir que arquivos, funções ou resultados permanecem idênticos.

## Etapa 1 — Auditoria causal sem alteração prematura

1. Mapear as funções reais em `jev_bot/candidatos.py`, `padroes.py`, `orquestrador.py`, `markets.py`, `jev.py`, cache e executor paper. Registrar linha/função, entradas, saídas e filtros.
2. Para cada símbolo operável amostrado, registrar funil: candles válidos → setups ativos → hipóteses → treino → OOS → custos → elegibilidade → fila JEv → resposta → veto de risco → paper. Distinguir **sem sinal**, **sinal sem edge**, **erro técnico** e **sinal válido expirado**.
3. Identificar definições exatas de `acerto`, retorno, horizonte, preço de entrada, base de cálculo BUY/SELL, taxas mínimas, Wilson, estabilidade, tamanho mínimo de amostra e múltiplas hipóteses. Não tratar hit rate baixo como prova isolada de inviabilidade.
4. Verificar se há erro de sinal, spread contado duas vezes, preço de entrada inconsistente, corte temporal vazando horizonte, candle em formação, sobreposição de eventos, custo por direção e viés de seleção.
5. Produzir um diagnóstico antes de editar: hipótese, evidência, impacto e correção mínima proposta.

## Etapa 2 — Economia da operação, não apenas direção do candle

Implementar ou corrigir avaliação **trade-level** reproduzível para cada família de setup:

- Entrada executável e coerente com MT5: BUY no ask, SELL no bid, saída pelo lado oposto, usando apenas cotações disponíveis no instante simulado; se só houver OHLC M1, explicitar aproximação e incerteza. Nunca usar spread futuro ou mid como preço executável.
- Estratégias com saída definida: stop, alvo, horizonte máximo, saída por tempo e eventual invalidação, sempre determinados antes do resultado; distinguir cenário em que stop e alvo ocorrem na mesma barra (assumir pior caso ou marcar ambíguo, nunca escolher o favorável).
- Reportar `win_rate`, payoff médio, perda média, profit factor, retorno esperado líquido por trade, distribuição/quantis, duração, drawdown, frequência de sinais e exposição, não apenas acertos direcionais.
- Modelar custo em camadas: **spread observado**, **comissão conhecida se houver fonte confiável**, **slippage por cenários conservadores configuráveis** (ex.: 0, 0,5 e 1,0 spread adicional, claramente identificados como hipóteses, não como medição). Quando comissão desconhecida, mostrar `NAO_ESTIMADA` e não promover uma oportunidade como comprovadamente rentável sem testar cenários robustos.
- Comparar resultados BUY e SELL de modo simétrico e unidades corretas (%/bps/pontos/moeda). Incluir testes para ativos com preços e casas decimais muito diferentes.
- Não usar Kelly, alavancagem adicional ou ajustes automáticos de risco por confiança do modelo sem calibração externa comprovada.

## Etapa 3 — Pesquisa quantitativa controlada

1. Preservar famílias já implementadas (aceleração, reversão por quantis, rompimento, impulso/pausa, compressão/expansão) e avaliar o baseline sem mudar parâmetros primeiro.
2. Usar 30 dias de M1 fechados como janela rolante; dividir treino, seleção de hipóteses e teste final OOS **cronologicamente**, purgando eventos cujo horizonte cruza cortes. Preferir walk-forward em múltiplos blocos temporais; documentar quando a amostra for insuficiente.
3. Avaliar diferentes stops, alvos e horizontes **somente no treino/validação interna**; congelar regras antes do OOS final. Limitar orçamento de busca e corrigir múltiplas comparações. Não escolher parâmetros olhando o OOS.
4. Comparar estratégias por expectativa líquida conservadora, robustez por subperíodo, intervalo de confiança, frequência útil e estabilidade por símbolo. Uma estratégia com baixa taxa de acerto pode ser válida se payoff líquido justificar; uma com alta taxa pode ser perdedora.
5. Rodar avaliação somente leitura com **amostra diversificada de símbolos operáveis** (forex, cripto e demais classes disponíveis) e, se viável dentro de prazo razoável, todos os 90 em lotes com cancelamento e progresso. Não se limitar a BTCUSD/ETHUSD/1INCHUSD. Registrar indisponibilidades.
6. Comparar `baseline` versus `motor revisado` no **mesmo snapshot, símbolos, dados e custos**, com métricas e diferenças; se não houver melhora verificável, manter baseline e explicar.
7. Priorização por potencial validado e custo de processamento, com justiça para todos os símbolos; não usar OOS final para selecionar símbolos e depois alegar desempenho OOS independente.

## Etapa 4 — Performance, cache e prazo M1

- Instrumentar `fetch MT5`, atualização incremental, desserialização, cálculo de features, geração de candidatos, validação, enfileiramento e latência JEv; reportar p50/p95 e volume de dados.
- Reproduzir e explicar o benchmark `cache hit ~0,407s` vs `miss ~0,08s` com **caminhos equivalentes**, cache quente/frio, mesmas amostras e sem I/O oculto; corrigir apenas gargalo comprovado. Considerar formato binário/SQLite/NumPy somente se houver ganho medido e migração segura; evitar nova infraestrutura desnecessária.
- Atualizar histórico por delta, não reler 43.200 candles por símbolo em todo ciclo quando desnecessário. Manter janela rolante e dados fechados.
- O scanner deve continuar atendendo candles M1; processamento pesado e benchmark não podem bloquear UI, agendador ou decisões. Limitar memória e concorrência, permitir cancelamento e descartar respostas vencidas.
- Assim que uma oportunidade elegível for confirmada, despachar ao JEv **sem esperar o restante dos símbolos**. Deduplicar por símbolo/candle/estratégia; garantir limites de fila e tratamento explícito de backpressure.

## Etapa 5 — GUI objetiva e telemetria

Preservar PySide6, `QSplitter` redimensionável e tamanhos persistidos. Exibir por ativo e estratégia: sinal, treino/OOS, expectativa líquida sob cada cenário de custo, amostra, motivo de rejeição, horário do candle e estado do envio JEv. Separar claramente:

- **Oportunidades — Python Quantitativo** (inclui rejeições e motivos, sem poluir a lista JEv);
- **Decisões JEv** (somente respostas reais ou mockadas explicitamente identificadas, com símbolo, BUY/SELL/HOLD, latência, request_id; erros não são HOLD);
- **Operações paper** e **Resumo compacto** (totais e tempos por etapa).

Sem listas verticais espremidas; manter responsividade em 1366×768 e 1920×1080. Não criar decisão artificial para preencher painel.

## Etapa 6 — Testes obrigatórios e prevenção de regressões

Executar suíte existente e acrescentar testes direcionados para:

1. BUY/SELL com bid/ask corretos, spread e unidades; spread extremo; preço ausente; slippage e comissão desconhecida.
2. Stop e alvo atingidos na mesma barra, gaps, timeout, posições simultâneas e horizonte expirado.
3. Baixa taxa de acerto com payoff positivo; alta taxa com expectativa negativa; OOS sem lookahead; purga temporal; múltiplas hipóteses; robustez entre regimes.
4. Cache incremental e integridade de janela; comparação de benchmark justa; MT5 lento, desconectado e com candles faltantes.
5. Oportunidade válida → JEv enfileirado imediatamente → resposta → revalidação → paper; candidato inválido nunca enviado.
6. JEv lento, HTTP inválido, indisponível, fila cheia, retorno fora de ordem, resposta duplicada e candle expirado; erro técnico nunca vira decisão HOLD/NAO.
7. Risco determinístico: exposição total, número de posições, drawdown, emergência, pausa e nenhuma ordem real.
8. DeepSeek com **zero chamadas**, inclusive com configurações antigas presentes; testes nunca podem usar API paga sem autorização.
9. GUI offscreen, estados, contadores, listas redimensionáveis e ciclos 1→2→3 sob carga.

Usar mocks/fakes por padrão. MT5 real apenas **leitura** e com relatório separado. Não executar chamada real ao JEv nem enviar ordens reais. Se uma chamada paga de diagnóstico for indispensável, **pedir autorização antes**.

## Entrega e critérios de aceite

Fornecer relatório contendo: (a) causas-raiz comprovadas com arquivos/linhas; (b) baseline e comparação no mesmo dataset; (c) tabela por classe/símbolo/estratégia, com n, OOS, expectativa líquida e custos; (d) funil até JEv; (e) benchmark p50/p95 e gargalos; (f) alterações mínimas e arquivos; (g) testes exatos e resultados; (h) limitações reais, inclusive custo desconhecido; (i) plano de verificação manual Windows/MT5; (j) commit local e status Git.

**Aceite funcional:** em cenários sintéticos com edge demonstrável, oportunidades validadas devem chegar imediatamente ao JEv e permitir decisão paper; em dados reais sem edge, mostrar rejeições claras e justificadas. Nenhuma promessa de número mínimo de trades ou lucro. DeepSeek desligado; JEv somente decide; Python controla risco; somente paper trading.

**Controle de escopo:** corrigir primeiro defeitos demonstrados; não reescrever a aplicação, não alterar credenciais, `.env`, tokens, permissões, integrações ou ativar modo real. **Não fazer push sem autorização explícita nesta execução.**
