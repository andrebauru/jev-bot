"""Medições sintéticas: publicações por ciclo, memória e tempo de cancelamento."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from unittest.mock import Mock

from apoio import FonteFake, LoggerSilencioso, candles_sinteticos, estado_mt5, janela
from jev_bot import mercados
from jev_bot import mercados
from jev_bot.execution.paper import Book
from jev_bot.orquestrador import Orquestrador, CAUDA_CANDLES

# 1) Publicações por ciclo com catálogo de 800 símbolos (antes: 1601).
import tempfile
with tempfile.TemporaryDirectory() as pasta:
    arquivo = Path(pasta) / "mercados.json"
    mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}}, arquivo)
    orch = Orquestrador(banco=Path(pasta) / "db.json", pasta_dados=Path(pasta) / "padroes",
                        caminho_selecao=arquivo)
    orch.logger = LoggerSilencioso()
    orch.fonte = FonteFake()
    orch.fonte.evento = orch._evento_fonte
    orch.book = Book()
    orch.publicar = lambda canal, registro: orch.publicacoes.append((canal, registro))
    orch.publicacoes = []
    N = 800
    for i in range(N):
        orch._evento_fonte("verificando", f"S{i}", "Verificando")
        orch._evento_fonte("dados", f"S{i}", "Dados recebidos")
    entregas = []
    orch.entrega = lambda pacote: entregas.append(pacote)
    orch.descarregar()
    registros_entregues = sum(len(v) for pacote in entregas for v in pacote.values()
                              if isinstance(v, list))
    print(f"eventos do scanner: {N * 2} → pacotes entregues à GUI: {len(entregas)} "
          f"({registros_entregues} registros)")

# 2) Memória: janela completa vs cauda por ativo.
janela_completa = candles_sinteticos(43200)
cauda = janela_completa[-CAUDA_CANDLES:]


def tamanho(objetos):
    total = 0
    for objeto in objetos:
        total += sys.getsizeof(objeto)
        for valor in objeto.values():
            total += sys.getsizeof(valor)
    return total


print(f"memória por ativo: janela completa ~{tamanho(janela_completa) / 1e6:.1f} MB → "
      f"cauda ({CAUDA_CANDLES}) ~{tamanho(cauda) / 1e3:.0f} KB")

# 2b) Cache incremental: hit evita refetch completo (economy de chamadas).
with tempfile.TemporaryDirectory() as pasta:
    arquivo = Path(pasta) / "mercados.json"
    mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}}, arquivo)
    orch = Orquestrador(banco=Path(pasta) / "db.json", pasta_dados=Path(pasta) / "padroes",
                        caminho_selecao=arquivo)
    orch.logger = LoggerSilencioso()
    orch.fonte = FonteFake(cotacoes={"TESTE": (99.99, 100.0, time.time())})
    from apoio import candles_deterministicos
    janela_det = janela(candles_deterministicos(43200))
    orch.fonte.janela_m1 = Mock(return_value=janela_det)
    orch.fonte.incremento_m1 = Mock(return_value=None)
    orch.repositorio = Mock()
    orch.repositorio.padroes_ativos.return_value = []
    orch.dados_ativo.clear()
    t0 = time.time()
    orch._janela_com_cache("TESTE", "id")
    miss = time.time() - t0
    novos = [{"time": candles_sinteticos(43200)[-1]["time"] + (i + 1) * 60,
              "open": 100, "high": 100.5, "low": 99.5, "close": 100.1,
              "tick_volume": 10} for i in range(30)]
    orch.fonte.incremento_m1 = Mock(return_value=novos)
    t0 = time.time()
    orch._janela_com_cache("TESTE", "id")
    hit = time.time() - t0
    print(f"cache: miss (janela completa paginada) {miss:.2f}s → hit (incremento 30 candles) {hit:.3f}s")

# 2c) Funil por ciclo: candidatos locais gerados e validados sem IA.
with tempfile.TemporaryDirectory() as pasta:
    arquivo = Path(pasta) / "mercados.json"
    mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}}, arquivo)
    orch = Orquestrador(banco=Path(pasta) / "db.json", pasta_dados=Path(pasta) / "padroes",
                        caminho_selecao=arquivo)
    orch.logger = LoggerSilencioso()
    from jev_bot import padroes
    orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes")
    orch.fonte = FonteFake(cotacoes={"TESTE": (99.99, 100.0, time.time())})
    from apoio import candles_deterministicos
    janela_det = janela(candles_deterministicos(43200))
    orch.fonte.janela_m1 = Mock(return_value=janela_det)
    orch.fonte.incremento_m1 = Mock(return_value=None)
    orch.dados_ativo.clear()
    orch.contadores_ciclo = {}
    orch._atualizar_cache("TESTE", orch._janela_com_cache("TESTE", "id"), "id")
    stats = orch.dados_ativo["TESTE"]["stats"]
    from jev_bot import candidatos
    regras = candidatos.regras_candidatas(stats, orch.dados_ativo["TESTE"]["tail"])
    print(f"candidatos locais determinísticos no padrão sintético: {len(regras)} "
          f"({[r['tipo_regra'] + '/' + r['direcao'] for r in regras]})")

# 3) Tempo para cancelar um download paginado em andamento.
with tempfile.TemporaryDirectory() as pasta:
    arquivo = Path(pasta) / "mercados.json"
    mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}}, arquivo)
    orch = Orquestrador(banco=Path(pasta) / "db.json", pasta_dados=Path(pasta) / "padroes",
                        caminho_selecao=arquivo)
    orch.logger = LoggerSilencioso()
    orch.fonte = FonteFake()

    def fonte_lento(simbolo, dias=30, lote=4320, cancelado=None, progresso=None):
        for _ in range(10):
            if cancelado():
                return None
            time.sleep(0.1)
            if progresso:
                progresso(4320, 43200)
        return janela(candles_sinteticos(1000))

    orch.fonte.janela_m1 = fonte_lento
    resultado = {}
    concluido = __import__("threading").Event()

    def baixar():
        resultado["janela"] = orch._coletar_janela("TESTE", "id")
        concluido.set()

    thread = __import__("threading").Thread(target=baixar, daemon=True)
    thread.start()
    time.sleep(0.3)  # download em andamento
    inicio = time.time()
    orch.cancelar_ciclo()
    thread.join(3)
    latencia = time.time() - inicio
    print(f"cancelamento: concluído={concluido.is_set()} em {latencia:.2f}s "
          f"(resposta entre lotes de 0,1s)")
