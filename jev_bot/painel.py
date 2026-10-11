"""Dashboard contínuo no terminal com scanner, comandos e carteira paper."""

from collections import OrderedDict, deque
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sys
import threading
import time
import re

from . import jev
from .execution.paper import Book
from .loop import run
from .markets import FonteMT5, eh_memecoin
from .risk import bloqueio_spread

FUSO = timezone(timedelta(hours=9), "Asia/Tokyo")
RAIZ_PROJETO = Path(__file__).resolve().parent.parent
BANCO_PADRAO = RAIZ_PROJETO / "db.json"


def status_curto(status):
    """Preserva ação, probabilidades e bloqueio numa célula compacta do painel."""
    resposta = re.search(r"Jev (BUY|SELL|HOLD|AVOID) p=(\d+)% c=(\d+)%", status)
    if resposta:
        acao, prob, confianca = resposta.groups()
        bloqueio = ""
        if "Spread" in status:
            bloqueio = " CUSTO"
        elif "Lote mínimo" in status:
            bloqueio = " LOTE"
        elif "abaixo do limite" in status:
            bloqueio = " LIMIAR"
        elif "SIM:" in status:
            bloqueio = " ABRIU"
        elif acao in ("BUY", "SELL"):
            bloqueio = " RISCO"
        return f"{acao:<5} {prob:>2}/{confianca:>2}%{bloqueio}"
    if "WAITING" in status:
        return "API EM ESPERA"
    if status.startswith("jev:"):
        return "ANALISANDO ONLINE"
    if status.startswith("ignorado:"):
        return "INATIVO / SEM DADOS"
    return status.split(": ", 1)[-1]


def carregar_carteira(arquivo, moeda):
    """Importa o estado legado quando db.json ainda não existe, sem apagar perdas."""
    destino = Path(arquivo)
    legado = RAIZ_PROJETO / "data/paper.json"
    if destino.resolve() == BANCO_PADRAO.resolve() and not destino.exists() and legado.exists():
        book = Book.load(legado, moeda)
        if Path(str(legado) + ".pause").exists():
            book.active = False
        return book
    return Book.load(destino, moeda)


