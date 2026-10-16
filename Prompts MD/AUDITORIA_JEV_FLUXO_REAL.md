# JEv Bot — Auditoria do fluxo real e correções pontuais

## Missão
Investigar e corrigir, no repositório existente, as inconsistências observadas na gravação da interface, sem reescrever a arquitetura, sem retirar proteções, sem tornar a GUI pesada e mantendo **exclusivamente paper trading**. Antes de alterar, reproduzir o comportamento e identificar a causa no código e nos logs. Não presumir que uma lista vazia seja defeito.

## Evidências observadas no vídeo
- A janela mostra estado **PRONTO** enquanto há downloads paginados de histórico M1 em andamento (ex.: 30240/43200 candles).
- A lista Matemática recebe linhas de progresso de múltiplos símbolos, inclusive KAIUSD, ATOMUSD e ARBUSD.
- A lista DeepSeek contém consultas, hipóteses e rejeições por validação local, inclusive `Taxa/expectativa abaixo do mínimo fora da amostra`.
- A lista Decisão JEv está vazia durante o trecho gravado. Isso **não prova falha**: pode não haver candidatos válidos.
- Existem muitos símbolos selecionados (90 nas capturas), com possível custo excessivo de varredura e download.
- A interface mostra texto de início automático/aguardando próximo ciclo enquanto ocorre atividade; confirmar se a mensagem é atual ou obsoleta.

## Auditoria obrigatória
1. Traçar o fluxo real: agendador → catálogo/seleção → download M1 → cálculos locais → propostas DeepSeek → validação local → elegibilidade por `Operar` → gate de risco → decisão JEv → paper order/histórico. Instrumentar `analysis_id`, símbolo, duração, motivo de saída e contadores por etapa, sem registrar chaves ou payloads sensíveis.
2. Inspecionar a máquina de estados e o `Coalescedor`. `PRONTO` deve significar efetivamente ocioso/pronto, e `BAIXANDO_HISTORICO`, `CALCULANDO`, `ANALISANDO_DEEPSEEK`, `VALIDANDO`, `DECIDINDO_JEV`, `AGUARDANDO`, `PAUSADO`, `BLOQUEADO_POR_RISCO`, `ERRO` devem ser estados ou subestados coerentes. Evitar estado antigo sobrescrevendo estado novo; o chip deve refletir o trabalho em andamento sem spam de sinais Qt.
3. Confirmar que a terceira lista vazia resulta de ausência de oportunidades válidas ou de um erro de encaminhamento. Criar teste de ponta a ponta com dados sintéticos que gerem (a) hipótese rejeitada e (b) hipótese validada e elegível, com mocks de IA e JEv. No caso (b), comprovar a chegada à terceira lista. Não relaxar critérios matemáticos para forçar operações.
4. Mostrar motivos resumidos de não encaminhamento: `SEM_PADRAO`, `REJEITADO_VALIDACAO`, `MERCADO_SOMENTE_MONITORAR`, `RISCO_BLOQUEADO`, `IA_INDISPONIVEL`, `AGUARDANDO_DECISAO`, etc., distinguindo rejeição estatística de erro técnico. Não transformar cada rejeição em uma chamada adicional de API.
5. Verificar se o ciclo de 1 minuto (sem posição) e 15 minutos (com posição) está respeitado **sem sobreposição**, e se uma varredura de 90 símbolos pode ultrapassar o intervalo. Definir política de fila, orçamento de tempo, prioridade e continuidade incremental; não iniciar novo ciclo enquanto o anterior estiver em curso.
6. Garantir cache incremental M1 por símbolo, com verificação de integridade e lacunas, evitando baixar novamente 43.200 candles a cada ciclo. Janela de análise permanece estritamente nos últimos 30 dias, usando M1; padrões salvos devem ser revalidados com dados da janela atual. Usar armazenamento limitado e escrita atômica. Se não houver cache hoje, implementar de forma mínima e testada.
7. Conferir se download e chamadas OpenRouter ocorrem fora da thread Qt; preservar cancelamento cooperativo, agregação de eventos, limites de memória e atualizações em lote. Não executar requisições pagas nos testes automáticos.
8. Melhorar disposição da aba Operação: três painéis identificados por títulos visíveis (Matemática, DeepSeek, JEv), com contagem de itens e último processamento; QSplitters redimensionáveis e tamanhos mínimos; histórico de trades com rolagem; mensagem de vazio contextual em vez de silêncio. Evitar adicionar widgets caros ou redesenho completo.
9. Conferir autostart seguro e botões INICIAR, PAUSAR, CANCELAR CICLO e EMERGÊNCIA, inclusive em download e chamada de IA. Nunca liberar emergência automaticamente, nunca enviar ordem real. Ao atingir drawdown ou exposição, suspender novas análises e entradas, preservando supervisão das posições abertas.
10. Verificar se seleção `Monitorar` e `Operar` está corretamente aplicada por símbolo, inclusive quando há seleção em categorias e filtros visuais. Não inferir seleção de uma categoria apenas pelo tick mostrado sem verificar filhos e persistência.

## Testes e diagnóstico
- Criar/atualizar testes somente dentro de `testes/`; mover qualquer script de teste solto da raiz para `testes/` sem quebrar imports.
- Executar testes existentes, compilação, teste Qt offscreen e, quando seguro, smoke test da interface visível e leitura MT5. Testes reais de API só com autorização explícita, pois consomem tokens.
- Verificar logs em `logs/` com rotação 10 MB, até cinco arquivos, retenção 14 dias, sem segredos.
- Registrar métricas: duração por etapa, número de símbolos percorridos, cache hit/miss, chamadas de IA, candidatos validados/rejeitados, eventos entregues à GUI, uso de RAM e latência de CANCELAR.
- Não afirmar que houve execução real de uma etapa sem evidência; distinguir teste simulado, offscreen e integração real.

## Entrega exigida
1. Causa-raiz confirmada para cada anomalia, com arquivo/função e evidência.
2. Alterações mínimas realizadas, com caminhos dos arquivos.
3. Comparativo de desempenho antes/depois e funcionamento dos estados.
4. Contagem de testes descobertos e executados (explicar a diferença entre `test_base.py` e descoberta padrão).
5. Pendências e instruções de verificação na janela Windows com MT5.
6. Commit e push **somente se o ambiente estiver autorizado e os testes passarem**; relatar hash real, nunca inventar.

## Restrições inegociáveis
- M1 exclusivamente, últimos 30 dias; DeepSeek descobre padrões, Python valida matematicamente, JEv aprova ou rejeita.
- Nenhum indicador técnico tradicional acrescentado.
- Sem execução real: somente paper trading.
- Não alterar arbitrariamente limiares de validação, limites de risco ou credenciais.
- Preservar o layout existente e otimizar, não refatorar tudo por preferência estética.
