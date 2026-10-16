# MISSÃO — JEv Bot: corrigir travamento no Ciclo 1, simplificar e entregar GUI funcional

## Contexto e prioridade
O projeto JEv Bot (Python/PySide6, MT5, OpenRouter/DeepSeek e JEv) foi ampliado além do objetivo inicial de construir uma interface gráfica funcional. No vídeo de execução, o painel permanece em **Ciclo 1** enquanto alterna AGUARDANDO/ANALISANDO e publica eventos. A área JEv não mostra decisões úteis por ativo. **Não presumir a causa pelo vídeo**: investigar código e logs atuais e reproduzir. Commit local reportado `e51ccc8` na `main`; não assumir que o workspace está exatamente nesse commit.

**Prioridade absoluta:** corrigir o ciclo de vida, entregar um painel responsivo e um fluxo operacional simples, observável e testável. Não redesenhar a arquitetura, não acrescentar features cosméticas, não introduzir bibliotecas/frameworks desnecessários. Preferir patches mínimos com evidência.

## Restrições inegociáveis
- Somente **paper trading**; não implementar, habilitar nem enviar ordens reais.
- M1 com **candles fechados**; janela histórica móvel de até 30 dias; sem indicadores técnicos tradicionais.
- Python: aquisição de dados, matemática, identificação de candidatos, regras determinísticas de risco, limites de posições, tamanho e execução paper.
- JEv: somente decisão estruturada sobre oportunidade elegível (SIM/NÃO/HOLD ou BUY/SELL/HOLD conforme contrato), identificada por símbolo, timestamp e ID. Não gerencia risco, não aprende e não executa.
- DeepSeek: aprendizado e revalidação **auxiliares**; nunca bloqueia a varredura nem a decisão operacional. Não obrigar padrão recém-aprendido para cada decisão; não inventar edge para forçar operações.
- Não expor chaves, senhas ou dados pessoais nos logs; não fazer chamadas pagas em testes; sem push sem autorização explícita.

## Fase 0 — diagnóstico reproduzível ANTES de editar
1. Inspecionar `orquestrador.py`, `candidatos.py`, `padroes.py`, `jev.py`, GUI, timers, threads, filas e testes existentes. Localizar a origem do texto `Ciclo 1` e todas as alterações de `ciclo_id`, `estado`, `_ciclo`, `_aguardar_slot`, `_varrer_e_processar`, `_atualizar_cache`, `fila_deepseek`, deadlines e cancelamento.
2. Instrumentar temporariamente eventos correlacionados: `cycle_id`, `run_id`, `phase`, `symbol`, `started_at_monotonic`, `elapsed_ms`, `deadline_ms`, `queue_depth`, `thread_id`, `reason_code`; logs rotativos e sanitizados.
3. Reproduzir a falha com fake MT5 e DeepSeek/JEv mockados: 90 símbolos, chamadas que demoram, timeouts, exceções, fila cheia, pausa/retomada e emergência. Distinguir **contador visual desatualizado**, **ciclo que nunca encerra**, **ciclo reentrante**, **loop de espera**, **trabalho pós-varredura que bloqueia**, **starvation**, **sinal Qt não entregue**.
4. Relatar a causa-raiz com arquivo, função, linha, sequência de eventos e teste que falha antes do patch. Se não reproduzível, declarar isso e registrar as evidências sem inventar causa.

## Fase 1 — corrigir a máquina de estados e o agendamento
- Definir transições explícitas: `PRONTO → ANALISANDO → FINALIZANDO → AGUARDANDO → ANALISANDO`; PAUSADO, BLOQUEADO_RISCO e EMERGÊNCIA são estados separados, com regras claras.
- Cada varredura recebe um `cycle_id` monotônico e um `run_id` único; o contador deve representar **varreduras operacionais**, não tarefas DeepSeek. Incrementar **somente ao iniciar uma nova varredura real**.
- Garantir encerramento do ciclo com `try/finally` e resultado `CONCLUIDO`, `PARCIAL`, `TIMEOUT`, `CANCELADO` ou `ERRO`, com motivo e métricas; nunca ficar em fase intermediária sem deadline.
- Tempo máximo configurável por varredura; ao estourar, interromper o agendamento de novos símbolos, registrar pendentes e avançar para a próxima janela M1. Não usar `sleep` bloqueante no thread da GUI. Não supor que uma thread Python bloqueada em rede pode ser terminada à força: usar timeouts reais, isolamento e ignorar resultados tardios.
- Uma única varredura ativa por vez. Evitar reentrada, deadlocks e concorrência no cache/estado. Uma resposta de ciclo antigo não pode modificar o estado, o preço de referência ou executar paper no ciclo atual.
- Agendar próximo ciclo pela **próxima vela M1 fechada** (ou política explícita configurada), sem acúmulo infinito de ticks. Quando existir posição aberta, preservar supervisão e regras já definidas sem impedir o restante do sistema.
- Fila DeepSeek limitada, deduplicada e com TTL/validade; processar em worker separado ou fatias não bloqueantes, com cancelamento lógico, deadline e prioridade inferior. **Mover para depois da varredura na mesma thread não é assíncrono**; verificar e corrigir isso.
- PAUSAR, RETOMAR, CANCELAR CICLO e EMERGÊNCIA devem responder rapidamente e ter efeitos comprovados. Emergência veta novas operações e preserva supervisão segura.

