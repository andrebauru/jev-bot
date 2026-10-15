"""Orquestrador: cadência 1/15min e pipeline Python → JEv → risco → paper.

DeepSeek está DESATIVADO no fluxo operacional: nenhuma chamada, fila, timer
ou agendamento. Toda descoberta é determinística (candidatos locais Python)
e o JEv (OpenRouter) é consultado imediatamente na mesma passagem do ativo.
`deepseek_calls` permanece publicado em 0 como evidência auditável.

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
from collections import Counter
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import copy
import json
import math
import queue
import sys
import threading
import time
import traceback

from . import cache_m1, candidatos, configuracao, jev, logs, mercados, padroes, risk
from .markets import FonteMT5
from .evento import evento_quantitativo
from .pool_matematico import PoolMatematico, max_paralelo
from .types import Decision
from .motor_matematico import estatisticas, janela_descoberta, resumo_compacto
from .painel import BANCO_PADRAO, carregar_carteira

JANELA_TTL_S = 1800          # janela M1 de 30 dias é refrescada a cada 30min por ativo
JANELAS_POR_CICLO = 4        # atualizações de janela por ciclo (round-robin)
TENTATIVA_JEV_S = 60         # recuo após falha JEv
MARGEM_CANDLE_S = 2          # segundos após o fechamento do M1
CAUDA_CANDLES = 720          # cauda guardada por ativo (~12h; cobre regras de 240min)
TENTATIVAS_CONEXAO = 3       # tentativas de conexão MT5 com recuo (2s/4s/6s)
ORCAMENTO_CICLO_S = 50       # orçamento de tempo do ciclo sem posições (cadência 1min)
ORCAMENTO_CICLO_COM_POSICAO_S = 600  # com posições (cadência 15min)
RESERVA_PROCESSAMENTO_S = 15  # fatia final do orçamento reservada ao processamento
HOLD_TTL_S = 300             # HOLD adia a oportunidade no máximo 5 minutos (expira, sem loop)
JEV_QUEUE_TTL_S = 25         # descarta requisições que aguardaram além do candle útil
# Estados por ativo do pool matemático (cálculo direto, sem padrão persistido).
ESTADOS_ATIVO = ("WAITING_HISTORY", "READY", "CALCULATING", "REJECTED",
                 "ELIGIBLE_FOR_JEV", "WAITING_JEV", "JEV_RESPONSE", "STALE", "ERROR")
MIN_CANDLES_HISTORICO = 2000  # histórico mínimo para entrar no pool matemático
# Motivos tipados por retorno do pipeline de decisão (rastreáveis na GUI).
MOTIVOS_CODIGO = {
    "inativo": "RISK_BLOCK",
    "somente_monitorar": "MERCADO_SOMENTE_MONITORAR",
    "sem_padrao": "NO_SETUP",
    "sem_dados": "INSUFFICIENT_SAMPLE",
    "condicao_ausente": "NO_SETUP",
    "pre_bloqueado": "SPREAD_TOO_HIGH",
    "contrato_invalido": "AI_INVALID_RESPONSE",
    "hold": "DECISION_HOLD",
    "jev_nao": "DECISION_NO",
    "risco_bloqueado": "RISK_BLOCK",
    "executada": "PAPER_EXECUTED",
    "hold_expirado": "HOLD_EXPIRED",
    "condicao_presente_sem_decisao": "DECISION_PENDING",
    "sem_edge_matematico": "NO_MATHEMATICAL_EDGE",
    "history": "INSUFFICIENT_HISTORY",
    "tick": "INVALID_TICK",
    "stale": "STALE_CANDLE",
    "enviado_jev": "ANALISE_ENVIADA",
    "sem_jev": "IA_INDISPONIVEL",
    "fila_cheia": "FILA_BLOQUEADA",
    "candle_ja_enviado": "ANALISE_JA_ENVIADA",
    "dados_insuficientes": "DADOS_INSUFICIENTES",
    "sem_entrada_python": "SEM_ENTRADA_PYTHON",
    "pre_risk_reject": "RISK_REJECTED_PRE_JEV",
    "post_risk_reject": "RISK_REJECTED_POST_JEV",
    "backoff": "IA_INDISPONIVEL",
    "encerrada": "PAPER_CLOSED",
}
FAS = {"VARRENDO": "VARRENDO CATÁLOGO", "BAIXANDO_HISTORICO": "BAIXANDO HISTÓRICO",
       "CALCULANDO": "CALCULANDO", "VALIDANDO": "VALIDANDO PADRÕES",
       "DECIDINDO_JEV": "DECIDINDO JEV"}
MAX_AVALIADOS = 2000         # memoria de deduplicação por candle
LOTE_DOWNLOAD = 4320         # candles por chamada ao terminal (3 dias)
CANDLE_TTL_S = 120           # validade da proposta JEv (candle M1 + 2 min)


def _agora_iso():
    return datetime.now(timezone.utc).isoformat()


def _fmt_num(valor, nd=4):
    """Número legível; `None` é dado ausente e vira N/D (nunca zero inventado)."""
    if valor is None:
        return "N/D"
    if isinstance(valor, (int, float)):
        return f"{float(valor):.{nd}g}" if nd else f"{float(valor):g}"
    return str(valor)


class Coalescedor:
    """Agrupa publicações: listas com teto por canal e registros únicos.

    A thread do motor envia aqui; a interface recebe um pacote por flush.
    Com catálogo grande, isso reduz milhares de mensagens por ciclo a
    dezenas, mantendo a janela fluida.
    """
    TETOS = {"calc": 60, "oportunidade": 60, "decisao": 60, "log": 60}
    UNICOS = ("metricas", "estado", "catalogo", "trades", "resumo")

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
        self.fila_jev = queue.Queue(maxsize=128)
        self.resultados_jev = queue.Queue()
        self.eventos_worker_jev = queue.Queue()
        self.pendentes_jev = {}
        self.worker_jev = None
        self.resumos_emitidos = {}
        self.funil_total = Counter()
        self.funil_rejeicoes = Counter()
        self.funil_rejeicoes_por_run = {}
        self.funil_por_ativo = {}
        self.funil_por_run = {}
        self.funil_eventos = deque(maxlen=1000)
        self.quant_validated_ids = set()
        self.ultimo_envio_jev = None
        self.ultima_resposta_jev = None
        self.estados_ativos = {}          # simbolo -> estado do pool matemático
        self.validados_memoria = {}       # simbolo -> candidatos validados no ciclo
        self.analises_memoria = {}        # simbolo -> análise matemática completa (BUY/SELL)
        self.matematica_memoria = {}      # simbolo -> dados brutos da última matemática
        self.analisados_jev = {}          # simbolo -> último candle enviado ao JEv
        self.analises_pendentes = {}      # simbolo -> Future da análise matemática
        self.pool_matematico = PoolMatematico(relogio=relogio)
        self.fonte = None
        self.book = None
        self.repositorio = None
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
        self.avaliados = {}        # (simbolo, padrao_id) -> candle_time já avaliado
        self.em_analise = set()    # defesa contra análise duplicada do mesmo ativo
        self.proxima_tentativa_jev = 0
        self.ultimo_monitor = 0
        self.ultimo_persist = 0
        self.trades_conhecidos = 0
        self.jev_configurado = False
        self.deepseek_configurado = False  # desativado no fluxo operacional
        self.ultimo_aviso_slot = 0
        self.contagem_scan = {}    # resumo agregado do scanner para a GUI
        self._progresso_publicado = None
        self.motivo_fonte_simbolo = {}
        self.arquivo_emergencia = Path(str(self.banco) + ".emergencia")
        self.arquivo_pausa = Path(str(self.banco) + ".pause")
        self.ultimo_resumo_hash = {}
        self.ultimo_resumo_stats = {}
        self.holds = {}            # (simbolo, oportunidade_id) -> expira_em
        self.ultimo_motivo = {}    # simbolo -> (codigo, detalhe) do último motivo
        self.ultimos = {"decisao": None, "oportunidade": None, "trade": None}
        self.cache_m1 = cache_m1.CacheM1(self.pasta_dados.parent / "cache_m1",
                                         max_dias=configuracao.dias_historico(),
                                         relogio=relogio)
        self.dias_historico = configuracao.dias_historico()
        self.prazo_ciclo = None
        self.prazo_scan = None
        self.pendentes_ciclo = []
        self.contadores_ciclo = {}
        # Sessão separada dos acumulados: zera ao reiniciar o motor, nunca
        # mistura histórico persistido com a execução corrente.
        self.contadores_sessao = Counter()
        self.sessao_rejeicoes = {}   # código -> [quantidade, exemplo_simbolo]

    def _evento_funil(self, etapa, simbolo="", *, candidate_id=None,
                      request_id=None, candle_ref=None, motivo=None,
                      duracao_ms=None, run_id=None, **metricas):
        """Contabiliza e registra uma transição auditável sem conteúdo sensível."""
        instante = self.relogio()
        run_id = run_id or getattr(self, "run_id_atual", None)
        evento = {"run_id": run_id, "etapa": etapa, "symbol": simbolo or None,
                  "candle_ref": candle_ref, "candidate_id": candidate_id,
                  "request_id": request_id, "duracao_ms": duracao_ms,
                  "motivo": motivo, **metricas}
        self.funil_eventos.append(evento)
        self.funil_total[etapa] += 1
        self.contadores_sessao[etapa] += 1
        if self.contadores_ciclo is not None:
            self.contadores_ciclo[etapa] = self.contadores_ciclo.get(etapa, 0) + 1
        if motivo and etapa in ("candidate_rejected", "ineligible_for_jev", "queue_full",
                                "queue_expired", "fetch_error",
                                "jev_http_error", "jev_parse_error", "jev_timeout",
                                "risk_veto",
                                "response_discarded_stale", "response_discarded_run_mismatch",
                                "response_discarded_duplicate"):
            duplicado = (etapa == "ineligible_for_jev" and motivo in (
                "NO_SETUP", "SEM_SETUP", "SEM_DADOS", "FILA_BLOQUEADA",
                "SPREAD_TOO_HIGH", "RISK_BLOCK", "VETO_RISCO",
                "RESPONSE_DISCARDED_STALE", "NO_VALID_MT5_STATE", "NO_VALID_TICK",
                "NO_VALID_CLOSED_M1_STATE", "CONTRACT_NOT_TRADABLE",
                "SYMBOL_SELECT_FAILED", "HISTORY_FETCH_FAILED",
                "STATISTICS_UNAVAILABLE", "CANDIDATE_GENERATION_ERROR"))
            if not duplicado:
                codigo = {"NO_SETUP": "SEM_SETUP", "QUEUE_FULL": "FILA_BLOQUEADA"}.get(
                    str(motivo), str(motivo))
                codigo = {"NO_VALID_MT5_STATE": "SEM_DADOS",
                          "NO_VALID_TICK": "SEM_DADOS",
                          "NO_VALID_CLOSED_M1_STATE": "SEM_DADOS",
                          "CONTRACT_NOT_TRADABLE": "SEM_DADOS",
                          "SYMBOL_SELECT_FAILED": "SEM_DADOS",
                          "HISTORY_FETCH_FAILED": "SEM_DADOS",
                          "STATISTICS_UNAVAILABLE": "SEM_DADOS",
                          "CANDIDATE_GENERATION_ERROR": "SEM_ELEGIBILIDADE",
                          "PIPELINE_ERROR": "SEM_ELEGIBILIDADE"}.get(codigo, codigo)
                if etapa == "risk_veto":
                    codigo = "VETO_RISCO"
                if etapa == "ineligible_for_jev":
                    codigo = {"CONTRACT_INVALID": "SEM_ELEGIBILIDADE",
                              "CONDITION_NOT_CURRENT": "SEM_ELEGIBILIDADE",
                              "PIPELINE_ERROR": "SEM_ELEGIBILIDADE",
                              "SPREAD_TOO_HIGH": "VETO_RISCO",
                              "RISK_BLOCK": "VETO_RISCO"}.get(codigo, codigo)
                self.funil_rejeicoes[codigo] += 1
                self._contar_nao_execucao(codigo, simbolo)
                if run_id:
                    self.funil_rejeicoes_por_run.setdefault(run_id, Counter())[codigo] += 1
                for extra_code in dict.fromkeys(metricas.get("reason_codes") or ()):
                    if extra_code == codigo:
                        continue
                    self.funil_rejeicoes[extra_code] += 1
                    self._contar_nao_execucao(extra_code, simbolo)
                    if run_id:
                        self.funil_rejeicoes_por_run.setdefault(run_id, Counter())[extra_code] += 1
        if etapa == "paper_not_opened" and motivo:
            self._contar_nao_execucao(str(motivo), simbolo)
        if simbolo:
            ativo = self.funil_por_ativo.setdefault(simbolo, {})
            ativo[etapa] = ativo.get(etapa, 0) + 1
            ativo.update(run_id=run_id, candle_ref=candle_ref,
                         candidate_id=candidate_id, request_id=request_id,
                         ultimo_motivo=motivo or ativo.get("ultimo_motivo"))
            if run_id:
                por_ativo = self.funil_por_run.setdefault(run_id, {}).setdefault(simbolo, {})
                por_ativo[etapa] = por_ativo.get(etapa, 0) + 1
                por_ativo.update(run_id=run_id, candle_ref=candle_ref,
                                 candidate_id=candidate_id, request_id=request_id,
                                 ultimo_motivo=motivo or por_ativo.get("ultimo_motivo"))
        if etapa == "quant_validated" and simbolo and candidate_id:
            self.quant_validated_ids.add((run_id, simbolo, candidate_id))
        if etapa in ("queue_enqueued", "jev_request_started"):
            self.ultimo_envio_jev = instante
        if etapa in ("jev_response_received", "jev_http_error", "jev_parse_error", "jev_timeout"):
            self.ultima_resposta_jev = instante
        campos = {"run_id": run_id, "symbol": simbolo or None,
                  "candle_ref": candle_ref, "candidate_id": candidate_id,
                  "request_id": request_id, "duracao_ms": duracao_ms,
                  "motivo": motivo, **metricas}
        if etapa in {"fetch_error", "history_insufficient", "candidate_generated",
                     "candidate_rejected", "quant_validated", "quant_rejected",
                     "eligible_for_jev", "ineligible_for_jev", "queue_enqueued",
                     "queue_full", "queue_deduped", "queue_expired", "worker_dequeued",
                     "jev_request_started", "jev_http_success", "jev_http_error",
                     "jev_parse_error", "jev_timeout", "jev_response_received",
                     "response_accepted", "response_discarded_stale",
                     "response_discarded_run_mismatch", "response_discarded_duplicate",
                     "decision_buy", "decision_sell", "decision_hold", "risk_veto",
                     "paper_opened", "paper_not_opened", "calculations_started",
                     "calculations_completed", "history_ready", "mathematical_edge_found",
                "decision_apply_error"}:
            self.logger.info("funil " + etapa, **campos)

    def _contar_nao_execucao(self, codigo, simbolo):
        """Motivos de n?o execu??o da sess?o, com quantidade e exemplo de s?mbolo."""
        if not codigo:
            return
        entrada = self.sessao_rejeicoes.setdefault(codigo, [0, simbolo or "-"])
        entrada[0] += 1
        if entrada[1] == "-" and simbolo:
            entrada[1] = simbolo

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
        """Reavalia a configuração do JEv (OpenRouter) após edição na UI."""
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

    def _cancelado_scan(self):
        """Cancelamento da varredura: operador, encerramento ou prazo vencido.

        Reserva RESERVA_PROCESSAMENTO_S do orçamento para processar o que
        foi coletado, evitando ciclos que consomem a cadência inteira.
        """
        if (self.encerrar.is_set() or self.evento_emergencia.is_set()
                or self.evento_ciclo_cancelado.is_set() or self.evento_pausa.is_set()):
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
                erros_consecutivos = 0
                while not self.encerrar.is_set():
                    try:
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
                        erros_consecutivos = 0
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as exc:  # recuperável: registra e segue o loop
                        erros_consecutivos += 1
                        self.logger.erro("erro recuperável no loop do motor",
                                         detalhe=str(exc)[:160], consecutive=erros_consecutivos,
                                         traceback=traceback.format_exc()[-900:])
                        self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                              "mensagem": f"Erro recuperável no ciclo: {str(exc)[:120]}"})
                        self.descarregar()
                        if erros_consecutivos >= 30:
                            raise RuntimeError(
                                f"Falha sistêmica persistente no loop: {str(exc)[:120]}") from exc
                        self.encerrar.wait(0.5)
            finally:
                self._estado_set("ENCERRANDO", "Salvando carteira paper")
                gerenciador.__exit__(None, None, None)
        except (RuntimeError, OSError, ValueError, TypeError, AttributeError) as exc:
            self.erro = str(exc) or "Falha controlada no MT5 ou banco local."
            self.logger.erro("falha fatal do motor", detalhe=self.erro,
                             traceback=traceback.format_exc()[-1200:])
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
        if etapa == "ignorado" and simbolo:
            texto = str(mensagem).casefold()
            codigo = ("NO_VALID_TICK" if "tick" in texto else
                      "CONTRACT_NOT_TRADABLE" if "contrato" in texto else
                      "SYMBOL_SELECT_FAILED" if "seleção" in texto else
                      "NO_VALID_CLOSED_M1_STATE")
            self.motivo_fonte_simbolo[simbolo] = codigo
        elif etapa == "dados" and simbolo:
            self.motivo_fonte_simbolo.pop(simbolo, None)
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
            self._processar_resultados_jev()
            self._drenar_analises(f"ciclo{self.ciclo}")
            self._monitorar()
            self.descarregar()
            if self.pausado or self.parado_risco or self.emergencia_ativa:
                return True
            # DeepSeek desativado no fluxo operacional; sem fila/timer.
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

    def _worker_jev_loop(self):
        """Thread único de rede JEv; nunca toca na carteira, GUI ou scanner."""
        while not self.encerrar.is_set():
            try:
                request_id, estado, meta, enqueued_at = self.fila_jev.get(timeout=0.25)
            except queue.Empty:
                continue
            if request_id is None:
                return
            inicio = time.monotonic()
            queue_age_ms = max(0, round((self.relogio() - enqueued_at) * 1000))
            self.eventos_worker_jev.put({"stage": "worker_dequeued", "meta": meta,
                                         "request_id": request_id,
                                         "duracao_ms": queue_age_ms})
            if queue_age_ms > JEV_QUEUE_TTL_S * 1000:
                self.eventos_worker_jev.put({"stage": "queue_expired", "meta": meta,
                                             "request_id": request_id,
                                             "duracao_ms": queue_age_ms,
                                             "motivo": "QUEUE_TTL_EXPIRED"})
                self.resultados_jev.put((request_id, meta["run_id"], None,
                                         {"code": "QUEUE_TTL_EXPIRED", "stage": "queue_expired"},
                                         0, meta))
                continue
            self.eventos_worker_jev.put({"stage": "jev_request_started", "meta": meta,
                                         "request_id": request_id,
                                         "duracao_ms": queue_age_ms})
            try:
                resposta = jev.avaliar_entrada(meta["proposta"])
                erro = None
                elapsed = max(0, round((time.monotonic() - inicio) * 1000))
                self.eventos_worker_jev.put({"stage": "jev_http_success", "meta": meta,
                                             "request_id": request_id, "duracao_ms": elapsed})
                self.eventos_worker_jev.put({"stage": "jev_response_received", "meta": meta,
                                             "request_id": request_id, "duracao_ms": elapsed})
            except Exception as exc:  # a thread só devolve erro tipado ao orquestrador
                resposta = None
                erro = {"code": getattr(exc, "code", "JEV_CLIENT_ERROR"),
                        "stage": getattr(exc, "stage", "http_error"),
                        "http_success": bool(getattr(exc, "http_success", False))}
                etapa = ("jev_timeout" if erro["stage"] == "timeout" else
                         "jev_parse_error" if erro["stage"] == "parse_error" else
                         "jev_http_error")
                if erro.get("http_success"):
                    elapsed = max(0, round((time.monotonic() - inicio) * 1000))
                    self.eventos_worker_jev.put({"stage": "jev_http_success", "meta": meta,
                                                 "request_id": request_id, "duracao_ms": elapsed})
                    self.eventos_worker_jev.put({"stage": "jev_response_received", "meta": meta,
                                                 "request_id": request_id, "duracao_ms": elapsed})
                self.eventos_worker_jev.put({"stage": etapa, "meta": meta,
                                             "request_id": request_id,
                                             "duracao_ms": max(0, round((time.monotonic() - inicio) * 1000)),
                                             "motivo": erro["code"]})
            self.resultados_jev.put((request_id, meta["run_id"], resposta, erro,
                                     max(0, round((time.monotonic() - inicio) * 1000)),
                                     meta))

    def _enfileirar_jev(self, request_id, estado, meta):
        """Despacha sem bloquear; um consumidor serializa as chamadas de rede."""
        enqueued_at = self.relogio()
        try:
            self.fila_jev.put_nowait((request_id, copy.deepcopy(estado), meta, enqueued_at))
        except queue.Full:
            return False
        if self.worker_jev is None or not self.worker_jev.is_alive():
            self.worker_jev = threading.Thread(target=self._worker_jev_loop,
                                               name="JEvNetworkWorker", daemon=True)
            self.worker_jev.start()
        return True

    def _processar_resultados_jev(self):
        """Aplica respostas na thread do orquestrador; devolve quantas aplicou."""
        self._drenar_eventos_worker_jev()
        aplicadas = 0
        while True:
            try:
                request_id, result_run_id, decisao, erro, latencia_ms, meta = \
                    self.resultados_jev.get_nowait()
            except queue.Empty:
                return aplicadas
            aplicadas += 1
            contexto = self.pendentes_jev.pop(request_id, None)
            if contexto is None:
                self._evento_funil("response_discarded_duplicate", meta["symbol"],
                                   candidate_id=meta["candidate_id"], request_id=request_id,
                                   candle_ref=meta["candle_ref"], motivo="REQUEST_CONTEXT_NOT_PENDING",
                                   run_id=result_run_id)
                continue
            contadores_atuais = self.contadores_ciclo
            self.contadores_ciclo = contexto["contadores"]
            try:
                if result_run_id != contexto["run_id"]:
                    self._evento_funil("response_discarded_run_mismatch", contexto["simbolo"],
                                       candidate_id=contexto["padrao"], request_id=request_id,
                                       candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                                       motivo="RUN_ID_MISMATCH", response_run_id=result_run_id)
                else:
                    self._finalizar_decisao_jev(contexto, decisao, erro, latencia_ms)
            except Exception:  # falha recuperável: nunca derruba o loop por um evento
                self.logger.erro("falha ao finalizar decisão JEv", simbolo=contexto["simbolo"],
                                 run_id=contexto["run_id"], request_id=request_id,
                                 detalhe=str(sys.exc_info()[1])[:160],
                                 traceback=traceback.format_exc()[-800:])
                self._evento_funil("decision_apply_error", contexto["simbolo"],
                                   candidate_id=contexto["padrao"], request_id=request_id,
                                   candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                                   motivo="DECISION_APPLY_ERROR")
                self.publicar("decisao", {**contexto["registro_base"], "instante": _agora_iso(),
                                          "decisao": "ERRO_INTERNO", "estado_api": "ERROR_DECISION_APPLY",
                                          "justificativa": "falha interna ao aplicar a resposta; "
                                                           "nenhuma ordem paper foi enviada",
                                          "risco": "-", "policy": "ERROR", "paper": "NOT_OPENED"})
            finally:
                contexto["contadores"]["aguardando_decisao"] = max(
                    0, contexto["contadores"].get("aguardando_decisao", 0) - 1)
                self.contadores_ciclo = contadores_atuais
            self._atualizar_resumo_jev(contexto["run_id"], contexto["contadores"])
        self._drenar_eventos_worker_jev()

    def _drenar_eventos_worker_jev(self):
        """Aplica telemetria publicada pela thread de rede no thread do motor."""
        while True:
            try:
                event = self.eventos_worker_jev.get_nowait()
            except queue.Empty:
                return
            meta = event["meta"]
            self._evento_funil(event["stage"], meta["symbol"],
                               candidate_id=meta["candidate_id"],
                               request_id=event["request_id"],
                               candle_ref=meta["candle_ref"], run_id=meta["run_id"],
                               duracao_ms=event.get("duracao_ms"),
                               motivo=event.get("motivo"))

    def _atualizar_resumo_jev(self, run_id, contadores):
        """Publica contadores finais do run_id quando a rede termina em background."""
        resumo = self.resumos_emitidos.get(run_id)
        if resumo is None:
            return
        em_fila = sum(c.get("run_id") == run_id for c in self.pendentes_jev.values())
        funil = {chave: valor for chave, valor in contadores.items()
                 if valor and chave != "deepseek_calls"}
        funil["deepseek_calls"] = 0
        resumo["funil"] = funil
        resumo["jev_pendentes"] = em_fila
        resumo["ativos"] = self.funil_por_run.get(run_id, {})
        resumo["motivos_rejeicao_top3"] = [
            {"codigo": k, "quantidade": v}
            for k, v in self.funil_rejeicoes_por_run.get(run_id, Counter()).most_common(3)]
        resumo["motivos"] = "; ".join(f"{k}={v}" for k, v in sorted(contadores.items()))
        if resumo.get("resultado") == "PARCIAL" and not em_fila and not resumo.get("pendentes"):
            resumo["resultado"] = "CONCLUIDO"
        self.publicar("resumo", dict(resumo))

    def _publicar_aprovacao(self, base, side, decisao, estado_api, motivo,
                            latencia_ms=None, risco=None, paper="NOT_OPENED", execucao="nenhuma"):
        """Publica a linha de aprovação JEv (nunca PENDING como decisão final)."""
        self.publicar("decisao", {**base, "direcao": side, "side_proposed": side,
                                  "decisao": decisao, "estado_api": estado_api,
                                  "justificativa": motivo or "-",
                                  "risco": risco or "-", "policy": decisao,
                                  "paper": paper, "execucao": execucao,
                                  "latencia_ms": latencia_ms})

    def _finalizar_decisao_jev(self, contexto, resposta, erro, latencia_ms):
        """Aprovação binária do JEv; o Python mantém direção e risco (paper)."""
        simbolo = contexto["simbolo"]
        proposta = contexto["proposta"]
        side = proposta["side_proposed"]
        base = {**contexto["registro_base"], "instante": _agora_iso(),
                "direcao": side, "side_proposed": side,
                "run_id": contexto.get("run_id"), "request_id": contexto.get("request_id"),
                "candle_ref": contexto.get("candle_ref"),
                "python_validated_at": contexto.get("python_validated_at"),
                "jev_dispatched_at": contexto.get("jev_dispatched_at"),
                "latencia_detecao_ms": contexto.get("latencia_detecao_ms")}
        candidato = contexto["padrao"]
        if erro:
            code = erro.get("code", "JEV_CLIENT_ERROR")
            stage = erro.get("stage", "http_error")
            if stage == "queue_expired":
                self._publicar_motivo(simbolo, contexto["id"], "FILA_BLOQUEADA", code,
                                      candle_ref=contexto["candle_ref"],
                                      candidate_id=candidato, run_id=contexto["run_id"])
                self._publicar_aprovacao(base, side, "EXPIRADO", f"ERROR_{code}",
                                         "análise expirou na fila antes do envio", latencia_ms)
                return
            self.proxima_tentativa_jev = self.relogio() + TENTATIVA_JEV_S
            self._evento_funil("jev_error", simbolo, candidate_id=candidato,
                               request_id=contexto.get("request_id"),
                               candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                               motivo=code, error_stage=stage)
            self._evento_funil("paper_not_opened", simbolo, candidate_id=candidato,
                               request_id=contexto.get("request_id"),
                               candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                               motivo="JEV_ERROR")
            self._publicar_motivo(simbolo, contexto["id"], "JEV_ERROR", code,
                                  candle_ref=contexto["candle_ref"],
                                  candidate_id=candidato, run_id=contexto["run_id"])
            self._publicar_aprovacao(base, side, "ERRO", f"ERROR_{code}",
                                     f"JEv indisponível ({stage}); nenhuma ordem", latencia_ms)
            self.logger.aviso("JEv indisponível", simbolo=simbolo,
                              run_id=contexto["run_id"], request_id=contexto.get("request_id"),
                              error_code=code, error_stage=stage)
            return

        atualizado = self.fonte.revalidar(contexto["estado"])
        expirado = (atualizado is None or not atualizado.candles
                    or atualizado.candles[-1].get("time") != contexto["candle_ref"])
        if not expirado and proposta.get("expires_at"):
            expirado = self.relogio() > proposta["expires_at"]
        if expirado:
            self.contadores_ciclo["stale_jev_response"] = \
                self.contadores_ciclo.get("stale_jev_response", 0) + 1
            self._evento_funil("jev_expired", simbolo, candidate_id=candidato,
                               request_id=contexto.get("request_id"),
                               candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                               motivo="STALE")
            self._evento_funil("paper_not_opened", simbolo, candidate_id=candidato,
                               request_id=contexto.get("request_id"),
                               candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                               motivo="STALE")
            self._publicar_motivo(simbolo, contexto["id"], "RESPONSE_DISCARDED_STALE",
                                  "candle M1/validade expirados antes da execução",
                                  candle_ref=contexto["candle_ref"],
                                  candidate_id=candidato, run_id=contexto["run_id"])
            self.estados_ativos[simbolo] = "STALE"
            self._publicar_aprovacao(base, side, "EXPIRADO", "DISCARDED_STALE",
                                     "resposta não executada: candle M1/validade expirados",
                                     latencia_ms)
            return

        if (not isinstance(resposta, dict) or not isinstance(resposta.get("approved"), bool)
                or not isinstance(resposta.get("reason"), str) or not resposta["reason"].strip()):
            self._evento_funil("jev_error", simbolo, candidate_id=candidato,
                               request_id=contexto.get("request_id"),
                               candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                               motivo="PROTOCOL_ERROR")
            self._evento_funil("paper_not_opened", simbolo, candidate_id=candidato,
                               request_id=contexto.get("request_id"),
                               candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                               motivo="JEV_ERROR")
            self._publicar_motivo(simbolo, contexto["id"], "JEV_ERROR",
                                  "resposta fora do protocolo binário",
                                  candle_ref=contexto["candle_ref"],
                                  candidate_id=candidato, run_id=contexto["run_id"])
            self._publicar_aprovacao(base, side, "ERRO", "ERROR_PROTOCOL",
                                     "resposta ambígua/legado; rejeitada pelo protocolo", latencia_ms)
            return

        self.estados_ativos[simbolo] = "JEV_RESPONSE"
        self.ultimos["decisao"] = self.relogio()
        motivo_jev = str(resposta.get("reason") or "sem motivo")[:300]
        if not resposta["approved"]:
            self._evento_funil("jev_no", simbolo, candidate_id=candidato,
                               request_id=contexto.get("request_id"),
                               candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                               motivo="JEV_REJECTED")
            self._evento_funil("paper_not_opened", simbolo, candidate_id=candidato,
                               request_id=contexto.get("request_id"),
                               candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                               motivo="JEV_REJECTED")
            self._publicar_motivo(simbolo, contexto["id"], "JEV_REJECTED", motivo_jev,
                                  candle_ref=contexto["candle_ref"],
                                  candidate_id=candidato, run_id=contexto["run_id"])
            self._publicar_aprovacao(base, side, "NAO", "COMPLETED", motivo_jev, latencia_ms)
            return

        # Aprovado: revalidar risco imediatamente antes de executar (PAPER).
        gate = risk.pre_check(side, self.book, self.book.limits, atualizado)
        if gate.verdict != "EXECUTE":
            self._evento_funil("post_risk_reject", simbolo, candidate_id=candidato,
                               request_id=contexto.get("request_id"),
                               candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                               motivo=gate.reason or "RISK_REJECTED_POST_JEV")
            self._evento_funil("paper_not_opened", simbolo, candidate_id=candidato,
                               request_id=contexto.get("request_id"),
                               candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                               motivo="RISK_REJECTED_POST_JEV")
            self._publicar_motivo(simbolo, contexto["id"], "RISK_REJECTED_POST_JEV",
                                  gate.reason or "risco mudou após a aprovação",
                                  candle_ref=contexto["candle_ref"],
                                  candidate_id=candidato, run_id=contexto["run_id"])
            self._publicar_aprovacao(base, side, "SIM", "COMPLETED",
                                     f"JEv aprovou, mas risco pós-veto: {gate.reason}",
                                     latencia_ms, risco=gate.reason)
            return

        aprovacao = Decision(simbolo, side, None, None, "python",
                             {"aprovacao_jev": motivo_jev})
        self._evento_funil("jev_yes", simbolo, candidate_id=candidato,
                           request_id=contexto.get("request_id"),
                           candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                           motivo="JEV_APPROVED", side=side)
        self.book.execute(aprovacao, atualizado, gate, extra={
            "candidate_id": candidato, "candle_ref": contexto["candle_ref"],
            "run_id": contexto["run_id"], "request_id": contexto.get("request_id"),
            "side_proposed": side, "spread_pct": proposta.get("spread"),
            "expected_net_pct": proposta.get("expectancy_net_pct"),
            "sample_size": proposta.get("sample_size"), "oos_n": proposta.get("oos_n"),
            "jev_reason": motivo_jev, "fill_price": gate.entry,
            "fill_basis": "paper_bid_ask_mt5"})
        self._evento_funil("paper_opened", simbolo, candidate_id=candidato,
                           request_id=contexto.get("request_id"),
                           candle_ref=contexto["candle_ref"], run_id=contexto["run_id"],
                           motivo="PAPER_OPENED", side=side, volume=gate.volume)
        self.book.update(self.fonte)
        self._persistir()
        self.ultimos["trade"] = self.relogio()
        self._publicar_aprovacao(base, side, "SIM", "COMPLETED", motivo_jev,
                                 latencia_ms,
                                 risco=f"autorizado; risco {_fmt_num(gate.risk_amount, 2)} {self.book.currency}",
                                 paper="OPENED",
                                 execucao=(f"paper aberto: {gate.volume:g} lote(s) a "
                                           f"{gate.entry:.8g}; stop {gate.stop:.8g}; alvo {gate.target:.8g}"))
        self.publicar("trades", list(reversed(self.book.trades[-500:])))
        self.logger.info("entrada paper aberta", simbolo=simbolo, side=side,
                         volume=gate.volume, entrada=round(gate.entry, 8),
                         analysis_id=contexto["id"], run_id=contexto["run_id"])

    def _propor_entrada(self, simbolo, dados, analise):
        """Direção determinística do Python a partir das hipóteses quantitativas."""
        itens = [(r, v) for r, v in dados.get("candidatos", [])]
        itens += [(r, v) for r, v, _o in dados.get("revalidacoes", [])]
        if not itens:
            return None

        def chave(item):
            regra, validacao = item
            total = validacao.get("total") or {}
            oos = validacao.get("oos") or {}
            return (validacao.get("status") == "validado",
                    total.get("media_pct") if total.get("media_pct") is not None else -1e9,
                    oos.get("taxa") if oos.get("taxa") is not None else -1)

        regra, validacao = max(itens, key=chave)
        direcao = regra.get("direcao")
        if direcao not in ("BUY", "SELL"):
            return None
        return {"side_proposed": direcao,
                "strategy_id": regra.get("strategy_id", regra.get("tipo_regra")),
                "candidate_id": regra.get("strategy_id", regra.get("tipo_regra")),
                "status": validacao.get("status"),
                "expectancy_net_pct": (validacao.get("total") or {}).get("media_pct"),
                "sample_size": (validacao.get("total") or {}).get("eventos"),
                "oos_n": (validacao.get("oos") or {}).get("eventos"),
                "oos_rate": (validacao.get("oos") or {}).get("taxa"),
                "reason_codes": validacao.get("reason_codes", [])}

    def _montar_proposta(self, simbolo, proposta, estado, gate, analise,
                         candle_ref, run_id, request_id):
        """Payload imutável enviado ao JEv: entrada já decidida e pré-aprovada."""
        return {"symbol": simbolo, "side_proposed": proposta["side_proposed"],
                "entry_reference": gate.entry, "stop_loss": gate.stop,
                "take_profit": gate.target, "volume": gate.volume,
                "risk_amount": gate.risk_amount, "risk_pct": self.book.limits.risk_per_trade,
                "spread": round(estado.ask - estado.bid, 10),
                "candidate_id": proposta["candidate_id"], "request_id": request_id,
                "run_id": run_id, "candle_ref": candle_ref,
                "expires_at": candle_ref + CANDLE_TTL_S,
                "evidencias": {"strategy_id": proposta["strategy_id"],
                               "status_python": proposta["status"],
                               "expectancy_net_pct": proposta["expectancy_net_pct"],
                               "sample_size": proposta["sample_size"],
                               "oos_n": proposta["oos_n"], "oos_rate": proposta["oos_rate"],
                               "reason_codes": proposta["reason_codes"],
                               "data_quality": (analise.get("history") or {}).get("data_quality"),
                               "evento": analise.get("evento")}}

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
        self.deepseek_configurado = False  # DeepSeek desativado no fluxo operacional
        mensagem = (f"Configuração: JEv {'ok' if self.jev_configurado else 'não configurado'}; "
                    "DeepSeek desativado no fluxo operacional")
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

        A configuração do JEv NÃO bloqueia mais o início: sem
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
        if not self.jev_configurado:
            self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                  "mensagem": "JEv/OpenRouter não configurado: scanner matemático continua; decisões operacionais ficam em SEM_DECISAO."})
            self.logger.aviso("JEv não configurado; scanner local continua")
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
        if any(e["event"] in ("open", "close") for e in self.book.log[eventos_antes:]):
            self.ultimos["trade"] = agora
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
        agora = self.relogio()
        por_idade = [ctx.get("enqueued_at") for ctx in self.pendentes_jev.values()
                     if ctx.get("enqueued_at") is not None]
        diagnostico = {
            "totais": {k: self.funil_total.get(k, 0) for k in (
                "symbol_selected", "symbol_operable", "symbol_available_mt5",
                "fetch_attempt", "fetch_ok", "fetch_error", "closed_m1_count",
                "cache_hit", "history_sufficient", "history_insufficient",
                "candidate_generated", "candidate_rejected", "quant_validated",
                "quant_rejected", "eligible_for_jev", "ineligible_for_jev",
                "queue_enqueue_attempt", "queue_enqueued", "queue_full",
                "queue_deduped", "queue_expired", "worker_dequeued",
                "jev_request_started", "jev_http_success", "jev_http_error",
                "jev_parse_error", "jev_timeout", "jev_response_received",
                "response_accepted", "response_discarded_stale",
                "response_discarded_run_mismatch", "response_discarded_duplicate",
                "decision_buy", "decision_sell", "decision_hold", "decision_no", "risk_veto",
                "paper_opened", "paper_not_opened",
                "symbols_selected", "history_ready", "calculations_started",
                "calculations_completed", "buy_evaluated", "sell_evaluated",
                "mathematical_edge_found")},
            "motivos_rejeicao_top3": [
                {"codigo": k, "quantidade": v}
                for k, v in self.funil_rejeicoes.most_common(3)],
            "sessao": dict(sorted(self.contadores_sessao.items())),
            "motivos_nao_execucao_top3": [
                {"codigo": k, "quantidade": qtd, "exemplo_simbolo": exemplo}
                for k, (qtd, exemplo) in sorted(self.sessao_rejeicoes.items(),
                                                key=lambda kv: -kv[1][0])[:3]],
            "motivos_codigo": {code: self.funil_rejeicoes.get(code, 0) for code in (
                "SEM_SETUP", "SEM_DADOS", "SEM_ELEGIBILIDADE", "FILA_BLOQUEADA",
                "IA_INDISPONIVEL", "SEM_DECISAO", "VETO_RISCO", "NO_MATHEMATICAL_EDGE",
                "INSUFFICIENT_HISTORY", "INVALID_TICK", "STALE_CANDLE")},
            "atividade": [
                {"simbolo": simbolo, "estado": estado,
                 "candle_ref": self.funil_por_ativo.get(simbolo, {}).get("candle_ref"),
                 "buy": self.funil_por_ativo.get(simbolo, {}).get("buy", 0),
                 "sell": self.funil_por_ativo.get(simbolo, {}).get("sell", 0),
                 "motivo": self.funil_por_ativo.get(simbolo, {}).get("ultimo_motivo")}
                for simbolo, estado in sorted(self.estados_ativos.items())],
            "pool_matematico": self.pool_matematico.metricas(),
            "analises_pendentes": len(self.analises_pendentes),
            "max_parallel_symbols": self.pool_matematico.max_workers,
            "ultimo_envio_jev_age_s": (round(agora - self.ultimo_envio_jev, 1)
                                       if self.ultimo_envio_jev is not None else None),
            "idade_fila_s": (round(agora - min(por_idade), 1) if por_idade else 0),
            "ultima_resposta_jev_age_s": (round(agora - self.ultima_resposta_jev, 1)
                                          if self.ultima_resposta_jev is not None else None),
            "fila_size": self.fila_jev.qsize(),
            "worker_alive": bool(self.worker_jev and self.worker_jev.is_alive()),
            "cost_model": {"basis": "spread_only", "commission_pct": None,
                           "slippage_pct": None,
                           "notice": "comissão/slippage não foram estimados"},
        }
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
                "jev_ok": self.jev_configurado, "deepseek_calls": 0,
                "monitorados": monitorados, "operaveis": operaveis,
                "fase": self.fase, "contadores": dict(self.contadores_ciclo),
                "diagnostico_funil": diagnostico,
                "ultimos": {chave: (round(self.relogio() - valor, 1) if valor else None)
                            for chave, valor in self.ultimos.items()},
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
        instante_inicio = _agora_iso()
        self.ciclo += 1
        id_ciclo = f"ciclo{self.ciclo}"
        run_id = __import__("uuid").uuid4().hex[:8]
        resultado_ciclo = "CONCLUIDO"
        jev_pendentes = 0
        self.logger.info("ciclo iniciado", ciclo=self.ciclo, analysis_id=id_ciclo,
                         run_id=run_id)
        self._estado_set("ANALISANDO", f"Ciclo {self.ciclo}", fase="VARRENDO")
        try:
            self._executar_ciclo(id_ciclo, run_id=run_id, emitir_resumo=False)
            if self.evento_ciclo_cancelado.is_set():
                resultado_ciclo = "CANCELADO"
            elif self.pendentes_ciclo:
                resultado_ciclo = "PARCIAL"
            jev_pendentes = sum(ctx.get("run_id") == run_id
                                for ctx in self.pendentes_jev.values())
            if jev_pendentes and resultado_ciclo == "CONCLUIDO":
                resultado_ciclo = "PARCIAL"
        except RuntimeError as exc:
            resultado_ciclo = "ERRO"
            self.logger.erro("ciclo com erro", ciclo=self.ciclo, detalhe=str(exc)[:160])
        finally:
            self.ciclo_ativo = False
            duracao = round(self.relogio() - inicio_ciclo, 1)
            if self.evento_ciclo_cancelado.is_set():
                resultado_ciclo = "CANCELADO"
                self.logger.aviso("ciclo cancelado pelo operador", ciclo=self.ciclo)
                self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                      "mensagem": "Ciclo cancelado pelo operador; restante adiado para o próximo ciclo"})
            self.descarregar()
            instante_fim = _agora_iso()
            resumo_ciclo = {"id": id_ciclo, "ciclo": self.ciclo,
                            "run_id": run_id, "instante": instante_fim,
                            "inicio": instante_inicio, "fim": instante_fim,
                            "resultado": resultado_ciclo,
                                      "oportunidades": self.contadores_ciclo.get("enviados_jev", 0),
                                      "motivos": "; ".join(f"{k}={v}" for k, v in sorted(self.contadores_ciclo.items())) or "sem candidatos monitorados",
                                      "funil": {**{e: self.contadores_ciclo[e] for e in (
                                          "symbol_selected", "symbol_operable", "symbol_available_mt5",
                                          "fetch_attempt", "fetch_ok", "fetch_error", "closed_m1_count",
                                          "cache_hit", "history_sufficient", "history_insufficient",
                                          "candidate_generated", "candidate_rejected", "quant_validated",
                                          "quant_rejected", "eligible_for_jev", "ineligible_for_jev",
                                          "queue_enqueue_attempt", "queue_enqueued", "queue_full",
                                          "queue_deduped", "queue_expired", "worker_dequeued",
                                          "jev_request_started", "jev_http_success", "jev_http_error",
                                          "jev_parse_error", "jev_timeout", "jev_response_received",
                                          "response_accepted", "response_discarded_stale",
                                          "response_discarded_run_mismatch", "response_discarded_duplicate",
                                          "decision_buy", "decision_sell", "decision_hold", "decision_no",
                                          "risk_veto", "paper_opened", "paper_not_opened",
                                          "candidatos", "candidatos_locais", "locais_validados",
                                          "rejeitado_validacao", "elegiveis", "enviados_jev",
                                          "jev_sim", "jev_nao", "jev_hold", "executadas",
                                          "pre_bloqueado", "risco_bloqueado", "sem_padrao",
                                          "hold_aguardando", "hold_expirado", "erros",
                                          "quant_candidates", "pre_risk_pass", "pre_risk_reject",
                                          "jev_enqueued", "jev_yes", "jev_no", "jev_error",
                                          "post_risk_reject", "sem_entrada_python", "jev_expired")
                                          if self.contadores_ciclo.get(e)},
                                         "deepseek_calls": 0},
                                      "tempos": {chave: (round(self.relogio() - valor, 1) if valor else None)
                                                 for chave, valor in self.ultimos.items()},
                            "pendentes": len(self.pendentes_ciclo),
                            "jev_pendentes": jev_pendentes,
                            "ativos": self.funil_por_run.get(run_id, {}),
                            "sessao": dict(sorted(self.contadores_sessao.items())),
                            "motivos_rejeicao_top3": [
                                {"codigo": k, "quantidade": v}
                                for k, v in self.funil_rejeicoes_por_run.get(run_id, Counter()).most_common(3)],
                            "duracao_s": duracao}
            self.resumos_emitidos[run_id] = resumo_ciclo
            while len(self.resumos_emitidos) > 12:
                expirado = next(iter(self.resumos_emitidos))
                self.resumos_emitidos.pop(expirado)
                self.funil_por_run.pop(expirado, None)
                self.funil_rejeicoes_por_run.pop(expirado, None)
            self.publicar("resumo", dict(resumo_ciclo))
            self.logger.info("ciclo concluído", ciclo=self.ciclo, resultado=resultado_ciclo,
                             duracao_s=duracao, run_id=run_id)

    def _executar_ciclo(self, id_ciclo, run_id=None, emitir_resumo=True):
        # Orçamento de tempo: varredura de 90+ símbolos pode ultrapassar a
        # cadência; o ciclo nunca excede o orçamento e o restante segue no
        # próximo ciclo (continuidade incremental, sem sobreposição).
        orcamento = (ORCAMENTO_CICLO_COM_POSICAO_S if self.book and self.book.fills
                     else ORCAMENTO_CICLO_S)
        self.prazo_ciclo = self.relogio() + orcamento
        self.prazo_scan = self.prazo_ciclo - RESERVA_PROCESSAMENTO_S
        self.contadores_ciclo = {"deepseek_calls": 0}
        self.quant_validated_ids = set()
        inicio_ciclo = self.relogio()
        self.run_id_atual = run_id or id_ciclo
        self._registrar_selecao()  # aplica mudanças de seleção no início do ciclo
        try:
            self._varrer_e_processar(id_ciclo, None)  # scanner incremental por símbolo
        finally:
            duracao = round(self.relogio() - inicio_ciclo, 1)
            FUNIL = ("symbol_selected", "symbol_operable", "symbol_available_mt5",
                     "fetch_attempt", "fetch_ok", "fetch_error", "closed_m1_count",
                     "cache_hit", "history_sufficient", "history_insufficient",
                     "candidate_generated", "candidate_rejected", "quant_validated",
                     "quant_rejected", "eligible_for_jev", "ineligible_for_jev",
                     "queue_enqueue_attempt", "queue_enqueued", "queue_full", "queue_deduped",
                     "queue_expired", "worker_dequeued", "jev_request_started", "jev_http_success",
                     "jev_http_error", "jev_parse_error", "jev_timeout", "jev_response_received",
                     "response_accepted", "response_discarded_stale",
                     "response_discarded_run_mismatch", "response_discarded_duplicate",
                     "decision_buy", "decision_sell", "decision_hold", "decision_no",
                     "risk_veto", "paper_opened", "paper_not_opened",
                     "candidatos", "sem_dados", "cache_incremental", "cache_completo",
                     "cache_invalido", "candidatos_locais", "locais_validados",
                     "rejeitado_validacao",
                     "validados_ativos", "elegiveis", "enviados_jev", "aguardando_decisao",
                     "jev_sim", "jev_nao", "jev_hold", "pre_bloqueado", "risco_bloqueado",
                     "contrato_invalido", "executadas", "hold_aguardando", "hold_expirado",
                     "erros", "sem_padrao", "somente_monitorar",
                     "quant_candidates", "pre_risk_pass", "pre_risk_reject",
                     "jev_enqueued", "jev_yes", "jev_no", "jev_error",
                     "post_risk_reject", "sem_entrada_python", "jev_expired")
            funil = {etapa: self.contadores_ciclo[etapa] for etapa in FUNIL
                     if self.contadores_ciclo.get(etapa)}
            tempos = {chave: (round(self.relogio() - valor, 1) if valor else None)
                      for chave, valor in self.ultimos.items()}
            resumo = "; ".join(f"{k}={v}" for k, v in sorted(self.contadores_ciclo.items()))
            if emitir_resumo:
                self.publicar("resumo", {"id": id_ciclo, "ciclo": self.ciclo,
                                         "run_id": self.run_id_atual,
                                         "instante": _agora_iso(),
                                         "resultado": "PARCIAL" if self.pendentes_ciclo else "CONCLUIDO",
                                         "oportunidades": self.contadores_ciclo.get("enviados_jev", 0),
                                         "motivos": resumo or "sem candidatos monitorados",
                                         "funil": {**funil, "deepseek_calls": 0},
                                         "sessao": dict(sorted(self.contadores_sessao.items())),
                                         "tempos": tempos,
                                         "pendentes": len(self.pendentes_ciclo),
                                         "duracao_s": duracao})
            self.logger.info("ciclo concluído (processamento)", ciclo=self.ciclo,
                             duracao_s=duracao, pendentes=len(self.pendentes_ciclo),
                             contadores=dict(self.contadores_ciclo))

    def _varrer_e_processar(self, id_ciclo, estados=None):
        """Processa símbolos incrementalmente, um estado MT5 por vez.

        Não materializa `list(FonteMT5.estados())`: isso baixava cotações,
        100 candles e contrato para todo o catálogo antes de começar a
        processar o primeiro ativo, podendo consumir o prazo inteiro sem
        uma decisão. O scanner recebe apenas os símbolos selecionados,
        Operar primeiro e pendentes de ciclo anterior prioritários.
        """
        selecionados = [s for s, (monitorar, _operar) in self.selecao.mapa.items()
                        if monitorar]
        prioridade_operar = [s for s in selecionados if self.selecao.operar(s)]
        apenas_monitorar = [s for s in selecionados if not self.selecao.operar(s)]
        for ativo in selecionados:
            self._evento_funil("symbol_selected", ativo)
            if self.selecao.operar(ativo):
                self._evento_funil("symbol_operable", ativo)
        pendentes = [s for s in self.pendentes_ciclo if s in selecionados]
        ordem = list(dict.fromkeys(pendentes + prioridade_operar + apenas_monitorar))
        self.contadores_ciclo["candidatos"] = len(ordem)
        processados = self.processados_ciclo = set()
        for simbolo in ordem:
            self._processar_resultados_jev()
            self._drenar_analises(id_ciclo)
            if self._cancelado_ciclo() or self.pausado or self.parado_risco:
                break
            self._processar_acoes()
            self.descarregar()
            if self._cancelado_ciclo() or self.pausado or self.parado_risco:
                break
            if self.relogio() > self.prazo_scan:
                self.logger.aviso("orçamento de varredura esgotado; pendentes no próximo ciclo",
                                  processados=len(processados), restantes=len(ordem) - len(processados))
                self.publicar("log", {"instante": _agora_iso(), "nivel": "aviso",
                                      "mensagem": "Orçamento de tempo da varredura esgotado; restantes seguem no próximo ciclo"})
                break
            if simbolo in self.em_analise:
                self.logger.debug("análise duplicada ignorada", simbolo=simbolo)
                continue
            estado = None
            fetch_completed = False
            inicio_fetch = time.monotonic()
            try:
                self.em_analise.add(simbolo)
                self._evento_funil("fetch_attempt", simbolo)
                try:
                    # FonteMT5 filtra símbolo antes de solicitar cotação/candles.
                    estado = next(iter(self.fonte.estados(simbolo=simbolo)), None)
                except TypeError:
                    # Dobros legados podem não aceitar o seletor; filtra localmente.
                    estado = next((s for s in self.fonte.estados() if s.symbol == simbolo), None)
                fetch_completed = True
                # A fonte pode enfileirar pausa/emergência enquanto obtém o
                # estado; aplicá-las antes de executar o ativo mantém cancelamento cooperativo.
                self._processar_acoes()
                if self.pausado or self.parado_risco or self._cancelado_ciclo():
                    break
                if estado is None:
                    motivo = self.motivo_fonte_simbolo.pop(simbolo, "NO_VALID_MT5_STATE")
                    self.contadores_ciclo["sem_dados"] = self.contadores_ciclo.get("sem_dados", 0) + 1
                    self._evento_funil("fetch_error", simbolo, motivo=motivo,
                                       duracao_ms=round((time.monotonic() - inicio_fetch) * 1000))
                    self._evento_funil("history_insufficient", simbolo,
                                       motivo=motivo)
                    self._publicar_motivo(simbolo, id_ciclo, motivo,
                                          "sem M1/tick/contrato válido")
                else:
                    self._evento_funil("fetch_ok", simbolo,
                                       candle_ref=(estado.candles[-1].get("time") if estado.candles else None),
                                       duracao_ms=round((time.monotonic() - inicio_fetch) * 1000))
                    self._evento_funil("symbol_available_mt5", simbolo,
                                       candle_ref=(estado.candles[-1].get("time") if estado.candles else None))
                    self._evento_funil("closed_m1_count", simbolo,
                                       candle_ref=(estado.candles[-1].get("time") if estado.candles else None),
                                       quantidade=len(estado.candles))
                    self._aguardar_vaga(id_ciclo)
                    retorno = self._processar_ativo(estado, id_ciclo, paralelo=True)
                    if retorno == "CALCULATING":
                        pass  # aplicação ocorre em _drenar_analises
                    else:
                        codigo = MOTIVOS_CODIGO.get(retorno, retorno or "SEM_DECISAO")
                        self._publicar_motivo(simbolo, id_ciclo, codigo,
                                              retorno or "sem candidato elegível",
                                              candle_ref=(estado.candles[-1].get("time")
                                                          if estado.candles else None))
            except (RuntimeError, ValueError, KeyError) as exc:
                self.contadores_ciclo["erros"] = self.contadores_ciclo.get("erros", 0) + 1
                codigo_erro = ("COST_UNKNOWN" if "COST_UNKNOWN" in str(exc)
                               else "SEM_ELEGIBILIDADE")
                if not fetch_completed:
                    self._evento_funil("fetch_error", simbolo, motivo="MT5_STATE_ERROR",
                                       detalhe=str(exc)[:120],
                                       duracao_ms=round((time.monotonic() - inicio_fetch) * 1000))
                else:
                    self._evento_funil("ineligible_for_jev", simbolo, motivo=codigo_erro,
                                       detalhe=str(exc)[:120])
                self._publicar_motivo(simbolo, id_ciclo, codigo_erro, str(exc)[:160])
                self.logger.erro("falha na análise", simbolo=simbolo, detalhe=str(exc)[:160])
            finally:
                self.em_analise.discard(simbolo)
                if simbolo not in self.analises_pendentes:
                    processados.add(simbolo)
                self.ultimo_simbolo_ciclo = simbolo
                self.descarregar()
        # Aplica o que já terminou e aguarda o restante dentro do orçamento;
        # o JEv é despachado assim que cada análise conclui, sem esperar o lote.
        while self.analises_pendentes and not self.encerrar.is_set():
            self._drenar_analises(id_ciclo)
            if not self.analises_pendentes or self.relogio() > self.prazo_ciclo:
                break
            futuro = next(iter(self.analises_pendentes.values()))["futuro"]
            try:
                futuro.result(timeout=0.05)
            except Exception:
                pass
            self._processar_acoes()
            self._processar_resultados_jev()
        self._drenar_analises(id_ciclo)
        # Dreno curto e limitado das respostas JEv já prontas (não espera o lote;
        # respostas tardias continuam sendo tratadas em _aguardar_slot).
        fim_dreno = self.relogio() + 0.5
        while self.pendentes_jev and self.relogio() < fim_dreno and not self.encerrar.is_set():
            self._processar_acoes()
            if self._processar_resultados_jev():
                continue
            self.encerrar.wait(0.02)
        # Continuidade incremental: nenhum símbolo monitorado é esquecido.
        self.pendentes_ciclo = [s for s in ordem
                                if s not in processados or s in self.analises_pendentes]
        self.contadores_ciclo["varridos"] = len(processados)
        self.contadores_ciclo["max_concorrencia_matematica"] = \
            self.pool_matematico.metricas()["pico_concorrencia"]
        self.logger.info("varredura incremental concluída", candidatos=len(ordem),
                         varridos=len(processados), pendentes=len(self.pendentes_ciclo),
                         em_calculo=len(self.analises_pendentes),
                         deepseek_operacional=0)

    def _publicar_motivo(self, simbolo, cycle_id, codigo, detalhe,
                         candle_ref=None, candidate_id=None, run_id=None):
        """Resultado individual de triagem, sem fabricar decisão JEv.

        Publica no canal `oportunidade` (painel Python Quantitativo da GUI):
        ativo, estado matemático e motivo — inclusive rejeições.
        """
        if codigo not in ("DECISION_PENDING", "DECISION_HOLD", "DECISION_NO",
                          "PAPER_EXECUTED", "SEM_DECISAO", "ERRO_API"):
            self._evento_funil("ineligible_for_jev", simbolo,
                               candidate_id=candidate_id, candle_ref=candle_ref,
                               motivo=codigo, detalhe=detalhe, run_id=run_id)
        chave = (codigo, detalhe)
        if self.ultimo_motivo.get(simbolo) == chave:
            return
        self.ultimo_motivo[simbolo] = chave
        self.publicar("oportunidade", {"id": cycle_id, "simbolo": simbolo,
                                       "instante": _agora_iso(),
                                       "run_id": run_id or getattr(self, "run_id_atual", cycle_id),
                                       "candle_ref": candle_ref,
                                       "candidate_id": candidate_id,
                                       "motivo": codigo, "estado": detalhe})

    def _coletar_janela(self, simbolo, id_analise):
        """Download paginado da janela M1 configurada (padrão 7 dias)."""
        self._estado_set("ANALISANDO", f"Ciclo {self.ciclo} · BAIXANDO HISTÓRICO {simbolo}",
                         registrar=False, fase="BAIXANDO_HISTORICO")

        def progresso(coletados, solicitado):
            self.publicar("calc", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                   "etapa": "janela",
                                   "mensagem": f"Baixando histórico M1 ({self.dias_historico} dias)",
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
        self._evento_funil("fetch_attempt", simbolo, history_days=self.dias_historico,
                           fetch_kind="history")
        janela = self.fonte.janela_m1(simbolo, dias=self.dias_historico, lote=LOTE_DOWNLOAD,
                                      cancelado=self._cancelado_scan, progresso=progresso)
        duracao = round(time.time() - inicio, 2)
        if janela is None:
            motivo = "cancelado" if self._cancelado_scan() else "indisponível"
            self._evento_funil("fetch_error", simbolo, motivo="HISTORY_FETCH_FAILED",
                               duracao_ms=round(duracao * 1000), history_days=self.dias_historico)
            self._evento_funil("history_insufficient", simbolo,
                               motivo="HISTORY_FETCH_FAILED", history_days=self.dias_historico)
            self.logger.info("download de histórico não concluído", simbolo=simbolo, motivo=motivo)
            return None
        self._evento_funil("fetch_ok", simbolo, candle_ref=janela["fim"],
                           duracao_ms=round(duracao * 1000),
                           closed_m1_count=janela["recebidos"],
                           gap_minutes=janela["minutos_lacuna"],
                           history_days=self.dias_historico)
        self._evento_funil("closed_m1_count", simbolo, candle_ref=janela["fim"],
                           quantidade=janela["recebidos"], history_days=self.dias_historico)
        self.logger.info("download de histórico concluído", simbolo=simbolo,
                         candles=janela["recebidos"], lacunas=janela["lacunas"],
                         lotes=max(1, -(-janela["recebidos"] // LOTE_DOWNLOAD)), duracao_s=duracao)
        return janela

    def _processar_ativo(self, estado, id_ciclo, paralelo=False):
        """Pipeline de um ativo monitorado.

        Com `paralelo=True` e janela nova, o cálculo pesado vai ao pool
        matemático (Future) e a aplicação ocorre em `_drenar_analises` na
        thread do orquestrador; sem janela nova, usa o cache e avalia.
        """
        simbolo = estado.symbol
        id_analise = f"{id_ciclo}-{simbolo}"
        agora = self.relogio()
        cache = self.dados_ativo.get(simbolo)
        candle_ref = estado.candles[-1].get("time") if estado.candles else None
        vencida = cache is None or agora - cache["instante"] >= JANELA_TTL_S
        if cache is not None and not vencida:
            self._evento_funil("cache_hit", simbolo,
                               candle_ref=(estado.candles[-1].get("time")
                                           if estado.candles else None),
                               cache_age_s=round(agora - cache["instante"], 1),
                               cache_kind="memory")
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
                cotacao = self.fonte.cotacao(simbolo)
                if cotacao is None:
                    raise RuntimeError("COST_UNKNOWN: cotação atual indisponível; validação de custos suspensa")
                bid, ask, _ = cotacao
                preco_atual = (bid + ask) / 2
                spread_abs = ask - bid
                if not (0 < preco_atual and math.isfinite(spread_abs) and spread_abs >= 0):
                    raise RuntimeError("COST_UNKNOWN: bid/ask inválidos; validação de custos suspensa")
                if paralelo:
                    spec = (estado.mathematics or {}).get("mt5_execution_spec")
                    self.estados_ativos[simbolo] = "CALCULATING"
                    self._evento_funil("calculations_started", simbolo,
                                       candle_ref=(estado.candles[-1].get("time")
                                                   if estado.candles else None),
                                       closed_m1_count=len(janela["candles"]),
                                       pool_max=self.pool_matematico.max_workers)
                    self.analises_pendentes[simbolo] = {
                        "futuro": self.pool_matematico.submeter(
                            simbolo, self._matematica_pura, simbolo, janela, preco_atual,
                            spread_abs, spec, self.repositorio.padroes_ativos(simbolo)),
                        "estado": estado, "janela": janela, "id_analise": id_analise,
                        "run_id": getattr(self, "run_id_atual", id_analise)}
                    return "CALCULATING"
                self._atualizar_cache(simbolo, janela, id_analise,
                                      execution_spec=(estado.mathematics or {}).get(
                                          "mt5_execution_spec"))
                cache = self.dados_ativo.get(simbolo)
        if cache is None or cache.get("stats") is None:
            self.estados_ativos[simbolo] = "WAITING_HISTORY"
            self._evento_funil("history_insufficient", simbolo,
                               candle_ref=(estado.candles[-1].get("time")
                                           if estado.candles else None),
                               motivo="STATISTICS_UNAVAILABLE")
            self.publicar("calc", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                   "etapa": "dados", "mensagem": "Estatísticas aguardando atualização da janela",
                                   "amostra": None, "padroes_testados": None,
                                   "resultado": None, "erro": None})
            return "sem_dados"
        stats = cache["stats"]
        custo_pct = cache["custo_pct"]
        self.estados_ativos[simbolo] = "READY"
        self._evento_funil("history_ready", simbolo,
                           candle_ref=(estado.candles[-1].get("time")
                                       if estado.candles else None),
                           closed_m1_count=cache.get("stats", {}).get("candles_janela_completa"))
        stage = "history_sufficient" if stats.get("disponivel") else "history_insufficient"
        self._evento_funil(stage, simbolo,
                           candle_ref=(estado.candles[-1].get("time")
                                       if estado.candles else None),
                           closed_m1_count=stats.get("candles_janela_completa",
                                                    stats.get("candles")),
                           window_days=stats.get("dias_janela_completa", stats.get("dias")),
                           training_days=stats.get("dias"),
                           gap_minutes=stats.get("minutos_lacuna"),
                           motivo=None if stats.get("disponivel") else
                           stats.get("motivo", "STATISTICS_UNAVAILABLE"))
        ativos_repo = self.repositorio.padroes_ativos(simbolo)
        self.publicar("calc", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                               "etapa": "calculado", "mensagem": "Estatísticas locais prontas (Python; cálculo não consome tokens)",
                                "amostra": {"candles": stats.get("candles"), "dias": stats.get("dias"),
                                            "candles_janela": stats.get("candles_janela_completa"),
                                            "dias_janela": stats.get("dias_janela_completa"),
                                           "lacunas": stats.get("lacunas"),
                                           "retorno_janela_pct": stats.get("retorno_janela_pct")},
                               "padroes_testados": len(ativos_repo),
                               "resultado": f"movimento mediano {stats['movimento_1min']['mediana_pct']}% | custo spread {custo_pct}%",
                               "erro": None})
        dados_mem = self.matematica_memoria.get(simbolo)
        if dados_mem is not None:
            dados = dict(dados_mem)
            if candle_ref:
                dados["candle_ref"] = candle_ref
            return self._despachar_analise(estado, dados, id_analise)
        return self._oportunidade(estado, id_analise)

    def _aguardar_vaga(self, id_ciclo):
        """Garante no máximo `max_workers` análises em voo (5º aguarda vaga)."""
        while len(self.analises_pendentes) >= self.pool_matematico.max_workers:
            self._processar_acoes()
            self._processar_resultados_jev()
            if self._drenar_analises(id_ciclo):
                continue
            futuro = next(iter(self.analises_pendentes.values()))["futuro"]
            try:
                futuro.result(timeout=0.05)
            except Exception:  # timeout/erro tratados em _drenar_analises
                pass
            self._drenar_analises(id_ciclo)
            if self.encerrar.is_set() or self.pausado or self.parado_risco:
                return
            if self.relogio() > self.prazo_scan:
                return

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
            self._evento_funil("fetch_attempt", simbolo, candle_ref=ultimo,
                               fetch_kind="incremental")
            novos = self.fonte.incremento_m1(simbolo, desde=ultimo)
            if novos is not None:
                combinados, info = self.cache_m1.anexar(simbolo, novos)
                if not info["recarregar"] and len(combinados) >= 2:
                    self.contadores_ciclo["cache_incremental"] = \
                        self.contadores_ciclo.get("cache_incremental", 0) + 1
                    self._evento_funil("fetch_ok", simbolo, candle_ref=combinados[-1]["time"],
                                       fetch_kind="incremental", appended=info["anexados"],
                                       closed_m1_count=len(combinados))
                    self._evento_funil("cache_hit", simbolo, candle_ref=combinados[-1]["time"],
                                       cache_kind="incremental", appended=info["anexados"],
                                       closed_m1_count=len(combinados))
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

    @staticmethod
    def _validar_candidato_m1(regra, candles, custo_pct, stats, familias_testadas):
        """Aplica OOS e, com especificação MT5, simulação trade-level sob custos."""
        validacao = padroes.validar(regra, candles, custo_pct,
                                    familias_testadas=familias_testadas)
        spec = stats.get("execution_spec")
        if "execution_spec" not in stats:
            return validacao  # fixtures/relatórios legados sem contrato MT5
        if not isinstance(spec, dict):
            validacao["cost_model_complete"] = False
            validacao["cost_model_status"] = "COST_UNKNOWN"
            validacao["reason_codes"] = list(dict.fromkeys(
                [*validacao.get("reason_codes", []), "COST_UNKNOWN"]))
            if validacao["status"] == "validado":
                validacao.update(status="rejeitado", motivo="Especificação de execução ausente",
                                 reason_code="COST_UNKNOWN", reason_codes=["COST_UNKNOWN"])
            return validacao
        simulacao = padroes.simular_trade_level(
            regra, candles, spec["spread_abs"], point=spec["point"],
            trade_stops_level=spec["trade_stops_level"], commission_pct=None)
        validacao["trade_level"] = simulacao
        validacao["cost_model_complete"] = False  # comissão/slippage reais indisponíveis
        validacao["cost_model_status"] = "COST_INCOMPLETE"
        if validacao["status"] != "validado":
            return validacao
        stress = simulacao.get("scenarios", {}).get("extra_1_spread", {})
        treino, oos = stress.get("train", {}), stress.get("oos", {})
        if oos.get("n", 0) < padroes.MIN_EVENTOS_OOS:
            validacao.update(status="rejeitado", motivo="Amostra trade-level OOS insuficiente",
                             reason_code="TRADE_LEVEL_OOS_INSUFFICIENT",
                             reason_codes=["TRADE_LEVEL_OOS_INSUFFICIENT"])
        elif (treino.get("expectancy_pct") is None or oos.get("expectancy_pct") is None
              or treino["expectancy_pct"] <= 0 or oos["expectancy_pct"] <= 0):
            validacao.update(status="rejeitado", motivo="Expectativa não resiste ao cenário de slippage",
                             reason_code="TRADE_LEVEL_STRESS_NEGATIVE",
                             reason_codes=["TRADE_LEVEL_STRESS_NEGATIVE"])
        return validacao

    # ----------------------------------------------------- pool matemático M1
    def _matematica_pura(self, simbolo, janela, preco_atual, spread_abs,
                         execution_spec, padroes_ativos):
        """Cálculo M1 puro por símbolo, sem MT5/disco/GUI/estado compartilhado.

        Roda em worker do pool: recebe candles já materializados e devolve
        dados; registro, cache, publicação e despacho JEv ficam na thread do
        orquestrador. Bilaterais BUY/SELL saem das famílias matemáticas.
        """
        descoberta, _ = janela_descoberta(janela["candles"])
        recorte = {**janela, "candles": descoberta, "fim": descoberta[-1]["time"]}
        stats = estatisticas(recorte, 0.0)
        custo_pct = round(spread_abs / preco_atual * 100, 4)
        spread_pct = spread_abs / preco_atual if preco_atual > 0 else None
        evento = evento_quantitativo(janela["candles"], spread_pct=spread_pct)
        stats["custo_spread_pct"] = custo_pct
        stats["cost_basis"] = "spread_only"
        stats["commission_pct"] = None
        stats["slippage_pct"] = None
        stats["execution_spec"] = ({**execution_spec, "spread_abs": spread_abs}
                                   if isinstance(execution_spec, dict)
                                   and math.isfinite(float(execution_spec.get("point", 0)))
                                   and float(execution_spec.get("point", 0)) > 0
                                   and int(execution_spec.get("trade_stops_level", -1)) >= 0
                                   else None)
        dias_janela = round((janela["candles"][-1]["time"] -
                             janela["candles"][0]["time"]) / 86400, 2)
        stats["dias_janela_completa"] = dias_janela
        stats["candles_janela_completa"] = len(janela["candles"])
        revalidacoes = []
        for registro in padroes_ativos:
            interna = registro["regra"]
            regra = {"tipo_regra": interna["tipo"], "parametros": interna["parametros"],
                     "direcao": interna["direcao"], "horizonte_minutos": interna["horizonte_minutos"],
                     "justificativa": registro.get("justificativa", ""),
                     "strategy_id": registro.get("strategy_id", interna["tipo"])}
            validacao = self._validar_candidato_m1(
                regra, janela["candles"], custo_pct, stats,
                familias_testadas=candidatos.MAX_LOCAL_HYPOTHESES)
            revalidacoes.append((regra, validacao, registro.get("origem", "local")))
        erro_geracao = None
        try:
            regras = candidatos.regras_candidatas(stats, janela["candles"])
        except (ValueError, TypeError, KeyError) as exc:
            regras, erro_geracao = [], str(exc)[:120]
        avaliacoes = []
        for regra in regras:
            validacao = self._validar_candidato_m1(
                regra, janela["candles"], custo_pct, stats,
                familias_testadas=max(1, len(regras)))
            avaliacoes.append((regra, validacao))
        return {"stats": stats, "custo_pct": custo_pct, "dias_janela": dias_janela,
                "candle_ref": janela["candles"][-1]["time"], "evento": evento,
                "revalidacoes": revalidacoes, "candidatos": avaliacoes,
                "erro_geracao": erro_geracao, "regras_total": len(regras),
                "buy_evaluated": sum(1 for r, _ in avaliacoes if r["direcao"] == "BUY"),
                "sell_evaluated": sum(1 for r, _ in avaliacoes if r["direcao"] == "SELL")}

    def _aplicar_matematica(self, simbolo, estado, dados, janela, id_analise, id_ciclo):
        """Aplica na thread do orquestrador o resultado do pool (registro/cache/GUI)."""
        stats = dados["stats"]
        custo_pct = dados["custo_pct"]
        if estado.candles:
            # O candle de referência é o último M1 fechado do snapshot analisado.
            dados["candle_ref"] = estado.candles[-1]["time"]
        candle_ref = dados["candle_ref"]
        janela_info = {"inicio": janela["inicio"], "fim": janela["fim"],
                       "dias": dados["dias_janela"], "candles": janela["recebidos"]}
        self._evento_funil("calculations_completed", simbolo, candle_ref=candle_ref,
                           duracao_ms=dados.get("duracao_ms"),
                           closed_m1_count=stats.get("candles_janela_completa", stats.get("candles")),
                           window_days=dados["dias_janela"], training_days=stats.get("dias"),
                           buy_evaluated=dados["buy_evaluated"],
                           sell_evaluated=dados["sell_evaluated"],
                           regras_total=dados["regras_total"])
        self.contadores_ciclo["buy_evaluated"] = \
            self.contadores_ciclo.get("buy_evaluated", 0) + dados["buy_evaluated"]
        self.contadores_ciclo["sell_evaluated"] = \
            self.contadores_ciclo.get("sell_evaluated", 0) + dados["sell_evaluated"]
        self.funil_por_ativo.setdefault(simbolo, {}).update(
            buy=dados["buy_evaluated"], sell=dados["sell_evaluated"],
            candle_ref=candle_ref)
        validados = []
        for regra, validacao, origem in dados["revalidacoes"]:
            atualizado = self.repositorio.registrar(simbolo, regra, validacao, origem, janela_info)
            trades_padrao = [tr for tr in self.book.trades if tr.get("padrao") == atualizado["id"]]
            atualizado["calibracao"] = padroes.calibracao_brier(trades_padrao)
            if validacao["status"] == "rejeitado":
                self.contadores_ciclo["rejeitado_validacao"] = \
                    self.contadores_ciclo.get("rejeitado_validacao", 0) + 1
            if validacao["status"] == "validado":
                validados.append(atualizado)
        if dados["erro_geracao"]:
            self._evento_funil("candidate_rejected", simbolo, candle_ref=candle_ref,
                               motivo="CANDIDATE_GENERATION_ERROR",
                               detalhe=dados["erro_geracao"])
            self.logger.aviso("falha ao gerar candidatos locais", simbolo=simbolo,
                              detalhe=dados["erro_geracao"])
        if not dados["candidatos"] and not dados["erro_geracao"]:
            self._evento_funil("candidate_rejected", simbolo, candle_ref=candle_ref,
                               motivo="SEM_SETUP" if stats.get("disponivel") else "INSUFFICIENT_HISTORY",
                               disponivel=stats.get("disponivel"),
                               closed_m1_count=len(janela.get("candles", [])),
                               custo_spread_pct=custo_pct, window_days=dados["dias_janela"])
        for regra, validacao in dados["candidatos"]:
            registro = self.repositorio.registrar(simbolo, regra, validacao, "local", janela_info)
            candidate_id = registro["id"]
            self._evento_funil("candidate_generated", simbolo, candidate_id=candidate_id,
                               candle_ref=candle_ref,
                               strategy_id=registro.get("strategy_id", regra["tipo_regra"]),
                               direcao=regra["direcao"],
                               horizonte_minutos=regra["horizonte_minutos"])
            self.contadores_ciclo["candidatos_locais"] = \
                self.contadores_ciclo.get("candidatos_locais", 0) + 1
            self.publicar("calc", {"id": id_analise, "simbolo": simbolo,
                                   "run_id": getattr(self, "run_id_atual", id_analise),
                                   "strategy_id": registro.get("strategy_id", regra["tipo_regra"]),
                                   "instante": _agora_iso(), "etapa": "validacao_local",
                                   "padrao": registro["id"], "candidate_id": registro["id"],
                                   "candle_ref": candle_ref,
                                   "direcao": regra["direcao"],
                                   "status_consulta": "CANDIDATO MATEMÁTICO (Python)",
                                   "reason_code": validacao.get("reason_code"),
                                   "hipotese": self._texto_hipotese(regra),
                                   "validacao": f"{validacao['status']}: {validacao['motivo']}",
                                   "resultado": self._texto_validacao(validacao)})
            if validacao["status"] == "validado":
                self._evento_funil("mathematical_edge_found", simbolo, candidate_id=candidate_id,
                                   candle_ref=candle_ref, motivo="QUANT_VALIDATED")
                self._evento_funil("quant_validated", simbolo, candidate_id=candidate_id,
                                   candle_ref=candle_ref, motivo="QUANT_VALIDATED",
                                   sample_size=validacao["total"]["eventos"],
                                   oos_n=validacao["oos"]["eventos"],
                                   oos_rate=validacao["oos"]["taxa"],
                                   expected_net_return=validacao["total"]["media_pct"],
                                   cost_model_status=validacao.get("cost_model_status", "COST_INCOMPLETE"))
                self.contadores_ciclo["locais_validados"] = \
                    self.contadores_ciclo.get("locais_validados", 0) + 1
                validados.append(registro)
            else:
                reason_code = validacao.get("reason_code", "QUANT_REJECTED")
                self._evento_funil("candidate_rejected", simbolo, candidate_id=candidate_id,
                                   candle_ref=candle_ref, motivo=reason_code,
                                   reason_codes=validacao.get("reason_codes", [reason_code]))
                self._evento_funil("quant_rejected", simbolo, candidate_id=candidate_id,
                                   candle_ref=candle_ref, motivo=reason_code,
                                   reason_codes=validacao.get("reason_codes", [reason_code]))
                self.contadores_ciclo["rejeitado_validacao"] = \
                    self.contadores_ciclo.get("rejeitado_validacao", 0) + 1
        self.validados_memoria[simbolo] = validados
        self.dados_ativo[simbolo] = {
            "instante": self.relogio(), "stats": stats, "custo_pct": custo_pct,
            "inicio_janela": janela["inicio"], "fim_janela": janela["fim"],
            "tail": janela["candles"][-CAUDA_CANDLES:]}
        self.estados_ativos[simbolo] = "REJECTED" if not validados else "ELIGIBLE_FOR_JEV"
        self.publicar("calc", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                               "etapa": "calculado",
                               "mensagem": "Estatísticas locais prontas (Python; cálculo não consome tokens)",
                               "amostra": {"candles": stats.get("candles"),
                                           "dias": stats.get("dias"),
                                           "candles_janela": stats.get("candles_janela_completa"),
                                           "dias_janela": stats.get("dias_janela_completa"),
                                           "lacunas": stats.get("lacunas"),
                                           "retorno_janela_pct": stats.get("retorno_janela_pct")},
                               "padroes_testados": len(dados["candidatos"]) + len(dados["revalidacoes"]),
                               "resultado": (f"BUY {dados['buy_evaluated']} | SELL {dados['sell_evaluated']} | "
                                             f"movimento mediano {stats.get('movimento_1min', {}).get('mediana_pct')}% | "
                                             f"custo spread {custo_pct}%"),
                               "erro": None})
        self.logger.info("estatísticas locais calculadas", simbolo=simbolo,
                         candles=stats.get("candles"), dias=stats.get("dias"))
        self.matematica_memoria[simbolo] = dados
        return self._despachar_analise(estado, dados, id_analise)

    def _montar_analise(self, simbolo, dados):
        """Payload quantitativo BUY/SELL entregue ao JEv (informação, não veto)."""
        stats = dados["stats"]
        itens = [(regra, validacao) for regra, validacao in dados["candidatos"]]
        itens += [(regra, validacao) for regra, validacao, _origem in dados["revalidacoes"]]

        def lado(direcao):
            candidatos = [(r, v) for r, v in itens if r["direcao"] == direcao]
            if not candidatos:
                return {"sample_size": stats.get("candles_janela_completa"),
                        "oos_n": 0, "expectancy_net_pct": None,
                        "estado": "NENHUM_SETUP_ATIVO"}
            regra, v = max(candidatos, key=lambda rv: rv[1]["total"].get("media_pct") or -1e9)
            return {"strategy_id": regra.get("strategy_id", regra["tipo_regra"]),
                    "sample_size": v["total"]["eventos"],
                    "in_sample_rate": v["amostra"]["taxa"],
                    "oos_n": v["oos"]["eventos"], "oos_rate": v["oos"]["taxa"],
                    "expectancy_gross_pct": v["total"]["media_pct"],
                    "expectancy_net_pct": v["total"]["media_pct"],
                    "wilson_lower_oos_adjusted": v["estabilidade"].get("wilson_inferior_oos_ajustado"),
                    "reason_codes": v.get("reason_codes", []),
                    "estado": v.get("status")}

        ev = dados.get("evento") or {}
        return {"symbol": simbolo, "candle_time": dados["candle_ref"], "timeframe": "M1",
                "history": {"days": dados["dias_janela"],
                            "bars_available": ev.get("bars_available"),
                            "bars_used": ev.get("bars_used"),
                            "last_closed_m1": ev.get("last_closed_m1"),
                            "coverage_minutes": ev.get("coverage_minutes"),
                            "data_quality": ev.get("data_quality")},
                "evento": ev,
                "cost_spread_pct": dados["custo_pct"], "cost_basis": "spread_only",
                "commission": "NAO_ESTIMADA", "slippage": "NAO_ESTIMADA",
                "buy": lado("BUY"), "sell": lado("SELL"),
                "candidates_evaluated": len(dados["candidatos"]) + len(dados["revalidacoes"]),
                "instructions": "JEv decide BUY, SELL ou HOLD; HOLD é adequado sem suporte "
                                "direcional. Python não escolhe direção.",
                "aviso": "métricas informam a decisão; Python não veta por qualidade estatística"}

    def _despachar_analise(self, estado, dados, id_analise):
        """Direção e risco são do Python; o JEv só aprova a entrada proposta."""
        simbolo = estado.symbol
        candle_ref = dados["candle_ref"]
        analise = self._montar_analise(simbolo, dados)
        self.analises_memoria[simbolo] = analise
        evento = dados.get("evento") or {}
        if evento.get("status") != "OK":
            self.contadores_ciclo["dados_insuficientes"] = \
                self.contadores_ciclo.get("dados_insuficientes", 0) + 1
            self._publicar_motivo(simbolo, id_analise, "DADOS_INSUFICIENTES",
                                  evento.get("reason", "dados essenciais ausentes"),
                                  candle_ref=candle_ref)
            return "dados_insuficientes"
        if self.book is None or not self.book.active or self.parado_risco or self.emergencia_ativa:
            return "inativo"
        if not self.selecao.operar(simbolo):
            self.contadores_ciclo["somente_monitorar"] = \
                self.contadores_ciclo.get("somente_monitorar", 0) + 1
            return "somente_monitorar"
        self._evento_funil("quant_candidates", simbolo, candle_ref=candle_ref,
                           motivo="ANALISE_PYTHON")
        proposta = self._propor_entrada(simbolo, dados, analise)
        if proposta is None:
            self._evento_funil("sem_entrada_python", simbolo, candle_ref=candle_ref,
                               motivo="SEM_HIPOTESE_DIRECIONAL")
            self._publicar_motivo(simbolo, id_analise, "SEM_ENTRADA_PYTHON",
                                  "sem hipótese direcional do Python", candle_ref=candle_ref)
            self.estados_ativos[simbolo] = "REJECTED"
            return "sem_entrada_python"
        if self.analisados_jev.get(simbolo) == candle_ref:
            self._evento_funil("queue_deduped", simbolo, candle_ref=candle_ref,
                               motivo="ANALISE_JA_ENVIADA")
            return "candle_ja_enviado"
        if self.proxima_tentativa_jev > self.relogio():
            self._evento_funil("paper_not_opened", simbolo, candle_ref=candle_ref,
                               motivo="JEV_BACKOFF")
            self._publicar_motivo(simbolo, id_analise, "IA_INDISPONIVEL",
                                  "recuo após erro de API; sem chamada nova",
                                  candle_ref=candle_ref)
            return "backoff"
        side = proposta["side_proposed"]
        gate = risk.pre_check(side, self.book, self.book.limits, estado)
        if gate.verdict != "EXECUTE":
            self.contadores_ciclo["risco_bloqueado"] = \
                self.contadores_ciclo.get("risco_bloqueado", 0) + 1
            self._evento_funil("pre_risk_reject", simbolo, candle_ref=candle_ref,
                               side=side, motivo=gate.reason or "RISK_REJECTED_PRE_JEV")
            self._evento_funil("paper_not_opened", simbolo, candle_ref=candle_ref,
                               motivo="RISK_REJECTED_PRE_JEV")
            self._publicar_motivo(simbolo, id_analise, "RISK_REJECTED_PRE_JEV",
                                  gate.reason or "veto de risco pré-JEv", candle_ref=candle_ref)
            self.estados_ativos[simbolo] = "REJECTED"
            return "pre_risk_reject"
        self._evento_funil("pre_risk_pass", simbolo, candle_ref=candle_ref, side=side,
                           motivo="RISCO_OK", volume=gate.volume)
        run_id = getattr(self, "run_id_atual", id_analise)
        request_id = f"{run_id}:{simbolo}:{side}:{candle_ref}"
        payload = self._montar_proposta(simbolo, proposta, estado, gate, analise,
                                        candle_ref, run_id, request_id)
        estado.mathematics = dict(estado.mathematics or {})
        estado.mathematics["proposta_entrada"] = payload
        registro_base = {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                         "padrao": proposta["candidate_id"], "candle_ref": candle_ref,
                         "direcao": side, "side_proposed": side,
                         "oportunidade": "entrada proposta pelo Python, pré-aprovada pelo risco"}
        contexto = {"id": id_analise, "simbolo": simbolo, "estado": estado,
                    "padrao": proposta["candidate_id"],
                    "identificador": f"{side}@{candle_ref}", "candle_ref": candle_ref,
                    "request_id": request_id, "run_id": run_id,
                    "contadores": self.contadores_ciclo,
                    "python_validated_at": _agora_iso(), "jev_dispatched_at": _agora_iso(),
                    "latencia_detecao_ms": 0, "registro_base": registro_base,
                    "proposta": payload}
        meta = {"run_id": run_id, "symbol": simbolo,
                "candidate_id": proposta["candidate_id"], "candle_ref": candle_ref,
                "side": side, "proposta": payload}
        self.pendentes_jev[request_id] = contexto
        self.analisados_jev[simbolo] = candle_ref
        if not self._enfileirar_jev(request_id, estado, meta):
            self.pendentes_jev.pop(request_id, None)
            self.estados_ativos[simbolo] = "PENDENTE_ENVIO"
            self._evento_funil("queue_full", simbolo, candle_ref=candle_ref,
                               motivo="QUEUE_FULL")
            self._publicar_motivo(simbolo, id_analise, "FILA_BLOQUEADA",
                                  "Fila JEv cheia; análise pendente de envio",
                                  candle_ref=candle_ref)
            return "fila_cheia"
        self.estados_ativos[simbolo] = "WAITING_JEV"
        self.contadores_ciclo["enviados_jev"] = self.contadores_ciclo.get("enviados_jev", 0) + 1
        self.contadores_ciclo["aguardando_decisao"] = \
            self.contadores_ciclo.get("aguardando_decisao", 0) + 1
        self._evento_funil("jev_enqueued", simbolo, request_id=request_id,
                           candle_ref=candle_ref, side=side, motivo="ENCAMINHADO")
        self.logger.info("proposta encaminhada ao JEv", simbolo=simbolo, side=side,
                         run_id=run_id, request_id=request_id, candle_ref=candle_ref)
        return "enviado_jev"

    def _drenar_analises(self, id_ciclo):
        """Aplica análises concluídas no pool; retorna quantas foram aplicadas."""
        aplicadas = 0
        for simbolo in list(self.analises_pendentes):
            contexto = self.analises_pendentes[simbolo]
            futuro = contexto["futuro"]
            if not futuro.done():
                continue
            self.analises_pendentes.pop(simbolo)
            try:
                dados = futuro.result()
            except Exception as exc:  # erro de um símbolo não derruba o pool
                self.pool_matematico.registrar_erro()
                self.estados_ativos[simbolo] = "ERROR"
                self.processados_ciclo.add(simbolo)
                self.contadores_ciclo["erros"] = self.contadores_ciclo.get("erros", 0) + 1
                self._publicar_motivo(simbolo, id_ciclo, "PIPELINE_ERROR", str(exc)[:160])
                self.logger.erro("falha na análise matemática", simbolo=simbolo,
                                 detalhe=str(exc)[:160])
                continue
            aplicadas += 1
            run_id_salvo = getattr(self, "run_id_atual", None)
            self.run_id_atual = contexto.get("run_id", run_id_salvo)
            try:
                self._processar_acoes()
                if self.pausado or self.parado_risco or self._cancelado_ciclo():
                    continue
                retorno = self._aplicar_matematica(
                    simbolo, contexto["estado"], dados, contexto["janela"],
                    contexto["id_analise"], id_ciclo)
                codigo = MOTIVOS_CODIGO.get(retorno, retorno or "SEM_DECISAO")
                self._publicar_motivo(simbolo, id_ciclo, codigo,
                                      retorno or "sem candidato elegível",
                                      candle_ref=dados.get("candle_ref"))
            except Exception as exc:  # erro recuperável: registra e continua o ciclo
                codigo_erro = ("COST_UNKNOWN" if "COST_UNKNOWN" in str(exc)
                               else "SEM_ELEGIBILIDADE")
                self.estados_ativos[simbolo] = "ERROR"
                self._publicar_motivo(simbolo, id_ciclo, codigo_erro, str(exc)[:160])
                self.logger.erro("falha ao aplicar análise", simbolo=simbolo,
                                 detalhe=str(exc)[:160],
                                 traceback=traceback.format_exc()[-800:])
            finally:
                self.run_id_atual = run_id_salvo
                self.processados_ciclo.add(simbolo)
                self.descarregar()
        return aplicadas

    def _atualizar_cache(self, simbolo, janela, id_analise, execution_spec=None):
        """Estatísticas + revalidação + candidatos locais com a janela completa; guarda só a cauda.

        A janela completa (~43k candles) vive apenas durante este método — a
        memória por ativo fica limitada à cauda (CAUDA_CANDLES) e ao resumo.
        Toda a descoberta é determinística (Python local); nenhuma IA externa
        participa desta etapa.
        """
        self._estado_set("ANALISANDO", f"Ciclo {self.ciclo} · CALCULANDO {simbolo}",
                         registrar=False, fase="CALCULANDO")
        descoberta, _ = janela_descoberta(janela["candles"])
        recorte = {**janela, "candles": descoberta, "fim": descoberta[-1]["time"]}
        cotacao = self.fonte.cotacao(simbolo)
        if cotacao is None:
            raise RuntimeError("COST_UNKNOWN: cotação atual indisponível; validação de custos suspensa")
        bid, ask, _ = cotacao
        preco_atual = (bid + ask) / 2
        spread_abs = ask - bid
        if not (0 < preco_atual and math.isfinite(spread_abs) and spread_abs >= 0):
            raise RuntimeError("COST_UNKNOWN: bid/ask inválidos; validação de custos suspensa")
        stats = estatisticas(recorte, 0.0)
        custo_pct = round(spread_abs / preco_atual * 100, 4)
        stats["custo_spread_pct"] = custo_pct
        stats["cost_basis"] = "spread_only"
        stats["commission_pct"] = None
        stats["slippage_pct"] = None
        if execution_spec is None:
            api = getattr(self.fonte, "api", None)
            info = api.symbol_info(simbolo) if api is not None else None
            if info is not None:
                execution_spec = {"point": float(info.point),
                                  "trade_stops_level": int(info.trade_stops_level),
                                  "digits": int(info.digits)}
        stats["execution_spec"] = ({**execution_spec, "spread_abs": spread_abs}
                                   if isinstance(execution_spec, dict)
                                   and math.isfinite(float(execution_spec.get("point", 0)))
                                   and float(execution_spec.get("point", 0)) > 0
                                   and int(execution_spec.get("trade_stops_level", -1)) >= 0
                                   else None)
        dias_janela = round((janela["candles"][-1]["time"] -
                             janela["candles"][0]["time"]) / 86400, 2)
        stats["dias_janela_completa"] = dias_janela
        stats["candles_janela_completa"] = len(janela["candles"])
        # Descoberta DeepSeek desativada; Python local é a única fonte de candidatos.
        janela_info = {"inicio": janela["inicio"], "fim": janela["fim"],
                       "dias": dias_janela, "candles": janela["recebidos"]}
        # Revalida padrões ativos com a janela recém-baixada (cronologia preservada).
        self._estado_set("ANALISANDO", f"Ciclo {self.ciclo} · VALIDANDO {simbolo}",
                         registrar=False, fase="VALIDANDO")
        for registro in self.repositorio.padroes_ativos(simbolo):
            interna = registro["regra"]
            regra = {"tipo_regra": interna["tipo"], "parametros": interna["parametros"],
                     "direcao": interna["direcao"], "horizonte_minutos": interna["horizonte_minutos"],
                     "justificativa": registro.get("justificativa", ""),
                     "strategy_id": registro.get("strategy_id", interna["tipo"])}
            validacao = self._validar_candidato_m1(
                regra, janela["candles"], custo_pct, stats,
                familias_testadas=candidatos.MAX_LOCAL_HYPOTHESES)
            atualizado = self.repositorio.registrar(simbolo, regra, validacao,
                                                    registro.get("origem", "local"), janela_info)
            trades_padrao = [tr for tr in self.book.trades
                             if tr.get("padrao") == atualizado["id"]]
            atualizado["calibracao"] = padroes.calibracao_brier(trades_padrao)
            if validacao["status"] == "rejeitado":
                self.contadores_ciclo["rejeitado_validacao"] = \
                    self.contadores_ciclo.get("rejeitado_validacao", 0) + 1
            if validacao["status"] != registro.get("status"):
                self.publicar("calc", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                       "etapa": "reexame", "padrao": atualizado["id"],
                                       "mensagem": f"REEXAME LOCAL: {validacao['status']}: {validacao['motivo']}",
                                       "amostra": None, "padroes_testados": None,
                                       "resultado": self._texto_validacao(validacao), "erro": None})
                self.logger.info("status de padrão alterado no reexame", simbolo=simbolo,
                                 padrao=atualizado["id"], status=validacao["status"])
        cache = {"instante": self.relogio(), "stats": stats, "custo_pct": custo_pct,
                 "inicio_janela": janela["inicio"], "fim_janela": janela["fim"],
                 "tail": janela["candles"][-CAUDA_CANDLES:]}
        self.dados_ativo[simbolo] = cache
        # Caminho operacional independente de IA: candidatos
        # determinísticos das estatísticas locais são validados JÁ (mesmo
        # rigor: fora da amostra, não sobrepostos, custo) e ficam disponíveis
        # no mesmo ciclo, sem chamadas externas.
        self._gerar_candidatos_locais(simbolo, janela, stats, custo_pct, id_analise)
        self.logger.info("estatísticas locais calculadas", simbolo=simbolo,
                         candles=stats.get("candles"), dias=stats.get("dias"))
        # DeepSeek desativado; atualização termina após candidatos Python.
        # Cauda permanece no cache; a janela completa é liberada ao sair deste método.

    def _gerar_candidatos_locais(self, simbolo, janela, stats, custo_pct, id_analise):
        """Valida candidatos determinísticos das estatísticas locais (sem IA).

        Cada regra candidata passa pela validação independente padrão; só
        vira `validado` com amostra e taxa fora da amostra suficientes.
        """
        inicio_candidatos = time.monotonic()
        try:
            regras = candidatos.regras_candidatas(stats, janela["candles"])
        except (ValueError, TypeError, KeyError) as exc:
            self._evento_funil("candidate_rejected", simbolo,
                               candle_ref=(janela["candles"][-1].get("time")
                                           if janela.get("candles") else None),
                               motivo="CANDIDATE_GENERATION_ERROR",
                               detalhe=str(exc)[:120])
            self.logger.aviso("falha ao gerar candidatos locais", simbolo=simbolo,
                              detalhe=str(exc)[:120])
            return
        candle_ref = janela["candles"][-1].get("time") if janela.get("candles") else None
        duracao_geracao_ms = max(0, round((time.monotonic() - inicio_candidatos) * 1000))
        dias_janela = stats.get("dias_janela_completa", stats.get("dias"))
        if not regras:
            self._evento_funil("candidate_rejected", simbolo, candle_ref=candle_ref,
                               motivo="SEM_SETUP" if stats.get("disponivel") else "HISTORY_INSUFFICIENT",
                               disponivel=stats.get("disponivel"),
                               closed_m1_count=len(janela.get("candles", [])),
                               custo_spread_pct=custo_pct,
                               window_days=dias_janela)
        janela_info = {"inicio": janela["inicio"], "fim": janela["fim"],
                       "dias": dias_janela, "candles": janela["recebidos"]}
        for regra in regras:
            inicio_validacao = time.monotonic()
            validacao = self._validar_candidato_m1(
                regra, janela["candles"], custo_pct, stats,
                familias_testadas=max(1, len(regras)))
            duracao_validacao_ms = max(0, round((time.monotonic() - inicio_validacao) * 1000))
            registro = self.repositorio.registrar(simbolo, regra, validacao, "local",
                                                  janela_info)
            candidate_id = registro["id"]
            self._evento_funil("candidate_generated", simbolo,
                               candidate_id=candidate_id, candle_ref=candle_ref,
                               strategy_id=registro.get("strategy_id", regra["tipo_regra"]),
                               direcao=regra["direcao"], horizonte_minutos=regra["horizonte_minutos"],
                               duracao_ms=duracao_geracao_ms)
            self.contadores_ciclo["candidatos_locais"] = \
                self.contadores_ciclo.get("candidatos_locais", 0) + 1
            self.publicar("calc", {"id": id_analise, "simbolo": simbolo,
                                   "run_id": getattr(self, "run_id_atual", id_analise),
                                   "strategy_id": registro.get("strategy_id", regra["tipo_regra"]),
                                   "instante": _agora_iso(), "etapa": "validacao_local",
                                   "padrao": registro["id"], "candidate_id": registro["id"],
                                   "candle_ref": candle_ref,
                                   "status_consulta": "CANDIDATO LOCAL (Python)",
                                        "reason_code": validacao.get("reason_code"),
                                        "hipotese": self._texto_hipotese(regra),
                                        "validacao": f"{validacao['status']}: {validacao['motivo']}",
                                        "resultado": self._texto_validacao(validacao)})
            if validacao["status"] == "validado":
                self._evento_funil("quant_validated", simbolo,
                                   candidate_id=candidate_id, candle_ref=candle_ref,
                                   motivo="QUANT_VALIDATED",
                                   sample_size=validacao["total"]["eventos"],
                                   oos_n=validacao["oos"]["eventos"],
                                   oos_rate=validacao["oos"]["taxa"],
                                   expected_net_return=validacao["total"]["media_pct"],
                                   cost_spread_pct=custo_pct,
                                   commission_pct=None, slippage_pct=None,
                                   cost_basis="spread_only",
                                   window_days=dias_janela,
                                    training_days=stats.get("dias"),
                                    validation_validity_days=padroes.VALIDADE_DIAS,
                                    validation_remaining_days=padroes.VALIDADE_DIAS,
                                    familias_testadas=validacao.get("familias_testadas", 1),
                                    oos_wilson_lower_adjusted=validacao["estabilidade"].get(
                                        "wilson_inferior_oos_ajustado"),
                                    duracao_ms=duracao_validacao_ms)
                self.contadores_ciclo["locais_validados"] = \
                    self.contadores_ciclo.get("locais_validados", 0) + 1
                self.logger.info("candidato local validado", simbolo=simbolo,
                                 padrao=registro["id"], analysis_id=id_analise)
            if validacao["status"] == "rejeitado":
                reason_code = validacao.get("reason_code", "QUANT_REJECTED")
                evidencias = {"sample_size": validacao["total"]["eventos"],
                              "reason_codes": validacao.get("reason_codes", [reason_code]),
                              "in_sample_n": validacao["amostra"]["eventos"],
                              "in_sample_rate": validacao["amostra"]["taxa"],
                              "oos_n": validacao["oos"]["eventos"],
                              "oos_rate": validacao["oos"]["taxa"],
                              "expected_net_return": validacao["total"]["media_pct"],
                              "familias_testadas": validacao.get("familias_testadas", 1),
                              "oos_wilson_lower_adjusted": validacao["estabilidade"].get(
                                  "wilson_inferior_oos_ajustado"),
                              "oos_primeira_metade": validacao["estabilidade"][
                                  "oos_primeira_metade"],
                              "oos_segunda_metade": validacao["estabilidade"][
                                  "oos_segunda_metade"],
                              "cost_spread_pct": custo_pct,
                              "commission_pct": None, "slippage_pct": None,
                              "cost_basis": "spread_only",
                              "window_days": dias_janela,
                              "training_days": stats.get("dias")}
                self._evento_funil("candidate_rejected", simbolo,
                                   candidate_id=candidate_id, candle_ref=candle_ref,
                                   motivo=reason_code, duracao_ms=duracao_validacao_ms,
                                   **evidencias)
                self._evento_funil("quant_rejected", simbolo,
                                   candidate_id=candidate_id, candle_ref=candle_ref,
                                   motivo=reason_code, duracao_ms=duracao_validacao_ms,
                                   **evidencias)
                self.contadores_ciclo["rejeitado_validacao"] = \
                    self.contadores_ciclo.get("rejeitado_validacao", 0) + 1

    def _oportunidade(self, estado, id_analise):
        """Padrão validado com condição verdadeira agora → decisão JEv.

        Somente símbolos marcados para Operar chegam a esta etapa. NÃO e HOLD
        encerram apenas AQUELA candidata: o laço continua para a próxima
        padrão/oportunidade do mesmo símbolo. Posições abertas continuam
        supervisionadas em _monitorar, independentemente da seleção.
        """
        if self.book is None or not self.book.active or self.parado_risco or self.emergencia_ativa:
            return "inativo"
        simbolo = estado.symbol
        if not self.selecao.operar(simbolo):
            self.contadores_ciclo["somente_monitorar"] = \
                self.contadores_ciclo.get("somente_monitorar", 0) + 1
            return "somente_monitorar"
        # Elegibilidade vem do cálculo matemático do ciclo (memória) e, na
        # ausência dele, do repositório legado — nunca de nome/padrão fixo.
        validados = self.validados_memoria.get(simbolo)
        if validados is None:
            validados = self.repositorio.validados(simbolo)
        if not validados:
            self.contadores_ciclo["sem_padrao"] = \
                self.contadores_ciclo.get("sem_padrao", 0) + 1
            self.estados_ativos[simbolo] = "REJECTED"
            return "sem_edge_matematico"
        self.contadores_ciclo["validados_ativos"] = \
            self.contadores_ciclo.get("validados_ativos", 0) + len(validados)
        instante_candle = estado.candles[-1]["time"] if estado.candles else None
        run_id = getattr(self, "run_id_atual", id_analise)
        for padrao in validados:
            chave_validado = (run_id, simbolo, padrao["id"])
            if chave_validado in self.quant_validated_ids:
                continue
            self._evento_funil("quant_validated", simbolo,
                               candidate_id=padrao["id"], candle_ref=instante_candle,
                               motivo="QUANT_VALIDATED",
                               sample_size=(padrao.get("eventos") or {}).get("eventos"),
                               oos_n=(padrao.get("fora_amostra") or {}).get("eventos"),
                               oos_rate=(padrao.get("fora_amostra") or {}).get("taxa"),
                               expected_net_return=(padrao.get("eventos") or {}).get("media_pct"),
                               cost_spread_pct=padrao.get("custo_spread_pct"),
                               commission_pct=padrao.get("commission_pct"),
                               slippage_pct=padrao.get("slippage_pct"),
                               cost_basis=padrao.get("cost_basis", "spread_only"),
                               window_days=padrao.get("janela_dias"),
                               validation_remaining_days=max(0, round(
                                   padroes.VALIDADE_DIAS - (self.relogio() -
                                   padrao.get("validado_em", self.relogio())) / 86400, 2)))
        if len(self.avaliados) > MAX_AVALIADOS:
            self.avaliados.clear()
        condicao_presente = False
        agora = self.relogio()
        motivos = []
        for padrao in validados:
            identificador = f"{padrao['id']}@{instante_candle}"
            # HOLD vigente: verifica ANTES do dedup (o dedup marcaria o candle
            # como avaliado e o hold nunca seria consultado/expirado).
            if (simbolo, identificador) in self.holds:
                if agora < self.holds[(simbolo, identificador)]:
                    self.contadores_ciclo["hold_aguardando"] = \
                        self.contadores_ciclo.get("hold_aguardando", 0) + 1
                    continue
                self.holds.pop((simbolo, identificador), None)
                self.contadores_ciclo["hold_expirado"] = \
                    self.contadores_ciclo.get("hold_expirado", 0) + 1
                self.logger.info("HOLD expirado; oportunidade descartada", simbolo=simbolo,
                                 padrao=padrao["id"], analysis_id=id_analise)
                if instante_candle is not None:
                    self.avaliados[(simbolo, padrao["id"])] = instante_candle
                continue
            chave = (simbolo, padrao["id"])
            if self.avaliados.get(chave) == instante_candle:
                self.contadores_ciclo["candle_ja_avaliado"] = \
                    self.contadores_ciclo.get("candle_ja_avaliado", 0) + 1
                self._evento_funil("queue_deduped", simbolo,
                                   candidate_id=padrao["id"],
                                   request_id=f"{getattr(self, 'run_id_atual', id_analise)}:{simbolo}:{padrao['id']}:{instante_candle}",
                                   candle_ref=instante_candle,
                                   motivo="CANDIDATE_ALREADY_EVALUATED")
                continue
            candles_condicao = self._candles_para_condicao(padrao, estado)
            if candles_condicao is None:
                self.contadores_ciclo["stale_snapshot"] = \
                    self.contadores_ciclo.get("stale_snapshot", 0) + 1
                continue
            if instante_candle is not None:
                self.avaliados[chave] = instante_candle
            if not padroes.condicao_atual(padrao, candles_condicao):
                continue
            condicao_presente = True
            contabilizar_elegivel = self.contadores_ciclo
            contabilizar_elegivel["elegiveis"] = contabilizar_elegivel.get("elegiveis", 0) + 1
            self.ultimos["oportunidade"] = agora
            t_deteccao = self.relogio()  # latência detecção → despacho JEv
            interna = padrao["regra"]
            regra = {"tipo_regra": interna["tipo"], "parametros": interna["parametros"],
                     "direcao": interna["direcao"], "horizonte_minutos": interna["horizonte_minutos"]}
            texto = self._texto_hipotese(regra)
            taxa_oos = padrao["fora_amostra"].get("taxa")
            evidencia = f"{texto} | taxa OOS {taxa_oos:.0%}" if taxa_oos is not None else texto
            # Contrato versionado Python → JEv: validado antes de qualquer uso.
            contrato = padroes.contrato_oportunidade(
                padrao, candles_condicao, id_analise,
                {"equity": self.book.equity(), "moeda": self.book.currency,
                 "exposicao_fracao": self.book.metrics()["risk_fraction"],
                 "max_total_risk": self.book.limits.max_total_risk,
                 "max_drawdown": self.book.limits.max_drawdown,
                 "precos_validos": self.book.prices_valid},
                agora=self.relogio())
            contrato.update({"run_id": getattr(self, "run_id_atual", id_analise),
                             "candle_ref": instante_candle,
                             "python_validated_at": _agora_iso()})
            ok_contrato, motivo_contrato = padroes.validar_contrato(
                contrato, agora=self.relogio())
            if not ok_contrato:
                self.contadores_ciclo["contrato_invalido"] = \
                    self.contadores_ciclo.get("contrato_invalido", 0) + 1
                motivos.append("contrato_invalido")
                self._publicar_motivo(simbolo, id_analise, "CONTRACT_INVALID", motivo_contrato)
                self.logger.aviso("contrato de oportunidade inválido", simbolo=simbolo,
                                  padrao=padrao["id"], motivo=motivo_contrato)
                continue

            def contabilizar(nome):
                """Contador por estágio do ciclo (nunca inventa decisão)."""
                self.contadores_ciclo[nome] = self.contadores_ciclo.get(nome, 0) + 1

            # Pré-checagem determinística ANTES do JEv: custo/autorização.
            pre = risk.bloqueio_spread(estado, self.book.limits)
            if pre:
                contabilizar("pre_bloqueado")
                self._evento_funil("ineligible_for_jev", simbolo,
                                   candidate_id=padrao["id"], candle_ref=instante_candle,
                                   motivo="SPREAD_TOO_HIGH", spread=estado.ask - estado.bid,
                                   cost_spread_pct=padrao.get("custo_spread_pct"))
                self._publicar_motivo(simbolo, id_analise, "SPREAD_TOO_HIGH", pre,
                                      candle_ref=instante_candle, candidate_id=padrao["id"])
                return "pre_bloqueado"
            request_id = f"{contrato['run_id']}:{simbolo}:{padrao['id']}:{instante_candle}"
            self.estados_ativos[simbolo] = "ELIGIBLE_FOR_JEV"
            self._evento_funil("eligible_for_jev", simbolo,
                               candidate_id=padrao["id"], candle_ref=instante_candle,
                               request_id=request_id,
                               motivo="QUANT_VALIDATED",
                               sample_size=padrao.get("eventos", {}).get("eventos"),
                               oos_n=padrao.get("fora_amostra", {}).get("eventos"),
                               oos_rate=padrao.get("fora_amostra", {}).get("taxa"),
                               expected_net_return=padrao.get("eventos", {}).get("media_pct"),
                               cost_spread_pct=padrao.get("custo_spread_pct"),
                                   validation_remaining_days=max(0, round(
                                   padroes.VALIDADE_DIAS - (self.relogio() -
                                   padrao.get("validado_em", self.relogio())) / 86400, 2)))
            if self.ciclo_ativo and not self.jev_configurado:
                self._publicar_motivo(simbolo, id_analise, "IA_INDISPONIVEL",
                                      "JEv/OpenRouter não configurado; candidato não enfileirado",
                                      candle_ref=instante_candle, candidate_id=padrao["id"])
                continue
            if self.relogio() < self.proxima_tentativa_jev:
                self._publicar_motivo(simbolo, id_analise, "ERRO_API",
                                      "JEv em recuo após falha; sem chamada neste candle",
                                      candle_ref=instante_candle, candidate_id=padrao["id"])
                continue
            instante_despacho = _agora_iso()
            self.logger.info("oportunidade detectada; consultando JEv", simbolo=simbolo,
                             padrao=padrao["id"], analysis_id=id_analise,
                             run_id=contrato["run_id"], python_validated_at=contrato["python_validated_at"],
                             jev_dispatched_at=instante_despacho,
                             latencia_detecao_ms=max(0, round((self.relogio() - t_deteccao) * 1000)))
            contabilizar("aguardando_decisao")
            contabilizar("enviados_jev")
            self._estado_set("ANALISANDO", f"Ciclo {self.ciclo} · DECIDINDO JEV {simbolo}",
                             registrar=False, fase="DECIDINDO_JEV")
            estado.mathematics = dict(estado.mathematics or {})
            estado.mathematics["oportunidade"] = contrato
            estado.mathematics["padrao_validado"] = {
                "id": padrao["id"], "regra": texto, "direcao": regra["direcao"],
                "horizonte_minutos": regra["horizonte_minutos"],
                "eventos": padrao["eventos"], "fora_amostra": padrao["fora_amostra"],
                "aviso": "Estatística histórica reproduzida localmente; não é garantia de retorno"}
            if self.ciclo_ativo:
                if request_id in self.pendentes_jev:
                    self._evento_funil("queue_deduped", simbolo,
                                       candidate_id=padrao["id"], request_id=request_id,
                                       candle_ref=instante_candle, motivo="REQUEST_ALREADY_PENDING")
                    continue
                self._evento_funil("queue_enqueue_attempt", simbolo,
                                   candidate_id=padrao["id"], request_id=request_id,
                                   candle_ref=instante_candle)
                registro_base = {"id": id_analise, "simbolo": simbolo,
                                 "instante": _agora_iso(), "padrao": padrao["id"],
                                 "candle_ref": instante_candle,
                                 "direcao": regra["direcao"], "oportunidade": texto}
                meta = {"run_id": contrato["run_id"], "symbol": simbolo,
                        "candidate_id": padrao["id"], "candle_ref": instante_candle}
                enqueued_at = self.relogio()
                contexto = {"id": id_analise, "simbolo": simbolo,
                            "estado": estado, "padrao": padrao["id"],
                            "identificador": identificador, "candle_ref": instante_candle,
                            "request_id": request_id, "enqueued_at": enqueued_at,
                            "run_id": contrato["run_id"],
                            "contadores": self.contadores_ciclo,
                            "python_validated_at": contrato["python_validated_at"],
                            "jev_dispatched_at": instante_despacho,
                            "latencia_detecao_ms": max(0, round((self.relogio() - t_deteccao) * 1000)),
                            "registro_base": registro_base}
                self.pendentes_jev[request_id] = contexto
                if not self._enfileirar_jev(request_id, estado, meta):
                    self.pendentes_jev.pop(request_id, None)
                    self._evento_funil("queue_full", simbolo, candidate_id=padrao["id"],
                                       request_id=request_id, candle_ref=instante_candle,
                                       motivo="QUEUE_FULL")
                    self._publicar_motivo(simbolo, id_analise, "FILA_BLOQUEADA",
                                          "Fila JEv cheia; oportunidade não foi enviada",
                                          candle_ref=instante_candle, candidate_id=padrao["id"])
                    continue
                self._evento_funil("queue_enqueued", simbolo,
                                   candidate_id=padrao["id"], request_id=request_id,
                                   candle_ref=instante_candle)
                self.estados_ativos[simbolo] = "WAITING_JEV"
                self.logger.info("oportunidade despachada ao worker JEv",
                                 simbolo=simbolo, padrao=padrao["id"],
                                 run_id=contrato["run_id"], request_id=request_id,
                                 latencia_detecao_ms=contexto["latencia_detecao_ms"])
                continue
            inicio_jev = self.relogio()  # modo síncrono de compatibilidade/teste
            self._evento_funil("jev_request_started", simbolo, candidate_id=padrao["id"],
                               candle_ref=instante_candle)
            try:
                decisao = jev.decide(estado)
            except RuntimeError as exc:
                contabilizar("ia_indisponivel")
                self.proxima_tentativa_jev = self.relogio() + TENTATIVA_JEV_S
                self._evento_funil("jev_http_error", simbolo, candidate_id=padrao["id"],
                                   candle_ref=instante_candle, motivo="JEV_CLIENT_ERROR",
                                   duracao_ms=max(0, round((self.relogio() - inicio_jev) * 1000)))
                self._evento_funil("ineligible_for_jev", simbolo,
                                   candidate_id=padrao["id"], candle_ref=instante_candle,
                                   motivo="IA_INDISPONIVEL", error_code="JEV_CLIENT_ERROR")
                self._publicar_motivo(simbolo, id_analise, "ERRO_API",
                                      "JEv indisponível; falha técnica, sem decisão de mercado",
                                      candle_ref=instante_candle, candidate_id=padrao["id"])
                self.logger.aviso("JEv indisponível", simbolo=simbolo, detalhe=str(exc)[:160])
                continue
            self._evento_funil("jev_http_success", simbolo, candidate_id=padrao["id"],
                               candle_ref=instante_candle,
                               duracao_ms=max(0, round((self.relogio() - inicio_jev) * 1000)))
            self._evento_funil("jev_response_received", simbolo,
                               candidate_id=padrao["id"], candle_ref=instante_candle)
            self.ultimos["decisao"] = agora
            latencia_jev_ms = max(0, round((self.relogio() - inicio_jev) * 1000))
            self.logger.info("decisão JEv recebida", simbolo=simbolo, acao=decisao.action,
                             probabilidade=round(decisao.probability, 2),
                             confianca=round(decisao.confidence, 2), analysis_id=id_analise)
            atualizado = self.fonte.revalidar(estado)
            if (atualizado is None or not atualizado.candles
                    or atualizado.candles[-1].get("time") != instante_candle):
                self._evento_funil("response_discarded_stale", simbolo,
                                   candidate_id=padrao["id"], candle_ref=instante_candle,
                                   motivo="CLOSED_M1_CHANGED")
                self._publicar_motivo(simbolo, id_analise, "RESPONSE_DISCARDED_STALE",
                                      "candle M1 mudou durante a espera da resposta JEv",
                                      candle_ref=instante_candle, candidate_id=padrao["id"])
                continue
            self._evento_funil("response_accepted", simbolo, candidate_id=padrao["id"],
                               candle_ref=instante_candle, duracao_ms=latencia_jev_ms)
            estado = atualizado
            gate = risk.check(decisao, self.book, self.book.limits, estado)
            acao = decisao.action
            decision_stage = {"BUY": "decision_buy", "SELL": "decision_sell",
                              "HOLD": "decision_hold", "AVOID": "decision_no"}.get(acao)
            if decision_stage:
                self._evento_funil(decision_stage, simbolo, candidate_id=padrao["id"],
                                   candle_ref=instante_candle)
            # Resposta do JEv normalizada: SIM (BUY/SELL), NAO (AVOID) ou HOLD.
            if acao == "HOLD":
                self._evento_funil("paper_not_opened", simbolo, candidate_id=padrao["id"],
                                   candle_ref=instante_candle, motivo="JEv_HOLD")
                contabilizar("jev_hold")
                expira = agora + HOLD_TTL_S
                self.holds[(simbolo, identificador)] = expira
                self.publicar("decisao", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                          "padrao": padrao["id"], "candle_ref": instante_candle,
                                          "direcao": regra["direcao"], "decisao": "HOLD",
                                          "oportunidade": texto,
                                          "justificativa": f"JEv HOLD p={decisao.probability:.0%} "
                                                           f"c={decisao.confidence:.0%}; oportunidade adiada",
                                          "risco": gate.reason or "aguardando",
                                           "policy": "PENDING", "paper": "NOT_OPENED",
                                           "execucao": f"adiada por até {HOLD_TTL_S // 60}min, depois descartada",
                                           "run_id": contrato["run_id"],
                                           "python_validated_at": contrato["python_validated_at"],
                                           "jev_dispatched_at": instante_despacho,
                                           "latencia_detecao_ms": max(0, round((self.relogio() - t_deteccao) * 1000)),
                                           "latencia_ms": latencia_jev_ms})
                self.logger.info("JEv HOLD; oportunidade adiada", simbolo=simbolo,
                                 padrao=padrao["id"], expira_em=round(expira))
                motivos.append("hold")
                continue
            if acao == "AVOID":
                self._evento_funil("paper_not_opened", simbolo, candidate_id=padrao["id"],
                                   candle_ref=instante_candle, motivo="JEv_AVOID")
                contabilizar("jev_nao")
                self.publicar("decisao", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                          "padrao": padrao["id"], "candle_ref": instante_candle,
                                          "direcao": regra["direcao"], "decisao": "NAO",
                                          "oportunidade": texto,
                                          "justificativa": f"JEv AVOID p={decisao.probability:.0%} "
                                                           f"c={decisao.confidence:.0%}; candidata descartada",
                                          "risco": gate.reason or "-", "execucao": "nenhuma",
                                           "policy": "VETO", "paper": "NOT_OPENED",
                                           "run_id": contrato["run_id"],
                                           "python_validated_at": contrato["python_validated_at"],
                                           "jev_dispatched_at": instante_despacho,
                                           "latencia_detecao_ms": max(0, round((self.relogio() - t_deteccao) * 1000)),
                                           "latencia_ms": latencia_jev_ms})
                motivos.append("jev_nao")
                continue  # NÃO encerra apenas esta candidata; próximas seguem
            # SIM: BUY/SELL aprovado pelo JEv; o gate decide a execução paper.
            contabilizar("jev_sim")
            if gate.verdict == "EXECUTE":
                contabilizar("executadas")
                self._evento_funil("paper_opened", simbolo, candidate_id=padrao["id"],
                                   candle_ref=instante_candle)
                self.book.execute(decisao, estado, gate)
                self.book.update(self.fonte)
                self._persistir()
                self.ultimos["trade"] = agora
                self.publicar("decisao", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                          "padrao": padrao["id"], "candle_ref": instante_candle,
                                          "direcao": acao, "decisao": "SIM",
                                          "oportunidade": texto,
                                          "justificativa": f"JEv SIM p={decisao.probability:.0%} "
                                                           f"c={decisao.confidence:.0%}; padrão validado",
                                          "risco": f"autorizado; risco {gate.risk_amount:.2f} {self.book.currency}",
                                           "policy": "ALLOW", "paper": "OPENED",
                                           "execucao": f"paper aberto: {gate.volume:g} lote(s) a {gate.entry:.8g}; stop {gate.stop:.8g}; alvo {gate.target:.8g}",
                                           "run_id": contrato["run_id"],
                                           "python_validated_at": contrato["python_validated_at"],
                                           "jev_dispatched_at": instante_despacho,
                                           "latencia_detecao_ms": max(0, round((self.relogio() - t_deteccao) * 1000)),
                                           "latencia_ms": latencia_jev_ms})
                self.publicar("trades", list(reversed(self.book.trades[-500:])))
                self.logger.info("entrada paper aberta", simbolo=simbolo, volume=gate.volume,
                                 entrada=round(gate.entry, 8), analysis_id=id_analise,
                                 padrao=padrao["id"], candle_ref=instante_candle)
                return "executada"
            contabilizar("risco_bloqueado")
            self._evento_funil("risk_veto", simbolo, candidate_id=padrao["id"],
                               candle_ref=instante_candle, motivo=gate.reason or "RISK_VETO")
            self._evento_funil("paper_not_opened", simbolo, candidate_id=padrao["id"],
                               candle_ref=instante_candle, motivo=gate.reason or "RISK_VETO")
            self.publicar("decisao", {"id": id_analise, "simbolo": simbolo, "instante": _agora_iso(),
                                      "padrao": padrao["id"], "candle_ref": instante_candle,
                                      "direcao": acao, "decisao": "SIM",
                                      "oportunidade": texto,
                                      "justificativa": f"JEv SIM p={decisao.probability:.0%} "
                                                       f"c={decisao.confidence:.0%}; recusado pelo risco",
                                      "risco": f"não autorizado: {gate.reason}",
                                      "policy": "VETO", "paper": "NOT_OPENED",
                                      "execucao": "nenhuma",
                                      "run_id": contrato["run_id"],
                                      "python_validated_at": contrato["python_validated_at"],
                                      "jev_dispatched_at": instante_despacho,
                                      "latencia_detecao_ms": max(0, round((self.relogio() - t_deteccao) * 1000)),
                                      "latencia_ms": latencia_jev_ms})
            self.logger.info("entrada recusada pelo gate de risco", simbolo=simbolo,
                             motivo=gate.reason or "-")
            motivos.append("risco_bloqueado")
        # Motivo agregado do símbolo neste ciclo (prioridade: execução > hold
        # > NAO > risco > contrato > condição ausente).
        for prioridade in ("executada", "hold", "jev_nao", "risco_bloqueado",
                           "contrato_invalido"):
            if prioridade in motivos:
                return prioridade
        if condicao_presente:
            return "condicao_presente_sem_decisao"
        self.contadores_ciclo["validado_condicao_ausente"] = \
            self.contadores_ciclo.get("validado_condicao_ausente", 0) + 1
        return "condicao_ausente"

    def _candles_para_condicao(self, padrao, estado):
        """Escolhe a série para avaliar a condição no candle mais recente.

        Regras com minutos > 99 exigem a cauda da janela de hist?rico; a cauda
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
        params = ", ".join(f"{k}={v}" for k, v in hipotese["parametros"].items())
        return f"{hipotese['tipo_regra']}({params}) → {hipotese['direcao']} em {hipotese['horizonte_minutos']}min"

    @staticmethod
    def _texto_validacao(validacao):
        def _pct(valor):
            # `None` é dado ausente, não zero: exibe N/D em vez de fabricar número.
            return f"{valor:.0%}" if isinstance(valor, (int, float)) else "N/D"

        total, oos = validacao["total"], validacao["oos"]
        if total["eventos"] == 0:
            return f"sem ocorrências | custo {validacao.get('custo_pct', 0)}%"
        if oos["eventos"] == 0:
            return (f"eventos {total['eventos']}, taxa {_pct(total['taxa'])}, "
                    f"média {_fmt_num(total['media_pct'])}% | "
                    f"OOS sem eventos (amostra insuficiente) | "
                    f"custo/spread {validacao.get('custo_pct', 0)}% | "
                    f"motivo {validacao.get('reason_code', validacao.get('motivo', '-'))}")
        return (f"eventos {total['eventos']}, taxa {_pct(total['taxa'])}, "
                f"média {_fmt_num(total['media_pct'])}% | "
                f"OOS {oos['eventos']}, taxa {_pct(oos['taxa'])}, média {_fmt_num(oos['media_pct'])}% | "
                f"custo/spread {validacao.get('custo_pct', 0)}% | "
                f"motivo {validacao.get('reason_code', validacao.get('motivo', '-'))}")

    def resumo(self):
        """Instantâneo seguro para a UI, sem segredos."""
        monitorados, operaveis = self.selecao.contagens()
        return {"estado": self.estado, "detalhe": self.detalhe, "ciclo": self.ciclo,
                "jev_ok": self.jev_configurado, "deepseek_calls": 0,
                "parado_risco": self.parado_risco, "pausado": self.pausado,
                "emergencia": self.emergencia_ativa, "monitorados": monitorados,
                "operaveis": operaveis}
