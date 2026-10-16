# PROMPT --- JEv Bot: Motor Matemático Local M1 + 4 Ativos em Paralelo

## Objetivo principal

Corrigir o JEv Bot para que o scanner **não dependa de DeepSeek, IA de
descoberta, padrões persistidos, nomes de padrões ou reconhecimento de
setups pré-cadastrados para decidir se existe uma oportunidade**.

O fluxo operacional desejado é:

**MT5 → histórico M1 suficiente → cálculos matemáticos locais em Python
→ validação econômica/estatística → oportunidade elegível → envio
imediato ao JEv → revalidação → risco Python → Paper Trading**

O JEv continua responsável apenas pela decisão final sobre uma
oportunidade que o Python já calculou e qualificou.

DeepSeek deve permanecer completamente fora do caminho operacional.

------------------------------------------------------------------------

## 1. Regra obrigatória: somente cálculo matemático local

Após o histórico mínimo do ativo estar disponível, o Python deve
analisar diretamente as séries OHLC/tick/spread.

Não usar como gate obrigatório:

-   `SEM_PADRAO`;
-   `NO_SETUP` baseado em biblioteca de padrões;
-   padrão aprendido anteriormente;
-   arquivo persistido de padrões;
-   nome de formação;
-   DeepSeek;
-   LLM para descobrir oportunidade;
-   resultado de outro ativo.

É permitido produzir internamente features matemáticas, mas elas devem
ser calculadas diretamente dos dados atuais/históricos.

Calcular, quando aplicável:

-   retornos simples e log-retornos;
-   retorno em múltiplos horizontes M1;
-   volatilidade realizada;
-   ATR/range normalizado;
-   desvio-padrão e z-score;
-   momentum;
-   aceleração/desaceleração do retorno;
-   inclinação/regressão local;
-   distância relativa de máximas/mínimas;
-   posição dentro do range;
-   compressão/expansão de volatilidade;
-   volume/tick-volume quando confiável;
-   spread atual em relação ao preço;
-   distribuição empírica dos retornos futuros;
-   quantis;
-   probabilidade empírica de movimentos favoráveis/adversos;
-   MAE/MFE;
-   expectativa matemática;
-   payoff;
-   risco/retorno;
-   custo observado;
-   estabilidade temporal.

Não criar um novo sistema de "padrões" apenas com outro nome.

------------------------------------------------------------------------

## 2. Histórico primeiro, análise depois

Cada símbolo deve possuir estado próprio.

Estados mínimos:

`WAITING_HISTORY` `READY` `CALCULATING` `REJECTED` `ELIGIBLE_FOR_JEV`
`WAITING_JEV` `JE V_RESPONSE` `STALE` `ERROR`

Corrigir o nome `JE V_RESPONSE` no código para `JEV_RESPONSE`.

Enquanto não houver histórico suficiente:

-   baixar/completar candles;
-   atualizar cache incremental;
-   não chamar JEv;
-   não fabricar decisão;
-   não bloquear os outros símbolos.

Assim que o histórico mínimo estiver completo, o ativo passa para
`READY` e pode entrar no pool matemático.

O histórico mínimo deve ser configurável e o diagnóstico deve mostrar:

-   candles necessários;
-   candles disponíveis;
-   primeiro timestamp;
-   último candle M1 fechado;
-   idade do último candle;
-   motivo caso o histórico seja insuficiente.

------------------------------------------------------------------------

## 3. Processar 4 ativos simultaneamente

Hoje o comportamento visual aparenta ser sequencial: ativo 1 → ativo 2 →
ativo 3.

Alterar para um pool limitado de **4 análises de símbolos simultâneas**.

Requisitos:

-   `MAX_PARALLEL_SYMBOLS = 4` por padrão;
-   valor configurável;
-   máximo seguro definido para impedir explosão de memória/CPU;
-   cada símbolo isolado;
-   erro de um símbolo não cancela os outros;
-   símbolo lento não impede os outros três;
-   não processar o mesmo símbolo duas vezes simultaneamente;
-   proteger estruturas compartilhadas contra race conditions;
-   não bloquear a thread da GUI;
-   preservar responsividade do PySide6;
-   não compartilhar DataFrame mutável entre workers.

A concorrência é para **análise matemática por símbolo**.

Não criar quatro chamadas JEv descontroladas. A fila JEv continua
limitada e independente.

------------------------------------------------------------------------

## 4. Pipeline matemático por ativo

Para cada símbolo `READY`:

### A. Snapshot consistente

Capturar:

-   último candle M1 fechado;
-   OHLC;
-   ticks/cotação atual;
-   bid;
-   ask;
-   mid;
-   spread;
-   propriedades do símbolo.

Registrar `analysis_candle_time`.

### B. Features

Calcular features matemáticas diretamente.

