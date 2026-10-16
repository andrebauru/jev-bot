# JEv Bot — Auditoria e correção do fluxo operacional de trading

## Missão
Investigue o repositório existente do JEv Bot e corrija o pipeline que passa horas exibindo `RESUMO` em vez de decisões por ativo. Preserve o projeto, a GUI PySide6, o MT5, o cache M1, logs, testes e o modo exclusivamente PAPER. Não reescreva o sistema do zero.

## Evidência observada
Vídeo de 19 segundos da GUI: lista Python com aproximadamente 30.239 candles por símbolo e vários `0` padrões; lista DeepSeek mostra hipóteses descartadas, validações rejeitadas e `limite de chamadas DeepSeek por hora atingido`; lista JEv mostra linhas `RESUMO` sem ativo, com candidatos agregados e pendentes zero, mas nenhuma decisão SIM/NÃO/HOLD por símbolo. Não concluir que há loop infinito sem instrumentação; distinguir falta de oportunidade válida, gargalo de descoberta, bloqueio por rate limit, erro de roteamento e ausência de evento de decisão.

## Responsabilidades obrigatórias
1. **Motor Python:** aquisição de candles fechados M1, janela móvel dos últimos 30 dias, cálculos matemáticos determinísticos, geração de candidatos operacionais, validação independente, checagem de spreads/custos, dimensionamento, exposição, limites de risco, quantidade máxima de posições, drawdown e supervisão das posições abertas.
2. **DeepSeek:** descoberta/interpretação de comportamento e sugestão de hipóteses; memória de padrões por ativo com validade e evidências. Não gerencia risco, não abre posições e não deve bloquear o caminho operacional por estar aprendendo.
3. **JEv:** recebe uma oportunidade elegível com contexto mínimo, responde estritamente `SIM`, `NAO` ou `HOLD`, com motivo curto e identificadores. Não recalcula risco, não gerencia carteira, não escolhe volume nem abre/fecha operações.
4. **Executor paper:** só executa `SIM` quando uma última verificação determinística de risco, preço, expiração e autorização permitir. `NAO` descarta a oportunidade; `HOLD` adia apenas aquela oportunidade até expiração, sem loop infinito.

## Auditoria de causa-raiz (antes de alterar)
- Rastrear funções, arquivos e linhas desde candidatos Python até DeepSeek, validação, elegibilidade, chamada `jev.decide`, emissão do evento de GUI e executor.
- Medir contagens por símbolo/ciclo: `candles`, `candidatos`, `hipoteses`, `validos`, `condicao_ativa`, `risco_aprovado`, `enviados_jev`, `SIM`, `NAO`, `HOLD`, `expirados`, `bloqueados` e `executados_paper`.
- Identificar por que a GUI exibe `RESUMO` com ativo vazio; distinguir linha agregada de decisão. Nunca usar `RESUMO` como substituto de uma decisão individual.
- Investigar limites de chamadas por hora, TTL e deduplicação: separar orçamento de descoberta do orçamento operacional. Evitar chamadas repetidas para dados equivalentes; não inventar uma chamada JEv se não existir candidato elegível.
- Verificar se a política de validação, contratos ou expiração impossibilitam que qualquer candidato chegue à decisão. Não reduzir limiares silenciosamente.
- Conferir discrepâncias de custos/spreads, normalização por instrumento e moeda, janela M1 e eventos não sobrepostos.

## Prioridade operacional
- O objetivo principal é trading paper, não treinamento permanente. Usar imediatamente padrões já validados e vigentes para avaliar sinais nos candles recentes.
- Descoberta/reaprendizado deve ser assíncrono, com orçamento próprio, acionado por mudanças estatisticamente relevantes ou revisão periódica; nunca monopolizar os ciclos de trading.
- Se não houver padrão validado, registrar `SEM_OPORTUNIDADE` com motivo por símbolo. Não forçar SIM/NÃO/HOLD fictício e não executar trades artificiais.
- Cada candle M1 fechado deve permitir avaliar oportunidades com dados disponíveis, sem rebaixar segurança nem esperar uma nova sessão de aprendizagem.
- Aplicar justiça entre símbolos e limites de trabalho por ciclo; preservar responsividade e cancelamento cooperativo.

## Interface gráfica
- Na terceira lista, apresentar apenas decisões reais do JEv com colunas: horário, ativo, `analysis_id`, `pattern_id`, direção, decisão (`SIM`/`NAO`/`HOLD`), justificativa, autorização de risco, status paper.
- Resumos agregados devem ir para painel de métricas/status separado, não ocupar a lista de decisões.
- Quando nenhum candidato chega ao JEv, mostrar mensagem clara: `0 oportunidades elegíveis`, com contadores por motivo e por símbolo, sem sugerir que o JEv decidiu.
- Exibir estado da descoberta DeepSeek separado do estado de trading, bem como contagem de consultas, erros e limites de taxa.
- Atualizar em lote, manter limites de linhas e não bloquear a thread Qt.

## Testes obrigatórios
- Teste end-to-end sintético: oportunidade elegível chega ao JEv e produz cada uma das respostas SIM, NAO e HOLD, com ativo correto na GUI.
- Rejeição por risco ocorre antes do JEv; última verificação de risco no executor continua obrigatória.
- Ausência de oportunidade gera motivo e contagem, sem chamada artificial ao JEv.
- Rate limit ou indisponibilidade DeepSeek não bloqueia a avaliação de padrões previamente validados.
- HOLD expira; NAO descarta; SIM autorizado gera apenas ordem paper; emergência/drawdown impedem novas entradas.
- Nenhum teste pode consumir tokens reais: mock obrigatório para todas as chamadas OpenRouter.
- Testar 90 símbolos, memória, tempo de ciclo, cache incremental, GUI offscreen e, quando disponível, smoke test manual Windows/MT5.
- Colocar todos os arquivos de teste em `testes/`; manter logs rotacionados e sem segredos.

## Entrega exigida
Apresentar (a) causa-raiz comprovada com arquivos/funções/linhas; (b) fluxo antes/depois; (c) alterações por arquivo; (d) exemplos sanitizados de decisão real e de ausência de oportunidade; (e) métricas por estágio, incluindo motivo dos descartes; (f) testes e resultados executados; (g) limitações e validação manual pendente. Não afirmar que houve trades ou decisões reais sem evidência. Não enviar ordens reais nem ativar trading real. Não fazer push remoto sem autorização explícita.
