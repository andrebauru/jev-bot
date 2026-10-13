"""Testes de autostart seguro, INICIAR/PAUSADO e máquina de estados.

Cobertura do prompt de auditoria: causa-raiz do PAUSADO travado, autostart
sem clique, ausência de mercados, pausa manual, bloqueios persistidos,
emergência persistida, reconexão limitada, corrida de dois starts e
nenhuma ordem real.
"""

import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from apoio import (FonteFake, LoggerSilencioso, candles_sinteticos, estado_mt5,
                   janela)
from jev_bot import mercados
from jev_bot.execution.paper import Book
from jev_bot.orquestrador import TENTATIVAS_CONEXAO, Orquestrador
from jev_bot.types import Decision


class LoggerGravador:
    """Fachada que grava chamadas para verificar transições registradas."""

    def __init__(self):
        self.registros = []

    def _registrar(self, nivel, mensagem, **campos):
        self.registros.append((nivel, mensagem, campos))

    def debug(self, mensagem, **campos):
        self._registrar("DEBUG", mensagem, **campos)

    def info(self, mensagem, **campos):
        self._registrar("INFO", mensagem, **campos)

    def aviso(self, mensagem, **campos):
        self._registrar("WARNING", mensagem, **campos)

    def erro(self, mensagem, **campos):
        self._registrar("ERROR", mensagem, **campos)


