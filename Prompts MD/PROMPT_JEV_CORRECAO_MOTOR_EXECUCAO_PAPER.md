# JEv Bot — Correção assertiva do crash e execução efetiva de operações Paper Trading

## Missão
Auditar o código EXISTENTE, identificar com evidências a causa exata da interrupção do motor e o motivo pelo qual decisões não se transformam em operações de teste. Corrigir apenas o necessário, preservando arquitetura Python quantitativo → JEv → risco Python → executor PAPER. Não reintroduzir DeepSeek, não criar arquitetura paralela, não reescrever a aplicação.

## Evidências da tela (não presumir causas)
- Motor encerra com `unsupported format string passed to NoneType.__format__`.
- Ciclo 33, oportunidades e requisições JEv aparecem na interface.
- Painel JEv apresenta decisões `HOLD` e coluna `Estado API` como `PENDING` simultaneamente.
- Funil apresenta `paper 0` apesar de requisições/respostas e decisões registradas.
- Há histórico de trades paper anteriores; distinguir histórico persistido de execuções da sessão atual.
- Não é possível inferir pela imagem se o problema é ausência de BUY/SELL, bloqueio de risco, resposta incompleta, estado de fila, expiração do candle ou erro de execução. Provar qual é.

## 1. Corrigir a exceção fatal com causa raiz
1. Reproduzir ou localizar o traceback COMPLETO e a linha `arquivo:linha` que executa formatação numérica sobre `None` (`:.2f`, `:.4f`, `:.2%`, `format`, etc.). Identificar a origem do campo e quando ele fica ausente.
2. Corrigir no ponto apropriado. Para UI/log, exibir `N/D` ou `—` quando o valor estiver ausente; para matemática, preço, volume e risco, validar e rejeitar o dado ausente explicitamente, nunca transformar `None` em 0 silenciosamente.
3. Garantir isolamento por símbolo/evento: falhas recuperáveis registram `symbol`, `cycle_id`, `candidate_id`, traceback e motivo e não derrubam o loop principal. Falhas sistêmicas críticas devem colocar o motor em estado seguro, com mensagem clara.
4. Adicionar testes de regressão reproduzindo o mesmo `None` e conferindo que o motor continua no ciclo seguinte quando o erro é recuperável.

## 2. Provar por que não existem novas ordens Paper
Rastrear uma oportunidade válida de ponta a ponta com IDs correlacionados e timestamps:
`closed_m1_candle → python_candidate → quant_validated → jev_enqueue → jev_request → jev_response_parsed → BUY/SELL/HOLD → risk_check → paper_order_submit → paper_fill/reject → position_update → trade_history`.

Instrumentar contadores POR SESSÃO (separados dos acumulados/históricos):
- candidatos encontrados, elegíveis, enviados, respostas concluídas, BUY, SELL, HOLD, respostas inválidas, timeouts;
- rejeitados por spread, expectativa líquida, risco, limite de exposição, posição existente, candle expirado, fila cheia, deduplicação e outros motivos;
- ordens paper solicitadas, aceitas, executadas, rejeitadas, encerradas;
- mostrar os três motivos principais de não execução, com quantidade e símbolo de exemplo.

Localizar o PRIMEIRO estágio onde uma oportunidade BUY/SELL elegível desaparece e demonstrar com log sanitizado, arquivo/linha e correção mínima. `HOLD` não deve gerar ordem; se todas as respostas forem HOLD, `paper 0` pode ser correto. Não fabricar BUY/SELL, não converter HOLD em BUY, não afrouxar limiares quantitativos apenas para produzir trades.

## 3. Corrigir estados JEv e fluxo assíncrono
- `PENDING` somente enquanto a requisição estiver realmente pendente; após resposta válida, atualizar para `COMPLETED`/equivalente. Erros `401/429/5xx`, timeout, JSON inválido e cancelamento devem ter estados distintos, nunca mascarados como HOLD.
- Confirmar se a coluna `Decisão` está mostrando a resposta real do JEv ou um valor default de interface. Provar com request_id e resposta parseada, sem expor chave API.
- Confirmar que a decisão retorna ao orquestrador, com mesmo `run_id`, `symbol`, `candle_time` e `candidate_id`, e que o callback realmente chama a validação de risco e o executor paper.
- Garantir que processamento de resposta, atualização GUI, filas, locks e exceções não bloqueiem o ciclo M1.
- Descartar decisões de candles expirados com motivo explícito, não silenciosamente.

