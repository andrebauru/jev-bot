![jev-bot](assets/banner.png)

# jev-bot

Bot de análise com dados reais do MetaTrader 5, decisões externas JEV e
execução exclusivamente paper. O fluxo separa aquisição de dados, decisão,
gerenciamento de risco e registro de operações. A decisão do modelo não
dispensa os limites da carteira.

## Requisitos e instalação

- Windows, Python 3.10 ou superior e os pacotes MetaTrader5 e python-dotenv.
- Terminal MT5 conectado a uma conta, com acesso ao histórico do broker.
- Para decisões JEV: chave e endpoint HTTPS fornecidos pelo serviço.

```powershell
python -m pip install -e .
python -m jev_bot --help
```

O arquivo `.env` do projeto é carregado automaticamente, sem modificar seu
conteúdo. Variáveis já definidas no ambiente do processo têm prioridade;
`--terminal` e `--mt5-time-offset-hours` têm prioridade sobre a configuração local.

O terminal padrão é
`C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe`.
O programa usa a conta já aberta: não solicita login, não troca de conta e
não envia ordens reais. Para outro terminal, informe `--terminal` antes do comando.

## Estrutura do projeto

| Módulo | Responsabilidade |
| --- | --- |
| `jev_bot/markets.py` | Conexão ao terminal MT5, catálogo, candles M1 (100 candles e janela de 30 dias), cotações e cálculo de contrato |
| `jev_bot/motor_matematico.py` | Estatísticas locais da janela M1 de 30 dias: movimentos, retornos condicionais, recuperação, retração, tempos e custos; corte cronológico 70/30 |
| `jev_bot/deepseek.py` | Descoberta de padrões pelo DeepSeek via OpenRouter: schema rígido, timeout, retry controlado e orçamento de tokens |
| `jev_bot/padroes.py` | Tipos de regra reproduzíveis, validação local (dentro/fora da amostra) e repositório JSON por ativo |
| `jev_bot/orquestrador.py` | Cadência 1/15min, pipeline Python → DeepSeek → JEv → risco → paper, PARADO POR RISCO e emergência |
| `jev_bot/risk.py` | Limites (`Limits`), dimensionamento de lote e veredito de entrada (`Gate`) |
| `jev_bot/execution/paper.py` | Carteira paper persistente (`Book`/`Fill`): posições, stops, trades e métricas |
| `jev_bot/gui/` | Interface PySide6: abas de operação, resumo/gráficos e configurações; worker Qt sobre o orquestrador |
| `jev_bot/loop.py` | Ciclo MT5 → JEV → risco → execução paper |
| `jev_bot/painel.py` | Dashboard contínuo no terminal com scanner, comandos e carteira |
| `jev_bot/render.py` | Saída de texto: tabela de decisões, cartão de decisão e resumo da carteira |
| `jev_bot/cli.py` | Comandos de linha e bloqueio de carteira contra processos concorrentes |
| `jev_bot/configuracao.py` | Leitura e gravação do `.env` sem registrar credenciais; cofre keyring opcional |

## Fontes e métricas

Não existe gerador aleatório, motor de decisão offline ou fallback de preços.
Falhas de conexão e ausência de dados são informadas ou excluem o ativo da análise.

O catálogo vem de `symbols_get()`, sem lista fixa ou amostragem. Símbolos fora
do Market Watch podem ser selecionados para receber cotações; essa operação
altera a seleção de símbolos do terminal, sem negociar. Contratos desabilitados,
somente para fechamento, personalizados ou sem cotação válida e recente são
excluídos **antes** da consulta de candles e do JEV. Em modo contínuo, esses
símbolos são rechecados a cada 15 minutos, sem análise durante a espera.

