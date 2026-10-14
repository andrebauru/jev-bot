"""Cache incremental de candles M1 por símbolo, em disco, com escrita atômica.

Evita refetch de 43.200 candles a cada ciclo: o orquestrador anexa apenas
os candles novos desde o último armazenado, verifica continuidade e
lacunas e recorta a janela para os últimos 30 dias. A janela de análise
permanece estritamente em M1 e nos últimos 30 dias. Sem credenciais no
arquivo; corrupção invalida o cache (nunca gera candles fictícios).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

SCHEMA = 1
LACUNA_MAXIMA_MINUTOS = 1440  # recarregar a janela completa se a lacuna exceder 1 dia
COLUNAS = ("time", "open", "high", "low", "close", "tick_volume")


def _empacotar(candle):
    """Candle dict → lista compacta para o arquivo."""
    return [float(candle["time"]), float(candle["open"]), float(candle["high"]),
            float(candle["low"]), float(candle["close"]), int(candle["tick_volume"])]


def _desempacotar(linha):
    """Lista compacta → candle dict validado; None se inválida."""
    try:
        if len(linha) != len(COLUNAS):
            return None
        tempo, abre, alto, baixo, fecha, volume = linha
        tempo, abre, alto, baixo, fecha = (float(tempo), float(abre), float(alto),
                                           float(baixo), float(fecha))
        volume = int(volume)
    except (TypeError, ValueError):
        return None
    if any(v <= 0 or v != v for v in (tempo, abre, alto, baixo, fecha)):
        return None
    if volume < 0:
        return None
    if not baixo <= min(abre, fecha) <= max(abre, fecha) <= alto:
        return None
    return {"time": tempo, "open": abre, "high": alto, "low": baixo,
            "close": fecha, "tick_volume": volume}


class CacheM1:
    """Armazenamento limitado por símbolo (JSON atômico) com anexo incremental."""

    def __init__(self, pasta, max_dias=1, relogio=time.time):
        self.pasta = Path(pasta)
        self.max_dias = max_dias
        self.relogio = relogio
        self.pasta.mkdir(parents=True, exist_ok=True)

    def caminho(self, simbolo):
        return self.pasta / f"{str(simbolo).upper()}.m1.json"

    def carregar(self, simbolo):
        """Lê os candles armazenados; arquivo ausente/corrompido devolve []."""
        caminho = self.caminho(simbolo)
        if not caminho.exists():
            return []
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
            linhas = dados["candles"]
            if dados.get("schema_version") != SCHEMA or not isinstance(linhas, list):
                raise ValueError("schema")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
            self.invalidar(simbolo)
            return []
        candles = []
        for linha in linhas:
            candle = _desempacotar(linha)
            if candle is None:
                self.invalidar(simbolo)
                return []
            if candles and candle["time"] <= candles[-1]["time"]:
                continue  # dedup/ordem: ignora regressões do arquivo
            candles.append(candle)
        return candles

    def salvar(self, simbolo, candles):
        """Escrita atômica idêntica ao padrão do projeto (temp + fsync + replace)."""
        caminho = self.caminho(simbolo)
        conteudo = json.dumps({"schema_version": SCHEMA,
                               "simbolo": str(simbolo).upper(),
                               "atualizado_em": self.relogio(),
                               "candles": [_empacotar(c) for c in candles]},
                              allow_nan=False)
        temporario = None
        try:
            # Cache regenerável: atomicidade por temp+replace; sem fsync
            # (o refetch completo recompõe o cache em caso de queda).
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=caminho.parent,
                                             prefix=caminho.name + ".", suffix=".tmp",
                                             delete=False) as saida:
                temporario = Path(saida.name)
                saida.write(conteudo)
            for tentativa in range(5):
                try:
                    temporario.replace(caminho)
                    break
                except PermissionError:
                    if tentativa == 4:
                        raise
                    time.sleep(.05 * (tentativa + 1))
        finally:
            if temporario is not None:
                temporario.unlink(missing_ok=True)

    def invalidar(self, simbolo):
        """Descarta o cache do símbolo (integridade comprometida)."""
        try:
            self.caminho(simbolo).unlink(missing_ok=True)
        except OSError:
            pass

    def anexar(self, simbolo, novos):
        """Anexa candles novos ao cache com verificação de continuidade.

        Devolve (candles_janela, info) onde info indica se a janela precisa
        ser recarregada por completo (lacuna grande, sobreposição inválida
        ou cache inexistente). `novos` deve estar em ordem cronológica.
        """
        armazenados = self.carregar(simbolo)
        if not armazenados:
            return [], {"fonte": "cache", "anexados": 0, "lacunas": 0, "recarregar": True}
        if not novos:
            return armazenados, {"fonte": "cache", "anexados": 0, "lacunas": 0,
                                 "recarregar": False}
        ultimo = armazenados[-1]["time"]
        anexados = []
        for candle in novos:
            if candle["time"] <= ultimo:
                continue  # sobreposição: ignora, sem regressão
            if not anexados and (candle["time"] - ultimo) / 60 > LACUNA_MAXIMA_MINUTOS:
                return [], {"fonte": "cache", "anexados": 0,
                            "lacunas": int((candle["time"] - ultimo) / 60),
                            "recarregar": True}  # lacuna grande: janela completa
            anexados.append(candle)
            ultimo = candle["time"]
        combinados = armazenados + anexados
        # Recorte estrito para os últimos max_dias a partir do candle mais recente.
        fim = combinados[-1]["time"]
        limite = fim - self.max_dias * 86400
        combinados = [c for c in combinados if c["time"] >= limite]
        lacunas = sum(int((b["time"] - a["time"]) / 60 - 1)
                      for a, b in zip(combinados, combinados[1:])
                      if (b["time"] - a["time"]) / 60 - 1 > 0)
        self.salvar(simbolo, combinados)
        return combinados, {"fonte": "cache", "anexados": len(anexados),
                            "lacunas": lacunas, "recarregar": False}