## 4. Execução Paper obrigatória quando todas as condições forem satisfeitas
- Para cada resposta real `BUY` ou `SELL` do JEv, com candidato ainda válido e aprovado pelo motor de risco Python, criar uma solicitação ao executor PAPER e persistir resultado e identificadores. Não deixar operação apenas na tabela visual.
- Simular fill usando bid/ask do MT5 com spread, custos, slippage configurável e regras existentes; registrar entrada, stop, alvo/saída, volume, P/L, expectativa prevista vs realizada, duração e motivo de saída. Não afirmar que simulação equivale à execução real.
- Confirmar consistência de unidades (pontos, pips, ticks, moeda da conta, tamanho de contrato, lotes), normalização por símbolo, limites de volume e precisão; evitar lookahead bias.
- Validar que limites de risco são aplicados pelo Python e revalidados imediatamente antes da ordem.
- **MODO EXCLUSIVAMENTE PAPER**: bloquear `mt5.order_send`, quaisquer endpoints de envio real, alterações de conta real e ativação de live trading. Usar MT5 somente para dados de mercado e metadados. Se houver interface para live, mantê-la inativa nesta tarefa.

## 5. Teste determinístico de execução ponta a ponta (sem custo API)
Criar teste de integração local com candles/quotes FIXTURES e JEv MOCK CONTROLADO:
A. Resposta BUY válida + risco aprovado → 1 ordem paper BUY, fill, posição e histórico.
B. Resposta SELL válida + risco aprovado → 1 ordem paper SELL.
C. HOLD → 0 ordens, status COMPLETED, motivo claro.
D. BUY/SELL + risco reprovado → 0 ordens, rejeição rastreável.
E. Candle expirado → 0 ordens, motivo STALE_CANDLE.
F. Erro de API, timeout e resposta malformada → 0 ordens, status correto.
G. `None` em campos de UI/log → motor não encerra; `None` em preço/risco → evento rejeitado de forma segura.
H. Duas respostas para o mesmo candidato → no máximo uma ordem (idempotência).
I. Persistência e restauração de posição/trade sem duplicação.
J. Teste de múltiplos símbolos, filas e atualização GUI sem congelamento.

O mock serve APENAS para demonstrar que o encadeamento técnico executa BUY/SELL. Não misturar operações de fixture com métricas de produção nem exibir decisões simuladas como reais. Se houver acesso ao MT5, executar diagnóstico de mercado SOMENTE LEITURA e registrar limitações; não exigir API paga para testes.

## 6. Validação matemática do Paper Trading
Exibir ou exportar, por operação, `candidate_id`, símbolo, candle de referência, probabilidade estimada, amostra, expectativa líquida estimada, spread e custos previstos, decisão real do JEv, preço bid/ask utilizado, volume, stop/alvo, risco monetário, fill simulado, P/L líquido e diferença previsto-realizado. Separar métricas in-sample/out-of-sample quando aplicável. Não declarar precisão ou rentabilidade com base em poucas operações.

## 7. Entrega obrigatória
1. Causa raiz do crash com traceback e `arquivo:linha`.
2. Primeiro ponto comprovado do funil em que BUY/SELL deixam de virar ordens, com números da sessão e exemplos.
3. Arquivos alterados e explicação curta de cada correção.
4. Resultado dos testes A–J, incluindo prova de pelo menos uma BUY e uma SELL **paper via mock**, sem ordens reais.
5. Evidência de que a UI distingue `PENDING`, `COMPLETED`, `ERROR` e `HOLD` corretamente.
6. Evidência de que o motor continua após evento recuperável e não trava em ciclo.
7. Limitações para testes com JEv real e dados MT5 reais; não alegar operação real ou chamada real sem prova.
8. Commit local com hash; não executar push sem autorização.

## Restrições finais
Não remover salvaguardas de risco; não alterar credenciais, `.env`, tokens ou provedores sem autorização; não introduzir DeepSeek; não ativar negociação real; não mascarar erros com HOLD; não inventar sinais; não modificar matemática para forçar trades; corrigir somente causas verificadas e adicionar testes de regressão.
