# PROMPT UNIVERSAL DE IMPLEMENTAÇÃO — JEv Bot

## Missão
Analise o projeto JEv Bot existente antes de alterar qualquer arquivo. Preserve as funcionalidades, integrações e o modo paper trading atual. Implemente uma aplicação gráfica Python moderna, intuitiva, redimensionável e integrada ao backend existente, com descoberta autônoma de padrões pelo DeepSeek via OpenRouter, cálculos locais verificáveis e decisão final do JEv. Não reescreva o projeto do zero nem presuma nomes de arquivos, frameworks ou APIs: inspecione o repositório, identifique pontos de integração e trabalhe incrementalmente.

## Arquitetura e responsabilidades
1. **Motor matemático local (Python):** utiliza exclusivamente candles M1 dos últimos 30 dias, com janela móvel, timestamps normalizados, tratamento de lacunas e custos operacionais. Calcula estatísticas de movimentos percentuais, retornos condicionais, recuperação após quedas, retração após altas, tempos até eventos, frequências, amostras e incerteza. Não use indicadores técnicos tradicionais (RSI, MACD, médias móveis como sinais etc.). Os cálculos locais não consomem tokens; somente os dados enviados à API e suas respostas consomem tokens.
2. **DeepSeek via OpenRouter:** inicia análises automaticamente, sem pedidos do JEv. Recebe resumos estatísticos compactos, procura padrões específicos de cada ativo e propõe hipóteses estruturadas. Consulta o repositório de padrões existentes por ativo, verifica se permanecem relevantes e sugere novos. Não inventa probabilidades nem modifica resultados calculados pelo Python. A chamada à API deve ter timeout, retry controlado, limite de custo e saída JSON validada por schema.
3. **Validação local:** toda hipótese do DeepSeek deve ser reproduzida em Python com regras explícitas, contagem de ocorrências, taxa de acerto, magnitude média/mediana, dispersão, custos e testes cronológicos fora da amostra, evitando vazamento de dados. Rejeite hipóteses sem evidência suficiente ou não reproduzíveis. Armazene status como candidato, validado, rejeitado ou expirado; nunca confunda uma correlação histórica com garantia de retorno.
4. **JEv:** recebe apenas oportunidades com evidências validadas e decide **SIM** ou **NÃO**, registrando a justificativa. Independentemente da decisão do JEv, um mecanismo determinístico de risco deve bloquear operações que violem os limites configurados. Não permita que a IA altere os limites.

## Frequência e execução
- Sem posições abertas: executar o ciclo de análise a cada **1 minuto**, sincronizado ao fechamento do candle M1, evitando dados incompletos e ciclos simultâneos.
- Com uma ou mais posições abertas: executar o ciclo de análise a cada **15 minutos**; o monitoramento de preços, posições, risco e controles de emergência **continua em tempo real**, independentemente dessa frequência.
- Ao fechar a última posição, voltar à cadência de 1 minuto.
- Quando o limite de exposição/risco máximo ou drawdown máximo for atingido: interromper imediatamente **novas análises e novas entradas**, cancelar chamadas pendentes quando possível e apresentar estado **PARADO POR RISCO**. Manter ativo o monitoramento de posições, limites e emergência. Retomar apenas após revisão e liberação manual, nunca automaticamente.
- Aplicar limites antes de abrir qualquer operação e após alterações de posição/saldo. Especificar claramente se o risco de exposição é percentual do patrimônio e como são agregados riscos de posições correlacionadas. Drawdown deve ter definição inequívoca, baseada no pico de patrimônio (equity), incluindo P/L não realizado.

## Persistência de padrões
Armazene por ativo em JSON estruturado (ou coluna JSON no banco existente), sem quebrar o armazenamento atual. Cada registro deve incluir: símbolo, versão de schema, intervalo M1 analisado, condição matemática reproduzível, direção, horizonte temporal, número de eventos, resultados estatísticos, custos, validação fora da amostra, data da última validação, validade/status e origem. Use escrita atômica, deduplicação, migração/versionamento e logs. O histórico operacional pode ser retido, mas **nenhuma decisão deve usar candles fora da janela móvel de 30 dias**.

## Interface gráfica Python
Use preferencialmente **PySide6 (Qt)**, caso compatível com a estrutura existente. Crie interface visual moderna, inspirada em dashboards Bootstrap, com tema escuro elegante, tipografia legível, indicadores de status e layout responsivo. A janela deve maximizar, restaurar e redimensionar; painéis e tabelas devem permitir ajuste de tamanho via divisores arrastáveis (QSplitter). Nunca bloquear a thread da interface com cálculos, rede ou negociação. Use workers e sinais Qt para atualizações em tempo real.

### Aba 1 — Operação e fluxo
No topo, exiba botões funcionais:
- **INICIAR:** inicia o ciclo, se conexões e limites estiverem válidos.
- **PAUSAR / RETOMAR:** pausa novas análises/entradas sem desativar monitoramento de risco.
- **EMERGÊNCIA — FECHAR TUDO:** exige confirmação apropriada, bloqueia novas entradas imediatamente, cancela ordens pendentes e tenta fechar todas as posições, com confirmação de cada resultado e alerta explícito sobre falhas; nunca marcar como encerrado sem confirmação da corretora/simulador.