Aos **sábados e domingos em Asia/Tokyo (UTC+9)**, novas análises são restritas
à categoria Crypto do catálogo do broker. Não há filtro fixo de BTC: todos os
símbolos Crypto são candidatos. Em dias úteis, todas as categorias elegíveis
voltam ao scanner. O filtro ocorre antes de ticks, candles e JEV dos demais
mercados; não remove catálogo, trades ou posições existentes. O catálogo é
consultado na abertura, na mudança do dia ou a cada 15 minutos; entre essas
consultas, o painel monitora o universo escolhido sem remapear outras categorias.

A API Python do MT5 não disponibiliza o calendário de sessões. Portanto,
o filtro identifica **inativos ou sem dados recentes**, sem afirmar que um
tick recente garante sessão aberta. Bid/ask válidos, tick com até 120 segundos,
permissão de negociação e candles recentes são requisitos conservadores.
Ativos sem informação suficiente não são analisados.

O estado de mercado contém 100 candles M1 encerrados, com OHLC e volume
de ticks, cotação bid/ask e metadados do contrato. Não usa indicadores
técnicos convencionais, médias, momentum ou regime de tendência calculado.
Volume de ticks não é volume financeiro; notícias/sentimento não são inventados.
O JEV recebe os dados reais serializados e escolhe a ação.

Uma proposta de cálculo inspirado em amplitudes complexas está em
[PROPOSTA-CALCULO.md](PROPOSTA-CALCULO.md), ainda sem implementação.
Não representa computação quântica física nem previsão financeira validada.

Os timestamps observados neste terminal IC Markets estavam três horas à
frente do UTC do computador. O ajuste padrão é explícito, de três horas:

```powershell
python -m jev_bot --mt5-time-offset-hours 3 scan
```

Verifique esse ajuste após mudanças de horário do broker. Para um terminal
que retorne UTC, use `--mt5-time-offset-hours 0`. Um ajuste incorreto pode excluir
ativos por dados antigos/futuros; o programa não corrige o relógio por suposição.

## Uso

As opções globais devem vir antes do comando.

```powershell
# Dashboard contínuo, em uma única tela fixa. Sem subcomando abre o mesmo painel.
python -m jev_bot
python -m jev_bot dashboard

# Consulta todo o catálogo elegível, sem exigir JEV e sem abrir posições.
python -m jev_bot scan
python -m jev_bot --continuous --interval 60 scan

# Consulta JEV, sem executar entradas paper.
python -m jev_bot decisions
python -m jev_bot card EURUSD

# Mostra o estado registrado da carteira; marcação não é atualizada por status.
python -m jev_bot status

# Iniciar trading: habilita paper explicitamente, sem realizar um ciclo.
python -m jev_bot start

# Abre o painel e ativa trading paper automaticamente se Jev estiver configurado.
python -m jev_bot run
python -m jev_bot --interval 60 dashboard

# Verificação com um único scan completo e saída em texto para logs.
python -m jev_bot --once --plain dashboard

# Pausa novas entradas, inclusive em outro processo que esteja rodando.
python -m jev_bot stop
```

Ao abrir dashboard/run com Jev configurado, a carteira inicia automaticamente ativa, com capital paper de **10.000 na moeda da conta MT5**.
O saldo real da conta não é utilizado como capital paper. `start` não limpa
perdas, posições ou limites. `stop` cria um sinal de pausa observado antes da
próxima entrada; posições permanecem registradas. Para acompanhar stops,
é necessário manter o painel em execução. Mesmo pausado, ele escaneia os
candidatos do dia, acompanha posições e pode consultar JEV quando configurado;
a pausa bloqueia entradas. Sem JEV, mostra `WAITING` por ativo e continua
coletando dados sem inventar BUY/SELL/probabilidade. Com o processo parado, não existe
monitoramento ou stop no broker. Gaps podem ultrapassar o risco planejado.

## Painel e comandos

