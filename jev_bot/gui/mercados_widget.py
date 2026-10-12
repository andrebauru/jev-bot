"""Painel de Mercados para Monitoramento e Operação (aba Configurações).

Árvore por categoria descoberta no catálogo real do MetaTrader 5 conectado
(ou cache identificado quando offline), com duas colunas de caixas de
seleção independentes — Monitorar e Operar. Regra de consistência:
Operar exige Monitorar, aplicada na interface e na normalização dos dados.
Filhos são criados sob demanda (expansão da categoria) para manter a
janela fluida com milhares de instrumentos.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import logs, mercados

COL_SIMBOLO, COL_MONITORAR, COL_OPERAR = 0, 1, 2
Categoria = dict  # {categoria: [símbolos]}


class PainelMercados(QWidget):
    """Seleção por categoria/símbolo com Monitorar e Operar independentes."""

    selecao_salva = Signal()

    def __init__(self, pedir_catalogo=None, caminho_selecao=None, parent=None):
        super().__init__(parent)
        self._pedir_catalogo = pedir_catalogo or (lambda: None)
        self._caminho_selecao = caminho_selecao  # None usa data/mercados.json
        self._catalogo = {}       # categoria -> [símbolos] descobertos
        self._origem = "sem catálogo"
        self._expandidos = set()

        colunas = QVBoxLayout(self)
        colunas.setContentsMargins(0, 4, 0, 4)
        titulo = QLabel("Mercados para Monitoramento e Operação (somente paper: "
                        "o tick Operar não habilita operações reais)")
        titulo.setStyleSheet("color:#8ea3c8;")
        titulo.setWordWrap(True)
        colunas.addWidget(titulo)

        linha_filtro = QHBoxLayout()
        self.filtro = QLineEdit()
        self.filtro.setPlaceholderText("Filtrar por símbolo ou categoria…")
        self.filtro.setClearButtonEnabled(True)
        self.filtro.textChanged.connect(self._filtrar_adiado)
        linha_filtro.addWidget(self.filtro, 1)
        self.btn_marcar = QPushButton("MARCAR VISÍVEIS")
        self.btn_marcar.setToolTip("Marca Monitorar (e Operar) para os símbolos visíveis da categoria selecionada")
        self.btn_marcar.clicked.connect(lambda: self._marcar_visiveis(True))
        self.btn_desmarcar = QPushButton("DESMARCAR VISÍVEIS")
        self.btn_desmarcar.clicked.connect(lambda: self._marcar_visiveis(False))
        linha_filtro.addWidget(self.btn_marcar)
        linha_filtro.addWidget(self.btn_desmarcar)
        self.btn_catalogo = QPushButton("RECARREGAR CATÁLOGO")
        self.btn_catalogo.clicked.connect(self._pedir_catalogo)
        linha_filtro.addWidget(self.btn_catalogo)
        colunas.addLayout(linha_filtro)

        self.arvore = QTreeWidget()
        self.arvore.setColumnCount(3)
        self.arvore.setHeaderLabels(["Categoria / Símbolo", "Monitorar", "Operar"])
        self.arvore.setUniformRowHeights(True)
        self.arvore.itemChanged.connect(self._item_alterado)
        self.arvore.itemExpanded.connect(self._expandir_categoria)
        colunas.addWidget(self.arvore, 1)

        rodape = QHBoxLayout()
        self.contagens = QLabel("sem seleção")
        self.contagens.setStyleSheet("color:#8ea3c8;")
        rodape.addWidget(self.contagens, 1)
        self.btn_salvar = QPushButton("SALVAR SELEÇÃO")
        self.btn_salvar.setObjectName("btnAplicar")
        self.btn_salvar.clicked.connect(self._salvar)
        rodape.addWidget(self.btn_salvar)
        colunas.addLayout(rodape)

        self.regra = QLabel("Operar exige Monitorar: desmarcar Monitorar desmarca Operar. "
                            "Somente símbolos do catálogo real; o histórico e a análise DeepSeek "
                            "ocorrem apenas para Monitorar, e somente Operar chega à decisão do JEv "
                            "(limites de risco prevalecem).")
        self.regra.setStyleSheet("color:#8ea3c8;")
        self.regra.setWordWrap(True)
        colunas.addWidget(self.regra)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        colunas.addWidget(self.status)

        self._aplicando = False      # evita reentrância ao ajustar caixas
        self._recalculo = QTimer(self)
        self._recalculo.setSingleShot(True)
        self._recalculo.setInterval(300)
        self._recalculo.timeout.connect(self._atualizar_contagens)
        self.carregar_selecao()

    # ------------------------------------------------------------- catálogo
    def preencher_catalogo(self, dados):
        """Popula categorias vindas do motor; estados preservam a seleção salva.

        Com milhares de símbolos, cria apenas os nós de categoria; os filhos
        surgem quando a categoria é expandida (carregamento sob demanda).
        """
        self._catalogo = dados.get("categorias") or {}
        self._origem = dados.get("origem", "sem catálogo")
        self.arvore.blockSignals(True)
        self.arvore.clear()
        self._expandidos.clear()
        for categoria in sorted(self._catalogo):
            no = QTreeWidgetItem([categoria, "", ""])
            no.setData(COL_SIMBOLO, Qt.UserRole, "categoria")
            no.setCheckState(COL_MONITORAR, Qt.Unchecked)
            no.setCheckState(COL_OPERAR, Qt.Unchecked)
            no.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            self.arvore.addTopLevelItem(no)
        self.arvore.blockSignals(False)
        self.status.setText(f"Catálogo {self._origem}: "
                            f"{len(self._catalogo)} categoria(s).")
        self._recalculo.start()

    def _expandir_categoria(self, item):
        """Cria os filhos da categoria na primeira expansão."""
        categoria = item.text(COL_SIMBOLO)
        if categoria in self._expandidos or categoria not in self._catalogo:
            return
        self._expandidos.add(categoria)
        selecao = mercados.carregar(self._caminho_selecao)
        self.arvore.blockSignals(True)
        # Tick marcado na categoria antes da expansão vale para os filhos
        # sem estado salvo explícito; a seleção salva por símbolo tem prioridade.
        padrao_monitorar = item.checkState(COL_MONITORAR) == Qt.Checked
        padrao_operar = item.checkState(COL_OPERAR) == Qt.Checked
        filhos = []
        for simbolo in self._catalogo.get(categoria, []):
            filho = QTreeWidgetItem([simbolo, "", ""])
            filho.setData(COL_SIMBOLO, Qt.UserRole, "simbolo")
            monitorar, operar = selecao.mapa.get(simbolo, (padrao_monitorar, padrao_operar))
            filho.setCheckState(COL_MONITORAR, Qt.Checked if monitorar else Qt.Unchecked)
            filho.setCheckState(COL_OPERAR, Qt.Checked if operar else Qt.Unchecked)
            filhos.append(filho)
        item.addChildren(filhos)
        self.arvore.blockSignals(False)
        self._filtrar_agora()

    # ------------------------------------------------------------- filtros
    def _filtrar_adiado(self):
        self._recalculo.start()

    def _filtrar_agora(self):
        """Filtra símbolos e categorias pelo texto; atualização incremental."""
        texto = self.filtro.text().strip().upper()
        raiz = self.arvore.invisibleRootItem()
        for i in range(raiz.childCount()):
            categoria = raiz.child(i)
            filhos_visiveis = 0
            for j in range(categoria.childCount()):
                filho = categoria.child(j)
                casar = not texto or texto in filho.text(COL_SIMBOLO).upper() \
                    or texto in categoria.text(COL_SIMBOLO).upper()
                filho.setHidden(not casar)
                filhos_visiveis += casar
            if categoria.childCount() == 0 and texto and texto in categoria.text(COL_SIMBOLO).upper():
                categoria.setHidden(False)
                categoria.setExpanded(True)
            elif categoria.childCount() == 0:
                categoria.setHidden(False)
            else:
                categoria.setHidden(filhos_visiveis == 0 and not (not texto))

    def _marcar_visiveis(self, marcar):
        """Marca/desmarca os símbolos visíveis da categoria selecionada.

        Sem seleção de categoria, aplica a todas as categorias.
        """
        raiz = self.arvore.invisibleRootItem()
        selecionadas = {item.text(COL_SIMBOLO) for item in self.arvore.selectedItems()}
        self.arvore.blockSignals(True)
        for i in range(raiz.childCount()):
            categoria = raiz.child(i)
            if selecionadas and categoria.text(COL_SIMBOLO) not in selecionadas:
                continue
            for j in range(categoria.childCount()):
                filho = categoria.child(j)
                if filho.isHidden():
                    continue
                filho.setCheckState(COL_MONITORAR, Qt.Checked if marcar else Qt.Unchecked)
                filho.setCheckState(COL_OPERAR, Qt.Checked if marcar else Qt.Unchecked)
            if marcar:
                categoria.setCheckState(COL_MONITORAR, Qt.Checked)
                categoria.setCheckState(COL_OPERAR, Qt.Checked)
            else:
                categoria.setCheckState(COL_MONITORAR, Qt.Unchecked)
                categoria.setCheckState(COL_OPERAR, Qt.Unchecked)
        self.arvore.blockSignals(False)
        self._atualizar_contagens()

    # ------------------------------------------------------------- consistência
    def _item_alterado(self, item, coluna):
        """Operar exige Monitorar; categoria altera todos os filhos visíveis."""
        if self._aplicando:
            return
        self._aplicando = True
        try:
            eh_categoria = item.data(COL_SIMBOLO, Qt.UserRole) == "categoria"
            if eh_categoria:
                marcar = item.checkState(coluna) == Qt.Checked
                for j in range(item.childCount()):
                    filho = item.child(j)
                    if coluna == COL_MONITORAR:
                        filho.setCheckState(COL_MONITORAR, Qt.Checked if marcar else Qt.Unchecked)
                        if not marcar:
                            filho.setCheckState(COL_OPERAR, Qt.Unchecked)
                    elif coluna == COL_OPERAR:
                        if marcar:
                            filho.setCheckState(COL_MONITORAR, Qt.Checked)
                            filho.setCheckState(COL_OPERAR, Qt.Checked)
                        else:
                            filho.setCheckState(COL_OPERAR, Qt.Unchecked)
            elif coluna == COL_OPERAR and item.checkState(COL_OPERAR) == Qt.Checked \
                    and item.checkState(COL_MONITORAR) != Qt.Checked:
                item.setCheckState(COL_MONITORAR, Qt.Checked)  # Operar ⇒ Monitorar
            elif coluna == COL_MONITORAR and item.checkState(COL_MONITORAR) != Qt.Checked:
                item.setCheckState(COL_OPERAR, Qt.Unchecked)
        finally:
            self._aplicando = False
        self._recalculo.start()

    def _atualizar_contagens(self):
        monitorados, operaveis = self._contagens_arvore()
        self.contagens.setText(f"{monitorados} mercado(s) monitorados, "
                               f"{operaveis} habilitado(s) para operar | catálogo: {self._origem}")

    def _contagens_arvore(self):
        monitorados = operaveis = 0
        raiz = self.arvore.invisibleRootItem()
        for i in range(raiz.childCount()):
            categoria = raiz.child(i)
            for j in range(categoria.childCount()):
                filho = categoria.child(j)
                if filho.checkState(COL_MONITORAR) == Qt.Checked:
                    monitorados += 1
                if filho.checkState(COL_OPERAR) == Qt.Checked:
                    operaveis += 1
        return monitorados, operaveis

    # ------------------------------------------------------------- persistência
    def _coletar(self):
        """Estado atual da árvore no formato de persistência.

        Categoria nunca expandida (sem filhos carregados): o tick da
        categoria se aplica a TODOS os símbolos do catálogo real dela —
        nada é inferido de ativos inexistentes.
        """
        selecao = {}
        raiz = self.arvore.invisibleRootItem()
        for i in range(raiz.childCount()):
            categoria = raiz.child(i)
            nome = categoria.text(COL_SIMBOLO)
            simbolos = {}
            if categoria.childCount() == 0:
                monitorar_cat = categoria.checkState(COL_MONITORAR) == Qt.Checked
                operar_cat = categoria.checkState(COL_OPERAR) == Qt.Checked and monitorar_cat
                if monitorar_cat:
                    simbolos = {simbolo: {"monitorar": True, "operar": operar_cat}
                                for simbolo in self._catalogo.get(nome, [])}
            else:
                for j in range(categoria.childCount()):
                    filho = categoria.child(j)
                    monitorar = filho.checkState(COL_MONITORAR) == Qt.Checked
                    operar = filho.checkState(COL_OPERAR) == Qt.Checked and monitorar
                    if monitorar or operar:
                        simbolos[filho.text(COL_SIMBOLO)] = {"monitorar": monitorar,
                                                             "operar": operar}
            if simbolos:
                selecao[nome] = simbolos
        return selecao

    def _salvar(self):
        try:
            dados = mercados.salvar({"selecao": self._coletar()}, self._caminho_selecao)
        except OSError as exc:
            self.status.setStyleSheet("color:#ef4444;")
            self.status.setText(f"Falha ao salvar seleção: {exc}")
            return
        monitorados = sum(1 for cat in dados["selecao"].values()
                          for s in cat.values() if s["monitorar"])
        operaveis = sum(1 for cat in dados["selecao"].values()
                        for s in cat.values() if s["operar"])
        self.status.setStyleSheet("color:#22c55e;")
        self.status.setText(f"Seleção salva em data/mercados.json: {monitorados} monitorados, "
                            f"{operaveis} para operar (aplicada no próximo ciclo).")
        self.selecao_salva.emit()
        logs.info("seleção de mercados salva na interface",
                  monitorados=monitorados, operaveis=operaveis)

    def carregar_selecao(self):
        """Aplica o estado salvo aos nós já criados (filhos criados sob demanda)."""
        selecao = mercados.carregar(self._caminho_selecao)
        monitorados, operaveis = selecao.contagens()
        self.contagens.setText(f"{monitorados} mercado(s) monitorados, "
                               f"{operaveis} habilitado(s) para operar (seleção salva)")
