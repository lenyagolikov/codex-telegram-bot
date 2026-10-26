from __future__ import annotations

import ssl
import tempfile
import unittest
from http.client import RemoteDisconnected
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
from urllib.error import HTTPError

from codex_telegram_bot.telegram_api import (
    TelegramApi,
    TelegramError,
    TelegramPollingConflictError,
    _safe_network_error_detail,
    _telegram_ssl_context,
    markdown_to_telegram_html,
)


class MarkdownToTelegramHtmlTests(unittest.TestCase):
    def test_renders_common_codex_markdown(self) -> None:
        markdown = (
            "## Итог\n\n"
            "**Важно:** используй `request_id`.\n\n"
            "- первый пункт\n"
            "- [документация](https://example.test/docs?a=1&b=2)"
        )

        self.assertEqual(
            markdown_to_telegram_html(markdown),
            '<b>Итог</b>\n\n'
            '<b>Важно:</b> используй <code>request_id</code>.\n\n'
            '• первый пункт\n'
            '• <a href="https://example.test/docs?a=1&amp;b=2">документация</a>',
        )

    def test_escapes_html_and_preserves_fenced_code(self) -> None:
        markdown = "```cpp\nif (a < b && b > 0) {\n    return;\n}\n```"

        self.assertEqual(
            markdown_to_telegram_html(markdown),
            '<pre><code class="language-cpp">'
            'if (a &lt; b &amp;&amp; b &gt; 0) {\n    return;\n}'
            '</code></pre>',
        )

    def test_does_not_create_unsafe_link(self) -> None:
        self.assertEqual(
            markdown_to_telegram_html("[файл](codex://review?id=1)"),
            "файл (codex://review?id=1)",
        )


class TelegramApiTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        _telegram_ssl_context.cache_clear()

    def test_ssl_context_uses_packaged_certifi_bundle(self) -> None:
        context = Mock()
        with (
            unittest.mock.patch(
                "codex_telegram_bot.telegram_api.certifi.where",
                return_value="/bundle/cacert.pem",
            ),
            unittest.mock.patch(
                "codex_telegram_bot.telegram_api.ssl.create_default_context",
                return_value=context,
            ) as create_context,
        ):
            _telegram_ssl_context.cache_clear()
            result = _telegram_ssl_context()

        self.assertIs(result, context)
        create_context.assert_called_once_with(cafile="/bundle/cacert.pem")

    def test_tls_error_detail_does_not_include_sensitive_exception_text(self) -> None:
        error = ssl.SSLCertVerificationError(
            1,
            "certificate failed for https://api.telegram.org/bot-secret-token",
        )

        detail = _safe_network_error_detail(error)

        self.assertEqual(detail, "TLS certificate verification failed")
        self.assertNotIn("secret-token", detail)

    def test_remote_disconnect_is_reported_as_retryable_telegram_error(self) -> None:
        api = TelegramApi("not-a-real-token")
        api._open = Mock(  # type: ignore[method-assign]
            side_effect=RemoteDisconnected("peer closed connection")
        )

        with self.assertRaisesRegex(TelegramError, "network error"):
            api._call_sync("getUpdates", {}, 45)

    def test_get_updates_conflict_reports_duplicate_bot_instance(self) -> None:
        api = TelegramApi("not-a-real-token")
        api._open = Mock(  # type: ignore[method-assign]
            side_effect=HTTPError(
                "https://api.telegram.org",
                409,
                "Conflict",
                hdrs=None,
                fp=None,
            )
        )

        with self.assertRaisesRegex(
            TelegramPollingConflictError, "another bot instance"
        ):
            api._call_sync("getUpdates", {}, 45)

    async def test_formatted_message_uses_html_without_link_preview(self) -> None:
        api = TelegramApi("not-a-real-token")
        api.call = AsyncMock(return_value={"message_id": 42})  # type: ignore[method-assign]

        message_id = await api.send_message(
            101,
            "**Готово.** [Подробности](https://example.test)",
            formatted=True,
        )

        self.assertEqual(message_id, 42)
        api.call.assert_awaited_once_with(
            "sendMessage",
            {
                "chat_id": 101,
                "text": '<b>Готово.</b> <a href="https://example.test">Подробности</a>',
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )

    def test_file_upload_uses_multipart_document_request(self) -> None:
        api = TelegramApi("not-a-real-token")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true, "result": {"message_id": 43}}'
        api._open = Mock(return_value=response)  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pptx"
            path.write_bytes(b"presentation")
            message_id = api._send_file_sync(
                101,
                path,
                "Презентация",
                False,
            )

        self.assertEqual(message_id, 43)
        request = api._open.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/sendDocument"))
        self.assertIn(b'name="chat_id"', request.data)
        self.assertIn(b'name="document"; filename="report.pptx"', request.data)
        self.assertIn("Презентация".encode(), request.data)
        self.assertIn(b"presentation", request.data)

    def test_photo_upload_uses_photo_endpoint(self) -> None:
        api = TelegramApi("not-a-real-token")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true, "result": {"message_id": 44}}'
        api._open = Mock(return_value=response)  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.png"
            path.write_bytes(b"image")
            message_id = api._send_file_sync(101, path, "", True)

        self.assertEqual(message_id, 44)
        request = api._open.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/sendPhoto"))
        self.assertIn(b'name="photo"; filename="result.png"', request.data)


if __name__ == "__main__":
    unittest.main()
