from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetFile, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType


class RecordingSession(BaseSession):
    """Test transport for Telegram request/response paths used by intake and cards."""

    def __init__(self, chat_id: int) -> None:
        super().__init__()
        self.chat_id = chat_id
        self.messages: list[str] = []
        self.requests: list[SendMessage | GetFile | AnswerCallbackQuery] = []
        self.before_send_check: Callable[[], Awaitable[bool]] | None = None
        self.poster_bytes = b"poster-bytes"

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        original_method = method
        result: dict[str, object] | bool
        if isinstance(method, SendMessage):
            if self.before_send_check is not None:
                assert await self.before_send_check(), "Проверка перед отправкой не пройдена."
            self.messages.append(method.text)
            result = {
                "message_id": 100 + len(self.messages),
                "date": 1793782800,
                "chat": {"id": self.chat_id, "type": "private"},
                "text": method.text,
            }
        elif isinstance(method, GetFile):
            result = {
                "file_id": method.file_id,
                "file_unique_id": "poster-unique-id",
                "file_path": "photos/original.jpg",
                "file_size": len(self.poster_bytes),
            }
        elif isinstance(method, AnswerCallbackQuery):
            result = True
        else:
            raise AssertionError(f"Unexpected Telegram method: {method.__class__.__name__}")

        self.requests.append(method)
        response = self.check_response(
            bot=bot,
            method=original_method,
            status_code=200,
            content=self.json_dumps({"ok": True, "result": result}),
        )
        assert response.result is not None
        return response.result

    async def stream_content(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        yield self.poster_bytes