def desenhar(dados, largura=110, altura=40):
    """Monta um quadro de status sem mostrar conta, login, servidor ou segredos."""
    m = dados.get("metricas", {})
    conta = dados.get("conta")
    resumo = dados.get("resumo", {})
    moeda = dados.get("moeda", "")
    animacao = "|/-\\"[int(time.monotonic() * 2) % 4]
    linhas = ["╔" + "═" * max(0, largura - 2) + "╗",
              f" AV TRADING  {animacao}  • SCALPING M1 • PAPER JPY | {datetime.now(FUSO):%d/%m/%Y %H:%M:%S} Tokyo",
              f"TRADING: {'ATIVO' if dados.get('ativo') else 'PAUSADO'} | SCANNER: {dados.get('fase', 'Conectando MT5')} | {dados.get('simbolo', '')}",
              f"ATIVIDADE: {dados.get('mensagem', 'Inicializando')} | ciclo {dados.get('ciclo', 0)}",
              f"UNIVERSO: {'SÁB/DOM (Tokyo): SOMENTE CRIPTO + MEMECOINS' if resumo.get('fim_de_semana') else 'Todos os mercados elegíveis'} | candidatos {resumo.get('candidatos', 0)}",
              f"SCAN: {resumo.get('verificados', 0)}/{resumo.get('candidatos', 0)} verificados | dados {resumo.get('analisados', 0)} | inativos {resumo.get('inativos', 0)} | excluídos pelo dia {resumo.get('fora_dia', 0)} | sem histórico {resumo.get('sem_historico', 0)}"]
    if conta:
        idade = max(0, time.monotonic() - dados.get("conta_instante", time.monotonic()))
        linhas.append(f"CONTA REAL ({conta['currency']}): saldo {conta['balance']:,.2f} | equity {conta['equity']:,.2f} | P&L {conta['profit']:+,.2f} | há {idade:.0f}s")
    else:
        linhas.append("CONTA REAL: aguardando métricas MT5")
    if m:
        linhas.extend([
            f"PAPER ({moeda}): SALDO SIMULADO {m['equity'] - m['open_profit']:,.2f} | equity {m['equity']:,.2f} | crescimento {m['growth']:+.2%}",
            f"P&L PAPER: realizado {m['realized']:+,.2f} | aberto {m['open_profit']:+,.2f} | ganhos {m['gains']:,.2f} | losses {m['losses']:,.2f}",
            f"DRAWDOWN: atual {m['drawdown']:.2%} | máximo {m['max_drawdown']:.2%} | resultado diário {m['daily_return']:+.2%}",
            f"RISCO: {m['risk']:,.2f} ({m['risk_fraction']:.2%}) | margem {m['margin']:,.2f} | posições {m['positions']} / {'sem teto fixo' if dados['limites']['max_positions'] is None else dados['limites']['max_positions']}",
            f"LIMITES: {dados['limites']['risk_per_trade']:.2%}/operação | {dados['limites']['max_total_risk']:.2%} total | {dados['limites']['max_daily_loss']:.2%} perda/dia | {dados['limites']['max_drawdown']:.2%} drawdown",
            f"RESULTADOS: {m['closed']} fechados | WIN {m['wins']} | LOSS {m['losing_trades']} | marcação {'válida' if dados.get('precos_validos') else 'pendente/antiga: entradas bloqueadas'}",
        ])
    else:
        linhas.extend(["PAPER: carregando carteira db.json", "P&L PAPER: aguardando", "DRAWDOWN: aguardando",
                       "RISCO: aguardando", "LIMITES: aguardando", "RESULTADOS: aguardando"])
    linhas.append("── POSIÇÕES PAPER • ativo / direção / lote / entrada / stop / alvo / P&L ──")
    for indice in range(3 if altura >= 38 else 1):
        posicoes = dados.get("posicoes", [])
        if indice < len(posicoes):
            p = posicoes[indice]
            linhas.append(f"{p['symbol']:<16} {p['action']:<4} {p['volume']:g} | {p['entry']:.8g} / {p['stop']:.8g} / {p['target']:.8g} | {p['profit']:+,.2f}")
        else:
            linhas.append("  Nenhuma posição aberta • aguardando sinal e aprovação de risco" if indice == 0 else f"  Slot {indice + 1} livre")
    linhas.append(f"── ÚLTIMOS ATIVOS • MEME {resumo.get('memecoins', 0)} • custo bloqueia {dados.get('custos_bloqueados', 0)} • AÇÃO P/CONF ──")
    quantidade = max(2, min(16, altura - len(linhas) - 6))
    colunas = 3 if largura >= 115 else 2 if largura >= 70 else 1
    ativos = dados.get("ativos", [])
    ativos = sorted(reversed(ativos), key=lambda ativo: not eh_memecoin(ativo[0]))[:quantidade * colunas]
    espaco = (largura - 3) // colunas
    for indice in range(quantidade):
        celulas = []
        for simbolo, status in ativos[indice * colunas:(indice + 1) * colunas]:
            ultimo = dados.get("decisoes", {}).get(simbolo)
            resumo_status = status_curto(status if status.startswith("jev:") else ultimo or status)
            celulas.append(f"{'*' if eh_memecoin(simbolo) else ' '}{simbolo:<12} {resumo_status}"[:espaco].ljust(espaco))
        linhas.append(" | ".join(celulas))
    linhas.append("── EVENTOS DE TRADING / STATUS ──")
    ultimos_eventos = dados.get("eventos", [])[-2:]
    linhas.extend(ultimos_eventos + [""] * (2 - len(ultimos_eventos)))
    rodape = [f"JEV: {dados.get('jev', 'verificando')} | banco: {dados.get('banco', 'db.json')}",
              "I: Iniciar trading paper | P: Pausar entradas | Q: Encerrar | Ctrl+C: encerrar",
              "M1 • Saída até 5min com cotação válida • P pausa entradas • Stops exigem processo ativo."]
    linhas = linhas[:max(1, altura - len(rodape))] + rodape
    return "\n".join(l[:largura] for l in linhas)


