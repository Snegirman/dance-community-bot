from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    GetChat,
    GetFile,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType


class RecordingSession(BaseSession):
    """Test transport for Telegram request/response paths used by intake and cards."""

    def __init__(self, chat_id: int) -> None:
        super().__init__()
        self.chat_id = chat_id
        self.messages: list[str] = []
        self.requests: list[AnswerCallbackQuery | GetChat | GetFile | SendMessage | SendPhoto] = []
        self.before_send_check: Callable[[], Awaitable[bool]] | None = None
        self.poster_bytes = b"poster-bytes"
        self.photos: list[str | None] = []

    @staticmethod
    def _chat_id(method: GetChat | SendMessage | SendPhoto, fallback: int) -> int:
        chat_id = method.chat_id
        return chat_id if isinstance(chat_id, int) else fallback

    @staticmethod
    def _chat_type(chat_id: int) -> str:
        return "supergroup" if chat_id < 0 else "private"

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
        if isinstance(method, GetChat):
            chat_id = self._chat_id(method, self.chat_id)
            result = {
                "id": chat_id,
                "type": self._chat_type(chat_id),
                "title": "Тестовая группа" if chat_id < 0 else None,
                "accent_color_id": 0,
                "max_reaction_count": 1,
                "accepted_gift_types": {
                    "unlimited_gifts": True,
                    "limited_gifts": True,
                    "unique_gifts": True,
                    "premium_subscription": True,
                    "gifts_from_channels": True,
                },
            }
        elif isinstance(method, SendMessage):
            if self.before_send_check is not None:
                assert await self.before_send_check(), "Проверка перед отправкой не пройдена."
            self.messages.append(method.text)
            chat_id = self._chat_id(method, self.chat_id)
            result = {
                "message_id": 100 + len(self.messages) + len(self.photos),
                "date": 1793782800,
                "chat": {"id": chat_id, "type": self._chat_type(chat_id)},
                "text": method.text,
            }
        elif isinstance(method, SendPhoto):
            self.photos.append(method.caption)
            chat_id = self._chat_id(method, self.chat_id)
            message_count = len(self.messages) + len(self.photos)
            result = {
                "message_id": 100 + message_count,
                "date": 1793782800,
                "chat": {"id": chat_id, "type": self._chat_type(chat_id)},
                "photo": [
                    {
                        "file_id": "sent-photo-file-id",
                        "file_unique_id": "sent-photo-unique-id",
                        "width": 640,
                        "height": 640,
                        "file_size": 1,
                    }
                ],
                "caption": method.caption,
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
