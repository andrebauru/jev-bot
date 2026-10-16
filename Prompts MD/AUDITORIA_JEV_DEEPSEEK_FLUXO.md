# PROMPT — AUDITORIA DO APRENDIZADO DEEPSEEK E FLUXO DEEPSEEK → JEV

## Missão
Auditar e corrigir **o projeto JEv Bot existente**, no estado atual da branch `main` (referência informada: commit `7309aa0`), com foco em descoberta matemática de padrões, aprendizado persistido, validação independente e entrega das oportunidades ao JEv. **Primeiro leia o código e os testes reais; não presuma que o relatório anterior corresponde exatamente à árvore atual.** Corrija causas-raiz com alterações pequenas e comprovadas, preservando o desempenho, a interface PySide6, o cache incremental M1, a seleção de mercados, o autostart e os controles de risco.

## Restrições inegociáveis
- **Exclusivamente paper trading.** Não implementar, ativar ou enviar ordens reais.
- **Somente candles M1 fechados**, dentro da **janela móvel dos últimos 30 dias**. Respeitar dias sem negociação, lacunas e timezone. Nunca usar candles futuros nem o candle em formação para validar previsões.
- **Não usar indicadores técnicos tradicionais** (RSI, MACD etc.). Analisar movimentos percentuais, recuperações, retrações, duração, distribuição de retornos e condições estatísticas verificáveis.
- O **DeepSeek descobre hipóteses e interpreta evidências**; o **Python executa os cálculos e valida matematicamente**; o **JEv decide SIM/NÃO** após receber uma oportunidade validada. Não permitir que o modelo invente números, altere amostras ou marque sozinho uma hipótese como validada.
- O DeepSeek trabalha autonomamente, sem pedido de análise pelo JEv. Sem posição aberta: cadência-alvo de 1 minuto; com posição aberta: 15 minutos. Orçamento de processamento e continuidade justa entre símbolos permanecem ativos.
- Preservar travas de risco, emergência, pausa, supervisão de posições abertas, logs com segredos mascarados, limites de memória e GUI responsiva.
- **Nenhum teste automatizado pode realizar chamadas pagas à OpenRouter**. Usar mocks por padrão; testes reais somente por comando explícito, com consentimento e limites de custo.

## 1. Auditoria ponta a ponta
Rastreie com arquivos/funções reais: seleção de símbolos → download/cache M1 → motor matemático → payload enviado ao DeepSeek → resposta estruturada → verificação local → persistência de padrões → elegibilidade da oportunidade → fila/contrato de decisão → JEv → gate de risco → paper trader → GUI/logs. Registre `analysis_id`, símbolo, candle de referência e `pattern_id` em cada transição. Identifique filtros que descartam oportunidades silenciosamente, campos incompatíveis, retornos prematuros, filas sem consumidor, erros engolidos e confusão entre `sem oportunidade` e `falha de integração`.

## 2. Descoberta e qualidade matemática
- O Python deve preparar estatísticas reproduzíveis de movimentos de preço, sem desperdiçar tokens com 43.200 candles brutos por símbolo.
- Investigar padrões como: após queda de X%, recuperação de Y% em N candles; após alta de X%, retração de Y% antes de certo horizonte; e outras hipóteses propostas pelo DeepSeek, desde que representáveis em regras matemáticas auditáveis.
- Definir precisamente referência de preço, janela temporal, evento, sucesso, horizonte, custos simulados, contagem de amostras e tratamento de eventos sobrepostos.
- Validar em sequência temporal, separando descoberta/treino e teste fora da amostra **dentro dos mesmos 30 dias**, sem vazamento de futuro; evitar otimização excessiva por múltiplas hipóteses. Reportar `n`, taxa de acerto, expectativa líquida, dispersão, intervalo de incerteza e estabilidade por subperíodo quando aplicável.
- Não inventar limiares de aprovação nem afrouxar os existentes para forçar trades. Se amostra insuficiente ou evidência fraca, marcar `INSUFICIENTE`/`REJEITADO` com motivo verificável.
- Reexecutar o padrão em Python sobre os candles e comparar números calculados; respostas inconsistentes ou JSON inválido não podem prosseguir.

## 3. Memória de padrões por ativo
Auditar a estrutura atual antes de alterá-la. Persistir de modo atômico, por símbolo e identificador estável, hipóteses, parâmetros, versão do algoritmo, métricas verificadas, período exato dos dados, amostras, data de atualização, status (`CANDIDATO`, `VALIDADO`, `REJEITADO`, `EXPIRADO`) e motivo. JSON é aceitável; reutilizar a solução já existente quando possível. Ao reencontrar o símbolo, consultar a memória e revalidar com dados atuais; **padrão antigo não é evidência atual**. Expirar ou desativar padrões fora da janela móvel. Evitar duplicatas, corrupção por escrita concorrente e crescimento ilimitado.

