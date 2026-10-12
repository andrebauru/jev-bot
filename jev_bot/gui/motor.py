"""Thread Qt que possui o orquestrador e entrega pacotes coalescidos à UI.

MT5 exige chamadas em uma única thread: o orquestrador roda inteiro aqui e
a UI apenas emite comandos (fila) e recebe pacotes por sinais. As
publicações são coalescidas pelo orquestrador (descarregar) — a interface
recebe dezenas de mensagens por ciclo, não milhares, permanecendo fluida
durante o download do histórico.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from ..orquestrador import Orquestrador


class MotorTrabalhador(QThread):
    """Envolve Orquestrador.executar() entregando pacotes coalescidos."""

    sinal_pacote = Signal(dict)

    def __init__(self, terminal=None, offset_horas=None, banco=None,
                 caminho_selecao=None, parent=None):
        super().__init__(parent)
        self.orquestrador = Orquestrador(terminal=terminal, offset_horas=offset_horas,
                                         banco=banco, caminho_selecao=caminho_selecao)
        self.orquestrador.entrega = self._entregar

    def _entregar(self, pacote):
        """Entrega o pacote coalescido por conexão em fila (thread-safe)."""
        self.sinal_pacote.emit(pacote)

    def run(self):
        self.orquestrador.executar()