### C. Avaliação BUY e SELL

Avaliar obrigatoriamente os dois lados quando os dados permitirem:

-   hipótese BUY;
-   hipótese SELL.

Não assumir direção antecipadamente.

### D. Resultado econômico

Para cada lado, estimar usando histórico:

-   probabilidade favorável;
-   probabilidade adversa;
-   ganho médio/mediano;
-   perda média/mediana;
-   MFE;
-   MAE;
-   horizonte;
-   stop candidato;
-   take-profit candidato;
-   payoff;
-   expectativa bruta;
-   spread atual;
-   expectativa líquida após spread;
-   estabilidade fora da amostra.

Se comissão/slippage reais não estiverem disponíveis:

-   não inventar números;
-   informar `NOT_AVAILABLE`;
-   manter separado do spread;
-   mostrar claramente que a expectativa líquida conhecida considera
    apenas custos verificáveis.

### E. Elegibilidade

O candidato só vai ao JEv se a matemática mostrar uma oportunidade
elegível conforme critérios documentados.

Mas a rejeição NÃO pode ser apenas `SEM_PADRAO`.

Usar motivos quantitativos específicos, por exemplo:

-   `INSUFFICIENT_HISTORY`
-   `INVALID_TICK`
-   `SPREAD_TOO_HIGH`
-   `NEGATIVE_EXPECTANCY`
-   `OOS_UNSTABLE`
-   `INSUFFICIENT_SAMPLE`
-   `LOW_PAYOFF`
-   `STALE_CANDLE`
-   `NO_MATHEMATICAL_EDGE`

Sempre incluir métricas que justificam o motivo.

------------------------------------------------------------------------

## 5. Envio imediato ao JEv

Se um dos quatro workers encontrar candidato elegível:

**não esperar os outros três terminarem.**

Fluxo:

`ELIGIBLE_FOR_JEV → queue_enqueued → worker_dequeued → jev_request_started`

Registrar:

-   `run_id`;
-   `candidate_id`;
-   `request_id`;
-   símbolo;
-   direção proposta;
-   candle analisado;
-   timestamp de elegibilidade;
-   timestamp de fila;
-   timestamp de início da chamada;
-   latência.

O scanner deve continuar analisando outros símbolos enquanto o JEv
responde.

------------------------------------------------------------------------

## 6. Revalidar antes de qualquer Paper Trade

Quando a resposta JEv chegar:

1.  retornar ao fluxo seguro do orquestrador;
2.  consultar novamente MT5;
3.  confirmar que o candle M1 fechado usado na análise ainda é válido;
4.  confirmar preço/spread;
5.  recalcular risco;
6.  descartar resposta vencida;
7.  somente então permitir execução PAPER.

Nunca transformar:

-   timeout;
-   HTTP error;
-   parse error;
-   fila cheia;
-   candle vencido;

em HOLD/NAO falso.

Esses estados são erros técnicos ou expiração.

------------------------------------------------------------------------

## 7. Remover dependências operacionais antigas

Auditar o código inteiro por:

-   `deepseek`;
-   `pattern`;
-   `padrao`;
-   `padrão`;
-   `setup`;
-   `SEM_PADRAO`;
-   `NO_SETUP`;
-   arquivos de padrões;
-   filas antigas;
-   funções antigas de descoberta.

Classificar cada ocorrência como:

1.  operacional;
2.  compatibilidade/migração;
3.  histórico/log;
4.  UI;
5.  teste morto.

Qualquer dependência operacional que impeça o cálculo matemático direto
deve ser removida do caminho.

Não apagar dados históricos sem necessidade.

Se estruturas antigas precisarem permanecer para compatibilidade, elas
**não podem decidir elegibilidade**.

`deepseek_calls` deve permanecer sempre 0.

------------------------------------------------------------------------

## 8. Dashboard: mostrar os 4 trabalhos simultâneos

A interface deve deixar claro que há até quatro ativos sendo calculados
em paralelo.

Criar/ajustar área de atividade com colunas como:

`Ativo | Estado | Candles | Candle | Etapa | BUY Edge | SELL Edge | Spread | Tempo`

Exemplos de estado:

`BTCUSD | CALCULATING` `ETHUSD | WAITING_JEV` `USDJPY | READY`
`XAUUSD | REJECTED: NEGATIVE_EXPECTANCY`

Não exibir apenas mensagens genéricas como "sem padrão".

Separar visualmente:

-   análises matemáticas;
-   oportunidades elegíveis;
-   fila/respostas JEv;
-   decisões JEv;
-   paper trades;
-   diagnóstico.

Manter `QSplitter`, rolagem independente e tamanhos persistidos.

------------------------------------------------------------------------

## 9. Telemetria obrigatória do funil

Por ciclo:

