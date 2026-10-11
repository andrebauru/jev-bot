"""Testes de logging: rotação, retenção e mascaramento de segredos."""

import logging
import logging.handlers
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
import unittest

from jev_bot import logs


class TestRotacaoLogs(unittest.TestCase):
    """Política: 10 MB por arquivo, 5 rotacionados, limpeza > 14 dias."""

    def setUp(self):
        """Logger isolado por teste, sem herdar handlers de outros testes."""
        logger = logging.getLogger("jev-bot")
        logger.handlers.clear()
        self._pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self._pasta.cleanup)
        self.pasta = Path(self._pasta.name)

    def tearDown(self):
        logger = logging.getLogger("jev-bot")
        for manipulador in logger.handlers[:]:
            try:
                manipulador.close()
            except Exception:
                pass
            logger.removeHandler(manipulador)

    def test_sem_handlers_duplicados(self):
        """Chamadas repetidas de configuração não duplicam manipuladores."""
        um = logs.configurar(self.pasta)
        dois = logs.configurar(self.pasta)
        manipuladores = [h for h in logging.getLogger("jev-bot").handlers
                         if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(manipuladores), 1)
        self.assertIs(um, dois)

    def test_rotacao_ao_atingir_o_limite(self):
        """Rotaciona quando o arquivo atinge o limite configurado."""
        with patch.object(logs, "LIMITE_MB", 2 / 1024):  # limite de 2 KB no teste
            logger = logs.configurar(self.pasta)
            texto = "x" * 200
            for i in range(60):
                logger.warning(f"linha {i} {texto}")
            arquivos = sorted(p.name for p in self.pasta.glob("jev-bot.log*"))
            self.assertIn("jev-bot.log.1", arquivos)
            ativo = self.pasta / "jev-bot.log"
            self.assertLessEqual(ativo.stat().st_size, 2 * 1024 + 400)

    def test_limpeza_mantem_ativo_e_remove_antigos(self):
        """Arquivos rotacionados com mais de 14 dias saem; o ativo fica."""
        agora = time.time()
        antigo = self.pasta / "jev-bot.log.3"
        antigo.write_text("antigo", encoding="utf-8")
        recente = self.pasta / "jev-bot.log.1"
        recente.write_text("recente", encoding="utf-8")
        ativo = self.pasta / "jev-bot.log"
        ativo.write_text("ativo", encoding="utf-8")
        relógio = [agora]
        logs.limpar_antigos(self.pasta, dias=14, relogio=lambda: relógio[0])
        self.assertTrue(antigo.exists() and recente.exists() and ativo.exists())
        # Envelhece os arquivos além da retenção; o ativo continua protegido.
        for arquivo in (antigo, recente, ativo):
            stamp = agora - (15 * 86400 if arquivo != ativo else 0)
            import os
            os.utime(arquivo, (stamp, stamp))
        removidos = logs.limpar_antigos(self.pasta, dias=14, relogio=lambda: relógio[0])
        self.assertIn(antigo.name, removidos)
        self.assertFalse(antigo.exists())
        self.assertFalse(recente.exists())
        self.assertTrue(ativo.exists())
        self.assertNotIn(ativo.name, removidos)

    def test_segredos_mascarados_no_arquivo(self):
        """Chave, token, bearer e senha nunca aparecem; *** no lugar."""
        logger = logs.configurar(self.pasta)
        logger.error("OPENROUTER_API_KEY=sk-or-1234567890abcdef e Authorization: Bearer abc.def")
        logger.error("MT5_PASSWORD=minhasenha e api_key=outra123")
        logger.error("erro de rede com token=xyz789")
        conteudo = (self.pasta / "jev-bot.log").read_text(encoding="utf-8")
        self.assertNotIn("sk-or-1234567890abcdef", conteudo)
        self.assertNotIn("Bearer abc.def", conteudo)
        self.assertNotIn("minhasenha", conteudo)
        self.assertNotIn("outra123", conteudo)
        self.assertNotIn("xyz789", conteudo)
        self.assertIn("***", conteudo)

    def test_evento_com_metadados_seguros(self):
        """Linha única com timestamp, nível, módulo e campos de correlação."""
        logger = logs.configurar(self.pasta)
        logs.info("ciclo concluído", ciclo=3, analysis_id="ciclo3-BTCUSD", simbolo="BTCUSD")
        conteudo = (self.pasta / "jev-bot.log").read_text(encoding="utf-8")
        self.assertIn("ciclo concluído", conteudo)
        self.assertIn("ciclo=3", conteudo)
        self.assertIn("analysis_id=ciclo3-BTCUSD", conteudo)
        self.assertIn("INFO", conteudo)
        self.assertIn("logs", conteudo)  # nome do módulo na linha


if __name__ == "__main__":
    unittest.main(verbosity=2)
