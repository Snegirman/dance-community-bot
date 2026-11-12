from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage, SendPhoto, TelegramMethod
from aiogram.methods.base import TelegramType
from telegram_transport import RecordingSession

from dance_community_bot.models import (
    EventData,
    PhotoPostPart,
    SourceInput,
    TextPostPart,
    WeeklyPost,
)
from dance_community_bot.publishing import (
    PublicationConfirmation,
    continue_publication,
    publish_week,
    retry_publication_part,
)
from dance_community_bot.storage import Store
from dance_community_bot.weekly import build_weekly_post

OWNER_ID = 111
GROUP_ID = -100123
TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
NOW = datetime(2026, 11, 9, 9, tzinfo=UTC)


class PausedFirstSendSession(RecordingSession):
    def __init__(self, chat_id: int) -> None:
        super().__init__(chat_id)
        self.first_send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        result = await super().make_request(bot, method, timeout)
        if isinstance(method, SendMessage) and not self.first_send_started.is_set():
            self.first_send_started.set()
            await self.release_first_send.wait()
        return result


async def add_event(store: Store) -> None:
    source = await store.save_source(
        SourceInput(
            chat_id=OWNER_ID,
            message_id=1,
            text="Джэм",
            received_at=NOW,
        )
    )
    draft = await store.save_draft(
        source.id,
        EventData(
            title="Джэм",
            event_type="Джэм",
            start_date=date(2026, 11, 10),
        ),
        poster_source_id=None,
    )
    await store.confirm_draft(draft.id, draft.version)


async def add_photo_event(store: Store) -> WeeklyPost:
    source = await store.save_source(
        SourceInput(
            chat_id=OWNER_ID,
            message_id=2,
            text="Афиша",
            received_at=NOW,
            telegram_file_id="telegram-file-id",
            is_photo=True,
        )
    )
    poster_path = Path("posters/2.jpg")
    (store.path.parent / poster_path).parent.mkdir(parents=True, exist_ok=True)
    (store.path.parent / poster_path).write_bytes(b"poster")
    await store.set_source_poster_path(source.id, poster_path)
    draft = await store.save_draft(
        source.id,
        EventData(
            title="Афишный джэм",
            event_type="Джэм",
            start_date=date(2026, 11, 10),
        ),
        poster_source_id=source.id,
    )
    await store.confirm_draft(draft.id, draft.version)
    return await build_weekly_post(date(2026, 11, 9), store)


async def add_extra_event(store: Store, *, message_id: int, title: str) -> None:
    source = await store.save_source(
        SourceInput(
            chat_id=OWNER_ID,
            message_id=message_id,
            text=title,
            received_at=NOW,
        )
    )
    draft = await store.save_draft(
        source.id,
        EventData(title=title, event_type="Джэм", start_date=date(2026, 11, 11)),
        poster_source_id=None,
    )
    await store.confirm_draft(draft.id, draft.version)


async def add_second_photo_event(store: Store) -> None:
    source = await store.save_source(
        SourceInput(
            chat_id=OWNER_ID,
            message_id=3,
            text="Вторая афиша",
            received_at=NOW,
            telegram_file_id="second-telegram-file-id",
            is_photo=True,
        )
    )
    poster_path = Path("posters/3.jpg")
    (store.path.parent / poster_path).write_bytes(b"second-poster")
    await store.set_source_poster_path(source.id, poster_path)
    draft = await store.save_draft(
        source.id,
        EventData(
            title="Второй афишный джэм",
            event_type="Джэм",
            start_date=date(2026, 11, 11),
        ),
        poster_source_id=source.id,
    )
    await store.confirm_draft(draft.id, draft.version)


def with_parts(post: WeeklyPost, parts: tuple[TextPostPart | PhotoPostPart, ...]) -> WeeklyPost:
    return WeeklyPost(
        week_start=post.week_start,
        catalog_revision=post.catalog_revision,
        parts=parts,
        records=post.records,
        occurrences=post.occurrences,
    )


