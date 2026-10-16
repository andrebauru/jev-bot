# PROMPT — Auditoria de integração, desempenho e inicialização automática do JEv Bot

## Missão
Inspecione o repositório JEv Bot existente, identifique e corrija a causa real pela qual o botão **INICIAR** não funciona e o sistema permanece em **PAUSADO**. Implemente inicialização automática segura ao abrir a aplicação, sem clique manual. Preserve arquitetura, interface PySide6, recursos existentes e baixo consumo de recursos. **Não reescreva o projeto do zero.**

## Regras inegociáveis
- Operar **exclusivamente em PAPER TRADING**. Não enviar ordens reais nem ativar automaticamente a integração de execução real com MT5.
- A inicialização automática só pode ocorrer após carregar e validar configurações, seleção de mercados, estado persistido e limites de risco. Nunca contornar travas de drawdown, exposição, risco por trade, emergência ou configuração inválida.
- Se houver parada de emergência persistida, bloqueio de risco, ausência de mercados habilitados ou falha crítica de conexão, mostrar estado e motivo claros; **não iniciar análises** até a condição ser resolvida e, quando necessário, houver liberação manual explícita.
- Ao atingir drawdown máximo ou exposição máxima, suspender novas análises e entradas, mas continuar a supervisão de posições abertas e a aplicação das proteções pertinentes. Não interromper a gestão de risco das posições existentes.
- Não gerar chamadas reais à OpenRouter durante testes automatizados; usar mocks. Nunca registrar chaves, tokens ou senhas em logs.

## 1. Diagnóstico obrigatório do botão INICIAR e estado PAUSADO
1. Rastrear todo o caminho do clique: botão na GUI → signal/slot → controlador/motor → fila de comandos → orquestrador → transições de estado → scanner → eventos de volta à GUI.
2. Verificar se o botão está desabilitado, sem conexão de sinal, se a fila está bloqueada, se há exceção silenciosa, corrida entre threads, estado sobrescrito por checkpoint ou configuração, ou gating indevido de mercados.
3. Instrumentar eventos de estado com IDs, timestamps, duração e motivo, em `logs/`, respeitando a política de rotação e mascaramento existente.
4. Corrigir a causa-raiz. Manter o botão INICIAR funcional como ação de retomada manual após PAUSAR, quando não houver bloqueio de segurança. Exibir mensagem objetiva se a retomada for negada.
5. Formalizar transições de estado: INICIALIZANDO, PRONTO, ANALISANDO, PAUSADO_PELO_USUARIO, BLOQUEADO_POR_RISCO, EMERGENCIA, ERRO e ENCERRANDO (adaptar à nomenclatura existente sem quebrar contratos). Evitar que PAUSADO seja usado como estado genérico para qualquer falha.

## 2. Inicialização automática segura
- Após a janela abrir, disparar bootstrap assíncrono, sem bloquear a thread Qt.
- Carregar `.env`/configurações seguras, seleção persistida `data/mercados.json`, limites e modo paper; validar integridade e permissões.
- Validar conectividade MT5 necessária para dados, com tentativas limitadas e backoff, sem loop apertado. Informar progresso na GUI.
- Se tudo estiver válido e ao menos um mercado estiver habilitado para monitoramento, iniciar automaticamente o ciclo de análise M1. Não exigir clique em INICIAR.
- Se não houver mercados selecionados, mostrar `AGUARDANDO SELEÇÃO DE MERCADOS`, sem análise nem consumo de tokens; quando o usuário habilitar mercados, iniciar automaticamente se não houver pausa voluntária nem bloqueio.
- Respeitar pausa manual durante toda a sessão e bloqueios persistidos de risco/emergência entre reinicializações; **nunca usar autostart para desfazer uma pausa voluntária na sessão ou uma trava de segurança**.
- PAUSAR interrompe novas análises, preservando monitoramento de posições abertas; INICIAR retoma se seguro; CANCELAR CICLO interrompe o ciclo corrente sem confundir com PAUSAR; EMERGÊNCIA bloqueia novas entradas e aciona o procedimento seguro já definido para ordens/posições, com confirmação apropriada e logs.
- Evitar iniciar dois ciclos simultâneos; proteger contra duplo clique, múltiplos signals e reinicialização concorrente.