## Fase 2 — reduzir a complexidade operacional
- Traçar o caminho mínimo: `M1 fechado → candidato local → validação determinística → JEv → gate Python → paper → GUI`.
- Não exigir nova chamada DeepSeek antes de cada decisão. Usar memória de padrões já validados quando pertinente, sem bloquear a busca por outros candidatos locais fundamentados.
- Registrar funil por ciclo: símbolos elegíveis, símbolos varridos, dados inválidos, candidatos, rejeições matemáticas, enviados ao JEv, SIM/NÃO/HOLD, vetos de risco, execuções paper, tempo por etapa e chamadas API.
- Para cada símbolo processado, publicar um resultado tipado e identificável (`symbol`, `cycle_id`, `timestamp`, `status`, `reason_code`). `RESUMO` deve ser evento separado e nunca substituir a lista de decisões JEv.
- `HOLD` é uma decisão válida e pontual, **não um estado de espera**: registrar, liberar o símbolo e prosseguir. `NÃO` idem. `SIM` só segue para paper após revalidação de preço, dados e risco. Em erro ou timeout, registrar `SEM_DECISAO` (não inventar HOLD).
- Evitar chamadas repetidas para mesmo snapshot; preservar limites de custo da OpenRouter e latência. Medir latência real e descartar respostas obsoletas.
- Não afrouxar validação estatística para gerar trades; distinguir claramente **zero trades legítimos** de **zero decisões por falha de pipeline**.

## Fase 3 — GUI: objetivo original, simples e utilizável
- Priorizar uma janela PySide6 estável com controles **INICIAR, PAUSAR/RETOMAR, CANCELAR CICLO, EMERGÊNCIA**, indicador do estado real, contador de ciclos, próximo candle M1, símbolo atual, tempo de ciclo, fila DeepSeek, status MT5/JEv e modo PAPER.
- Painéis separados e leves: **Oportunidades por ativo**, **Decisões JEv por ativo**, **Execuções paper/posições**, **Aprendizado DeepSeek**, **Resumo/diagnóstico**. Nenhuma linha genérica `RESUMO` na tabela de decisões.
- Não reconstruir widgets nem despejar centenas de sinais por candle; usar sinais Qt thread-safe, atualização coalescida e limites de linhas. Garantir que a GUI continue responsiva durante download histórico e chamadas de API.
- Exibir o motivo quando não há decisão: `SEM_SETUP`, `SEM_DADOS`, `REJEITADO_MATEMATICA`, `BLOQUEADO_RISCO`, `TIMEOUT_JEV`, `API_INDISPONIVEL` etc. Mostrar **última decisão por ativo** e idade da decisão, sem falsificar atividade.
- Não adicionar gráficos/efeitos novos até corrigir o ciclo e as decisões.

## Fase 4 — auditorias objetivas adicionais
1. **Conectividade:** MT5 desconectado, dados vazios, símbolos indisponíveis, horário e fuso, candle em formação, gaps e cache incremental.
2. **Concorrência:** locks, eventos Qt, fila DeepSeek, chamadas externas, races de pausa/cancelamento, resposta atrasada, integridade JSON e gravação atômica.
3. **Risco e paper:** posição duplicada, exposição total, max drawdown, spread/slippage/custos, risco antes e imediatamente antes da execução, nunca enviar ordem real.
4. **Custo/latência:** número de chamadas, timeout e retries efetivos, tamanho dos payloads, TTL, dedup e limites de fila; custo da API não deve ser confundido com tempo de cálculo local.
5. **Confiabilidade:** erros silenciosos, exceções engolidas, estados persistidos, logs sanitizados, reinício após falha, consumo de memória/CPU e limpeza de recursos.
6. **Qualidade dos testes:** mocks anti-chamada paga, cenários E2E e compatibilidade com o ambiente Windows/MT5/PySide6.

## Critérios de aceitação obrigatórios (não encerrar sem evidências)
- Teste automatizado executa **Ciclo 1 → Ciclo 2 → Ciclo 3**, com IDs crescentes, estados coerentes, fim de cada ciclo e GUI atualizada; sem travar.
- Repetir o teste com DeepSeek demorando **mais que dois períodos M1**: ciclos operacionais continuam e fila permanece limitada; respostas antigas não interferem.
- Repetir com JEv lento/timeout: ciclo finaliza com `SEM_DECISAO` e próximo ciclo ocorre; nenhuma ordem é executada com resposta tardia.
- 90 símbolos, cache hit/miss, exceções MT5, PAUSAR/RETOMAR, CANCELAR e EMERGÊNCIA sem congelar GUI.
- Teste E2E com candidato sintético validado: JEv SIM → gate → paper; JEv NÃO/HOLD → sem paper; risco VETO → sem paper; cada evento mostra símbolo.
- Teste sem candidato: encerra o ciclo, registra `SEM_SETUP` e **avança para o próximo**, sem loop preso.
- Medir tempo máximo por ciclo, p50/p95, profundidade da fila, eventos GUI/segundo, chamadas API por ciclo e memória; comparar antes/depois quando possível.
- Rodar `python -X utf8 -m unittest discover -s testes`, `python -m compileall -q jev_bot testes` e GUI offscreen; testes de API sempre mockados.
- Confirmar que nenhum caminho de execução real foi criado/acionado.

## Entrega exigida
Entregar relatório curto e verificável: (a) causas-raiz e evidências, (b) arquivos/linhas modificados, (c) fluxo simplificado, (d) logs de **três ciclos completos** e um cenário de timeout, (e) testes e tempos medidos, (f) riscos restantes e passos exatos de smoke test Windows, (g) confirmação de paper-only. Não dizer que corrigiu sem demonstrar os três ciclos. Não realizar push sem autorização explícita; informar hash do commit local se criado. **Evitar refatoração generalizada**: corrigir primeiro o defeito, só depois problemas adjacentes demonstrados.
