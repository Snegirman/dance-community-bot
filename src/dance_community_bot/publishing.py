"""Устойчивая ручная отправка сохранённых недельных выпусков."""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from aiogram import Bot
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import BufferedInputFile

from .models import (
    PhotoPostPart,
    PublicationClaimResult,
    PublicationState,
    TextPostPart,
    WeeklyPost,
)
from .storage import Store
from .weekly import build_weekly_post

PublicationResultStatus = Literal[
    "prepared", "sending", "partial", "unknown", "delivered", "stale", "rejected"
]
SendOutcome = Literal["sent", "failed", "unknown"]
_EXPLICIT_REFUSALS = (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)


@dataclass(frozen=True)
class PublicationResult:
    publication_id: int | None
    status: PublicationResultStatus
    part_message_ids: tuple[int | None, ...]


@dataclass(frozen=True)
class PublicationConfirmation:
    publication_id: int
    preview_revision: int
    owner_chat_id: int
    owner_message_id: int


@dataclass(frozen=True)
class PreparedPhoto:
    file_id: str
    path: Path


def _result(
    publication: PublicationState | None, status: PublicationResultStatus
) -> PublicationResult:
    if publication is None:
        return PublicationResult(publication_id=None, status=status, part_message_ids=())
    return PublicationResult(
        publication_id=publication.id,
        status=status,
        part_message_ids=tuple(part.message_id for part in publication.parts),
    )


def _stored_result(publication: PublicationState) -> PublicationResult:
    return _result(publication, publication.status)


