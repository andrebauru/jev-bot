"""Ciclo MT5 → JEV → risco → paper, somente quando explicitamente iniciado."""

from . import jev, risk


def run(states, book, limits, engine="jev", execute=False, fonte=None, persistir=None, pausado=lambda: False, evento=None, cancelado=lambda: False):
    """Revalida o tick após JEV e salva cada entrada aprovada imediatamente."""
    records = []
    for state in states:
        if cancelado():
            break
        if fonte is not None and execute:
            book.update(fonte)
            if persistir:
                persistir()
        if evento:
            evento("jev", state.symbol, "Consultando JEV: decidir sim/não")
        try:
            decision = jev.decide(state, engine=engine, limites=limits)
        except RuntimeError as exc:
            gate = risk.Gate("SKIP", f"WAITING: {exc}")
            records.append((state, None, gate))
            if evento:
                evento("resultado", state.symbol, gate.reason)
            continue
        if fonte is not None:
            atualizado = fonte.revalidar(state)
            if atualizado is None:
                gate = risk.Gate("SKIP", "Mercado sem cotação recente após JEV")
                records.append((state, decision, gate))
                if evento:
                    evento("resultado", state.symbol, gate.reason)
                continue
            state = atualizado
            book.update(fonte)
        if pausado():
            book.active = False
        gate = risk.check(decision, book, limits, state)
        if execute and gate.verdict == "EXECUTE" and not cancelado():
            book.execute(decision, state, gate)
            if fonte is not None:
                book.update(fonte)
            if persistir:
                persistir()
        records.append((state, decision, gate))
        if evento:
            evento("resultado", state.symbol, f"Jev {decision.action} p={decision.probability:.0%} c={decision.confidence:.0%} | {'SIM: posição aberta paper' if execute and gate.verdict == 'EXECUTE' else 'NÃO: ' + (gate.reason or 'consulta sem execução')}")
    return records