Execute `iniciar-painel.cmd` ou `python -m jev_bot` no terminal interativo.
É **um único quadro**, com linhas atualizadas no mesmo lugar e cursor oculto
durante a execução; não abre janelas e não imprime uma nova tela a cada evento.
Os campos ficam em posições fixas, inclusive quando posições abrem/fecham.
`--plain` é destinado a logs: imprime atividades e um único quadro final.

- **I**: Iniciar trading paper, exigindo configuração JEV e limites de risco.
- **P**: Pausar novas entradas; scanner e acompanhamento continuam.
- **Q** ou **Ctrl+C**: Encerrar, aguardar a chamada em andamento e salvar o banco.

`dashboard` e `run` ativam trading paper automaticamente quando a configuração Jev é válida. Uma pausa anterior é removida ao iniciar um novo painel; P/stop interrompem as entradas da sessão.
Ao encerrar o painel, a carteira fica pausada. `stop` funciona em outro terminal.
`start` altera a carteira somente quando não há outro processo gravando;
use **I** no painel em execução.

O quadro separa **conta real MT5** (saldo, equity e P&L) de **carteira paper**
(capital, crescimento, resultado realizado/aberto, ganhos, losses, drawdown
atual/máximo, risco agregado, margem, WIN/LOSS e posições). Identificadores
de conta, login, servidor e credenciais não são exibidos ou gravados no banco.
O progresso mostra catálogo, candidatos do dia, quantidade verificada, dados
recebidos, inativos e históricos indisponíveis. Cada ativo tem etapas e motivo
de SIM/NÃO. Uma falha JEV não encerra o universo inteiro; o painel aguarda
60 segundos antes de tentar o serviço novamente.

O intervalo padrão entre scans completos é **15 segundos**. Entre eles,
o painel consulta cotações reais em rodízio a cada meio segundo, mostra
bid/ask, mudança de preço, total de consultas e contagem regressiva para
o próximo scan. `TRADING: PAUSADO` bloqueia entradas, sem parar esse monitoramento.

## Banco local

`db.json` é a fonte de verdade: guarda capital, moeda, limites,
posições, eventos, referência diária, drawdown e **todos os trades paper**.
`trades` mantém identificador, ativo, direção, lote, entrada, stop, alvo,
risco, margem, data de abertura, P&L e estado OPEN/CLOSED. No fechamento,
registra preço de saída, data e motivo STOP/TARGET. Trades fechados são mantidos.
O histórico anterior permanece em `log`; posições legadas são importadas
sem inventar dados de abertura ausentes. Se existir `data/paper.json` e não
existir `db.json`, a primeira gravação importa a carteira anterior sem resetar
capital, perdas, limites ou posições. O arquivo antigo é preservado.

A gravação é atômica e a
execução usa um arquivo de bloqueio contra processos concorrentes.
Não apague esse estado para reiniciar perdas. Corrupção ou mudança da moeda
da conta bloqueia a utilização da carteira. `--book` permite escolher o
caminho; use sempre o mesmo arquivo para a mesma carteira. Uma interrupção
forçada pode deixar `.lock`; verifique que não existe processo usando a
carteira antes de removê-lo. O arquivo `.pause` mantém a pausa até `start` ou **I**.

## Gerenciamento de risco

| Controle | Limite padrão |
| --- | --- |
| Risco por operação, calculado até o stop | 2% do patrimônio paper |
| Risco agregado de posições abertas | 15% |
| Risco compartilhado por moeda | 15% |
| Perda diária, incluindo P&L aberto | 2% |
| Drawdown desde o maior patrimônio registrado | 10% |
| Posições simultâneas | Sem teto fixo; sujeitas ao risco agregado |
| Margem estimada comprometida | 30% do patrimônio |
| Confiança / probabilidade mínimas | 75% / 65% |
| Spread | até 0,1% do preço e 7,5% da distância do stop |

