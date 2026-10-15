"""Cliente JEv binário: aprovação estrita SIM/NÃO, sem direção nem parâmetros."""

import json
from unittest.mock import Mock, patch
import unittest

from jev_bot import jev

PROPOSTA = {"symbol": "TESTE", "side_proposed": "BUY", "entry_reference": 100.0,
            "stop_loss": 99.5, "take_profit": 101.0, "volume": 1.0, "risk_amount": 5.0,
            "risk_pct": 0.01, "spread": 0.01, "candidate_id": "s", "request_id": "r",
            "run_id": "run", "candle_ref": 1, "expires_at": 121, "evidencias": {}}


def _resposta(payload):
    resposta = Mock()
    resposta.__enter__ = Mock(return_value=resposta)
    resposta.__exit__ = Mock(return_value=False)
    resposta.read.return_value = json.dumps(payload).encode()
    return resposta


def _ambiente():
    return patch.dict("os.environ", {"OPENROUTER_API_KEY": "mock"}, clear=True)


class TestClienteBinario(unittest.TestCase):
    """Respostas fora do contrato nunca viram aprovação implícita."""

    def _chamar(self):
        return jev.avaliar_entrada(PROPOSTA)

    def test_aprovar_e_reprovar(self):
        for aprovado in (True, False):
            with self.subTest(approved=aprovado), _ambiente(), \
                    patch("jev_bot.jev.carregar_ambiente"), \
                    patch("jev_bot.jev.urlopen",
                          return_value=_resposta({"answers": {"approval": {
                              "approved": aprovado, "reason": "ok"}}})):
                resultado = self._chamar()
            self.assertIs(resultado["approved"], aprovado)
            self.assertEqual(resultado["reason"], "ok")

    def test_formato_legado_buy_sell_hold_e_rejeitado(self):
        for escolha in ("BUY", "SELL", "HOLD", "AVOID"):
            with self.subTest(escolha=escolha), _ambiente(), \
                    patch("jev_bot.jev.carregar_ambiente"), \
                    patch("jev_bot.jev.urlopen",
                          return_value=_resposta({"answers": {"action": {
                              "type": "choice", "choice": escolha, "confidence": .9,
                              "probabilities": {"BUY": .5, "SELL": .3, "HOLD": .2}}}})):
                with self.assertRaises(jev.JevError) as err:
                    self._chamar()
                self.assertEqual(err.exception.stage, "parse_error")

    def test_esquema_estrito(self):
        invalidos = [{"answers": {"approval": {"approved": "yes", "reason": "x"}}},
                     {"answers": {"approval": {"approved": True}}},
                     {"answers": {"approval": {"approved": True, "reason": "  "}}},
                     {"answers": {}},
                     {"answers": {"approval": {"approved": True, "reason": "x",
                                               "side": "SELL"}}}]
        for payload in invalidos[:-1]:  # extra keys são ignoradas pelo parse estrito
            with self.subTest(payload=payload), _ambiente(), \
                    patch("jev_bot.jev.carregar_ambiente"), \
                    patch("jev_bot.jev.urlopen", return_value=_resposta(payload)):
                with self.assertRaises(jev.JevError):
                    self._chamar()

    def test_erros_http_timeout_parse(self):
        from urllib.error import HTTPError
        for status in (401, 429, 500):
            with self.subTest(http=status), _ambiente(), \
                    patch("jev_bot.jev.carregar_ambiente"), \
                    patch("jev_bot.jev.urlopen",
                          side_effect=HTTPError("", status, "erro", None, None)):
                with self.assertRaises(jev.JevError) as err:
                    self._chamar()
                self.assertEqual(err.exception.code, f"HTTP_{status}")
                self.assertEqual(err.exception.stage, "http_error")
        with _ambiente(), patch("jev_bot.jev.carregar_ambiente"), \
                patch("jev_bot.jev.urlopen", side_effect=TimeoutError()):
            with self.assertRaises(jev.JevError) as err:
                self._chamar()
            self.assertEqual(err.exception.stage, "timeout")
        with _ambiente(), patch("jev_bot.jev.carregar_ambiente"), \
                patch("jev_bot.jev.urlopen", return_value=_resposta({"x": 1})):
            with self.assertRaises(jev.JevError) as err:
                self._chamar()
            self.assertEqual(err.exception.stage, "parse_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