class FailSecondSendSession(RecordingSession):
    def __init__(self) -> None:
        super().__init__(GROUP_ID)
        self.group_attempts = 0
        self.fail_second = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        if isinstance(method, SendMessage) and method.chat_id == GROUP_ID:
            self.group_attempts += 1
            if self.fail_second and self.group_attempts == 2:
                raise TelegramForbiddenError(method=method, message="private payload")
        return await super().make_request(bot, method, timeout)


class LosePhotoResponseSession(RecordingSession):
    def __init__(self) -> None:
        super().__init__(GROUP_ID)
        self.lose_next_response = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        result = await super().make_request(bot, method, timeout)
        if isinstance(method, SendPhoto) and self.lose_next_response:
            self.lose_next_response = False
            raise TimeoutError("response lost after possible delivery")
        return result


class RejectFileIdSession(RecordingSession):
    def __init__(self, rejection_message: str = "wrong file identifier") -> None:
        super().__init__(GROUP_ID)
        self.rejection_message = rejection_message
        self.photo_attempts: list[SendPhoto] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        if isinstance(method, SendPhoto):
            self.photo_attempts.append(method)
            if isinstance(method.photo, str):
                raise TelegramBadRequest(method=method, message=self.rejection_message)
        return await super().make_request(bot, method, timeout)


class FailSecondPhotoSession(RecordingSession):
    def __init__(self) -> None:
        super().__init__(GROUP_ID)
        self.photo_attempts: list[SendPhoto] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        if isinstance(method, SendPhoto):
            self.photo_attempts.append(method)
            if len(self.photo_attempts) == 2:
                raise TelegramForbiddenError(method=method, message="private payload")
        return await super().make_request(bot, method, timeout)


def test_parallel_publish_calls_from_separate_stores_send_each_part_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        other_store = Store(store.path)
        await store.initialize()
        await other_store.initialize()
        await add_event(store)
        post = await build_weekly_post(date(2026, 11, 9), store)

        session = PausedFirstSendSession(GROUP_ID)
        bot = Bot(token="987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno", session=session)
        try:
            first = asyncio.create_task(publish_week(post, GROUP_ID, store, bot))
            await asyncio.wait_for(session.first_send_started.wait(), timeout=2)

            second = await publish_week(post, GROUP_ID, other_store, bot)
            session.release_first_send.set()
            first_result = await first

            assert len(session.messages) == len(post.parts) == 1
            assert first_result.publication_id == second.publication_id
            assert first_result.part_message_ids == (101,)
        finally:
            session.release_first_send.set()
            await bot.session.close()

    asyncio.run(scenario())


