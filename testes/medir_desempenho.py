"""Medições sintéticas: publicações por ciclo, memória e tempo de cancelamento."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from apoio import FonteFake, LoggerSilencioso, candles_sinteticos, estado_mt5, janela
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
