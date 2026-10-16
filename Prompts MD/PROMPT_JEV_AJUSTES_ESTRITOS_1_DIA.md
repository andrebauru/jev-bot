# JEv Bot — implementação estrita: 1 dia, evento quantitativo, envio JEv e interface

## Missão

Implemente **somente** as mudanças descritas neste documento no código existente do JEv Bot. Não refatore outros módulos, não introduza indicadores tradicionais, não reative DeepSeek, não altere credenciais, não execute ordens reais e não faça chamadas pagas ao OpenRouter durante testes automatizados. Preserve paper trading.

## Observações do vídeo anexado (20261014-1133-51.5914064.mp4)

A gravação mostra a tabela de seleção de mercados com caixas pequenas e colunas `Monitorar`/`Operar`, controles com pouco contraste e a área de atividades com altura útil pequena. No painel operacional aparecem mensagens de estado/resultado como `ANALISE`, `JEV_RESPONSE`, `JEV_AVOID` e `SEM_ELEGIBILIDADE`; também há listas de análises enviadas e decisões JEv. Portanto, **não presumir que nunca há envio**: investigar os bloqueios residuais por falta de informação, rejeições anteriores ao envio e eventuais decisões `JEV_AVOID` no código e logs reais. O vídeo não prova que a API foi chamada de fato: verificar os identificadores e logs HTTP sanitizados.

## 1. Histórico estritamente de 1 dia

- Substituir a janela operacional de 7/30 dias por **1 dia de candles M1 fechados**, aproximadamente 1.440 barras quando o mercado tiver negociação contínua. Não usar 43.200 candles como pré-requisito.
- Tratar fins de semana, feriados, pausas de mercado e símbolos com negociação intermitente: janela de 24 horas de tempo de mercado disponível ou máximo de 1.440 barras fechadas recentes, conforme comportamento correto do MT5 e disponibilidade; mostrar claramente a cobertura real e nunca fabricar candles.
- Baixar apenas o necessário; cache incremental para novos candles; invalidar cache ao trocar conta, servidor ou símbolo; não repetir downloads completos em cada ciclo.
- O cálculo deve operar com a quantidade real de barras disponíveis, registrando `bars_available`, `bars_used`, `last_closed_m1`, `coverage_minutes` e `data_quality`. Se não houver barras suficientes para **um cálculo matemático mínimo**, registrar erro de dados específico, sem fabricar análise.
- Remover gates operacionais que exigem 7/30 dias, OOS longo ou histórico persistido de padrões para enviar uma análise calculável ao JEv. Não representar amostras de um dia como validação estatística robusta.

## 2. Um único cálculo quantitativo/evento por ativo e candle

Eliminar o catálogo de famílias de padrões como pré-requisito do fluxo. Para cada ativo operável, calcular **um evento matemático simples, reproduzível e autocontido**, com prioridade para retração; quando a retração não existir, representar evento de gap ou movimento neutro/ausente sem inventar sinal.

### Evento principal: retração relativa

Usar somente candles M1 fechados, sem look-ahead:

1. Em uma janela recente configurável (ex.: 20 candles), obter máximo `H` e mínimo `L`, amplitude `A = H - L` e o fechamento `C`.
2. Se `A > 0`, calcular `posicao_no_range = (C - L) / A`, `retracao_da_maxima = (H - C) / A`, `recuperacao_do_minimo = (C - L) / A`.
3. Calcular `retorno_1m = C/C_prev - 1`, `retorno_5m = C/C_5 - 1`, `volatilidade` como desvio-padrão dos retornos de 1 minuto numa janela recente, e `spread_pct = (ask-bid)/((ask+bid)/2)` quando houver tick válido.
4. Classificar apenas o **evento descritivo** (`RETRACAO_ALTA`, `RECUPERACAO_BAIXA`, `MOVIMENTO_NEUTRO`), com regras simples documentadas, sem atribuir BUY/SELL como ordem.
5. Se houver descontinuidade entre fechamento anterior e abertura atual, registrar adicionalmente `gap_pct = (open_atual-close_anterior)/close_anterior` e `GAP_ALTA`/`GAP_BAIXA` como informação de mercado. Para Forex/cripto 24h, não inventar gap se não houver descontinuidade real.
6. Retornar **um único objeto de análise por símbolo/candle**, contendo todos os números disponíveis e campos `null` + motivo explícito para números indisponíveis. Não criar múltiplas hipóteses que consumam chamadas extras.

