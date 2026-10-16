# JEv Bot — Correção definitiva do fluxo, histórico M1 de 7 dias e dashboard lado a lado

## Missão

Implementar alterações mínimas, testáveis e localizadas no projeto existente. O usuário constatou no vídeo `20261014-0646-45.9982053.mp4` que o dashboard apresenta painéis horizontais estreitos, estados de rejeição e nenhuma análise chegando ao JEv. Não assumir que o vídeo prova a causa no código: auditar o pipeline e demonstrar o comportamento com telemetria e testes.

**Nova regra de responsabilidade:** Python calcula informações quantitativas e gerencia risco; **JEv, modelo configurado via OpenRouter, é o único responsável por escolher BUY, SELL ou HOLD**. Python não pode descartar análises por baixa taxa OOS, expectativa negativa, ausência de padrão, baixa confiança, payoff, spread elevado enquanto critério de oportunidade ou outro juízo sobre atratividade. Python pode impedir **execução** por risco determinístico ou impossibilidade técnica de segurança; jamais alterar ou fabricar a decisão do JEv.

## 1. Fluxo obrigatório

`MT5 → histórico M1 fechado de até 7 dias → cálculo matemático Python por símbolo (até 4 simultâneos) → envio de TODAS as análises concluídas ao JEv/OpenRouter → resposta estruturada BUY/SELL/HOLD → verificação de validade e gerenciamento de risco Python → paper trade ou recusa justificada`

- Enviar **todas** as análises completas, incluindo edge positivo, zero e negativo; BUY e SELL com métricas, sem pré-seleção quantitativa que bloqueie o envio.
- Não exigir padrões validados, repositório de padrões, `SEM_PADRAO`, `NO_SETUP`, `IN_SAMPLE_RATE`, `OOS_RATE`, `NEGATIVE_EXPECTANCY` ou `COST_EXCEEDS_GROSS_EDGE` como condição de despacho.
- Métricas de amostra, OOS, custo, incerteza e confiabilidade são **informações para o JEv**, não veto Python de oportunidade.
- Não fabricar análise quando faltam candles mínimos ou OHLC válido. Se os dados não permitem calcular, mostrar `DADOS_INSUFICIENTES` e retentar; distinguir de rejeição de estratégia. Evitar chamadas repetitivas sem novos dados.
- Despachar ao JEv assim que cada worker conclui; não aguardar outros símbolos nem o fim do ciclo. Um símbolo não pode ser despachado repetidamente para o mesmo candle/mesmo snapshot. Controlar backpressure, limite de fila, rate limits e orçamento de tokens; se fila cheia, manter estado `PENDENTE_ENVIO`/erro explícito, não marcar HOLD e não perder silenciosamente.
- JEv retorna decisão estruturada com `symbol`, `candle_time`, `decision` (BUY/SELL/HOLD), `reason`, `confidence` quando disponível e identificadores de correlação. Validar JSON/schema, modelo e origem. Erros 401/429/timeout/parse são erros técnicos, não HOLD.
- Não criar ordens reais; preservar **PAPER ONLY**.

## 2. Histórico: 1 semana, não 1 mês

- Trocar janela rolante padrão de 30 dias/43.200 candles para **7 dias corridos de M1 fechado** (até 10.080 candles por símbolo; pode haver menos por sessão/mercado fechado).
- Configuração clara `JEV_HISTORY_DAYS=7` (ou equivalente existente) e teto consistente em fetch, cache, indicadores estatísticos, treino/OOS, persistência, diagnósticos e GUI. Não deixar código antigo recarregar 30 dias escondido.
- Fazer carregamento inicial suficiente por símbolo e, depois, apenas atualização incremental. Não reconstruir tudo a cada minuto.
- Ajustar mínimos de amostra, divisão temporal e horizontes à janela de 7 dias **sem falsificar robustez estatística**. Quando amostra for fraca, enviar a incerteza ao JEv se ainda houver dados suficientes para calcular; se cálculo impossível, estado técnico explícito.
- Calcular somente com candles M1 **fechados**, sem vazamento do futuro, com relógio do servidor MT5 e tratamento de fim de semana, símbolos com horários distintos, timestamps e candles duplicados.

## 3. Concorrência e ciclos

- Manter pool de **4 cálculos matemáticos simultâneos** (configurável, máximo seguro existente).
- Não reiniciar, cancelar ou perder análises em voo ao mudar ciclo. A troca de ciclo não pode impedir que símbolos restantes sejam atendidos.
- Garantir justiça/fairness: os 90 ativos operáveis devem ser escalonados sem starvation. Registrar símbolos analisados/pendentes por janela e último candle processado.
- O fetch MT5 pode permanecer serializado se necessário à segurança da API; minimizar espera com cache incremental e pipeline contínuo.
- Separar fila de cálculo, fila JEv e respostas. Limitar concorrência/chamadas OpenRouter segundo configuração e orçamento; mostrar atrasos e pendências.

## 4. Python não decide atratividade