async def _send_text(bot: Bot, group_id: int, part: TextPostPart) -> tuple[SendOutcome, int | None]:
    try:
        message = await bot.send_message(
            group_id,
            html.escape(part.text),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except asyncio.CancelledError:
        raise
    except _EXPLICIT_REFUSALS:
        return "failed", None
    except Exception:
        return "unknown", None
    return "sent", message.message_id


def _is_explicit_file_id_rejection(error: TelegramBadRequest) -> bool:
    message = error.message.casefold()
    return any(
        fragment in message
        for fragment in (
            "wrong file identifier",
            "wrong remote file identifier",
            "invalid remote file identifier",
            "file_id is invalid",
            "file identifier is invalid",
            "can't use file_id",
            "cannot use file_id",
            "file reference expired",
        )
    )


async def _mark_part(
    store: Store,
    publication_id: int,
    part_index: int,
    active_token: str,
    status: Literal["sent", "failed", "unknown"],
    message_id: int | None = None,
) -> None:
    await store.finish_publication_part(
        publication_id,
        part_index,
        active_token,
        status,
        message_id,
    )


async def _send_photo(
    bot: Bot,
    group_id: int,
    part: PhotoPostPart,
    prepared: PreparedPhoto,
) -> tuple[SendOutcome, int | None]:
    try:
        message = await bot.send_photo(
            group_id,
            photo=prepared.file_id,
            caption=html.escape(part.caption),
            parse_mode=ParseMode.HTML,
        )
    except asyncio.CancelledError:
        raise
    except TelegramBadRequest as error:
        if not _is_explicit_file_id_rejection(error):
            return "failed", None
        try:
            poster_bytes = await asyncio.to_thread(prepared.path.read_bytes)
        except OSError:
            return "failed", None
        try:
            fallback = await bot.send_photo(
                group_id,
                photo=BufferedInputFile(poster_bytes, filename=prepared.path.name),
                caption=html.escape(part.caption),
                parse_mode=ParseMode.HTML,
            )
        except asyncio.CancelledError:
            raise
        except _EXPLICIT_REFUSALS:
            return "failed", None
        except Exception:
            return "unknown", None
        return "sent", fallback.message_id
    except _EXPLICIT_REFUSALS:
        return "failed", None
    except Exception:
        return "unknown", None
    return "sent", message.message_id


async def _send_part(
    bot: Bot,
    group_id: int,
    part: TextPostPart | PhotoPostPart,
    prepared_photo: PreparedPhoto | None = None,
) -> tuple[SendOutcome, int | None]:
    if isinstance(part, TextPostPart):
        return await _send_text(bot, group_id, part)
    if prepared_photo is None:
        return "failed", None
    return await _send_photo(bot, group_id, part, prepared_photo)


async def _prepare_photo(store: Store, part: PhotoPostPart) -> PreparedPhoto | None:
    source = await store.get_source_by_id(part.poster_source_id)
    if source is None or source.telegram_file_id is None:
        return None
    path = store.path.parent / part.poster_path
    return PreparedPhoto(
        file_id=source.telegram_file_id,
        path=path,
    )


async def _complete_part_safely(
    store: Store,
    publication_id: int,
    part_index: int,
    active_token: str,
    status: SendOutcome,
    message_id: int | None = None,
) -> None:
    completion = asyncio.create_task(
        _mark_part(store, publication_id, part_index, active_token, status, message_id)
    )
    try:
        await asyncio.shield(completion)
    except asyncio.CancelledError:
        await completion
        raise


async def _send_claimed_parts(
    post: WeeklyPost,
    group_id: int,
    store: Store,
    bot: Bot,
    publication_id: int,
    active_token: str,
    part_index: int,
    *,
    continue_after_success: bool = True,
) -> PublicationResult:
    current_index: int | None = part_index
    try:
        while current_index is not None:
            part = post.parts[current_index]
            network_started = False
            try:
                prepared_photo: PreparedPhoto | None = None
                if isinstance(part, PhotoPostPart):
                    prepared_photo = await _prepare_photo(store, part)
                    if prepared_photo is None:
                        await _mark_part(
                            store,
                            publication_id,
                            current_index,
                            active_token,
                            "failed",
                        )
                        break
                network_started = True
                outcome, message_id = await _send_part(bot, group_id, part, prepared_photo)
            except asyncio.CancelledError:
                await _complete_part_safely(
                    store,
                    publication_id,
                    current_index,
                    active_token,
                    "unknown" if network_started else "failed",
                )
                raise
            await _complete_part_safely(
                store,
                publication_id,
                current_index,
                active_token,
                outcome,
                message_id,
            )
            if outcome != "sent":
                break
            if not continue_after_success:
                break
            next_claim = await store.claim_next_publication_part(publication_id, active_token)
            if next_claim.outcome != "claimed" or next_claim.part is None:
                break
            current_index = next_claim.part.index
    finally:
        await store.release_publication_claim(publication_id, active_token)

    publication = await store.get_publication(publication_id)
    if publication is None:
        return PublicationResult(
            publication_id=publication_id, status="unknown", part_message_ids=()
        )
    return _stored_result(publication)


async def _valid_target(bot: Bot, group_id: int) -> bool:
    try:
        chat = await bot.get_chat(group_id)
    except Exception:
        return False
    return chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def _claim_safely(
    operation: Awaitable[PublicationClaimResult], store: Store, active_token: str
) -> PublicationClaimResult:
    # SQLite продолжает транзакцию в потоке даже при отмене ожидающего обработчика.
    pending = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        claim = await pending
        if claim.outcome == "claimed" and claim.publication is not None and claim.part is not None:
            try:
                await _mark_part(
                    store, claim.publication.id, claim.part.index, active_token, "failed"
                )
            finally:
                await store.release_publication_claim(claim.publication.id, active_token)
        raise


async def publish_week(
    post: WeeklyPost,
    group_id: int,
    store: Store,
    bot: Bot,
    *,
    automatic: bool = False,
    confirmation: PublicationConfirmation | None = None,
) -> PublicationResult:
    """Проверить целевую группу, атомарно захватить выпуск и отправить его части."""
    existing = await store.get_publication_for_week(group_id, post.week_start)
    if existing is not None and existing.status != "prepared":
        return _stored_result(existing)
    if not await _valid_target(bot, group_id):
        return _result(existing, "rejected")

    active_token = uuid4().hex
    current_post = post
    for rebuild in range(3):
        if not current_post.parts:
            return _result(existing, "rejected")
        claim = await _claim_safely(
            store.claim_publication(
                current_post,
                group_id,
                active_token,
                automatic=automatic,
                expected_publication_id=confirmation.publication_id if confirmation else None,
                expected_preview_revision=confirmation.preview_revision if confirmation else None,
                owner_chat_id=confirmation.owner_chat_id if confirmation else None,
                owner_message_id=confirmation.owner_message_id if confirmation else None,
            ),
            store,
            active_token,
        )
        if claim.outcome == "stale":
            if automatic and confirmation is None and rebuild < 2:
                current_post = await build_weekly_post(post.week_start, store)
                continue
            return _result(claim.publication, "stale")
        if claim.outcome != "claimed" or claim.publication is None or claim.part is None:
            if claim.outcome == "invalid":
                return _result(claim.publication, "rejected")
            return (
                _stored_result(claim.publication)
                if claim.publication
                else _result(None, "rejected")
            )
        return await _send_claimed_parts(
            claim.publication.snapshot,
            group_id,
            store,
            bot,
            claim.publication.id,
            active_token,
            claim.part.index,
        )
    return PublicationResult(publication_id=None, status="stale", part_message_ids=())


async def resolve_part(
    publication_id: int,
    part_index: int,
    delivered: bool,
    store: Store,
) -> None:
    """Зафиксировать решение владельца по неизвестной доставке части."""
    await store.resolve_publication_part(publication_id, part_index, delivered)


async def retry_publication_part(
    publication_id: int,
    part_index: int,
    group_id: int,
    store: Store,
    bot: Bot,
    *,
    expected_attempt_revision: int,
    expected_preview_revision: int,
    owner_chat_id: int,
    owner_message_id: int,
) -> PublicationResult:
    publication = await store.get_publication(publication_id)
    if publication is None:
        return _result(None, "rejected")
    if not await _valid_target(bot, group_id):
        return _result(publication, "rejected")
    active_token = uuid4().hex
    claim = await _claim_safely(
        store.retry_publication_part(
            publication_id,
            part_index,
            group_id,
            active_token,
            expected_attempt_revision,
            expected_preview_revision,
            owner_chat_id,
            owner_message_id,
        ),
        store,
        active_token,
    )
    if claim.outcome != "claimed" or claim.publication is None or claim.part is None:
        return _stored_result(claim.publication) if claim.publication else _result(None, "rejected")
    return await _send_claimed_parts(
        claim.publication.snapshot,
        group_id,
        store,
        bot,
        publication_id,
        active_token,
        claim.part.index,
        continue_after_success=False,
    )


async def continue_publication(
    publication_id: int,
    group_id: int,
    store: Store,
    bot: Bot,
    *,
    expected_preview_revision: int,
    owner_chat_id: int,
    owner_message_id: int,
) -> PublicationResult:
    publication = await store.get_publication(publication_id)
    if publication is None:
        return _result(None, "rejected")
    if not await _valid_target(bot, group_id):
        return _result(publication, "rejected")
    active_token = uuid4().hex
    claim = await _claim_safely(
        store.continue_publication(
            publication_id,
            group_id,
            active_token,
            expected_preview_revision,
            owner_chat_id,
            owner_message_id,
        ),
        store,
        active_token,
    )
    if claim.outcome != "claimed" or claim.publication is None or claim.part is None:
        return _stored_result(claim.publication) if claim.publication else _result(None, "rejected")
    return await _send_claimed_parts(
        claim.publication.snapshot,
        group_id,
        store,
        bot,
        publication_id,
        active_token,
        claim.part.index,
    )