Não exigir `edge > 0`, OOS, taxa de acerto, padrões validados, `SEM_PADRAO` ou `SEM_ELEGIBILIDADE` para enviar uma análise com dados suficientes. Não inventar expectativa, probabilidade ou rentabilidade a partir de uma amostra insuficiente. Os números são observações, não recomendações de trade.

## 3. Enviar toda análise calculável ao JEv/OpenRouter

Fluxo obrigatório:

`MT5 → 1 dia M1 → 1 cálculo/evento Python → fila JEv → resposta BUY/SELL/HOLD → risco Python → PAPER`

- Assim que o cálculo de um ativo terminar, enfileirar o **resumo quantitativo válido** ao JEv, inclusive se o evento for neutro, o spread for alto ou a direção for incerta. Não esperar as outras três análises.
- O Python **não deve vetar o envio** por qualidade do sinal, expectativa, ausência de padrão, evento neutro ou decisão matemática própria. Ele só prepara dados e, após decisão JEv, aplica gerenciamento de risco e requisitos técnicos de execução segura.
- Não inventar dados ausentes. Diferenciar `MISSING_OPTIONAL_FIELD` (enviar com `null`) de `INVALID_CORE_DATA` (não é possível calcular nem identificar símbolo/candle/preço; registrar impedimento técnico).
- Investigar e remover gates indevidos relacionados a `SEM_ELEGIBILIDADE`, `JEV_AVOID`, `NO_MATHEMATICAL_EDGE`, `NO_SETUP`, `IN_SAMPLE_RATE`, `OOS_RATE`, `COST_EXCEEDS_GROSS_EDGE` quando usados **antes da chamada JEv**. `JEV_AVOID` pode permanecer somente se for **resultado real e identificável do próprio JEv**, nunca decisão silenciosa do Python.
- O payload deve conter símbolo, timestamp do candle fechado, timeframe, cobertura de histórico, OHLC recente/resumo, métricas de retração/gap, bid/ask/spread quando disponíveis, campos ausentes, limites de interpretação e um `request_id` único.
- No prompt do modelo OpenRouter, deixar explícito: **JEv decide BUY, SELL ou HOLD** com base no resumo, podendo escolher HOLD quando os dados não sustentam uma entrada; o Python não escolhe a direção nem substitui a decisão.
- Preservar fila limitada, deduplicação por `(symbol, candle_time, event_type)`, timeout, retry com backoff limitado para erros transitórios, controle de taxa e orçamento; nunca disparar solicitações ilimitadas a cada refresh da GUI. Registrar rejeição por fila cheia como erro técnico e política de reprocessamento sem duplicação.
- Confirmar na configuração qual modelo JEv da OpenRouter está efetivamente selecionado. A interface mostra referência legada a `deepseek/deepseek-chat`; **não confundir esse campo com o modelo JEv**. Não trocar automaticamente modelos ou chaves. Exibir claramente `Modelo JEv` e separar campos legados desativados.
- Erros HTTP, autenticação, rate limit, parse ou indisponibilidade devem ser exibidos como erros técnicos, nunca HOLD ou decisão do Python.

## 4. Gerenciamento de risco: único veto decisório do Python

Após BUY/SELL do JEv, Python valida somente: modo PAPER, posição/exposição, volume permitido, drawdown, risco por operação, spread máximo como limite de risco, cotação válida, símbolo negociável e validade temporal do candle/snapshot. Falha técnica de dados/conexão também impede execução insegura. Registrar `RISK_BLOCKED` com motivo específico. HOLD não abre posição. **Não aplicar novamente filtros de qualidade matemática para anular BUY/SELL.** Nunca habilitar ordens reais.