O Python **não pode** usar regras de expectativa, OOS, padrões, acerto, payoff ou spread como filtros para **não enviar análise ao JEv**. Deve fornecer os valores e deixar a decisão de operar ao JEv.

Após BUY/SELL do JEv, Python só pode bloquear o paper trade por controles objetivos de risco/segurança, por exemplo:

- limite de perda, drawdown, exposição, número de posições, volume permitido, saldo e margem paper;
- limite de spread como **política explícita de custo/risco de execução**, não filtro escondido de oportunidade;
- falta de preço bid/ask válido, mercado fechado, símbolo indisponível;
- resposta JEv inválida ou de candle expirado, preço alterado fora da tolerância definida, dados inconsistentes;
- limites operacionais de segurança e duplicação de ordem.

Registrar o bloqueio como `JEV_BUY_RISK_BLOCKED` / `JEV_SELL_RISK_BLOCKED`, preservando a decisão original. Não converter em HOLD. JEv HOLD nunca gera ordem. Python jamais inventa BUY/SELL.

**Observação:** enviar toda análise não significa abrir toda operação. Custos de API podem crescer com 90 símbolos × ciclos M1. Adicionar contador de chamadas/tokens/custo estimado e limites configuráveis de uso; respeitar restrições e não executar chamadas pagas nos testes sem autorização explícita.

## 5. Dashboard — painéis lado a lado

Refazer **apenas o layout**, sem reescrever o backend:

- Área principal em **duas colunas redimensionáveis** (`QSplitter` horizontal): esquerda `Cálculos Python` e direita `Análises enviadas / Decisões JEv`.
- Na esquerda, tabela com `Ativo | Candle | Status | BUY edge | SELL edge | Custo | Duração`.
- Na direita, tabela com `Ativo | Enviado em | Estado API | BUY/SELL/HOLD | Motivo | Latência | Risco/Paper`.
- Uma linha de cálculo deve ser correlacionável visualmente à sua linha JEv via `analysis_id` e `request_id`.
- Abaixo, painel de **histórico paper** e diagnóstico compactos, com rolagem independente. Não deixar tabelas de 1 linha ocuparem a tela inteira.
- Cabeçalhos legíveis, largura mínima por coluna, elisão/tooltip de textos longos, alinhamento numérico, estados com cores discretas, ordenação por atualização recente.
- Persistir proporções via QSettings; suportar 1366×768 e 1920×1080; sem travar UI; sem `RESUMO` como decisão.
- Resumo superior: `Ativos operáveis | Cálculos ativos 0–4 | Concluídos | Aguardando JEv | Enviados | BUY | SELL | HOLD | Erros API | Bloqueios de risco | Paper`.

## 6. Instrumentação obrigatória

Por símbolo e candle, registrar: `history_ready`, `math_started`, `math_completed`, `jev_pending`, `jev_enqueued`, `jev_request_started`, `jev_response`, `jev_decision`, `risk_checked`, `paper_opened` / `risk_blocked`, além de `technical_error`. Incluir `run_id`, `analysis_id`, `request_id`, timestamp, idade do candle, latência por etapa, quantidade de candles e motivos. Não exibir tokens, credenciais nem conteúdo sensível nos logs.

**Invariante testável:** para cada `math_completed` com dados calculáveis, deve existir exatamente um envio ao JEv ou um estado de pendência/erro de transporte explícito, nunca rejeição por qualidade estatística.

## 7. Testes de regressão e aceitação

- Cenários com edge positivo, negativo, zero, OOS ruim, spread alto e sem padrões persistidos: **todos devem alcançar o JEv mockado** se houver dados calculáveis.
- OOS ruim e expectativa negativa **não** impedem envio; aparecem no payload.
- JEv BUY, SELL, HOLD: Python respeita decisão, somente risco pode bloquear execução; risco bloqueado preserva decisão.
- Candle vencido antes de executar não vira HOLD; não há ordem.
- 4 workers matemáticos simultâneos, quinto aguardando; envio de um resultado sem aguardar os demais.
- 90 símbolos em múltiplos ciclos, sem perder símbolos na mudança de janela.
- 7 dias, não 30; verificar chamadas MT5, cache e GUI.
- HTTP 401/429/500, timeout, JSON inválido, fila cheia, backpressure, reinício, deduplicação e cancelamento seguro.
- Teste de GUI offscreen, responsividade, layout duas colunas, persistência splitter.
- `deepseek_calls=0`; sem dependência operacional de padrões.
- Todos os testes sem chamadas pagas ao JEv e sem ordens reais; smoke MT5 apenas leitura, se disponível.

## 8. Relatório final

Informar: causa real do não despacho; arquivos/linhas modificados; prints ou evidências do dashboard lado a lado; histórico M1 comprovadamente limitado a 7 dias; contagem de análises calculadas e enviadas (inclusive negativas); rastreio de um símbolo até o JEv mockado; decisões e bloqueios de risco separados; testes, métricas e limitações. Não afirmar chamada real ao JEv se foi mock. Fazer commit local, **não dar push sem autorização explícita**. Não modificar credenciais, `.env` nem habilitar ordens reais.