Inclua campos editáveis e botão **APLICAR** para:
- Risco máximo por trade (% do patrimônio).
- Risco total/exposição máxima simultânea (% do patrimônio).
- Drawdown máximo permitido (% a partir do pico de equity).

Valide valores e persistência; alterações passam a valer no motor de risco, não apenas na tela. Mostre patrimônio, saldo, P/L aberto, exposição atual, drawdown atual, limites e estado do robô.

Abaixo, exiba **três listas sempre visíveis, empilhadas e redimensionáveis**:
1. **CÁLCULOS MATEMÁTICOS (Python):** ativo, candle/horário, progresso, amostra, padrões testados, resultado e erros.
2. **ANÁLISE DEEPSEEK:** ativo, padrão candidato, status de consulta, hipótese, validação local e resultado; exiba o fluxo de trabalho e os dados retornados, sem expor segredos ou raciocínio interno privado do modelo.
3. **DECISÃO JEv:** ativo, oportunidade validada, decisão SIM/NÃO, justificativa, resultado da checagem de risco e execução/simulação.

Use um ID de correlação por ciclo/ativo, timestamps e transições de status para acompanhar a mesma análise pelas três etapas. Atualize em tempo real e mantenha histórico recente com rolagem.

Na parte inferior, uma **quarta seção: HISTÓRICO DE TRADES**, com rolagem, ordenação e colunas de ativo, entrada/saída, lado, volume, preço, horário, P/L monetário e percentual, duração, modo paper/real e motivo de encerramento. Utilize o histórico persistido pelo sistema, sem criar operações fictícias.

### Aba 2 — Resumo e gráficos
Exiba curva de patrimônio (equity), crescimento/queda da conta, saldo, P/L realizado e não realizado, drawdown atual e máximo histórico, retorno percentual, taxa de acerto e estatísticas de trades. Diferencie valores realizados e não realizados. Atualize automaticamente ao abrir, alterar ou fechar operações, usando dados reais do simulador/corretora; evite redesenhos desnecessários. Permita filtros de período/ativo se houver dados.

Nos gráficos, ao clicar com o **botão direito**, disponibilize **COPIAR COMO IMAGEM** para a área de transferência do sistema, preservando o gráfico visível. Não é necessário exportar PNG para arquivo.

### Aba 3 — Configurações
- **OpenRouter:** campo de API key com máscara de pontos (modo senha), carregamento seguro da configuração existente (incluindo `.env`, se utilizado), edição, salvamento seguro e botão **TESTAR CONEXÃO**. Nunca mostrar chave em logs, notificações ou relatórios. Prefira armazenamento seguro do sistema operacional para credenciais; evite gravar novas senhas em texto puro. Configure o identificador exato do modelo DeepSeek disponibilizado na conta OpenRouter, sem presumir um ID fixo.
- **MetaTrader 5:** campos de login, senha mascarada, servidor da corretora e **caminho do executável** com botão **PROCURAR**. Detectar caminho quando possível, permitir edição e testar existência/compatibilidade e conexão. Persistir credenciais com segurança, respeitando o mecanismo já existente.
- **Modo de negociação:** manter **PAPER TRADING como padrão obrigatório nesta implementação**. Preparar a conexão MT5 real, mas não habilitar ordens reais automaticamente nem por simples teste de conexão. A ativação de trading real exigirá fluxo separado de autorização explícita, verificações e confirmação.
- Exibir status de conexões, mensagens de erro úteis e configurações salvas.

## Requisitos de confiabilidade
- Manter o funcionamento do projeto existente e separar UI, análise, persistência, corretora e motor de risco.
- Usar relógio/fuso horário consistente, evitar processamento repetido do mesmo candle e suportar falhas de rede sem duplicar ordens.
- Registrar logs estruturados e auditoria de decisões, sem credenciais ou dados sensíveis.
- Nunca usar saídas do LLM diretamente como ordens executáveis sem validação matemática, verificação de risco e decisão do JEv.
- Se houver limites atingidos, a análise de oportunidades para; **o gerenciamento de risco e a capacidade de fechar posições não param**.
- Não afirmar que uma estratégia é lucrativa sem backtest cronológico, avaliação fora da amostra e consideração de spreads, taxas e slippage.

## Execução da tarefa
1. Inspecione o repositório e relate brevemente a arquitetura atual, dependências, integrações e riscos de compatibilidade.
2. Apresente um plano de implementação por etapas e então implemente no código existente, sem remover funções válidas.
3. Crie/atualize dependências, migrações, configurações e testes automatizados.
4. Teste cálculo/validação, cadência 1/15 minutos, travas de risco, emergência, persistência, conexão simulada e atualização da GUI; utilize mocks para serviços externos quando necessário.
5. Entregue arquivos completos modificados, instruções de instalação/execução, resultados dos testes e pendências reais. Não alegue ter testado integrações que não foram executadas.

**Critério de aceitação:** consigo abrir uma janela Python gráfica e redimensionável, iniciar/pausar, configurar limites, acompanhar as três etapas Python → DeepSeek → JEv e o histórico de trades, visualizar gráficos atualizados por operações, copiar gráficos como imagem, configurar OpenRouter e MT5 de forma segura e continuar exclusivamente em paper trading, com bloqueio efetivo quando os limites forem atingidos.