## 5. Manter até 4 ativos em cálculo paralelo

- Preservar pool de 4 workers (configurável dentro dos limites já existentes).
- MT5 pode continuar com acesso serial seguro, mas fetch deve ser incremental e não deixar trabalhadores sem dados desnecessariamente.
- Cada análise concluída vai imediatamente para a fila JEv; não esperar fechamento do ciclo inteiro.
- Percorrer todos os ativos selecionados com justiça, sem reiniciar a janela/ciclo antes de alcançar os pendentes; não analisar o mesmo símbolo/candle repetidamente.

## 6. Melhorias visuais estritamente solicitadas

### Resumo e listas

- Reorganizar textos longos do estado do robô, carteira, métricas e diagnóstico em **linhas compactas, alinhadas por rótulo/valor**, com quebras previsíveis e detalhes extensos em área recolhível ou tooltip.
- Reduzir o espaço vertical ocupado por mensagens e resumos para **aumentar substancialmente a altura útil da lista de atividades**.
- Manter painéis **lado a lado**: `Atividades/Cálculos Python` à esquerda e `Envios/Decisões JEv` à direita; ambos com barras de rolagem independentes, colunas redimensionáveis, texto legível, cabeçalhos fixos quando possível e QSplitter persistido.
- Não deixar textos de status ou logs gigantes empurrarem a lista para fora da área visível. Testar em 1366×768 e 1920×1080.
- Destacar visualmente `CALCULANDO`, `ENVIADO`, `AGUARDANDO_JEV`, `BUY`, `SELL`, `HOLD`, `ERRO_API`, `RISCO_BLOQUEADO` sem ocultar motivos.

### Seleção de ativos

- Aumentar tamanho visual e área clicável dos checkboxes `Monitorar` e `Operar` (incluindo DPI alto), melhorar contraste no tema escuro e espaçamento entre linhas.
- Aumentar largura mínima das colunas de checkbox, alinhamento central e cabeçalhos claros.
- Preservar seleção por categoria, filtro, marcar/desmarcar visíveis e persistência; não inverter a regra `Operar` implica `Monitorar`.
- Testar clique no quadrado e na área ampliada, teclado, scroll e escala de tela Windows 100%/125%/150%.

## 7. Evidências e testes obrigatórios

- Teste determinístico com **1.440 candles**: cálculo do evento de retração, evento de gap, neutro, spread ausente opcional, BUY/SELL/HOLD devolvidos por mock.
- Teste de regressão: evento neutro, expectativa negativa ou ausência de padrão **não bloqueiam envio ao JEv**.
- Teste de dados essenciais inválidos: não enviar payload fabricado; mostrar erro técnico.
- Teste de quatro cálculos simultâneos e despacho imediato sem esperar lote.
- Teste de deduplicação por candle, fila cheia, 401, 429, 500, timeout, parse inválido, candle vencido, risco e paper.
- Teste de interface offscreen + inspeção Windows quando possível, com foco em checkboxes e altura das listas.
- MT5 real somente leitura para confirmar download de um dia, processamento e montagem do payload. Não fazer chamadas pagas ao JEv sem autorização específica.
- Entregar relatório com: causa exata de bloqueio antes do JEv; arquivo/linha; payload sanitizado de um evento enviado ao JEv mockado; número de análises calculadas/enfileiradas; métricas de latência; testes; arquivos alterados; commit local. **Não fazer push sem autorização explícita.**

## Critério final de aceitação

**Com dados essenciais válidos, cada ativo/candle analisado gera exatamente um resumo quantitativo e esse resumo chega à fila JEv independentemente de ser favorável, desfavorável ou neutro. Somente o JEv decide BUY/SELL/HOLD. Python só veta abertura por gerenciamento de risco ou impedimento técnico de execução segura. Histórico de um dia, quatro análises concorrentes, dashboard compacto lado a lado e checkboxes legíveis.**