O stop usa o maior entre duas vezes o maior deslocamento absoluto entre
fechamentos consecutivos da amostra, a distância mínima do contrato mais
um ponto e duas vezes o spread. Esse cálculo é uma proteção por movimento
bruto observado, não um indicador ou sinal de entrada;
o alvo usa duas vezes a distância do stop. `order_calc_profit` e
`order_calc_margin` fornecem os valores na moeda da conta. O lote é arredondado
para baixo conforme o passo do contrato. Se o lote mínimo exceder o orçamento,
não há entrada. Direções proibidas, duplicações de ativo, valores inválidos,
NaN e ausência de marcação recente de qualquer posição também bloqueiam entradas.
O limite por moeda é uma regra conservadora de exposição compartilhada,
não uma estimativa estatística de correlação entre ativos.

Entradas usam ask para compra e bid para venda; marcação e saídas usam o lado
oposto. Stops/alvos paper fecham ao preço observado, inclusive gaps. Custos
de comissão, swap, slippage e execução real não são modelados. Não há promessa
de que uma perda ficará exatamente limitada ao stop planejado.

A fronteira diária é UTC. O bloqueio diário permanece até o próximo dia;
reiniciar o programa não o remove. O bloqueio de drawdown fica registrado
permanentemente nessa carteira e não é reiniciado por `start` ou pela virada do dia.
Durante ciclos contínuos, posições são marcadas entre decisões e a cada
cinco segundos durante a espera. Latência do MT5/JEV pode atrasar a marcação.

## JEV e configuração

O scanner prioriza dez contratos memecoin identificados no catálogo IC Markets:
`DOGUSD`, `PENGUUSD`, `WIFUSD`, `FARTCOINUSD`, `SPXUSD`, `TRUMPUSD`,
`1000xSHIB`, `1000xBONK`, `1000xFLOKI` e `1000xPEPE`. Isso altera a ordem
da análise, mantendo todas as demais criptos elegíveis e os mesmos limites de risco.
Entre scans, o painel consulta até oito ativos por rodada de 0,5 segundo, em
rodízio por todo o universo coletado. Terminais com pelo menos 100 colunas mostram
até três colunas de ativos e mantêm memecoins primeiro, identificados com `*`.

Preencha `OPENROUTER_API_KEY` no `.env` da raiz. O código lê:

```dotenv
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=
OPENROUTER_API_ENDPOINT=https://openrouter.ai/api/alpha/decisions
OPENROUTER_MODEL=typesafe/jev-1.13
```