## 3. Auditoria de desempenho e GUI
- Confirmar que downloads M1 dos últimos 30 dias usam paginação e cancelamento cooperativo fora da thread Qt.
- Preservar agregação/coalescência de eventos, limite de itens, redraw incremental e QSplitter redimensionável.
- Medir tempo de resposta da GUI, número de eventos, CPU, RAM, duração por lote e tempo para cancelar; registrar métricas sintéticas e de execução, sem inventar resultados.
- Revisar chamadas bloqueantes ao MT5, persistência em disco, atualizações de gráficos e tratamento de falhas de rede. Não criar polling excessivo ou logs por candle.
- Garantir frequência de análise definida: sem posição aberta, ciclo de 1 minuto; com posição aberta, ciclo de 15 minutos para análises novas, mantendo supervisão de risco em cadência adequada e independente.

## 4. Auditoria do pipeline e segurança
- Verificar sequência: cálculo estatístico local → DeepSeek descobre/interpreta padrões → Python verifica matematicamente e valida fora da amostra → JEv responde SIM/NÃO; JEv não inicia pedidos de análise.
- Somente candles M1 na janela móvel dos últimos 30 dias para evidências de decisão; sem indicadores tradicionais.
- Padrões persistidos por ativo em JSON com versão, evidência, amostra, condições, validade e atualização. Nunca promover padrão sugerido pela IA sem validação reproduzível.
- Somente mercados marcados Monitorar recebem histórico/análise; somente os marcados Operar podem ser considerados para decisão/ordem em paper. `Operar ⇒ Monitorar`.
- Validar limites de risco **no backend**, imediatamente antes de qualquer ordem paper; se limite for alcançado, bloquear análises e novas ordens e exibir a causa. Manter a supervisão das posições já abertas.
- Confirmar que logs ocultam credenciais inclusive em exceções e que rotação (10 MB, cinco arquivos, 14 dias) funciona.

## 5. Testes obrigatórios e organização
- Criar ou atualizar testes de regressão para: botão INICIAR; autostart válido; ausência de mercados; seleção posterior; pausa manual; retomada; bloqueio por risco; emergência persistida; falha de MT5; reconexão limitada; corrida de dois starts; cancelamento durante download; janela Qt responsiva; proteção de posições abertas; nenhuma ordem real.
- Usar mocks de MT5, DeepSeek, JEv e rede para cenários determinísticos. Rodar também teste offscreen Qt e testes existentes; se disponível, fazer smoke test manual visível no desktop e reportar se não foi possível.
- Todos os scripts `test*.py`, `*_test.py`, verificadores e utilitários exclusivos de teste devem ficar em `testes/` (ou subpastas dela), nunca espalhados na raiz ou em módulos de produção. Não mover módulos legítimos de produção por engano. Ajustar imports e descoberta de testes.
- Não apagar testes úteis. Remover apenas temporários redundantes depois de migrar o que for necessário.

## 6. Critérios de aceite
1. Abrir a GUI com configuração válida e mercado habilitado inicia análise automaticamente, sem clique.
2. O botão INICIAR funciona após PAUSAR e a interface exibe o estado real do motor.
3. Com risco/emergência bloqueados, não ocorre autostart nem operação; motivo fica visível.
4. Sem mercados, não há download, análise nem consumo de tokens; selecionar um mercado permite iniciar quando seguro.
5. GUI continua utilizável durante downloads, e CANCELAR CICLO responde entre lotes.
6. Nenhuma ordem real; testes automatizados não gastam tokens.
7. Logs são rotacionados e seguros; arquivos de teste ficam em `testes/`.

## 7. Entrega obrigatória
Forneça relatório objetivo com: (a) causa-raiz comprovada do PAUSADO/botão inoperante; (b) arquivos e trechos alterados; (c) diagrama resumido de estados e regras do autostart; (d) testes executados, comandos e resultados reais; (e) medições de desempenho antes/depois, quando disponíveis; (f) limitações e pendências; (g) confirmação de paper trading e bloqueios de risco. Não alegue que algo foi testado se não foi. Se houver acesso ao repositório, faça commit somente após testes passarem e informe o hash; push apenas se o ambiente e as autorizações permitirem.