## 4. Contrato DeepSeek → JEv
Definir e testar um payload versionado, validado por esquema, contendo no mínimo:
`schema_version`, `analysis_id`, `symbol`, `timeframe=M1`, `window_start`, `window_end`, `pattern_id`, `hypothesis`, `validation_status`, `sample_size`, `out_of_sample_metrics`, `expected_net_return`, `direction`, `reference_price`, `expiry`, `risk_context`, `evidence_hash`.
- Somente oportunidade **validada, vigente e elegível** chega à decisão de entrada do JEv.
- O JEv recebe evidências verificadas e retorna **SIM/NÃO** e justificativa estruturada; não é responsável por descobrir padrões.
- Antes de executar paper trade, aplicar novamente o gate determinístico de risco e verificar idade dos dados, posição já aberta, duplicação, símbolo habilitado para Operar e estado de emergência.
- Falha/timeout/resposta inválida da IA resulta em **não operar**, log claro e retry controlado, nunca aprovação automática.
- Se o JEv for ele próprio um modelo via OpenRouter, verificar configuração, chamada real, parsing, timeouts, retries e custos sem alterar sua responsabilidade decisória.

## 5. Dashboard e observabilidade
Manter os painéis redimensionáveis e leves. Mostrar etapas **MATEMÁTICA → DEEPSEEK → VALIDAÇÃO PYTHON → JEV** (a validação pode aparecer como status/coluna entre as listas existentes, sem redesenho pesado). Cada item deve ter ID correlacionável, símbolo, duração, resultado e motivo. No resumo por ciclo, diferenciar: sem dados, sem padrão, rejeitado estatisticamente, DeepSeek não chamado, falha de IA, validado mas condição ausente, enviado ao JEv, JEv NÃO, JEv SIM, barrado por risco e trade paper executado. **Não criar decisões falsas para preencher lista.** Evitar spam, manter coalescência e tetos de linhas.

## 6. Desempenho e custo
Preservar cache incremental, paginação/cancelamento e janela deslizante correta (remover candles expirados, atualizar dados ainda não fechados somente para armazenamento transitório). Deduplicar análises equivalentes, priorizar símbolos pendentes sem starvation e medir tempo por fase, chamadas/tokens/custo estimado quando disponíveis. Não chamar DeepSeek em todo minuto sem mudança relevante ou hipótese candidata; manter a cadência de verificação do ciclo sem obrigar gasto de tokens a cada minuto.

## 7. Testes obrigatórios
Criar/atualizar testes somente em `testes/`, inclusive arquivos auxiliares. Testar: (1) padrão válido atravessa Python → DeepSeek → validação → JEv SIM/NÃO; (2) padrão inválido é barrado; (3) JSON malformado, timeout e erro OpenRouter falham fechados; (4) resposta alucinada é rejeitada por recálculo; (5) ausência de padrão produz resumo sem falsa decisão; (6) não há lookahead nem candle em formação; (7) memória é recuperada/revalidada/expirada; (8) 90 símbolos não travam GUI e recebem turnos justos; (9) deduplicação de oportunidades; (10) bloqueios de risco e emergência impedem operação; (11) nenhuma ordem real e nenhuma chamada paga em testes comuns; (12) logs não expõem segredos. Executar `python -X utf8 -m unittest discover -s testes`, `python -m compileall -q jev_bot testes` e testes GUI offscreen quando disponíveis. Relatar números reais, sem alegar testes não executados.

## 8. Entrega obrigatória
Entregar: (a) causas-raiz com arquivo/função/linha e evidências; (b) diagrama do fluxo efetivo antes/depois; (c) lista de arquivos alterados; (d) exemplos sanitizados de payload DeepSeek e JEv; (e) métricas de qualidade dos padrões e custos/latências quando medidos; (f) resultados dos testes; (g) limitações e próximos testes manuais no Windows/MT5; (h) confirmação explícita de **paper trading somente**. Não fazer push sem autorização explícita; se autorizado, informar commit e branch.

**Critério de sucesso:** uma oportunidade estatisticamente validada percorre todo o pipeline até a decisão do JEv, sem números inventados, sem bloqueios silenciosos e sem comprometer a segurança ou a leveza da aplicação. Se não houver oportunidade válida, o sistema explica o motivo e não opera.