def ativar_ansi():
    """Habilita atualização por cursor no console Windows sem abrir janelas."""
    if not sys.stdout.isatty():
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel.GetStdHandle(wintypes.DWORD(-11).value)
    modo = wintypes.DWORD()
    if kernel.GetConsoleMode(handle, ctypes.byref(modo)):
        return bool(kernel.SetConsoleMode(handle, modo.value | 0x0004))
    # Terminais VT como VS Code/ConPTY já podem interpretar ANSI sem HANDLE nativo.
    import os
    return bool(os.environ.get("TERM") or os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"))


class TelaFixa:
    """Atualiza somente linhas alteradas em uma única tela, sem scroll por quadros."""

    def __init__(self, saida):
        """Mantém a grade anterior e limpa o console apenas ao abrir o painel."""
        self.saida = saida
        self.anterior = []
        self.aberta = False

    def atualizar(self, texto):
        """Reposiciona o cursor nas linhas mudadas, sem imprimir novas telas."""
        linhas = texto.split("\n")
        comandos = []
        if not self.aberta:
            comandos.append("\033[?25l\033[2J\033[H")
            self.aberta = True
        for indice in range(max(len(linhas), len(self.anterior))):
            nova = linhas[indice] if indice < len(linhas) else ""
            antiga = self.anterior[indice] if indice < len(self.anterior) else None
            if nova != antiga:
                cor = "\033[37m"
                if nova.startswith((" AV TRADING", "╔")):
                    cor = "\033[1;96m"
                elif any(nova.startswith(t) for t in ("──", "ÚLTIMOS", "POSIÇÕES", "EVENTOS", "UNIVERSO")):
                    cor = "\033[1;94m"
                elif "WAITING" in nova or "PAUSADO" in nova:
                    cor = "\033[93m"
                elif "ERRO" in nova:
                    cor = "\033[91m"
                elif nova.startswith(("PAPER", "P&L", "RESULTADOS")):
                    cor = "\033[92m"
                colorida = re.sub(r"\b(BUY|SELL|HOLD|AVOID|CUSTO|LOTE|ABRIU)\b",
                                  lambda trecho: ("\033[92m" if trecho[0] in ("BUY", "ABRIU") else "\033[91m" if trecho[0] in ("SELL", "CUSTO", "LOTE", "AVOID") else "\033[93m") + trecho[0] + cor, nova)
                comandos.append(f"\033[{indice + 1};1H{cor}{colorida}\033[0m\033[K")
        if comandos:
            self.saida.write("".join(comandos))
            self.saida.flush()
        self.anterior = linhas

    def fechar(self):
        """Restaura o cursor no mesmo terminal após salvar a carteira."""
        if self.aberta:
            self.saida.write(f"\033[?25h\033[{len(self.anterior)};1H\n")
            self.saida.flush()


class PainelTerminal:
    """Mantém a tela responsiva enquanto MT5/JEV trabalham no scanner."""

    def __init__(self, args):
        """Separa solicitações de teclado da thread que possui a carteira e o MT5."""
        self.args = args
        self.mutex = threading.Lock()
        self.encerrar = threading.Event()
        self.iniciar = threading.Event()
        self.pausar = threading.Event()
        self.dados = {"fase": "Conectando", "banco": str(args.book), "eventos": [], "ativos": []}
        self.ativos = OrderedDict()
        self.eventos = deque(maxlen=80)
        self.book = None
        self.fonte = None
        self.erro = None
        self.ultimo_heartbeat = 0
        self.proxima_tentativa_jev = 0
        self.monitorados = OrderedDict()
        self.indice_monitor = 0
        self.cotacoes_verificadas = 0
        self.candles_avaliados = {}
        self.decisoes = {}
        self.pausa_arquivo = Path(str(args.book) + ".pause")

    def publicar(self, **valores):
        """Publica snapshots independentes para a tela sem compartilhar objetos mutáveis."""
        with self.mutex:
            self.dados.update(valores)

    def evento(self, etapa, simbolo, mensagem):
        """Atualiza o ativo atual e acompanha posições durante a varredura."""
        self.controles()
        self.heartbeat()
        if simbolo:
            self.ativos[simbolo] = f"{etapa}: {mensagem}"
            self.ativos.move_to_end(simbolo)
            if etapa == "resultado":
                self.decisoes[simbolo] = mensagem
        if etapa in ("resultado", "catalogo", "erro"):
            self.eventos.append(f"{datetime.now(FUSO):%H:%M:%S} {simbolo} {mensagem}")
        self.publicar(fase=etapa.upper(), simbolo=simbolo, mensagem=mensagem,
                      resumo=dict(self.fonte.resumo) if self.fonte else {},
                      ativos=list(self.ativos.items()), decisoes=dict(self.decisoes), eventos=list(self.eventos))

    def salvar(self):
        """Persiste trades e patrimônio antes de publicar as métricas exibidas."""
        self.book.save(self.args.book)
        self.publicar(metricas=self.book.metrics(), moeda=self.book.currency,
                      capital=self.book.initial_capital, ativo=self.book.active,
                      posicoes=[asdict(p) for p in self.book.fills], limites=asdict(self.book.limits),
                      precos_validos=self.book.prices_valid)

    def controles(self):
        """Aplica início/pausa no proprietário da carteira, sem permitir reset de perdas."""
        if self.book is None:
            return
        if self.pausar.is_set() or self.pausa_arquivo.exists() or self.encerrar.is_set():
            self.book.active = False
        if self.iniciar.is_set():
            self.iniciar.clear()
            if not self.encerrar.is_set():
                try:
                    jev.configuracao()
                    self.pausar.clear()
                    self.pausa_arquivo.unlink(missing_ok=True)
                    self.book.active = True
                    self.eventos.append("Iniciar trading: paper ativo, entradas continuam sujeitas ao risco")
                except RuntimeError:
                    self.book.active = False
                    self.eventos.append("WAITING: configure JEV no .env; scanner continua, entradas bloqueadas")
        self.publicar(ativo=self.book.active)

    def heartbeat(self, forcar=False):
        """Atualiza métricas reais e stops paper, informando marcação antiga na tela."""
        if self.book is None or (not forcar and time.monotonic() - self.ultimo_heartbeat < 5):
            return
        self.ultimo_heartbeat = time.monotonic()
        eventos_antes = len(self.book.log)
        try:
            conta = self.fonte.conta()
            self.book.update(self.fonte)
            self.publicar(conta=conta, conta_instante=time.monotonic())
        except RuntimeError:
            self.book.prices_valid = False
            self.eventos.append("MT5 sem métricas atuais; novas entradas bloqueadas")
        for e in self.book.log[eventos_antes:]:
            if e["event"] == "close":
                self.eventos.append(f"FECHOU {e['symbol']} {e['reason']} | P&L {e['profit']:+.2f} {self.book.currency}")
        self.salvar()

    def avaliar(self, estado):
        """Segue para o próximo ativo mesmo se JEV não responder ou não estiver configurado."""
        self.monitorados[estado.symbol] = estado
        self.controles()
        instante = estado.candles[-1]["time"] if estado.candles else None
        if instante is not None and self.candles_avaliados.get(estado.symbol) == instante:
            self.evento("monitorando", estado.symbol, "M1 já avaliado; aguardando novo candle fechado")
            return
        if time.monotonic() < self.proxima_tentativa_jev:
            self.evento("resultado", estado.symbol, "WAITING API: dados coletados; sem decisão Jev")
            return
        registros = run([estado], self.book, self.book.limits, execute=True, fonte=self.fonte,
                         persistir=self.salvar, evento=self.evento,
                         pausado=lambda: self.pausar.is_set() or self.pausa_arquivo.exists(),
                         cancelado=self.encerrar.is_set)
        if registros and registros[0][1] is None:
            self.proxima_tentativa_jev = time.monotonic() + 60
            self.publicar(jev="WAITING: indisponível/não configurado; nova tentativa em 60s")
        elif registros:
            self.publicar(jev="Resposta recebida")
            if instante is not None:
                self.candles_avaliados[estado.symbol] = instante
        self.salvar()

    def monitorar(self, restante):
        """Consulta até oito contratos por rodada, sem chamadas MT5 concorrentes."""
        self.controles()
        self.heartbeat()
        nomes = list(self.monitorados)
        if not nomes:
            self.publicar(fase="MONITORANDO", mensagem=f"Sem ativos com dados válidos; novo scan em {restante:.0f}s")
            return
        consultados = 0
        for _ in range(min(8, len(nomes))):
            if self.encerrar.is_set():
                break
            simbolo = nomes[self.indice_monitor % len(nomes)]
            self.indice_monitor += 1
            estado = self.monitorados[simbolo]
            if self.fonte.fim_de_semana() and estado.asset_class.casefold() not in ("crypto", "cryptos", "criptomoedas", "cryptocurrencies"):
                self.monitorados.pop(simbolo, None)
                continue
            tick = self.fonte.cotacao(simbolo)
            self.cotacoes_verificadas += 1
            consultados += 1
            if tick is None:
                mensagem = "Cotação indisponível/antiga"
            else:
                bid, ask, instante = tick
                estado.bid, estado.ask, estado.tick_time = bid, ask, instante
                mensagem = f"bid {bid:.8g} / ask {ask:.8g}"
            self.evento("monitorando", simbolo, mensagem)
        self.publicar(mensagem=f"{consultados} ativos nesta rodada | {len(self.monitorados)} monitorados | scan em {restante:.0f}s | cotações verificadas {self.cotacoes_verificadas}")

    def trabalhar(self):
        """Executa todos os candidatos, repete ciclos e mantém o banco consistente ao sair."""
        try:
            with FonteMT5(self.args.terminal, self.args.mt5_time_offset_hours) as fonte:
                self.fonte = fonte
                self.book = carregar_carteira(self.args.book, fonte.moeda)
                self.book.active = False
                self.salvar()
                try:
                    jev.configuracao()
                    self.pausa_arquivo.unlink(missing_ok=True)
                    self.book.active = True
                    self.salvar()
                    self.publicar(jev="OpenRouter / Jev configurado; aguardando resposta online")
                except RuntimeError:
                    self.publicar(jev="WAITING: confira OPENROUTER_API_KEY e configuração no .env")
                    self.proxima_tentativa_jev = time.monotonic() + 60
                fonte.evento, fonte.cancelado = self.evento, self.encerrar.is_set
                ciclo = 0
                while not self.encerrar.is_set():
                    inicio = time.monotonic()
                    ciclo += 1
                    self.publicar(ciclo=ciclo)
                    self.monitorados.clear()
                    self.heartbeat(forcar=True)
                    estados = list(fonte.estados())
                    self.publicar(custos_bloqueados=sum(bool(bloqueio_spread(s, self.book.limits)) for s in estados))
                    estados.sort(key=lambda s: (not eh_memecoin(s.symbol), -abs(s.mathematics.get("eficiencia_direcional", 0))))
                    for estado in estados:
                        if self.encerrar.is_set():
                            break
                        self.avaliar(estado)
                    self.publicar(resumo=dict(fonte.resumo))
                    self.evento("monitorando", "", "Scan concluído; monitoramento de cotações ativo")
                    if self.args.once:
                        break
                    while not self.encerrar.is_set() and time.monotonic() - inicio < self.args.interval:
                        restante = max(0, self.args.interval - (time.monotonic() - inicio))
                        self.monitorar(restante)
                        self.encerrar.wait(0.5)
        except (RuntimeError, OSError, ValueError, TypeError, AttributeError):
            self.erro = "Falha controlada no MT5/banco; confira conexão e integridade do arquivo. Nenhuma ordem real enviada."
            self.publicar(fase="ERRO", mensagem=self.erro)
        finally:
            if self.book is not None:
                self.book.active = False
                try:
                    self.salvar()
                except OSError as exc:
                    codigo = getattr(exc, "winerror", None) or exc.errno
                    self.erro = f"Não foi possível persistir db.json (código {codigo}); entradas interrompidas. Verifique permissões/espaço ou arquivo aberto."
                    self.publicar(fase="ERRO", mensagem=self.erro)
            if not self.erro:
                self.publicar(fase="ENCERRADO", mensagem="Scanner encerrado; carteira paper pausada e salva")

    def executar(self):
        """Renderiza continuamente e lê atalhos sem bloquear a coleta de dados."""
        trabalhador = threading.Thread(target=self.trabalhar, name="scanner-mt5")
        trabalhador.start()
        interativo = not self.args.plain and ativar_ansi()
        tela_fixa = TelaFixa(sys.stdout)
        teclado = None
        if sys.stdin.isatty() and sys.platform == "win32":
            import msvcrt as teclado
        ultima_impressao = 0
        ultima_atividade = None
        try:
            while trabalhador.is_alive():
                if teclado and teclado.kbhit():
                    tecla = teclado.getwch().lower()
                    if tecla == "i":
                        self.iniciar.set()
                    elif tecla == "p":
                        self.pausar.set()
                    elif tecla == "q":
                        self.encerrar.set()
                tamanho = shutil.get_terminal_size((110, 40))
                if interativo or time.monotonic() - ultima_impressao >= 2:
                    with self.mutex:
                        snapshot = dict(self.dados)
                    if interativo:
                        tela_fixa.atualizar(desenhar(snapshot, max(20, min(tamanho.columns - 1, 120)), max(10, tamanho.lines - 1)))
                    else:
                        atividade = (snapshot.get("fase"), snapshot.get("simbolo"), snapshot.get("mensagem"))
                        if atividade != ultima_atividade:
                            print(f"{datetime.now(FUSO):%H:%M:%S} {' | '.join(str(x or '') for x in atividade)}", flush=True)
                            ultima_atividade = atividade
                    ultima_impressao = time.monotonic()
                trabalhador.join(0.5)
        except KeyboardInterrupt:
            self.encerrar.set()
            print("Encerrando: aguardando a chamada MT5/JEV atual para salvar db.json...", flush=True)
            trabalhador.join()
        finally:
            self.encerrar.set()
            trabalhador.join()
        with self.mutex:
            snapshot = dict(self.dados)
        if interativo:
            tamanho = shutil.get_terminal_size((110, 40))
            tela_fixa.atualizar(desenhar(snapshot, max(20, min(tamanho.columns - 1, 120)), max(10, tamanho.lines - 1)))
            tela_fixa.fechar()
        else:
            print(desenhar(snapshot, 110, 40), flush=True)
        if self.erro:
            raise RuntimeError(self.erro)