-   `symbols_selected`;
-   `history_ready`;
-   `calculations_started`;
-   `calculations_completed`;
-   `buy_evaluated`;
-   `sell_evaluated`;
-   `mathematical_edge_found`;
-   `quant_validated`;
-   `eligible_for_jev`;
-   `queue_enqueued`;
-   `jev_request_started`;
-   `jev_response_received`;
-   `stale_discarded`;
-   `paper_opened`;
-   `rejected_by_reason`;
-   `deepseek_calls`.

Também registrar:

-   máximo de análises concorrentes observado;
-   tempo médio/p95 por símbolo;
-   uso aproximado de memória do pool;
-   tamanho/idade da fila JEv.

------------------------------------------------------------------------

## 10. Testes obrigatórios

Adicionar testes para provar:

### Concorrência

-   quatro símbolos entram em `CALCULATING` simultaneamente;
-   o quinto espera uma vaga;
-   quando um termina, o quinto começa;
-   um worker lento não bloqueia os demais;
-   exceção em um worker não derruba o pool;
-   GUI permanece responsiva.

### Matemática

-   BUY positivo;
-   SELL positivo;
-   ambos negativos;
-   spread elimina edge;
-   histórico insuficiente;
-   tick inválido;
-   OOS instável;
-   candle vencido;
-   custo indisponível não vira zero inventado.

### JEv

-   oportunidade elegível é enfileirada imediatamente;
-   não espera o lote de quatro terminar;
-   resposta lenta não pausa scanner;
-   fila cheia é erro técnico;
-   HTTP/timeout/parse são erros técnicos;
-   resposta para candle antigo é descartada.

### DeepSeek/padrões

Criar teste de regressão garantindo que:

-   nenhuma função DeepSeek seja chamada;
-   ausência de arquivo de padrões não impeça o scanner;
-   `SEM_PADRAO` não seja requisito para o fluxo;
-   uma oportunidade produzida exclusivamente por cálculo matemático
    consiga chegar ao JEv mockado.

------------------------------------------------------------------------

## 11. Teste real somente leitura

Sem ordens e sem chamada paga ao JEv, executar leitura MT5 real em uma
amostra representativa de ativos operáveis.

Demonstrar:

-   histórico baixado;
-   quatro análises concorrentes;
-   cálculos BUY/SELL;
-   motivos quantitativos de rejeição;
-   oportunidades elegíveis, se existirem naturalmente.

Não alterar critérios apenas para fabricar uma oportunidade.

Se houver oportunidade elegível, parar antes da chamada paga ao JEv caso
não exista autorização explícita.

------------------------------------------------------------------------

## 12. Critérios de aceitação

A tarefa só está concluída se houver evidência de que:

1.  DeepSeek não participa da operação.
2.  O scanner não depende de padrões persistidos.
3.  Histórico suficiente é obtido antes da análise.
4.  O Python calcula diretamente os dados M1.
5.  BUY e SELL são avaliados matematicamente.
6.  Até quatro ativos são calculados simultaneamente.
7.  Um ativo não bloqueia os demais.
8.  O quinto ativo aguarda vaga corretamente.
9.  Uma oportunidade elegível vai imediatamente ao JEv.
10. O scanner continua enquanto o JEv está pendente.
11. Respostas vencidas são descartadas.
12. O risco permanece em Python.
13. Execução permanece exclusivamente PAPER.
14. A GUI mostra claramente cálculo, rejeição quantitativa, JEv e paper
    em áreas distintas.
15. Testes existentes continuam passando.

------------------------------------------------------------------------

## 13. Restrições

-   Não reintroduzir DeepSeek.
-   Não fazer refatoração ampla sem necessidade.
-   Não alterar credenciais.
-   Não revelar tokens.
-   Não fazer ordem real.
-   Não habilitar live trading.
-   Não reduzir filtros arbitrariamente para gerar trades.
-   Não fazer chamada paga ao JEv sem autorização explícita.
-   Preservar integrações e comportamento que já estejam corretos.
-   Fazer a menor alteração arquitetural necessária para implementar o
    pool matemático concorrente.

------------------------------------------------------------------------

## 14. Relatório final obrigatório da IDE

Ao concluir, informar:

-   causa encontrada;
-   arquivos alterados;
-   dependências antigas removidas/desativadas;
-   arquitetura final do pool de 4;
-   evidência de concorrência real;
-   fórmulas/features efetivamente utilizadas;
-   funil quantitativo;
-   motivos de rejeição;
-   testes executados e resultados;
-   benchmark antes/depois;
-   limitações restantes;
-   commit(s);
-   confirmar explicitamente se houve ou não push;
-   confirmar explicitamente: `deepseek_calls=0`;
-   confirmar que nenhuma ordem real e nenhuma chamada paga JEv foi
    executada sem autorização.
