"""Orquestrador: cadência 1/15min e pipeline Python → DeepSeek → JEv → risco → paper.

Sem posições, o ciclo de análise roda a cada 1 minuto, sincronizado ao
fechamento do candle M1 (+2s de margem). Com posições abertas, o ciclo
passa a 15 minutos; monitoramento de preços, posições e risco continua em
tempo real. Ciclos nunca se sobrepõem: se o anterior ainda estiver em
andamento, o próximo é adiado com registro. Ao atingir limite de
exposição ou drawdown, novas análises e entradas param imediatamente
(estado PARADO POR RISCO) e só voltam com liberação manual; o
monitoramento de posições e a emergência nunca param.

Desempenho: o download do histórico é paginado em lotes curtos com
cancelamento cooperativo e progresso; a janela completa é descartada
após estatísticas/validação, mantendo apenas uma cauda recente por
ativo (memória limitada). As publicações para a interface são
coalescidas em pacotes por flush — a thread da GUI recebe dezenas de
mensagens por ciclo, não milhares.

Seleção de mercados: apenas símbolos marcados para Monitorar têm
histórico baixado e análise agendada; somente os marcados para Operar
chegam ao fluxo de aprovação JEv. Posições abertas continuam
supervisionadas mesmo se o símbolo for desmarcado. A IA não altera
limites; o gate de risco é determinístico.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import queue
import threading
import time

from . import cache_m1, deepseek, jev, logs, mercados, padroes, risk
from .markets import FonteMT5
from .motor_matematico import estatisticas, janela_descoberta, resumo_compacto
from .painel import BANCO_PADRAO, carregar_carteira

JANELA_TTL_S = 1800          # janela M1 de 30 dias é refrescada a cada 30min por ativo
JANELAS_POR_CICLO = 4        # atualizações de janela por ciclo (round-robin)
DEEPSEEK_TTL_S = 3600        # análise DeepSeek por ativo no máximo a cada hora
DEEPSEEK_POR_CICLO = 2       # novas consultas por ciclo
TENTATIVA_JEV_S = 60         # recuo após falha JEv
TENTATIVA_DEEPSEEK_S = 120   # recuo global após falha DeepSeek
MARGEM_CANDLE_S = 2          # segundos após o fechamento do M1
CAUDA_CANDLES = 720          # cauda guardada por ativo (~12h; cobre regras de 240min)
TENTATIVAS_CONEXAO = 3       # tentativas de conexão MT5 com recuo (2s/4s/6s)
ORCAMENTO_CICLO_S = 50       # orçamento de tempo do ciclo sem posições (cadência 1min)
ORCAMENTO_CICLO_COM_POSICAO_S = 600  # com posições (cadência 15min)
RESERVA_PROCESSAMENTO_S = 15  # fatia final do orçamento reservada ao processamento
FAS = {"VARRENDO": "VARRENDO CATÁLOGO", "BAIXANDO_HISTORICO": "BAIXANDO HISTÓRICO",
       "CALCULANDO": "CALCULANDO", "ANALISANDO_DEEPSEEK": "ANALISANDO DEEPSEEK",
       "VALIDANDO": "VALIDANDO PADRÕES", "DECIDINDO_JEV": "DECIDINDO JEV"}
MAX_AVALIADOS = 2000         # memoria de deduplicação por candle
LOTE_DOWNLOAD = 4320         # candles por chamada ao terminal (3 dias)


def _agora_iso():
    return datetime.now(timezone.utc).isoformat()


def limite_hora_seguro():
    """Teto de chamadas DeepSeek por hora; padrão se o ambiente estiver inválido."""
    from . import configuracao as cfg
    try:
        return cfg.limite_chamadas_hora()
    except RuntimeError:
        return 20


def tokens_dia_seguro():
    from . import configuracao as cfg
    try:
        return cfg.tokens_por_dia()
    except RuntimeError:
        return 300_000


class Coalescedor:
    """Agrupa publicações: listas com teto por canal e registros únicos.

    A thread do motor envia aqui; a interface recebe um pacote por flush.
    Com catálogo grande, isso reduz milhares de mensagens por ciclo a
    dezenas, mantendo a janela fluida.
    """
    TETOS = {"calc": 60, "deepseek": 60, "decisao": 60, "log": 60}
    UNICOS = ("metricas", "estado", "catalogo", "trades")

    def __init__(self):
        self.lotes = {}

    def enviar(self, canal, registro):
        if canal in self.UNICOS:
            self.lotes[canal] = registro
            return
        lote = self.lotes.setdefault(canal, [])
        lote.append(registro)
        teto = self.TETOS.get(canal, 60)
        if len(lote) > teto:
            del lote[:-teto]

    def descarregar(self):
        if not self.lotes:
            return None
        pacote = self.lotes
        self.lotes = {}
        return pacote


class Orquestrador:
    """Possui fonte MT5, carteira, repositório e a cadência do pipeline."""

    def __init__(self, terminal=None, offset_horas=None, banco=None, pasta_dados=None,
                 relogio=time.time, intervalo_com_posicoes=900, caminho_selecao=None):
        raiz = Path(__file__).resolve().parent.parent
        self.terminal = terminal
        self.offset_horas = offset_horas
        self.banco = Path(banco or BANCO_PADRAO)
        self.carga_carteira = carregar_carteira
        self.pasta_dados = Path(pasta_dados or (raiz / "data" / "padroes"))
        self.caminho_selecao = Path(caminho_selecao or mercados.caminho_padrao())
        self.relogio = relogio
        self.intervalo_com_posicoes = intervalo_com_posicoes
        self.coalescedor = Coalescedor()
        self.publicar = self.coalescedor.enviar
        self.entrega = lambda pacote: None
        self.logger = logs.configurar()
        self.encerrar = threading.Event()
        self.evento_pausa = threading.Event()
        self.evento_emergencia = threading.Event()
        self.evento_ciclo_cancelado = threading.Event()
        self.acoes = queue.Queue()
        self.fonte = None
        self.book = None
        self.repositorio = None
        self.orcamento = None
        self.selecao = mercados.carregar(self.caminho_selecao)
        self.ciclo = 0
        self.ciclo_ativo = False
        self.estado = "CONECTANDO"
        self.fase = None            # subestado do trabalho em andamento
        self.detalhe = "Aguardando conexão MT5"
        self.estado_desde = self.relogio()
        self.pausado = True          # pausa inicial; autostart avalia ao conectar
        self.autostart_permitido = True  # PAUSAR/EMERGÊNCIA da sessão desligam
        self.parado_risco = False
        self.emergencia_ativa = False
        self.motivo_risco = ""
        self.erro = None
        self.dados_ativo = {}      # simbolo -> {"instante", "stats", "tail", "custo_pct"}
        self.ordem_janelas = []    # round-robin de atualização
        self.ultimo_deepseek = {}
        self.avaliados = {}        # (simbolo, padrao_id) -> candle_time já avaliado
        self.em_analise = set()    # defesa contra análise duplicada do mesmo ativo
        self.proxima_tentativa_jev = 0
        self.proxima_tentativa_ds = 0
        self.ultimo_monitor = 0
        self.ultimo_persist = 0
        self.trades_conhecidos = 0
        self.jev_configurado = False
        self.deepseek_configurado = False
        self.ultimo_aviso_orcamento = 0
        self.ultimo_aviso_slot = 0
        self.contagem_scan = {}    # resumo agregado do scanner para a GUI
        self._progresso_publicado = None
        self.consultas_ds_ciclo = 0
        self.arquivo_emergencia = Path(str(self.banco) + ".emergencia")
        self.arquivo_pausa = Path(str(self.banco) + ".pause")
        self.cache_m1 = cache_m1.CacheM1(self.pasta_dados.parent / "cache_m1",
                                         relogio=relogio)
        self.prazo_ciclo = None
        self.prazo_scan = None
        self.pendentes_ciclo = []
        self.contadores_ciclo = {}

    # ------------------------------------------------------------------ API
    def iniciar(self):
        """INICIAR: enfileira habilitação de ciclo e entradas paper."""
        self.acoes.put(("iniciar", None, self.relogio()))

    def pausar(self):
        """PAUSAR: para análises e entradas; monitoramento continua."""
        self.evento_pausa.set()
        self.acoes.put(("pausar", None, self.relogio()))

    def retomar(self):
        """RETOMAR: volta ao ciclo normal, equivalente ao INICIAR."""
        self.acoes.put(("retomar", None, self.relogio()))

    def emergencia(self):
        """EMERGÊNCIA — FECHAR TUDO: bloqueia entradas e fecha posições paper."""
        self.evento_emergencia.set()
        self.acoes.put(("emergencia", None, self.relogio()))

    def liberar_risco(self):
        """Liberação manual explícita após revisão do bloqueio de risco."""
        self.acoes.put(("liberar_risco", None, self.relogio()))

    def cancelar_ciclo(self):
        """CANCELAR CICLO: aborta o ciclo atual com segurança no próximo lote."""
        self.evento_ciclo_cancelado.set()

    def aplicar_limites(self, risco_trade, risco_total, drawdown):
        """APLICAR: limites validados e persistidos no motor de risco."""
        self.acoes.put(("limites", {"risk_per_trade": risco_trade,
                                    "max_total_risk": risco_total,
                                    "max_drawdown": drawdown}, self.relogio()))

    def recarregar_config(self):
        """Reavalia configuração OpenRouter/DeepSeek após edição na UI."""
        self.acoes.put(("config", None, self.relogio()))

    def recarregar_selecao(self):
        """Relê a seleção de mercados após salvar na UI, registrando diferenças."""
        self.acoes.put(("selecao", None, self.relogio()))

    def recarregar_selecao_arquivo(self):
        """Relê imediatamente (mesma thread); usado por testes e comandos."""
        self._registrar_selecao()

    def pedir_catalogo(self):
        """Solicita o catálogo real ao motor (thread do MT5) ou o cache offline."""
        self.acoes.put(("catalogo", None, self.relogio()))

    def parar(self):
        """Encerra a thread do motor; a carteira é salva ao final."""
        self.encerrar.set()
        self.acoes.put(("sair", None, self.relogio()))

    def _estado_set(self, estado, detalhe="", registrar=True, fase=None):
        """Transição formal de estado: fonte única de verdade para a GUI.

        Transições para o MESMO estado apenas atualizam o detalhe (sem
        registro de log a cada 5s); mudanças de estado são registradas
        com duração no anterior. Nunca há desvio: o ciclo e a espera
        também passam por aqui, evitando o estado travado em PAUSADO.
        """
        anterior = self.estado
        agora = self.relogio()
        mudou = anterior != estado
        if fase is not None:
            self.fase = fase
        self.estado, self.detalhe = estado, detalhe
        if registrar and mudou:
            self.logger.info("transição de estado", de=anterior, para=estado,
                             duracao_s=round(agora - self.estado_desde, 1),
                             motivo=str(detalhe)[:80])
            self.estado_desde = agora
        self.publicar("estado", {"estado": estado, "detalhe": detalhe,
                                 "fase": self.fase, "quando": _agora_iso()})

    def descarregar(self):
        """Envia à interface um único pacote com tudo acumulado desde o último flush."""
        if self.contagem_scan and \
                self.contagem_scan.get("verificando") != self._progresso_publicado:
            self._progresso_publicado = self.contagem_scan.get("verificando")
            self.coalescedor.enviar("calc", {
                "id": "scan", "simbolo": "", "instante": _agora_iso(), "etapa": "progresso",
                "mensagem": (f"{self.contagem_scan.get('verificando', 0)} verificados | "
                             f"{self.contagem_scan.get('ignorado', 0)} inativos | "
                             f"{self.contagem_scan.get('dados', 0)} com dados | "
                             f"atual: {self.contagem_scan.get('ultimo', '-')}"),
                "amostra": None, "padroes_testados": None, "resultado": None, "erro": None})
        pacote = self.coalescedor.descarregar()
        if pacote:
            self.entrega(pacote)

    def _bloqueios_ativos(self, para_autostart=False):
        """Motivos objetivos que impedem iniciar/autostart (nunca alterados pela IA)."""
        motivos = []
        if self.book is not None:
            if self.book.drawdown_blocked:
                motivos.append("drawdown máximo atingido (persistido na carteira)")
            if self.book.daily_blocked:
                motivos.append("perda diária máxima atingida")
        if self.parado_risco:
            motivos.append("bloqueio de risco ativo")
        if self.emergencia_ativa:
            motivos.append("EMERGÊNCIA ativa nesta sessão")
        if self.arquivo_emergencia.exists():
            motivos.append("EMERGÊNCIA persistida; pressione INICIAR após revisão para liberar")
        if para_autostart and self.arquivo_pausa.exists():
            motivos.append("pausa manual persistida (stop); use INICIAR para retomar")
        return motivos

    def _avaliar_autostart(self, origem):
        """Autostart seguro: somente sem bloqueios e com mercados selecionados.

        Nunca desfaz pausa voluntária da sessão (autostart_permitido=False
        após PAUSAR/EMERGÊNCIA), bloqueios de risco ou emergência.
        """
        if self.encerrar.is_set() or self.book is None or self.fonte is None:
            return
        if not self.autostart_permitido:
            self.logger.debug("autostart ignorado: pausa/emergência da sessão", origem=origem)
            return
        motivos = self._bloqueios_ativos(para_autostart=True)
        if motivos:
            detalhe = "; ".join(motivos)
            if self.parado_risco or self.book.drawdown_blocked or self.book.daily_blocked:
                self.parado_risco = True
                self._estado_set("BLOQUEADO_POR_RISCO", f"Autostart suspenso: {detalhe}")
            else:
                self._estado_set("EMERGENCIA", f"Autostart suspenso: {detalhe}")
            self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                  "mensagem": f"Autostart suspenso: {detalhe}"})
            self.logger.aviso("autostart suspenso", origem=origem, motivo=detalhe[:120])
            return
        if self.selecao.vazia():
            self._estado_set("AGUARDANDO_SELECAO",
                             "Nenhum mercado marcado para Monitorar; selecione na aba Configurações → Mercados")
            return
        ja_ativo = self.book.active and not self.pausado
        self.pausado = False
        self.book.active = True
        if ja_ativo:
            # Já rodando: NÃO sobrescreve ANALISANDO/AGUARDANDO com PRONTO
            # (causa do chip em PRONTO durante downloads no vídeo).
            self.logger.debug("autostart: ciclo já ativo", origem=origem)
            return
        self._estado_set("PRONTO", f"Início automático ({origem}); aguardando próximo ciclo")
        self.publicar("log", {"instante": _agora_iso(), "nivel": "info",
                              "mensagem": f"Início automático ({origem}): ciclo ativo e entradas paper "
                                          f"habilitadas, sujeitas ao risco ({self.selecao.contagens()[0]} mercados)"})
        self.logger.info("início automático", origem=origem,
                         monitorados=self.selecao.contagens()[0])
        if not self.deepseek_configurado:
            self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                  "mensagem": "DeepSeek não configurado: sem novas hipóteses; padrões já validados continuam sendo usados."})

    def _cancelado_scan(self):
        """Cancelamento da varredura: encerramento, emergência ou prazo vencido.

        Reserva RESERVA_PROCESSAMENTO_S do orçamento para processar o que
        foi coletado, evitando ciclos que consomem a cadência inteira.
        """
        if self.encerrar.is_set() or self.evento_emergencia.is_set():
            return True
        return bool(self.ciclo_ativo and self.prazo_scan is not None
                    and self.relogio() > self.prazo_scan)

    def _cancelado_ciclo(self):
        """Cancelamento cooperativo: encerramento, emergência ou ciclo cancelado."""
        return self.encerrar.is_set() or self.evento_emergencia.is_set() \
            or self.evento_ciclo_cancelado.is_set()

    # ------------------------------------------------------------- ciclo principal
    def _conectar_com_retentativas(self):
        """Conecta ao MT5 com tentativas limitadas e recuo, sem loop apertado.

        Devolve (gerenciador, fonte_entered) para o encerramento correto do
        contexto, mesmo com dobro de fonte nos testes.
        """
        ultimo_erro = None
        for tentativa in range(1, TENTATIVAS_CONEXAO + 1):
            if self.encerrar.is_set():
                raise RuntimeError("Encerrado durante a conexão.")
            self._estado_set("INICIALIZANDO", f"Conectando ao terminal MT5 (tentativa {tentativa}/3)")
            try:
                gerenciador = FonteMT5(self.terminal, self.offset_horas)
                fonte = gerenciador.__enter__()
                return gerenciador, fonte
            except (RuntimeError, OSError, ValueError, TypeError, AttributeError) as exc:
                ultimo_erro = exc
                self.logger.aviso("conexão MT5 falhou", tentativa=tentativa,
                                  detalhe=str(exc)[:120])
                self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                      "mensagem": f"Conexão MT5 falhou ({tentativa}/3): {str(exc)[:120]}"})
                self.descarregar()
                recuo = 2.0 * tentativa
                while recuo > 0 and not self.encerrar.is_set():
                    self.encerrar.wait(min(0.5, recuo))
                    recuo -= 0.5
        raise RuntimeError(f"MT5 indisponível após {TENTATIVAS_CONEXAO} tentativas: "
                           f"{str(ultimo_erro)[:120]}")

    def executar(self):
        """Conecta (com retentativas), avalia autostart e mantém o loop."""
        self.logger.info("inicialização do motor", banco=str(self.banco))
        try:
            gerenciador, fonte = self._conectar_com_retentativas()
            try:
                self.fonte = fonte
                fonte.cancelado = self._cancelado_scan
                fonte.evento = self._evento_fonte
                self.book = self.carga_carteira(self.banco, fonte.moeda)
                self.arquivo_emergencia = Path(str(self.banco) + ".emergencia")
                self.repositorio = padroes.RepositorioPadroes(self.pasta_dados, self.relogio)
                self.orcamento = deepseek.Orcamento(self.pasta_dados, limite_hora_seguro(),
                                                    tokens_dia_seguro(), self.relogio)
                self._persistir()
                self._verificar_config()
                self._registrar_selecao()
                self.publicar("trades", list(reversed(self.book.trades[-500:])))
                # Bootstrap: bloqueios persistidos nunca são contornados pelo autostart.
                motivos = self._bloqueios_ativos(para_autostart=True)
                if motivos:
                    self.pausado = True
                    self.autostart_permitido = False
                    self.book.active = False
                    if self.arquivo_emergencia.exists() or self.emergencia_ativa:
                        self._estado_set("EMERGENCIA", "EMERGÊNCIA persistida: " + "; ".join(motivos))
                    else:
                        self.parado_risco = True
                        self._estado_set("BLOQUEADO_POR_RISCO", "Bloqueios persistidos: " + "; ".join(motivos))
                    self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                          "mensagem": "Autostart suspenso: " + "; ".join(motivos)})
                    self.logger.aviso("autostart suspenso no bootstrap", motivo="; ".join(motivos)[:120])
                else:
                    self.pausado = False  # aguardar seleção não é pausa do operador
                    self._estado_set("PRONTO", "Conectado; autostart avaliado")
                    self._avaliar_autostart("bootstrap")
                self.descarregar()
                while not self.encerrar.is_set():
                    self._processar_acoes()
                    self._monitorar()
                    self.descarregar()
                    if self.encerrar.is_set() or self.pausado or self.parado_risco or self.emergencia_ativa:
                        self.encerrar.wait(0.5)
                        continue
                    if self.selecao.vazia():
                        self._aguardar_selecao()
                        continue
                    if self._aguardar_slot():
                        continue
                    if self.pausado or self.parado_risco or self.emergencia_ativa:
                        continue
                    self._ciclo()
            finally:
                self._estado_set("ENCERRANDO", "Salvando carteira paper")
                gerenciador.__exit__(None, None, None)
        except (RuntimeError, OSError, ValueError, TypeError, AttributeError) as exc:
            self.erro = str(exc) or "Falha controlada no MT5 ou banco local."
            self.logger.erro("falha fatal do motor", detalhe=self.erro)
            self.publicar("log", {"instante": _agora_iso(), "nivel": "erro", "mensagem": self.erro})
            self._estado_set("ERRO", self.erro)
        finally:
            if self.book is not None:
                self.book.active = False
                try:
                    self._persistir()
                except OSError:
                    self._estado_set("ERRO", "Falha ao salvar db.json no encerramento.")
            self.descarregar()
            if self.erro is None:
                self._estado_set("ENCERRADO", "Motor encerrado; carteira paper pausada e salva")
            self.logger.info("encerramento do motor concluído")

    def _aguardar_selecao(self):
        """Sem mercados selecionados: sem análise, sem download, sem tokens.

        Reavalia a seleção periodicamente; ao ser habilitado um mercado,
        com retomada segura, o ciclo inicia automaticamente.
        """
        self._estado_set("AGUARDANDO_SELECAO",
                         "Nenhum mercado marcado para Monitorar; selecione na aba Configurações → Mercados")
        prazo = self.relogio() + 2.0
        anterior = self.selecao
        while not self.encerrar.is_set() and self.relogio() < prazo:
            self._processar_acoes()
            self._monitorar()
            self.descarregar()
            if self.encerrar.is_set():
                return
            self.encerrar.wait(0.25)
        if self.encerrar.is_set():
            return
        self.selecao = mercados.carregar(self.caminho_selecao)
        if not self.selecao.vazia() and (self.selecao.diferencas(anterior) or self.pausado):
            self._registrar_selecao()  # registra mudança e avalia autostart

    def _evento_fonte(self, etapa, simbolo, mensagem):
        """Agrega o progresso do scanner em contagens; um único registro por flush.

        Publicar um evento por símbolo satura a fila da interface (o
        congelamento original); aqui a GUI recebe um resumo consolidado.
        Erros de scan seguem como registros individuais.
        """
        self.contagem_scan[etapa] = self.contagem_scan.get(etapa, 0) + 1
        self.contagem_scan["ultimo"] = f"{simbolo}: {mensagem}" if simbolo else mensagem
        if etapa == "erro" and simbolo:
            self.publicar("calc", {"id": "scan-erro", "simbolo": simbolo, "instante": _agora_iso(),
                                   "etapa": "erro", "mensagem": mensagem, "amostra": None,
                                   "padroes_testados": None, "resultado": None, "erro": mensagem})

    def _proximo_alvo(self, agora):
        """Horário do próximo ciclo: fechamento do M1+2s, ou +15min com posições."""
        if self.book is not None and self.book.fills:
            return agora + self.intervalo_com_posicoes, "posições abertas: ciclo em 15min"
        return (int(agora // 60) + 1) * 60 + MARGEM_CANDLE_S, "sem posições: ciclo no fechamento do M1"

    def _aguardar_slot(self):
        """Espera o próximo horário de ciclo; True indica slot cancelado."""
        alvo, base = self._proximo_alvo(self.relogio())
        agora = self.relogio()
        while not self.encerrar.is_set():
            self._processar_acoes()
            self._monitorar()
            self.descarregar()
            if self.pausado or self.parado_risco or self.emergencia_ativa:
                return True
            restante = alvo - self.relogio()
            if restante <= 0:
                return False
            if agora - self.ultimo_aviso_slot >= 5:
                self.ultimo_aviso_slot = agora
                self._estado_set("AGUARDANDO", f"{base}; próximo ciclo em {restante:.0f}s",
                                 registrar=False)  # atualiza detalhe sem logar transição
            self.encerrar.wait(min(0.5, restante))
            agora = self.relogio()
        return True

    # ------------------------------------------------------------- comandos
    def _processar_acoes(self):
        while True:
            try:
                acao, dados, enfileirado = self.acoes.get_nowait()
            except queue.Empty:
                return
            atraso = self.relogio() - enfileirado
            if atraso > 1.0:
                self.logger.aviso("comando processado com atraso", comando=acao,
                                  atraso_s=round(atraso, 1))
            try:
                if acao == "iniciar":
                    self._acao_iniciar()
                elif acao == "pausar":
                    self._acao_pausar()
                elif acao == "retomar":
                    self._acao_iniciar("RETOMAR")
                elif acao == "emergencia":
                    self._acao_emergencia()
                elif acao == "liberar_risco":
                    self._acao_liberar_risco()
                elif acao == "limites":
                    self._acao_limites(dados)
                elif acao == "config":
                    self._verificar_config()
                elif acao == "selecao":
                    self._registrar_selecao()
                elif acao == "catalogo":
                    self._acao_catalogo()
                elif acao == "sair":
                    return
            except RuntimeError as exc:
                self.publicar("log", {"instante": _agora_iso(), "nivel": "erro", "mensagem": str(exc)})
                self.logger.erro("comando recusado", comando=acao, detalhe=str(exc))

    def _verificar_config(self):
        try:
            jev.configuracao()
            self.jev_configurado = True
        except RuntimeError:
            self.jev_configurado = False
        try:
            deepseek.configuracao_chat()
            self.deepseek_configurado = True
        except RuntimeError:
            self.deepseek_configurado = False
        mensagem = (f"Configuração: JEv {'ok' if self.jev_configurado else 'não configurado'}; "
                    f"DeepSeek {'ok' if self.deepseek_configurado else 'não configurado'}")
        self.publicar("log", {"instante": _agora_iso(), "nivel": "info", "mensagem": mensagem})
        self.logger.info(mensagem)

    def _registrar_selecao(self):
        """Relê a seleção de mercados e registra diferenças sem dados sensíveis."""
        anterior = self.selecao
        self.selecao = mercados.carregar(self.caminho_selecao)
        monitorados, operaveis = self.selecao.contagens()
        mudancas = self.selecao.diferencas(anterior)
        mensagem = f"Seleção de mercados: {monitorados} monitorados, {operaveis} para operar"
        if mudancas:
            mensagem += f"; alterados: {', '.join(mudancas)}"
        self.publicar("log", {"instante": _agora_iso(), "nivel": "info", "mensagem": mensagem})
        self.logger.info("seleção de mercados recarregada",
                         monitorados=monitorados, operaveis=operaveis, alterados=len(mudancas))
        if self.selecao.vazia():
            self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                  "mensagem": "Nenhum mercado marcado para Monitorar; use a aba Configurações → Mercados."})
        else:
            self._avaliar_autostart("seleção de mercados")

    def _acao_catalogo(self):
        """Catálogo real do terminal na thread do MT5; cache persistido para offline."""
        try:
            categorias = mercados.catalogo_por_categoria(self.fonte)
            mercados.salvar_cache(categorias)
            origem = "ao vivo"
        except (RuntimeError, OSError, AttributeError) as exc:
            categorias = mercados.carregar_cache()
            origem = "cache (terminal indisponível)"
            self.logger.aviso("catálogo indisponível; usando cache", detalhe=str(exc)[:120])
        self.publicar("catalogo", {"categorias": categorias, "origem": origem,
                                   "quando": _agora_iso()})

    def _acao_iniciar(self, rotulo="INICIAR"):
        """INICIAR/RETOMAR: ação manual do operador; recusas sempre explicadas.

        A configuração de JEv/DeepSeek NÃO bloqueia mais o início: sem
        chave, o ciclo de análise funciona e as decisões ficam em
        AGUARDANDO. Bloqueios de risco/emergência seguem invioláveis e
        são recusados com motivo objetivo na interface.
        """
        if self.book is None or self.fonte is None:
            raise RuntimeError("Conecte o MT5 antes de iniciar.")
        if not self.pausado and self.book.active and not self.emergencia_ativa                 and not self.arquivo_emergencia.exists():
            self.publicar("log", {"instante": _agora_iso(), "nivel": "info",
                                  "mensagem": f"{rotulo}: ciclo já ativo; comando ignorado (proteção contra duplo clique)"})
            return
        motivos = self._bloqueios_ativos(para_autostart=False)
        if self.parado_risco or self.book.drawdown_blocked or self.book.daily_blocked:
            detalhe = "; ".join(motivos) or "bloqueio de risco ativo"
            self.publicar("log", {"instante": _agora_iso(), "nivel": "erro",
                                  "mensagem": f"{rotulo} recusado: {detalhe}. Revise e use LIBERAR APÓS REVISÃO."})
            self.logger.aviso("INICIAR recusado por bloqueio de risco", motivo=detalhe[:120])
            return
        # INICIAR é a liberação manual explícita da emergência persistida.
        self.arquivo_emergencia.unlink(missing_ok=True)
        self.arquivo_pausa.unlink(missing_ok=True)
        self.emergencia_ativa = False
        self.autostart_permitido = True
        self.pausado = False
        self.book.active = True
        self._persistir()
        self._estado_set("PRONTO", f"{rotulo}: retomado pelo operador; aguardando próximo ciclo")
        self.publicar("log", {"instante": _agora_iso(), "nivel": "info",
                              "mensagem": f"{rotulo}: ciclo ativo e entradas paper habilitadas, sujeitas ao risco"})
        self.logger.info("início/retomada manual", rotulo=rotulo,
                         monitorados=self.selecao.contagens()[0])
        if not self.deepseek_configurado or not self.jev_configurado:
            faltando = []
            if not self.jev_configurado:
                faltando.append("JEv/OpenRouter")
            if not self.deepseek_configurado:
                faltando.append("DeepSeek")
            self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                  "mensagem": f"{' e '.join(faltando)} não configurado(s): o ciclo de análise roda; decisões de entrada ficarão em AGUARDANDO até configurar."})
            self.logger.aviso("IA não configurada; ciclo roda sem decisões", faltando=",".join(faltando))
        if self.selecao.vazia():
            self._estado_set("AGUARDANDO_SELECAO",
                             "Nenhum mercado marcado para Monitorar; selecione na aba Configurações → Mercados")

    def _acao_pausar(self):
        self.evento_pausa.clear()
        self.pausado = True
        self.autostart_permitido = False  # pausa voluntária: autostart não retoma
        if self.book is not None:
            self.book.active = False
            self._persistir()
        self._estado_set("PAUSADO_PELO_USUARIO", "Novas análises e entradas pausadas; monitoramento ativo")
        self.publicar("log", {"instante": _agora_iso(), "nivel": "info",
                              "mensagem": "PAUSAR: entradas e análises suspensas; posições continuam monitoradas"})
        self.logger.info("pausa acionada pelo operador")

    def _acao_emergencia(self):
        """Fecha tudo imediatamente; resultado confirmado posição por posição."""
        self.evento_emergencia.clear()
        self.emergencia_ativa = True
        self.pausado = True
        self.autostart_permitido = False  # emergência exige INICIAR manual
        if self.book is not None:
            self.book.active = False
        self._estado_set("EMERGENCIA", "Fechando todas as posições paper")
        self.publicar("log", {"instante": _agora_iso(), "nivel": "emergencia",
                              "mensagem": "EMERGÊNCIA: novas entradas bloqueadas; fechando posições paper"})
        self.logger.aviso("EMERGÊNCIA acionada", posicoes=len(self.book.fills) if self.book else 0)
        try:
            self.arquivo_emergencia.parent.mkdir(parents=True, exist_ok=True)
            self.arquivo_emergencia.write_text(_agora_iso(), encoding="ascii")
        except OSError as exc:
            self.logger.erro("falha ao registrar emergência persistida", detalhe=str(exc)[:120])
        self._executar_emergencia()

    def _executar_emergencia(self):
        if self.book is None or self.fonte is None:
            return
        fechadas, falhas = self.book.fechar_total(self.fonte, motivo="EMERGENCIA")
        self._persistir()
        id_emergencia = f"emerg-{int(self.relogio())}"
        for resultado in fechadas:
            self.publicar("decisao", {"id": id_emergencia, "simbolo": resultado["symbol"],
                                      "instante": _agora_iso(), "oportunidade": "EMERGÊNCIA",
                                      "decisao": "ENCERRADA", "justificativa": "Fechamento de emergência",
                                      "risco": "Execução confirmada no preço observado",
                                      "execucao": f"Fechada a {resultado['preco']:.8g}; P&L {resultado['lucro']:+.2f} {self.book.currency}"})
            self.logger.aviso("posição encerrada na emergência", simbolo=resultado["symbol"],
                              lucro=round(resultado["lucro"], 2))
        for falha in falhas:
            self.publicar("decisao", {"id": id_emergencia, "simbolo": falha["symbol"],
                                      "instante": _agora_iso(), "oportunidade": "EMERGÊNCIA",
                                      "decisao": "PENDENTE", "justificativa": "Falha no fechamento",
                                      "risco": "Posição permanece aberta",
                                      "execucao": f"{falha['motivo']}; nova tentativa automática"})
            self.logger.erro("falha ao fechar posição na emergência", simbolo=falha["symbol"])
        if falhas:
            self.publicar("log", {"instante": _agora_iso(), "nivel": "erro",
                                  "mensagem": f"EMERGÊNCIA: {len(falhas)} posição(ões) sem cotação para fechar agora; tentando novamente."})
        if not self.book.fills:
            self._estado_set("EMERGENCIA", "Todas as posições confirmadas encerradas; entradas bloqueadas até INICIAR")
        self.publicar("metricas", self._metricas())

    def _acao_liberar_risco(self):
        if not self.parado_risco and not (self.book and (self.book.drawdown_blocked or self.book.daily_blocked)):
            return
        self.parado_risco = False
        self.motivo_risco = ""
        if self.book is not None:
            self.book.drawdown_blocked = False
            self.book.daily_blocked = False
            self._persistir()
        self._estado_set("PRONTO", "Risco liberado manualmente após revisão")
        self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                              "mensagem": "Liberação manual registrada: bloqueios de perda diária/drawdown removidos pelo operador após revisão"})
        self.logger.aviso("liberação manual de risco registrada")

    def _acao_limites(self, dados):
        from dataclasses import replace
        if self.book is None:
            raise RuntimeError("Carteira ainda não carregada.")
        atual = self.book.limits
        try:
            novo = replace(atual, risk_per_trade=float(dados["risk_per_trade"]),
                           max_total_risk=float(dados["max_total_risk"]),
                           max_drawdown=float(dados["max_drawdown"]))
        except (TypeError, ValueError):
            raise RuntimeError("Limites inválidos: use frações entre 0 e 1.") from None
        self.book.limits = novo
        self._persistir()
        mensagem = (f"Limites aplicados: {novo.risk_per_trade:.2%}/operação, "
                    f"{novo.max_total_risk:.2%} total, drawdown {novo.max_drawdown:.2%}")
        self.publicar("log", {"instante": _agora_iso(), "nivel": "info", "mensagem": mensagem})
        self.logger.info("limites de risco alterados pelo operador",
                         por_operacao=round(novo.risk_per_trade, 4),
                         total=round(novo.max_total_risk, 4), drawdown=round(novo.max_drawdown, 4))
        self.publicar("metricas", self._metricas())

    # ------------------------------------------------------------- monitoramento
    def _monitorar(self):
        agora = self.relogio()
        if self.book is None or self.fonte is None or agora - self.ultimo_monitor < 2:
            return
        self.ultimo_monitor = agora
        eventos_antes = len(self.book.log)
        try:
            conta = self.fonte.conta()
            self.book.update(self.fonte)
        except RuntimeError:
            self.book.prices_valid = False
            conta = None
            self.publicar("log", {"instante": _agora_iso(), "nivel": "erro",
                                  "mensagem": "MT5 sem métricas ou cotações; marcação antiga bloqueia entradas"})
            self.logger.aviso("marcação indisponível; entradas bloqueadas")
        # Emergência persiste até fechar todas as posições, mesmo com falhas.
        if self.emergencia_ativa and self.book.fills:
            self._executar_emergencia()
        self._avaliar_risco()
        alterou_trades = len(self.book.trades) != self.trades_conhecidos or \
            any(e["event"] in ("open", "close") for e in self.book.log[eventos_antes:])
        if len(self.book.trades) != self.trades_conhecidos or \
                any(e["event"] == "close" for e in self.book.log[eventos_antes:]):
            self.trades_conhecidos = len(self.book.trades)
            self.publicar("trades", list(reversed(self.book.trades[-500:])))
        self.publicar("metricas", self._metricas(conta))
        # Persiste a cada mudança de trade; checkpoints de equity a cada 10s.
        if alterou_trades or agora - self.ultimo_persist >= 10:
            self.ultimo_persist = agora
            self._persistir()

    def _avaliar_risco(self):
        """Mecanismo determinístico: limites do motor, não da tela ou da IA."""
        book = self.book
        limites = book.limits
        m = book.metrics()
        motivos = []
        if book.drawdown_blocked:
            motivos.append("Bloqueio de drawdown registrado na carteira")
        if book.daily_blocked:
            motivos.append("Bloqueio de perda diária")
        if m["risk_fraction"] > limites.max_total_risk:
            motivos.append(f"Exposição {m['risk_fraction']:.1%} acima de {limites.max_total_risk:.1%}")
        if motivos:
            if not self.parado_risco:
                self.parado_risco = True
                self.motivo_risco = "; ".join(motivos)
                self._estado_set("BLOQUEADO_POR_RISCO", self.motivo_risco)
                self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                      "mensagem": f"PARADO POR RISCO: {self.motivo_risco}. Análises e entradas suspensas; liberação apenas manual."})
                self.logger.aviso("motor parado por risco", motivo=self.motivo_risco)
        elif self.parado_risco:
            self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                  "mensagem": "Limites voltaram ao normal; aguardando liberação manual para retomar análises"})

    def _metricas(self, conta=None):
        book = self.book
        m = book.metrics()
        monitorados, operaveis = self.selecao.contagens()
        return {"estado": self.estado, "detalhe": self.detalhe,
                "ativo": book.active, "pausado": self.pausado, "parado_risco": self.parado_risco,
                "emergencia": self.emergencia_ativa, "motivo_risco": self.motivo_risco,
                "moeda": book.currency, "equity": m["equity"], "saldo": book.cash,
                "realizado": m["realized"], "aberto": m["open_profit"],
                "exposicao": m["risk"], "exposicao_fracao": m["risk_fraction"],
                "margem": m["margin"], "posicoes": m["positions"],
                "drawdown": m["drawdown"], "drawdown_max": m["max_drawdown"],
                "growth": m["growth"], "daily_return": m["daily_return"],
                "wins": m["wins"], "losses": m["losing_trades"], "fechados": m["closed"],
                "win_rate": m["win_rate"], "precos_validos": book.prices_valid,
                "limites": asdict(book.limits),
                "conta": conta, "ciclo": self.ciclo,
                "jev_ok": self.jev_configurado, "deepseek_ok": self.deepseek_configurado,
                "orcamento": self.orcamento.consumo() if self.orcamento else {},
                "monitorados": monitorados, "operaveis": operaveis,
                "fase": self.fase, "contadores": dict(self.contadores_ciclo),
                "posicoes_detalhe": [asdict(p) for p in book.fills]}

    def _persistir(self):
        self.book.save(self.banco)

    # ------------------------------------------------------------- ciclo de análise
    def _ciclo(self):
        """Varredura dos candidatos monitorados, sem ciclos simultâneos."""
        if self.ciclo_ativo:
            self.logger.aviso("ciclo anterior ainda em andamento; próximo adiado")
            self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                  "mensagem": "Ciclo anterior ainda em andamento; próximo adiado"})
            return
        self.ciclo_ativo = True
        self.evento_ciclo_cancelado.clear()
        self.contagem_scan = {}
        inicio_ciclo = self.relogio()
        self.ciclo += 1
        id_ciclo = f"ciclo{self.ciclo}"
        self.logger.info("ciclo iniciado", ciclo=self.ciclo, analysis_id=id_ciclo)
        self._estado_set("ANALISANDO", f"Ciclo {self.ciclo}", fase="VARRENDO")
        try:
            self._executar_ciclo(id_ciclo)
        finally:
            self.ciclo_ativo = False
            if self.evento_ciclo_cancelado.is_set():
                self.logger.aviso("ciclo cancelado pelo operador", ciclo=self.ciclo)
                self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                      "mensagem": "Ciclo cancelado pelo operador; restante adiado para o próximo ciclo"})
            self.descarregar()
            self.logger.info("ciclo concluído", ciclo=self.ciclo)

    def _executar_ciclo(self, id_ciclo):
        # Orçamento de tempo: varredura de 90+ símbolos pode ultrapassar a
        # cadência; o ciclo nunca excede o orçamento e o restante segue no
        # próximo ciclo (continuidade incremental, sem sobreposição).
        orcamento = (ORCAMENTO_CICLO_COM_POSICAO_S if self.book and self.book.fills
                     else ORCAMENTO_CICLO_S)
        self.prazo_ciclo = self.relogio() + orcamento
        self.prazo_scan = self.prazo_ciclo - RESERVA_PROCESSAMENTO_S
        self.contadores_ciclo = {}
        inicio_ciclo = self.relogio()
        self._registrar_selecao()  # aplica mudanças de seleção no início do ciclo
        try:
            estados = list(self.fonte.estados())
        except RuntimeError as exc:
            self.publicar("log", {"instante": _agora_iso(), "nivel": "erro",
                                  "mensagem": f"Scanner falhou: {exc}; nova tentativa no próximo ciclo"})
            self.logger.erro("scanner falhou", detalhe=str(exc)[:160])
            return
        self.publicar("calc", {"id": id_ciclo, "simbolo": "", "instante": _agora_iso(),
                               "etapa": "ciclo", "mensagem": f"{len(estados)} ativos elegíveis no ciclo {self.ciclo}",
                               "amostra": None, "padroes_testados": None, "resultado": None, "erro": None})
        self.contadores_ciclo["candidatos"] = len(estados)
        self.consultas_ds_ciclo = 0
        try:
            self._varrer_e_processar(id_ciclo, estados)
        finally:
            duracao = round(self.relogio() - inicio_ciclo, 1)
            resumo = "; ".join(f"{k}={v}" for k, v in sorted(self.contadores_ciclo.items()))
            self.publicar("decisao", {"id": id_ciclo, "simbolo": "", "instante": _agora_iso(),
                                      "oportunidade": f"Ciclo {self.ciclo}",
                                      "decisao": "RESUMO",
                                      "justificativa": resumo or "sem candidatos monitorados",
                                      "risco": f"pendentes: {len(self.pendentes_ciclo)}",
                                      "execucao": f"duração {duracao}s"})
            self.logger.info("ciclo concluído (processamento)", ciclo=self.ciclo,
                             duracao_s=duracao, pendentes=len(self.pendentes_ciclo),
                             contadores=dict(self.contadores_ciclo))

    def _varrer_e_processar(self, id_ciclo, estados):
        # Continuidade incremental: pendentes do ciclo anterior têm prioridade.
        if self.pendentes_ciclo:
            self.logger.info("continuidade do ciclo anterior", pendentes=len(self.pendentes_ciclo))
        ordem = [s for s in self.pendentes_ciclo]
        ordem += [s.symbol for s in estados if s.symbol not in self.pendentes_ciclo]
        por_simbolo = {s.symbol: s for s in estados}
        processados = set()
        for simbolo in ordem:
            if self._cancelado_ciclo() or self.pausado or self.parado_risco:
                break
            self._processar_acoes()  # INICIAR/PAUSAR/EMERGÊNCIA respondem no meio do ciclo
            self.descarregar()
            estado = por_simbolo.get(simbolo)
            if estado is None:
                continue  # pendente que não veio nesta varredura
            if not self.selecao.monitorar(simbolo):
                continue  # desmarcado: sem histórico, sem análise, sem DeepSeek
            if self.relogio() > self.prazo_ciclo:
                self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                      "mensagem": "Orçamento de tempo do ciclo esgotado; restante segue no próximo ciclo"})
                self.logger.aviso("orçamento do ciclo esgotado", processados=len(processados))
                break
            if simbolo in self.em_analise:
                self.logger.debug("análise duplicada ignorada", simbolo=simbolo)
                continue
            try:
                self.em_analise.add(simbolo)
                self._processar_ativo(estado, id_ciclo)
            except (RuntimeError, ValueError, KeyError) as exc:
                self.publicar("calc", {"id": id_ciclo, "simbolo": simbolo, "instante": _agora_iso(),
                                       "etapa": "erro", "mensagem": "Falha na análise do ativo",
                                       "amostra": None, "padroes_testados": None,
                                       "resultado": None, "erro": str(exc)[:200]})
                self.contadores_ciclo["erros"] = self.contadores_ciclo.get("erros", 0) + 1
                self.logger.erro("falha na análise", simbolo=simbolo, detalhe=str(exc)[:160])
            finally:
                self.em_analise.discard(simbolo)
                processados.add(simbolo)
                self.descarregar()  # GUI acompanha o ciclo em tempo quase real
        # Pendências para o próximo ciclo: monitorados não processados nesta rodada.
        monitorados = [s.symbol for s in estados if self.selecao.monitorar(s.symbol)]
        self.pendentes_ciclo = [s for s in monitorados if s not in processados]
        self.logger.info("varredura e processamento concluídos",
                         processados=len(processados), pendentes=len(self.pendentes_ciclo))

    def _coletar_janela(self, simbolo, id_analise):
        """Download paginado da janela 30d com progresso, logs e cancelamento."""
        self._estado_set("ANALISANDO", f"Ciclo {self.ciclo} · BAIXANDO HISTÓRICO {simbolo}",
                         registrar=False, fase="BAIXANDO_HISTORICO")

        def progresso(coletados, solicitado):
            self.publicar("calc", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                   "etapa": "janela", "mensagem": "Baixando histórico M1 (30 dias)",
                                   "amostra": {"candles": coletados, "dias": None,
                                               "solicitados": solicitado},
                                   "padroes_testados": None, "resultado": None, "erro": None})
            # Mantém o chip/mensagem atuais durante o download (sem estado antigo).
            self.detalhe = (f"Ciclo {self.ciclo} · BAIXANDO HISTÓRICO {simbolo} "
                            f"({coletados}/{solicitado} candles)")
            self.publicar("estado", {"estado": self.estado, "detalhe": self.detalhe,
                                     "fase": self.fase, "quando": _agora_iso()})
            self.descarregar()
        inicio = time.time()
        janela = self.fonte.janela_m1(simbolo, dias=30, lote=LOTE_DOWNLOAD,
                                      cancelado=self._cancelado_ciclo, progresso=progresso)
        duracao = round(time.time() - inicio, 2)
        if janela is None:
            motivo = "cancelado" if self._cancelado_ciclo() else "indisponível"
            self.logger.info("download de histórico não concluído", simbolo=simbolo, motivo=motivo)
            return None
        self.logger.info("download de histórico concluído", simbolo=simbolo,
                         candles=janela["recebidos"], lacunas=janela["lacunas"],
                         lotes=max(1, -(-janela["recebidos"] // LOTE_DOWNLOAD)), duracao_s=duracao)
        return janela

    def _processar_ativo(self, estado, id_ciclo):
        """Pipeline completo de um ativo monitorado.

        O agendamento do DeepSeek acontece na atualização da janela
        (_atualizar_cache), única etapa com a janela completa em memória.
        """
        simbolo = estado.symbol
        id_analise = f"{id_ciclo}-{simbolo}"
        agora = self.relogio()
        cache = self.dados_ativo.get(simbolo)
        vencida = cache is None or agora - cache["instante"] >= JANELA_TTL_S
        if vencida and self._pode_atualizar_janela(simbolo):
            janela = self._janela_com_cache(simbolo, id_analise)
            if janela is None:
                if cache is None:
                    self.publicar("calc", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                           "etapa": "dados", "mensagem": "Janela M1 de 30 dias indisponível",
                                           "amostra": None, "padroes_testados": None,
                                           "resultado": None, "erro": None})
                    self.contadores_ciclo["sem_dados"] = \
                        self.contadores_ciclo.get("sem_dados", 0) + 1
                # Com cache anterior válido, ele permanece até a próxima tentativa.
            else:
                self._atualizar_cache(simbolo, janela, id_analise)
                cache = self.dados_ativo.get(simbolo)
        if cache is None or cache.get("stats") is None:
            self.publicar("calc", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                   "etapa": "dados", "mensagem": "Estatísticas aguardando atualização da janela",
                                   "amostra": None, "padroes_testados": None,
                                   "resultado": None, "erro": None})
            return
        stats = cache["stats"]
        custo_pct = cache["custo_pct"]
        ativos_repo = self.repositorio.padroes_ativos(simbolo)
        self.publicar("calc", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                               "etapa": "calculado", "mensagem": "Estatísticas locais prontas (Python; cálculo não consome tokens)",
                               "amostra": {"candles": stats.get("candles"), "dias": stats.get("dias"),
                                           "lacunas": stats.get("lacunas"),
                                           "retorno_janela_pct": stats.get("retorno_janela_pct")},
                               "padroes_testados": len(ativos_repo),
                               "resultado": f"movimento mediano {stats['movimento_1min']['mediana_pct']}% | custo spread {custo_pct}%",
                               "erro": None})
        self._oportunidade(estado, id_analise)

    def _pode_atualizar_janela(self, simbolo):
        """Primeira coleta sempre; depois teto por ciclo em ordem round-robin."""
        cache = self.dados_ativo.get(simbolo)
        if cache is None:
            return True
        if simbolo not in self.ordem_janelas:
            self.ordem_janelas.append(simbolo)
        pendentes = [s for s in self.ordem_janelas
                     if self.dados_ativo.get(s) is None
                     or self.relogio() - self.dados_ativo[s].get("instante", 0) >= JANELA_TTL_S]
        try:
            return pendentes.index(simbolo) < JANELAS_POR_CICLO
        except ValueError:
            return True

    def _janela_com_cache(self, simbolo, id_analise):
        """Janela atual preferindo cache incremental; janela completa no miss.

        Cache hit: anexa somente os candles novos (1 chamada curta) com
        verificação de continuidade; miss ou falha de integridade: fetch
        paginado completo. Em ambos os casos o cache em disco é atualizado
        de forma atômica e recortado aos últimos 30 dias.
        """
        armazenados = self.cache_m1.carregar(simbolo)
        if armazenados:
            ultimo = armazenados[-1]["time"]
            novos = self.fonte.incremento_m1(simbolo, desde=ultimo)
            if novos is not None:
                combinados, info = self.cache_m1.anexar(simbolo, novos)
                if not info["recarregar"] and len(combinados) >= 2:
                    self.contadores_ciclo["cache_incremental"] = \
                        self.contadores_ciclo.get("cache_incremental", 0) + 1
                    self.logger.info("cache incremental aplicado", simbolo=simbolo,
                                     anexados=info["anexados"], candles=len(combinados),
                                     lacunas=info["lacunas"])
                    return {"simbolo": simbolo, "candles": combinados,
                            "inicio": combinados[0]["time"], "fim": combinados[-1]["time"],
                            "solicitados": len(combinados), "recebidos": len(combinados),
                            "lacunas": info["lacunas"],
                            "minutos_lacuna": info["lacunas"], "maior_lacuna": 0}
            self.contadores_ciclo["cache_invalido"] = \
                self.contadores_ciclo.get("cache_invalido", 0) + 1
            self.cache_m1.invalidar(simbolo)
            self.logger.aviso("cache incremental indisponível; janela completa",
                              simbolo=simbolo)
        janela = self._coletar_janela(simbolo, id_analise)
        if janela is not None:
            self.contadores_ciclo["cache_completo"] = \
                self.contadores_ciclo.get("cache_completo", 0) + 1
            self.cache_m1.salvar(simbolo, janela["candles"])
        return janela

    def _atualizar_cache(self, simbolo, janela, id_analise):
        """Estatísticas + revalidação + DeepSeek com a janela completa; guarda só a cauda.

        A janela completa (~43k candles) vive apenas durante este método — a
        memória por ativo fica limitada à cauda (CAUDA_CANDLES) e ao resumo.
        O DeepSeek é agendado aqui porque as hipóteses novas precisam ser
        validadas contra a janela completa antes do descarte.
        """
        self._estado_set("ANALISANDO", f"Ciclo {self.ciclo} · CALCULANDO {simbolo}",
                         registrar=False, fase="CALCULANDO")
        descoberta, _ = janela_descoberta(janela["candles"])
        recorte = {**janela, "candles": descoberta, "fim": descoberta[-1]["time"]}
        spread = self.fonte.cotacao(simbolo)
        spread_fracao = (spread[1] - spread[0]) if spread else 0.0
        stats = estatisticas(recorte, spread_fracao)
        custo_pct = stats.get("custo_spread_pct") or 0.0
        janela_info = {"inicio": janela["inicio"], "fim": janela["fim"],
                       "dias": stats.get("dias"), "candles": janela["recebidos"]}
        # Revalida padrões ativos com a janela recém-baixada (cronologia preservada).
        self._estado_set("ANALISANDO", f"Ciclo {self.ciclo} · VALIDANDO {simbolo}",
                         registrar=False, fase="VALIDANDO")
        for registro in self.repositorio.padroes_ativos(simbolo):
            interna = registro["regra"]
            regra = {"tipo_regra": interna["tipo"], "parametros": interna["parametros"],
                     "direcao": interna["direcao"], "horizonte_minutos": interna["horizonte_minutos"],
                     "justificativa": registro.get("justificativa", "")}
            validacao = padroes.validar(regra, janela["candles"], custo_pct)
            atualizado = self.repositorio.registrar(simbolo, regra, validacao,
                                                    registro.get("origem", "local"), janela_info)
            if validacao["status"] == "rejeitado":
                self.contadores_ciclo["rejeitado_validacao"] = \
                    self.contadores_ciclo.get("rejeitado_validacao", 0) + 1
            if validacao["status"] != registro.get("status"):
                self.publicar("deepseek", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                           "padrao": atualizado["id"], "status_consulta": "REEXAME LOCAL",
                                           "hipotese": self._texto_hipotese(regra),
                                           "validacao": f"{validacao['status']}: {validacao['motivo']}",
                                           "resultado": self._texto_validacao(validacao)})
                self.logger.info("status de padrão alterado no reexame", simbolo=simbolo,
                                 padrao=atualizado["id"], status=validacao["status"])
        cache = {"instante": self.relogio(), "stats": stats, "custo_pct": custo_pct,
                 "inicio_janela": janela["inicio"], "fim_janela": janela["fim"],
                 "tail": janela["candles"][-CAUDA_CANDLES:]}
        self.dados_ativo[simbolo] = cache
        self.logger.info("estatísticas locais calculadas", simbolo=simbolo,
                         candles=stats.get("candles"), dias=stats.get("dias"))
        # DeepSeek é agendado aqui: hipóteses são validadas antes do descarte da janela.
        agora = self.relogio()
        if (self.deepseek_configurado and agora >= self.proxima_tentativa_ds
                and self.consultas_ds_ciclo < DEEPSEEK_POR_CICLO
                and agora - self.ultimo_deepseek.get(simbolo, 0) >= DEEPSEEK_TTL_S
                and stats.get("disponivel") and (janela["fim"] - janela["inicio"]) >= 3 * 86400):
            self.consultas_ds_ciclo += 1
            self._etapa_deepseek(simbolo, janela["candles"], cache, stats, id_analise)
        # Cauda permanece no cache; a janela completa é liberada ao sair deste método.

    def _etapa_deepseek(self, simbolo, candles, cache, stats, id_analise):
        """Consulta o DeepSeek, valida cada hipótese localmente e registra."""
        agora = self.relogio()
        permitido, motivo = self.orcamento.permitir()
        if not permitido:
            if agora - self.ultimo_aviso_orcamento > 300:
                self.ultimo_aviso_orcamento = agora
                self.publicar("deepseek", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                           "padrao": "-", "status_consulta": "ORÇAMENTO",
                                           "hipotese": "-", "validacao": "-", "resultado": motivo})
                self.logger.aviso("consulta DeepSeek bloqueada pelo orçamento", motivo=motivo)
            return False
        padroes_atuais = [{k: p.get(k) for k in ("id", "regra", "status", "eventos", "fora_amostra")}
                          for p in self.repositorio.padroes_ativos(simbolo)]
        self.publicar("deepseek", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                   "padrao": "-", "status_consulta": "CONSULTANDO",
                                   "hipotese": "Enviando resumo estatístico compacto (janela de descoberta)",
                                   "validacao": "-", "resultado": "-"})
        modelo = ""
        try:
            from . import configuracao as cfg
            modelo = cfg.modelo_deepseek()
        except RuntimeError:
            pass
        inicio = time.time()
        self.logger.info("consulta DeepSeek iniciada", simbolo=simbolo, modelo=modelo)
        try:
            hipoteses, revisoes, uso, descartadas = deepseek.analisar(
                resumo_compacto(stats), padroes_atuais, cancelado=self._cancelado_ciclo)
        except RuntimeError as exc:
            self.proxima_tentativa_ds = agora + TENTATIVA_DEEPSEEK_S
            self.publicar("deepseek", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                       "padrao": "-", "status_consulta": "ERRO",
                                       "hipotese": "-", "validacao": "-", "resultado": str(exc)})
            self.logger.aviso("consulta DeepSeek falhou", simbolo=simbolo,
                              detalhe=str(exc)[:160], duracao_s=round(time.time() - inicio, 2))
            return False
        self.orcamento.registrar(uso.get("tokens", 0))
        self.ultimo_deepseek[simbolo] = agora
        self._estado_set("ANALISANDO", f"Ciclo {self.ciclo} · VALIDANDO {simbolo}",
                         registrar=False, fase="VALIDANDO")
        self.logger.info("resposta DeepSeek recebida", simbolo=simbolo,
                         tokens=uso.get("tokens", 0), hipoteses=len(hipoteses),
                         descartadas=descartadas, revisoes=len(revisoes),
                         duracao_s=round(time.time() - inicio, 2))
        janela_info = {"inicio": cache["inicio_janela"], "fim": cache["fim_janela"],
                       "dias": stats.get("dias"), "candles": len(candles)}
        custo_pct = cache["custo_pct"]
        for hipotese in hipoteses:
            validacao = padroes.validar(hipotese, candles, custo_pct)
            registro = self.repositorio.registrar(simbolo, hipotese, validacao, "deepseek", janela_info)
            if validacao["status"] == "rejeitado":
                self.contadores_ciclo["rejeitado_validacao"] = \
                    self.contadores_ciclo.get("rejeitado_validacao", 0) + 1
            self.publicar("deepseek", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                       "padrao": registro["id"], "status_consulta": "hipótese recebida",
                                       "hipotese": self._texto_hipotese(hipotese),
                                       "validacao": f"{validacao['status']}: {validacao['motivo']}",
                                       "resultado": self._texto_validacao(validacao)})
            self.logger.info("hipótese validada localmente", simbolo=simbolo,
                             padrao=registro["id"], status=validacao["status"])
        aplicadas = self.repositorio.aplicar_revisoes(simbolo, revisoes)
        self.publicar("deepseek", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                   "padrao": "-", "status_consulta": "CONCLUÍDA",
                                   "hipotese": f"{len(hipoteses)} hipóteses; {descartadas} descartadas por schema",
                                   "validacao": f"{len(aplicadas)} revisão(ões) de expiração aplicadas",
                                   "resultado": f"tokens usados: {uso.get('tokens', 0)}"})
        return True

    def _oportunidade(self, estado, id_analise):
        """Padrão validado com condição verdadeira agora → decisão JEv.

        Somente símbolos marcados para Operar chegam a esta etapa. Posições
        abertas continuam supervisionadas em _monitorar, independentemente
        da seleção.
        """
        if self.book is None or not self.book.active or self.parado_risco or self.emergencia_ativa:
            return
        simbolo = estado.symbol
        if not self.selecao.operar(simbolo):
            self.contadores_ciclo["somente_monitorar"] = \
                self.contadores_ciclo.get("somente_monitorar", 0) + 1
            return
        validados = self.repositorio.validados(simbolo)
        if not validados:
            self.contadores_ciclo["sem_padrao"] = \
                self.contadores_ciclo.get("sem_padrao", 0) + 1
            return
        instante_candle = estado.candles[-1]["time"] if estado.candles else None
        if len(self.avaliados) > MAX_AVALIADOS:
            self.avaliados.clear()
        for padrao in validados:
            chave = (simbolo, padrao["id"])
            if self.avaliados.get(chave) == instante_candle:
                continue
            if instante_candle is not None:
                self.avaliados[chave] = instante_candle
            candles_condicao = self._candles_para_condicao(padrao, estado)
            if candles_condicao is None:
                continue
            if not padroes.condicao_atual(padrao, candles_condicao):
                continue
            interna = padrao["regra"]
            regra = {"tipo_regra": interna["tipo"], "parametros": interna["parametros"],
                     "direcao": interna["direcao"], "horizonte_minutos": interna["horizonte_minutos"]}
            texto = self._texto_hipotese(regra)
            taxa_oos = padrao["fora_amostra"].get("taxa")
            evidencia = f"{texto} | taxa OOS {taxa_oos:.0%}" if taxa_oos is not None else texto
            self.publicar("decisao", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                      "oportunidade": texto, "decisao": "CONSULTANDO JEV",
                                      "justificativa": "Padrão validado localmente com condição verdadeira no candle fechado",
                                      "risco": evidencia, "execucao": "-"})
            self.logger.info("oportunidade detectada; consultando JEv", simbolo=simbolo,
                             padrao=padrao["id"], analysis_id=id_analise)
            self.contadores_ciclo["aguardando_decisao"] = \
                self.contadores_ciclo.get("aguardando_decisao", 0) + 1
            self._estado_set("ANALISANDO", f"Ciclo {self.ciclo} · DECIDINDO JEV {simbolo}",
                             registrar=False, fase="DECIDINDO_JEV")
            if self.relogio() < self.proxima_tentativa_jev:
                self.publicar("decisao", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                          "oportunidade": texto, "decisao": "AGUARDANDO",
                                          "justificativa": "JEv indisponível; nova tentativa após recuo",
                                          "risco": "-", "execucao": "-"})
                continue
            estado.mathematics = dict(estado.mathematics or {})
            estado.mathematics["padrao_validado"] = {
                "id": padrao["id"], "regra": texto, "direcao": regra["direcao"],
                "horizonte_minutos": regra["horizonte_minutos"],
                "eventos": padrao["eventos"], "fora_amostra": padrao["fora_amostra"],
                "aviso": "Estatística histórica reproduzida localmente; não é garantia de retorno"}
            try:
                decisao = jev.decide(estado)
            except RuntimeError as exc:
                self.contadores_ciclo["ia_indisponivel"] = \
                    self.contadores_ciclo.get("ia_indisponivel", 0) + 1
                self.proxima_tentativa_jev = self.relogio() + TENTATIVA_JEV_S
                self.publicar("decisao", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                          "oportunidade": texto, "decisao": "AGUARDANDO",
                                          "justificativa": str(exc), "risco": "-", "execucao": "-"})
                self.logger.aviso("JEv indisponível", simbolo=simbolo, detalhe=str(exc)[:160])
                continue
            self.logger.info("decisão JEv recebida", simbolo=simbolo, acao=decisao.action,
                             probabilidade=round(decisao.probability, 2),
                             confianca=round(decisao.confidence, 2), analysis_id=id_analise)
            atualizado = self.fonte.revalidar(estado)
            if atualizado is None:
                self.publicar("decisao", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                          "oportunidade": texto, "decisao": "NÃO",
                                          "justificativa": "Sem cotação recente após JEv; entrada não executa",
                                          "risco": "-", "execucao": "nenhuma"})
                continue
            estado = atualizado
            gate = risk.check(decisao, self.book, self.book.limits, estado)
            if gate.verdict != "EXECUTE":
                self.contadores_ciclo["risco_bloqueado"] = \
                    self.contadores_ciclo.get("risco_bloqueado", 0) + 1
            if gate.verdict == "EXECUTE":
                self.contadores_ciclo["executadas"] = \
                    self.contadores_ciclo.get("executadas", 0) + 1
                self.book.execute(decisao, estado, gate)
                self.book.update(self.fonte)
                self._persistir()
                self.publicar("decisao", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                          "oportunidade": texto, "decisao": f"SIM ({decisao.action})",
                                          "justificativa": f"JEv aprovou p={decisao.probability:.0%} c={decisao.confidence:.0%}; padrão validado",
                                          "risco": f"gate EXECUTE; risco {gate.risk_amount:.2f} {self.book.currency}",
                                          "execucao": f"paper aberto: {gate.volume:g} lote(s) a {gate.entry:.8g}; stop {gate.stop:.8g}; alvo {gate.target:.8g}"})
                self.publicar("trades", list(reversed(self.book.trades[-500:])))
                self.logger.info("entrada paper aberta", simbolo=simbolo, volume=gate.volume,
                                 entrada=round(gate.entry, 8), analysis_id=id_analise)
            else:
                self.publicar("decisao", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                          "oportunidade": texto, "decisao": "NÃO",
                                          "justificativa": f"JEv {decisao.action} p={decisao.probability:.0%} c={decisao.confidence:.0%} ou limite de risco recusou",
                                          "risco": gate.reason or "-",
                                          "execucao": "nenhuma"})
                self.logger.info("entrada recusada pelo gate de risco", simbolo=simbolo,
                                 motivo=gate.reason or "-")

    def _candles_para_condicao(self, padrao, estado):
        """Escolhe a série para avaliar a condição no candle mais recente.

        Regras com minutos > 99 exigem a cauda da janela de 30 dias; a cauda
        é combinada com o estado somente quando contígua (sem lacunas entre
        elas), preservando a cronologia da avaliação.
        """
        interna = padrao["regra"]
        params = interna.get("parametros") or {}
        profundidade = max([int(params.get("minutos", 0))] +
                           [int(params.get("n", 0)) + 1] + [2])
        if len(estado.candles) > profundidade:
            return estado.candles
        cache = self.dados_ativo.get(estado.symbol)
        cauda = (cache or {}).get("tail")
        if not cauda or not estado.candles:
            return None
        if cauda[-1]["time"] + 60 == estado.candles[0]["time"]:
            return cauda[:-len(estado.candles)] + estado.candles
        return None

    # ------------------------------------------------------------- utilidades
    @staticmethod
    def _texto_hipotese(hipotese):
        params = ", ".join(f"{k}={v:g}" for k, v in hipotese["parametros"].items())
        return f"{hipotese['tipo_regra']}({params}) → {hipotese['direcao']} em {hipotese['horizonte_minutos']}min"

    @staticmethod
    def _texto_validacao(validacao):
        total, oos = validacao["total"], validacao["oos"]
        if total["eventos"] == 0:
            return "sem ocorrências"
        return (f"eventos {total['eventos']}, taxa {total['taxa']:.0%}, média {total['media_pct']}% | "
                f"OOS {oos['eventos']}, taxa {oos['taxa']:.0%}, média {oos['media_pct']}%")

    def resumo(self):
        """Instantâneo seguro para a UI, sem segredos."""
        monitorados, operaveis = self.selecao.contagens()
        return {"estado": self.estado, "detalhe": self.detalhe, "ciclo": self.ciclo,
                "jev_ok": self.jev_configurado, "deepseek_ok": self.deepseek_configurado,
                "parado_risco": self.parado_risco, "pausado": self.pausado,
                "emergencia": self.emergencia_ativa, "monitorados": monitorados,
                "operaveis": operaveis,
                "orcamento": self.orcamento.consumo() if self.orcamento else {}}