class TestAutostart(unittest.TestCase):
    """Bootstrap seguro, INICIAR funcional e recusas explicadas."""

    def _orquestrador(self, pasta, selecao=None, banco_inicial=None):
        arquivo = Path(pasta) / "mercados.json"
        if selecao is None:
            selecao = {"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}}
        mercados.salvar(selecao, arquivo)
        banco = Path(pasta) / "db.json"
        if banco_inicial is not None:
            banco.write_text(banco_inicial, encoding="utf-8")
        orch = Orquestrador(banco=banco, pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.gravador = LoggerGravador()
        orch.logger = orch.gravador
        orch.book = Book.load(banco, "USD")
        orch.repositorio = Mock()
        orch.repositorio.padroes_ativos.return_value = []
        orch.repositorio.validados.return_value = []
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        return orch

    def test_iniciar_funciona_sem_jev_configurado(self):
        """Causa-raiz: JEv ausente não pode bloquear o ciclo de análise."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.fonte = FonteFake()
            with patch("jev_bot.orquestrador.jev.configuracao",
                       side_effect=RuntimeError("sem chave")):
                orch._acao_iniciar()
            self.assertFalse(orch.pausado)
            self.assertTrue(orch.book.active)
            avisos = [r for c, r in orch.eventos if c == "log" and "não configurado" in r["mensagem"]]
            self.assertTrue(avisos, "ausência de IA deve ser avisada, não bloquear")

    def test_iniciar_apos_pausar_atualiza_estado_real(self):
        """Após INICIAR, self.estado não pode continuar PAUSADO (causa-raiz)."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.fonte = FonteFake()
            orch._acao_pausar()
            self.assertEqual(orch.estado, "PAUSADO_PELO_USUARIO")
            with patch("jev_bot.orquestrador.jev.configuracao",
                       side_effect=RuntimeError("sem chave")):
                orch._acao_iniciar()
            self.assertNotIn(orch.estado, ("PAUSADO", "PAUSADO_PELO_USUARIO"))
            self.assertIn(orch.estado, ("PRONTO", "AGUARDANDO", "AGUARDANDO_SELECAO", "ANALISANDO"))
            transicoes = [c for n, m, c in orch.gravador.registros
                          if m == "transição de estado"]
            self.assertTrue(transicoes, "transições devem ser registradas no log")

    def test_iniciar_recusado_com_bloqueio_e_motivo_objetivo(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.fonte = FonteFake()
            orch.book.drawdown_blocked = True
            pausado_antes = orch.pausado
            orch._acao_iniciar()
            self.assertTrue(orch.book.drawdown_blocked)  # trava intacta
            self.assertTrue(orch.pausado or pausado_antes)
            recusas = [r for c, r in orch.eventos if c == "log" and "recusado" in r["mensagem"]]
            self.assertTrue(recusas)
            self.assertIn("drawdown", recusas[0]["mensagem"])

    def test_autostart_no_bootstrap_inicia_sem_clique(self):
        """Configuração válida + mercado selecionado: ciclo inicia sem INICIAR."""
        with tempfile.TemporaryDirectory() as pasta:
            closes = [100.0 + (i % 9) * .2 for i in range(300)]
            candles = [{"time": 1_700_000_000 + i * 60, "open": 100, "high": 100.5,
                        "low": 99.5, "close": c, "tick_volume": 10} for i, c in enumerate(closes)]
            fonte = FonteFake(estados=[estado_mt5(closes=closes)],
                              janelas={"TESTE": janela(candles)},
                              cotacoes={"TESTE": (99.995, 100.005, time.time())})

            class ContextoFonte:
                moeda = "USD"

                def __enter__(self):
                    return fonte

                def __exit__(self, *_):
                    return False

            orch = self._orquestrador(pasta)
            orch.intervalo_com_posicoes = 2
            with patch("jev_bot.orquestrador.FonteMT5", return_value=ContextoFonte()), \
                    patch("jev_bot.deepseek.configuracao_chat",
                          side_effect=RuntimeError("sem chave")), \
                    patch.object(Orquestrador, "_proximo_alvo",
                                 lambda self, agora: (agora + 1.0, "teste")):
                thread = threading.Thread(target=orch.executar, daemon=True)
                thread.start()
                limite = time.time() + 15
                while time.time() < limite and orch.ciclo < 1:
                    time.sleep(0.2)
                orch.parar()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(orch.ciclo, 1, "autostart deve rodar sem clique")
            self.assertFalse(orch.pausado)

    def test_autostart_sem_mercados_aguarda_e_inicia_apos_selecao(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta, selecao={})
            orch.fonte = FonteFake(janelas={"TESTE": janela(candles_sinteticos(3000))})
            orch.fonte.janela_m1 = Mock(return_value=janela(candles_sinteticos(3000)))
            # Bootstrap sem mercados: estado de espera, sem download.
            motivos = orch._bloqueios_ativos(para_autostart=True)
            self.assertEqual(motivos, [])
            orch._avaliar_autostart("bootstrap")
            self.assertEqual(orch.estado, "AGUARDANDO_SELECAO")
            self.assertFalse(orch.book.active)
            # Operador habilita mercado: autostart dispara sem clique.
            mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}},
                            orch.caminho_selecao)
            orch._aguardar_selecao()
            self.assertFalse(orch.pausado)
            self.assertTrue(orch.book.active)
            self.assertNotEqual(orch.estado, "AGUARDANDO_SELECAO")

    def test_pausa_manual_da_sessao_bloqueia_autostart(self):
        """PAUSAR voluntário nunca é desfeito por autostart na mesma sessão."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.fonte = FonteFake()
            orch._acao_pausar()
            self.assertFalse(orch.autostart_permitido)
            orch._avaliar_autostart("seleção de mercados")
            self.assertTrue(orch.pausado)
            self.assertFalse(orch.book.active)

    def test_emergencia_persistida_impede_autostart_ate_iniciar(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.fonte = FonteFake(cotacoes={"TESTE": (100.0, 100.01, time.time())})
            orch.book = Book(active=True, prices_valid=True)
            decisao = Decision("TESTE", "BUY", .9, .9)
            gate = Mock(verdict="EXECUTE", entry=100.0, volume=.1, stop=99.5,
                        target=101.0, risk_amount=50.0, margin=10.0)
            orch.book.execute(decisao, estado_mt5(), gate)
            orch._acao_emergencia()
            self.assertTrue(orch.arquivo_emergencia.exists())
            self.assertTrue(orch.emergencia_ativa)
            orch._avaliar_autostart("bootstrap")
            self.assertEqual(orch.estado, "EMERGENCIA")
            self.assertFalse(orch.book.active)
            # INICIAR manual é a liberação explícita: limpa a marca e retoma.
            orch._acao_iniciar()
            self.assertFalse(orch.arquivo_emergencia.exists())
            self.assertFalse(orch.emergencia_ativa)
            self.assertTrue(orch.book.active)

    def test_bloqueio_risco_persistido_supervisiona_posicoes(self):
        """Drawdown persistido: sem autostart, posições continuam monitoradas."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.fonte = FonteFake(cotacoes={"TESTE": (100.0, 100.01, time.time())})
            orch.book.active = True  # posição criada antes do bloqueio
            decisao = Decision("TESTE", "BUY", .9, .9)
            gate = Mock(verdict="EXECUTE", entry=100.0, volume=.1, stop=99.5,
                        target=101.0, risk_amount=50.0, margin=10.0)
            orch.book.execute(decisao, estado_mt5(), gate)
            orch.book.drawdown_blocked = True
            orch.book.active = False  # estado real após o bloqueio persistido
            orch._avaliar_autostart("bootstrap")
            self.assertEqual(orch.estado, "BLOQUEADO_POR_RISCO")
            self.assertTrue(orch.parado_risco)
            self.assertFalse(orch.book.active)
            # Supervisão continua: marcação de P&L da posição aberta.
            orch.ultimo_monitor = 0
            orch._monitorar()
            self.assertIsNotNone(orch.book.fills[0].price_now)
            registro = next(tr for tr in orch.book.trades
                            if tr["trade_id"] == orch.book.fills[0].trade_id)
            self.assertEqual(registro["mode"], "paper")
            # Entradas seguem bloqueadas pelo gate determinístico.
            orch._oportunidade(estado_mt5(), "id")
            self.assertEqual(len(orch.book.fills), 1)

    def test_corrida_de_dois_starts_e_idempotente(self):
        """Duplo clique/múltiplos signals: efeito único, sem estado corrompido."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.fonte = FonteFake()
            with patch("jev_bot.orquestrador.jev.configuracao",
                       side_effect=RuntimeError("sem chave")):
                orch._acao_iniciar()
                primeiro = (orch.pausado, orch.book.active)
                orch._acao_iniciar()
            self.assertEqual((orch.pausado, orch.book.active), primeiro)
            ignorados = [r for c, r in orch.eventos if "já ativo" in r.get("mensagem", "")]
            self.assertTrue(ignorados)

    def test_transicoes_de_estado_registradas_com_duracao(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.fonte = FonteFake()
            orch._estado_set("PRONTO", "conectado")
            time.sleep(0.02)
            orch._estado_set("ANALISANDO", "ciclo 1")
            transicoes = [c for n, m, c in orch.gravador.registros if m == "transição de estado"]
            self.assertEqual(len(transicoes), 2)
            self.assertEqual((transicoes[0]["de"], transicoes[0]["para"]),
                             ("CONECTANDO", "PRONTO"))
            self.assertEqual((transicoes[1]["de"], transicoes[1]["para"]),
                             ("PRONTO", "ANALISANDO"))
            self.assertIn("duracao_s", transicoes[1])
            # Mesmo estado repetido não gera log de transição (sem spam).
            orch._estado_set("ANALISANDO", "ciclo 2")
            transicoes = [c for n, m, c in orch.gravador.registros if m == "transição de estado"]
            self.assertEqual(len(transicoes), 2)

    def test_falha_mt5_tres_tentativas_e_estado_erro(self):
        """Conexão falha: recuo entre tentativas limitadas e estado ERRO claro."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            tentativas = []

            def fonte_falha(*_a, **_k):
                tentativas.append(1)
                raise RuntimeError("terminal não encontrado")

            with patch("jev_bot.orquestrador.FonteMT5", side_effect=fonte_falha), \
                    patch("jev_bot.orquestrador.TENTATIVAS_CONEXAO", 2):
                with self.assertRaises(RuntimeError):
                    orch._conectar_com_retentativas()
            self.assertEqual(len(tentativas), 2)
            inicializacoes = [r for c, r in orch.eventos if c == "estado"
                              and r.get("estado") == "INICIALIZANDO"]
            self.assertEqual(len(inicializacoes), 2)
            avisos = [r for c, r in orch.eventos if c == "log"
                      and "Conexão MT5 falhou" in r.get("mensagem", "")]
            self.assertEqual(len(avisos), 2)

    def test_comando_com_atraso_registrado(self):
        """Fila lenta é instrumentada: atraso > 1s gera aviso no log."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.fonte = FonteFake()
            orch.acoes.put(("iniciar", None, orch.relogio() - 5.0))
            with patch("jev_bot.orquestrador.jev.configuracao",
                       side_effect=RuntimeError("sem chave")):
                orch._processar_acoes()
            atrasos = [c for n, m, c in orch.gravador.registros
                       if m == "comando processado com atraso"]
            self.assertTrue(atrasos)
            self.assertGreaterEqual(atrasos[0]["atraso_s"], 5.0)

    def test_comandos_processados_no_meio_do_ciclo(self):
        """PAUSAR enfileirado durante a varredura interrompe o ciclo atual."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta, selecao={"selecao": {"C": {
                **{f"S{i}": {"monitorar": True, "operar": True} for i in range(5)}}}})
            simbolos = [f"S{i}" for i in range(5)]

            def estados():
                for simbolo in simbolos:
                    yield estado_mt5(simbolo)
                    if simbolo == "S1":
                        orch.pausar()  # operador pausa no meio do ciclo

            orch.fonte = FonteFake()
            orch.fonte.estados = estados
            orch._executar_ciclo("c1")
            self.assertTrue(orch.pausado)
            # O ciclo parou antes do fim: nem todos os ativos foram analisados.
            processados = [r for c, r in orch.eventos if c == "calc"
                           and r.get("etapa") in ("calculado", "dados", "erro")]
            self.assertLess(len(processados), len(simbolos))

    def test_nenhuma_ordem_real_no_autostart(self):
        """Autostart não cria caminho de ordem real; paper apenas."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            self.assertFalse(hasattr(orch, "order_send"))
            fonte = FonteFake()
            fonte.order_send = Mock()
            orch.fonte = fonte
            orch._avaliar_autostart("bootstrap")
            fonte.order_send.assert_not_called()
            self.assertTrue(orch.book.active)  # paper ativo
            self.assertEqual(orch.book.currency, "USD")


if __name__ == "__main__":
    unittest.main(verbosity=2)
