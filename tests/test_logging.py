import logging
import os
import unittest

from kai.constants import PATH_TEST_DATA
from kai.kai_config import KaiConfig
from kai.logging.logging import KaiLogger, get_logger, init_logging_from_config


class TestLogging(unittest.TestCase):

    def test_logger_has_default(self):
        log = get_logger("testing")
        self.assertIsNotNone(log)

        from kai.logging.logging import log as base_logger

        self.assertEqual(log.getEffectiveLevel(), logging.NOTSET)
        self.assertTrue(isinstance(log, KaiLogger))
        self.assertEqual(log.configLogLevel, logging.NOTSET)
        self.assertEqual(base_logger.filters, log.filters)
        self.assertEqual(base_logger.handlers, log.handlers)

    def test_logger_init_updates_logs(self):

        config = KaiConfig.model_validate_filepath(
            os.path.join(PATH_TEST_DATA, "data", "01_config.toml")
        )
        test_first_log = get_logger("child")

        init_logging_from_config(config)

        from kai.logging.logging import log as base_logger

        self.assertTrue(
            any(
                isinstance(handler, logging.FileHandler)
                for handler in base_logger.handlers
            )
        )
        self.assertTrue(
            any(
                isinstance(handler, logging.StreamHandler)
                for handler in base_logger.handlers
            )
        )

        test_second_log = get_logger("child2")

        self.assertTrue(isinstance(test_first_log, KaiLogger))
        self.assertEqual(test_first_log.level, logging.NOTSET)
        self.assertEqual(test_first_log.configLogLevel, base_logger.level)
        self.assertEqual(test_first_log.filters.__len__(), 0)
        self.assertEqual(test_first_log.handlers.__len__(), 0)
        self.assertTrue(isinstance(test_second_log, KaiLogger))
        self.assertEqual(test_second_log.level, logging.NOTSET)
        self.assertEqual(test_second_log.configLogLevel, base_logger.level)
        self.assertEqual(test_second_log.filters.__len__(), 0)
        self.assertEqual(test_second_log.handlers.__len__(), 0)
