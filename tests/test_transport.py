import ssl
import unittest
from unittest.mock import patch

from campus_bot.network import Telegram, telegram_opener


class TransportTests(unittest.TestCase):
    def test_direct_transport_is_default(self):
        self.assertIsNone(telegram_opener(""))

    def test_proxy_requires_socks_and_does_not_echo_credentials(self):
        with self.assertRaises(ValueError) as error:
            telegram_opener("https://secret:password@proxy.invalid")
        self.assertNotIn("password", str(error.exception))

    def test_socks_uses_remote_dns_and_does_not_replace_global_opener(self):
        import socks
        from urllib.request import HTTPSHandler
        with patch("urllib.request.build_opener") as build:
            telegram_opener("socks5h://localhost:9050")
        handler = build.call_args.args[1]
        self.assertEqual(handler.args, (socks.SOCKS5, "localhost", 9050))
        self.assertTrue(handler.kw["rdns"])
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(handler, HTTPSHandler)

    def test_proxy_tls_verifies_certificate_and_hostname(self):
        from sockshandler import SocksiPyConnectionS
        connection = SocksiPyConnectionS(2, "localhost", 9050, host="api.telegram.org")
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