OpenRouter é o gateway; TypeSafe fornece o modelo Jev. A API de decisões
recebe `model`, `state` e `questions.action` com critérios BUY/SELL/HOLD/AVOID.
O adaptador valida `choice`, a distribuição `probabilities` e `confidence`.
As variáveis antigas `TYPESAFE_API_KEY`/`TYPESAFE_API_BASE` são preservadas no
arquivo, mas não são usadas por este adaptador. Não há fallback offline.
Reinicie o painel após editar o `.env`; variáveis do processo têm precedência.
Referência: [Jev no OpenRouter](https://openrouter.ai/blog/insights/what-is-jev/).

O mesmo arquivo contém `MT5_TERMINAL_PATH` e `MT5_TIME_OFFSET_HOURS`.
Use aspas simples para o caminho Windows, evitando interpretação de barras
como escapes. `MT5_LOGIN`, `MT5_PASSWORD` e `MT5_SERVER` ficam reservados para
uma integração futura: não são usados para fazer login, trocar conta ou
negociar. **Não existe execução real implementada ou habilitada**, mesmo
se esses campos forem preenchidos. Uma futura integração exigirá autorização.
O `.env` e o diretório de dados locais são ignorados pelo Git.

Uma resposta ausente, inválida ou falha externa bloqueia a entrada e mantém
o scanner ativo. Os testes do contrato usam HTTP simulado; a autenticação real
só pode ser confirmada após configurar uma chave válida. A confiança retornada
pelo modelo não representa uma probabilidade comprovada de lucro no mercado.

## Verificação

```powershell
python tests.py
python -X utf8 testes_novos.py
python -m compileall -q jev_bot
python -m jev_bot scan
```

Os testes usam doubles isolados de MT5/JEV/DeepSeek para verificar risco,
cotação antiga/futura, cache de inativos antes dos candles, stops com gap,
persistência, pausa, concorrência e consulta sem execução. `testes_novos.py`
cobre o motor matemático de 30 dias, as regras de padrões e a validação
fora da amostra, o schema e o orçamento do DeepSeek, o orquestrador
(cadência, PARADO POR RISCO, emergência, fila de comandos) e uma fumaça
da interface offscreen. Esses doubles não são fontes disponíveis no
programa. A verificação MT5 real é separada dos testes unitários;
`verificar_janela30d.py` (somente leitura) confere a janela de 30 dias e
`verificar_gui_offscreen.py` exercita a interface com motor simulado.

As imagens antigas em `assets/` e `examples/sample_run.txt` são registros
históricos da versão demonstrativa, preservados como referência; não
representam resultados atuais. Nenhum retorno paper deve ser apresentado
como performance de negociação real.

## Interface gráfica (PySide6)

```powershell
python -m jev_bot gui
```

ou `iniciar-gui.cmd`. A janela é redimensionável, maximizável, com tema
escuro e divisores arrastáveis (`QSplitter`) entre painéis e tabelas.
Todo trabalho de MT5, análise e negociação roda em uma thread separada
(`QThread`); a interface recebe atualizações por sinais Qt e nunca
bloqueia. GUI e painel de terminal compartilham o mesmo `db.json`, com
arquivo de bloqueio contra acesso concorrente.

O pipeline gráfico é **Python → DeepSeek → JEv**:

1. **Cálculos matemáticos (Python)**: janela M1 móvel de 30 dias por ativo,
   timestamps normalizados para UTC, lacunas contadas (não preenchidas),
   custo de spread e estatísticas de movimentos, retornos condicionais,
   recuperação após quedas, retração após altas e tempos até eventos.
   Não usa indicadores técnicos e não consome tokens.
2. **Análise DeepSeek**: iniciada automaticamente (no máximo uma consulta
   por ativo por hora, duas por ciclo, com teto de tokens por dia e de
   chamadas por hora configuráveis em `DEEPSEEK_*`). Recebe apenas o
   resumo compacto dos 70% iniciais da janela; os 30% finais são
   reservados para validação fora da amostra. Propõe hipóteses que
   precisam usar tipos de regra suportados com parâmetros dentro de
   faixas explícitas; nunca devolve probabilidades.
3. **Validação local**: cada hipótese é reproduzida em Python sobre a
   janela completa, com contagem de eventos, taxa de acerto, magnitude
   média/mediana, dispersão, custo de spread e avaliação cronológica
   fora da amostra (últimos 30%). Requisitos mínimos: 40 eventos totais,
   12 fora da amostra, taxa >= 55% em ambos e expectativa líquida
   positiva. Status: candidato, validado, rejeitado ou expirado
   (validade de 7 dias). Repositório em `data/padroes/<SIMBOLO>.json`,
   com escrita atômica, deduplicação e versionamento de schema.
4. **Decisão JEv**: somente padrões validados com a condição verdadeira no
   candle fechado geram consulta ao JEv, que decide SIM ou NÃO; o gate de
   risco (`risk.check`) é determinístico e bloqueia independentemente da
   decisão. O operador edita risco por trade, exposição máxima e drawdown
   máximo na aba Operação; a IA não altera limites.

Cadência: sem posições abertas, o ciclo roda a cada 1 minuto, alinhado ao
fechamento do candle M1 (+2s); com posições, a cada 15 minutos. O
monitoramento de preços, posições, stops e limites roda em tempo real
independente dessa cadência. Ao atingir limite de exposição ou drawdown,
o robô entra em **PARADO POR RISCO**: análises e entradas param, e só
voltam com "LIBERAR APÓS REVISÃO" (liberação manual registrada no log).
**EMERGÊNCIA — FECHAR TUDO** exige confirmação, bloqueia entradas na hora,
tenta fechar todas as posições paper ao preço observado e alerta falhas:
posição sem cotação válida permanece aberta, com nova tentativa
automática; nada é marcado como encerrado sem confirmação do simulador.

Aba Resumo exibe curva de patrimônio (realizada dos fechamentos mais
patrimônio atual com P&L aberto), drawdown desde o pico, retorno,
taxa de acerto e estatísticas, com filtros de período e ativo. Botão
direito nos gráficos: **COPIAR COMO IMAGEM** para a área de transferência.

Aba Configurações: chave OpenRouter mascarada (gravada no cofre do
sistema via `keyring` quando disponível; sem keyring, no `.env`, com
aviso explícito), identificador do modelo DeepSeek editável e verificado
pelo TESTAR CONEXÃO, caminho do terminal MT5 com PROCURAR e teste em
processo isolado, credenciais MT5 reservadas (o programa usa a conta já
aberta) e **PAPER TRADING fixo** — trading real não é habilitado por
teste de conexão e exigirá fluxo separado de autorização.

O painel de terminal (`python -m jev_bot`) continua disponível com o
fluxo direto JEv, sem padrões; não execute os dois ao mesmo tempo sobre
a mesma carteira (o `.lock` impede).

## Roadmap

Pendências conhecidas: modo real MT5 (fluxo de autorização separado),
gráficos de candles por ativo na GUI e alertas sonoros.

## Referências

- [Integração Python oficial do MT5](https://www.mql5.com/en/docs/python_metatrader5)
- [Candles por posição](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py)
- [Último tick](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfotick_py)
- [Informações do contrato](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py)

Licença MIT; veja [LICENSE](LICENSE).

## Operações curtas M1

O saldo simulado inicial é 10.000 JPY na conta JPY conectada e independe do
saldo real do MT5. O painel mostra saldo/equity paper e conta real separadamente;
o histórico não é reiniciado ao abrir a interface. A tabela compacta apresenta
ação, probabilidade/confiança e bloqueios CUSTO, LOTE ou LIMIAR. O contador
de custos usa exatamente a mesma regra de spread do controle de risco.
Os resumos matemáticos incluem ajuste linear, dispersão, concordância das
janelas 5/10/20 e deslocamento hipotético de cinco minutos medido em spreads.
Essa extrapolação não é uma previsão validada. O payload envia os últimos
20 candles brutos, mantendo a coleta de 100 candles para proteção e diagnóstico.

Somente candles M1 fechados são usados. Os últimos 20 candles alimentam
resumos de deslocamento, eficiência direcional, custo em spreads e amplitudes
complexas experimentais. São descrições do histórico, não probabilidades de lucro.
Jev recebe esses números e os candles brutos; BUY/SELL ainda exigem aprovação
pelos limites de risco. Não há ordem obrigatória quando Jev retorna HOLD/AVOID.
A lista prioriza memecoins e, dentro de cada grupo, eficiência direcional absoluta.
Cada candle fechado é avaliado uma vez por ativo na sessão, evitando consultas
repetidas ao mesmo estado. Após cinco minutos, posições paper encerram no próximo
bid/ask válido observado, incluindo eventual perda; stops/alvos podem encerrar antes.
Uma interrupção ou ausência de cotação pode atrasar essa saída. Ao sair, o programa
salva a carteira pausada. Falhas HTTP mostram o código sem registrar segredos.

O perfil de risco versão 2 migra o db.json antigo na próxima abertura do painel,
preservando saldo, histórico e bloqueios. Perda diária continua em 2%; margem,
spread, lote mínimo e decisão Jev continuam sendo verificados. Não há quantidade
fixa de posições permitidas: a soma do risco dos stops não pode exceder 15%.
