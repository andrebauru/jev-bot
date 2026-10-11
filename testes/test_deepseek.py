"""Testes do cliente DeepSeek: schema, orçamento e retry controlado (sem rede)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from jev_bot import deepseek

from urllib.error import HTTPError


class TestDeepseek(unittest.TestCase):
    """Schema, orçamento e retry controlado, sem rede real."""

    RESPOSTA_OK = {"choices": [{"message": {"content": json.dumps({"padroes": [
        {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 2}, "direcao": "BUY",
         "horizonte_minutos": 5, "justificativa": "sequências observadas"},
        {"tipo_regra": "rsi_nao_suportado", "parametros": {}, "direcao": "BUY", "horizonte_minutos": 5}
    ], "revisoes": []})}}], "usage": {"total_tokens": 42}}

    def config(self):
        return patch.dict("os.environ", {"OPENROUTER_API_KEY": "chave-de-teste"}, clear=True)

    def test_validar_resposta_filtra_invalidos(self):
        hipoteses, revisoes, descartadas = deepseek.validar_resposta(
            json.loads(self.RESPOSTA_OK["choices"][0]["message"]["content"]))
        self.assertEqual(len(hipoteses), 1)
        self.assertEqual(descartadas, 1)
        with self.assertRaises(ValueError):
            deepseek.validar_resposta({"outro": True})
        with self.assertRaises(ValueError):
            deepseek.validar_resposta({"padroes": [{}] * 9, "revisoes": []})

    def test_orcamento_bloqueia_por_hora_e_dia(self):
        with tempfile.TemporaryDirectory() as pasta:
            agora = [1_000_000.0]
            relogio = lambda: agora[0]  # noqa: E731
            orcamento = deepseek.Orcamento(Path(pasta), max_hora=100, tokens_dia=100, relogio=relogio)
            self.assertEqual(orcamento.permitir(), (True, ""))
            orcamento.registrar(60)
            self.assertTrue(orcamento.permitir()[0])
            orcamento.registrar(50)
            permitido, motivo = orcamento.permitir()
            self.assertFalse(permitido)
            self.assertIn("tokens", motivo)
            agora[0] += 86400
            self.assertTrue(orcamento.permitir()[0])  # dia novo zera tokens
            por_hora = deepseek.Orcamento(Path(pasta) / "b", max_hora=2, tokens_dia=1_000_000, relogio=relogio)
            por_hora.registrar(1)
            self.assertTrue(por_hora.permitir()[0])
            por_hora.registrar(1)
            permitido, motivo = por_hora.permitir()
            self.assertFalse(permitido)
            self.assertIn("hora", motivo)
            agora[0] += 3700
            self.assertTrue(por_hora.permitir()[0])  # janela de hora expira

    def test_analisar_sem_retry_em_401(self):
        from unittest.mock import Mock
        resposta = Mock()
        with self.config(), patch("jev_bot.deepseek.urlopen",
                                  side_effect=HTTPError("", 401, "não autorizado", None, None)) as chamada, \
                patch("jev_bot.deepseek._dormir") as espera:
            with self.assertRaises(RuntimeError) as contexto:
                deepseek.analisar({"simbolo": "X"}, [])
            self.assertIn("401", str(contexto.exception))
            self.assertEqual(chamada.call_count, 1)
            espera.assert_not_called()

    def test_analisar_retenta_5xx_e_sucesso(self):
        boas = Mock()
        boas.__enter__, boas.__exit__ = Mock(return_value=boas), Mock(return_value=False)
        boas.read.return_value = json.dumps(self.RESPOSTA_OK).encode()
        with self.config(), patch("jev_bot.deepseek.urlopen",
                                  side_effect=[HTTPError("", 503, "indisponível", None, None), boas]), \
                patch("jev_bot.deepseek._dormir") as espera:
            hipoteses, revisoes, uso, descartadas = deepseek.analisar({"simbolo": "X"}, [])
        self.assertEqual(len(hipoteses), 1)
        self.assertEqual(uso["tokens"], 42)
        espera.assert_called_once()

    def test_endpoint_ou_modelo_invalidos(self):
        with self.config(), patch.dict("os.environ", {"OPENROUTER_DEEPSEEK_MODEL": "sem barra"}), \
                patch("jev_bot.deepseek.urlopen") as chamada:
            with self.assertRaises(RuntimeError):
                deepseek.analisar({"simbolo": "X"}, [])
            chamada.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