def test_retry_after_partial_failure_sends_only_selected_part_and_rejects_stale_attempt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(store)
        original = await build_weekly_post(date(2026, 11, 9), store)
        post = with_parts(
            original,
            (TextPostPart(text="Первая часть"), TextPostPart(text="Вторая часть")),
        )
        prepared = await store.prepare_publication(post, GROUP_ID, OWNER_ID, 41)
        assert prepared.publication is not None
        confirmation = PublicationConfirmation(
            publication_id=prepared.publication.id,
            preview_revision=prepared.publication.preview_revision,
            owner_chat_id=OWNER_ID,
            owner_message_id=41,
        )
        session = FailSecondSendSession()
        bot = Bot(token=TOKEN, session=session)
        try:
            first = await publish_week(post, GROUP_ID, store, bot, confirmation=confirmation)
            assert first.status == "partial"
            assert session.messages == ["Первая часть"]
            publication = await store.get_publication(first.publication_id)
            assert publication is not None
            assert [part.status for part in publication.parts] == ["sent", "failed"]
            failed_attempt = publication.parts[1].attempt_revision

            session.fail_second = False
            retried = await retry_publication_part(
                publication.id,
                1,
                GROUP_ID,
                store,
                bot,
                expected_attempt_revision=failed_attempt,
                expected_preview_revision=publication.preview_revision,
                owner_chat_id=OWNER_ID,
                owner_message_id=41,
            )
            assert retried.status == "delivered"
            assert session.messages == ["Первая часть", "Вторая часть"]

            stale_retry = await retry_publication_part(
                publication.id,
                1,
                GROUP_ID,
                store,
                bot,
                expected_attempt_revision=failed_attempt,
                expected_preview_revision=publication.preview_revision,
                owner_chat_id=OWNER_ID,
                owner_message_id=41,
            )
            assert stale_retry.status == "delivered"
            assert session.messages == ["Первая часть", "Вторая часть"]
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_unknown_photo_requires_decision_and_retry_keeps_original_snapshot(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        post = await add_photo_event(store)
        photo_part = next(part for part in post.parts if isinstance(part, PhotoPostPart))
        post = with_parts(post, (photo_part, TextPostPart(text="Хвост сохранённого снимка")))
        prepared = await store.prepare_publication(post, GROUP_ID, OWNER_ID, 42)
        assert prepared.publication is not None
        confirmation = PublicationConfirmation(
            publication_id=prepared.publication.id,
            preview_revision=prepared.publication.preview_revision,
            owner_chat_id=OWNER_ID,
            owner_message_id=42,
        )
        session = LosePhotoResponseSession()
        bot = Bot(token=TOKEN, session=session)
        try:
            result = await publish_week(post, GROUP_ID, store, bot, confirmation=confirmation)
            assert result.status == "unknown"
            assert len(session.photos) == 1
            photo_request = next(
                request for request in session.requests if isinstance(request, SendPhoto)
            )
            assert photo_request.photo == "telegram-file-id"
            publication = await store.get_publication(result.publication_id)
            assert publication is not None
            assert [part.status for part in publication.parts] == ["unknown", "prepared"]
            attempt_revision = publication.parts[0].attempt_revision

            # Catalog changes after the first part do not alter the saved tail.
            await add_event(store)
            await store.resolve_publication_part(
                publication.id,
                0,
                False,
                expected_attempt_revision=attempt_revision,
                expected_preview_revision=publication.preview_revision,
                owner_chat_id=OWNER_ID,
                owner_message_id=42,
            )
            session.lose_next_response = False
            retried = await retry_publication_part(
                publication.id,
                0,
                GROUP_ID,
                store,
                bot,
                expected_attempt_revision=attempt_revision,
                expected_preview_revision=publication.preview_revision,
                owner_chat_id=OWNER_ID,
                owner_message_id=42,
            )
            assert retried.status == "partial"
            assert len(session.photos) == 2
            assert not any(
                isinstance(request, SendMessage) and request.chat_id == GROUP_ID
                for request in session.requests
            )

            await continue_publication(
                publication.id,
                GROUP_ID,
                store,
                bot,
                expected_preview_revision=publication.preview_revision,
                owner_chat_id=OWNER_ID,
                owner_message_id=42,
            )
            delivered_texts = [
                request.text
                for request in session.requests
                if isinstance(request, SendMessage) and request.chat_id == GROUP_ID
            ]
            assert delivered_texts == ["Хвост сохранённого снимка"]
            assert len(session.photos) == 2
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_marking_unknown_part_delivered_does_not_invent_message_id_or_resend(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        post = await add_photo_event(store)
        prepared = await store.prepare_publication(post, GROUP_ID, OWNER_ID, 43)
        assert prepared.publication is not None
        confirmation = PublicationConfirmation(
            publication_id=prepared.publication.id,
            preview_revision=prepared.publication.preview_revision,
            owner_chat_id=OWNER_ID,
            owner_message_id=43,
        )
        session = LosePhotoResponseSession()
        bot = Bot(token=TOKEN, session=session)
        try:
            result = await publish_week(post, GROUP_ID, store, bot, confirmation=confirmation)
            publication = await store.get_publication(result.publication_id)
            assert publication is not None
            part = next(part for part in publication.parts if part.status == "unknown")
            await store.resolve_publication_part(
                publication.id,
                part.index,
                True,
                expected_attempt_revision=part.attempt_revision,
                expected_preview_revision=publication.preview_revision,
                owner_chat_id=OWNER_ID,
                owner_message_id=43,
            )
            resolved = await store.get_publication(publication.id)
            assert resolved is not None
            assert resolved.status == "delivered"
            assert resolved.parts[part.index].message_id is None
            assert len(session.photos) == 1
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_startup_recovery_is_explicit_and_does_not_run_on_store_initialize(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(store)
        post = await build_weekly_post(date(2026, 11, 9), store)
        prepared = await store.prepare_publication(post, GROUP_ID, OWNER_ID, 44)
        assert prepared.publication is not None
        confirmation = prepared.publication
        claim = await store.claim_publication(
            post,
            GROUP_ID,
            "interrupted-at-send",
            expected_publication_id=confirmation.id,
            expected_preview_revision=confirmation.preview_revision,
            owner_chat_id=OWNER_ID,
            owner_message_id=44,
        )
        assert claim.outcome == "claimed"

        reopened = Store(store.path)
        await reopened.initialize()
        still_sending = await reopened.get_publication(confirmation.id)
        assert still_sending is not None
        assert still_sending.status == "sending"
        assert still_sending.parts[0].status == "sending"

        await reopened.recover_publications_on_startup()
        recovered = await reopened.get_publication(confirmation.id)
        assert recovered is not None
        assert recovered.status == "unknown"
        assert recovered.active_token is None
        assert recovered.parts[0].status == "unknown"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "rejection_message",
    [
        "wrong file identifier",
        "wrong remote file identifier specified: Wrong padding in the string",
        "Invalid remote file identifier",
    ],
)
@pytest.mark.parametrize("local_exists", [True, False])
def test_original_file_fallback_requires_explicit_file_id_rejection(
    tmp_path: Path, local_exists: bool, rejection_message: str
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        post = await add_photo_event(store)
        if not local_exists:
            (tmp_path / "posters/2.jpg").unlink()
        session = RejectFileIdSession(rejection_message)
        bot = Bot(token=TOKEN, session=session)
        try:
            result = await publish_week(post, GROUP_ID, store, bot)

            assert len(session.photo_attempts) == (2 if local_exists else 1)
            assert isinstance(session.photo_attempts[0].photo, str)
            if local_exists:
                assert result.status == "delivered"
                assert not isinstance(session.photo_attempts[1].photo, str)
            else:
                assert result.status == "partial"
        finally:
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("automatic", [False, True])
def test_catalog_change_after_preview_blocks_manual_but_rebuilds_automatic(
    tmp_path: Path, automatic: bool
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(store)
        post = await build_weekly_post(date(2026, 11, 9), store)
        confirmation: PublicationConfirmation | None = None
        if not automatic:
            prepared = await store.prepare_publication(post, GROUP_ID, OWNER_ID, 45)
            assert prepared.publication is not None
            confirmation = PublicationConfirmation(
                publication_id=prepared.publication.id,
                preview_revision=prepared.publication.preview_revision,
                owner_chat_id=OWNER_ID,
                owner_message_id=45,
            )
        await add_extra_event(store, message_id=4, title="Новое событие")
        session = RecordingSession(GROUP_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            result = await publish_week(
                post,
                GROUP_ID,
                store,
                bot,
                automatic=automatic,
                confirmation=confirmation,
            )

            group_text = [
                request.text
                for request in session.requests
                if isinstance(request, SendMessage) and request.chat_id == GROUP_ID
            ]
            if automatic:
                assert result.status == "delivered"
                assert len(group_text) == 1
                assert "Новое событие" in group_text[0]
            else:
                assert result.status == "stale"
                assert group_text == []
                publication = await store.get_publication_for_week(GROUP_ID, post.week_start)
                assert publication is not None
                assert publication.status == "prepared"
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_second_poster_failure_preserves_first_photo_and_retry_is_per_part(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_photo_event(store)
        await add_second_photo_event(store)
        post = await build_weekly_post(date(2026, 11, 9), store)
        photo_parts = [part for part in post.parts if isinstance(part, PhotoPostPart)]
        assert len(photo_parts) == 2
        prepared = await store.prepare_publication(post, GROUP_ID, OWNER_ID, 46)
        assert prepared.publication is not None
        confirmation = PublicationConfirmation(
            publication_id=prepared.publication.id,
            preview_revision=prepared.publication.preview_revision,
            owner_chat_id=OWNER_ID,
            owner_message_id=46,
        )
        session = FailSecondPhotoSession()
        bot = Bot(token=TOKEN, session=session)
        try:
            result = await publish_week(post, GROUP_ID, store, bot, confirmation=confirmation)
            assert result.status == "partial"
            publication = await store.get_publication(result.publication_id)
            assert publication is not None
            photo_states = [
                part for part in publication.parts if isinstance(part.part, PhotoPostPart)
            ]
            assert [part.status for part in photo_states] == ["sent", "failed"]
            assert [request.photo for request in session.photo_attempts] == [
                "telegram-file-id",
                "second-telegram-file-id",
            ]

            session.photo_attempts.clear()
            retried = await retry_publication_part(
                publication.id,
                photo_states[1].index,
                GROUP_ID,
                store,
                bot,
                expected_attempt_revision=photo_states[1].attempt_revision,
                expected_preview_revision=publication.preview_revision,
                owner_chat_id=OWNER_ID,
                owner_message_id=46,
            )
            assert retried.status == "delivered"
            assert [request.photo for request in session.photo_attempts] == [
                "second-telegram-file-id"
            ]
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_old_unknown_decision_cannot_resolve_second_unknown_attempt(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        post = await add_photo_event(store)
        prepared = await store.prepare_publication(post, GROUP_ID, OWNER_ID, 47)
        assert prepared.publication is not None
        confirmation = PublicationConfirmation(
            publication_id=prepared.publication.id,
            preview_revision=prepared.publication.preview_revision,
            owner_chat_id=OWNER_ID,
            owner_message_id=47,
        )
        session = LosePhotoResponseSession()
        bot = Bot(token=TOKEN, session=session)
        try:
            result = await publish_week(post, GROUP_ID, store, bot, confirmation=confirmation)
            publication = await store.get_publication(result.publication_id)
            assert publication is not None
            unknown = next(part for part in publication.parts if part.status == "unknown")
            stale_attempt_revision = unknown.attempt_revision

            await store.resolve_publication_part(
                publication.id,
                unknown.index,
                False,
                expected_attempt_revision=stale_attempt_revision,
                expected_preview_revision=publication.preview_revision,
                owner_chat_id=OWNER_ID,
                owner_message_id=47,
            )
            session.lose_next_response = True
            second_result = await retry_publication_part(
                publication.id,
                unknown.index,
                GROUP_ID,
                store,
                bot,
                expected_attempt_revision=stale_attempt_revision,
                expected_preview_revision=publication.preview_revision,
                owner_chat_id=OWNER_ID,
                owner_message_id=47,
            )
            assert second_result.status == "unknown"
            with pytest.raises(ValueError, match="устарело"):
                await store.resolve_publication_part(
                    publication.id,
                    unknown.index,
                    True,
                    expected_attempt_revision=stale_attempt_revision,
                    expected_preview_revision=publication.preview_revision,
                    owner_chat_id=OWNER_ID,
                    owner_message_id=47,
                )
            latest = await store.get_publication(publication.id)
            assert latest is not None
            assert latest.parts[unknown.index].status == "unknown"
            assert latest.parts[unknown.index].attempt_revision == stale_attempt_revision + 1
            assert len(session.photos) == 2
        finally:
            await bot.session.close()

    asyncio.run(scenario())
