from __future__ import annotations

import asyncio
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import GetChat, SendMessage, SendPhoto, TelegramMethod
from aiogram.methods.base import TelegramType
from telegram_transport import RecordingSession

from dance_community_bot.models import EventData, PublicationClaimResult, SourceInput, WeeklyPost
from dance_community_bot.publishing import publish_week
from dance_community_bot.storage import Store
from dance_community_bot.weekly import build_weekly_post

GROUP_ID = -100123
TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"


class RegressionSession(RecordingSession):
    def __init__(self, failure: str | None = None) -> None:
        super().__init__(GROUP_ID)
        self.failure = failure
        self.photo_requests: list[SendPhoto] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        result: dict[str, object]
        if isinstance(method, GetChat):
            result = {
                "id": GROUP_ID,
                "type": "supergroup",
                "title": "Тестовая группа",
                "accent_color_id": 0,
                "max_reaction_count": 1,
                "accepted_gift_types": {
                    "unlimited_gifts": False,
                    "limited_gifts": False,
                    "unique_gifts": False,
                    "premium_subscription": False,
                    "gifts_from_channels": False,
                },
            }
        elif isinstance(method, SendPhoto):
            self.photo_requests.append(method)
            result = {
                "message_id": 200 + len(self.photo_requests),
                "date": 1793782800,
                "chat": {"id": GROUP_ID, "type": "supergroup", "title": "Тестовая группа"},
                "photo": [
                    {"file_id": "saved-photo", "file_unique_id": "unique", "width": 1, "height": 1}
                ],
            }
        else:
            if isinstance(method, SendMessage):
                if self.failure == "forbidden":
                    raise TelegramForbiddenError(method=method, message="private error payload")
                if self.failure == "rate_limit":
                    raise TelegramRetryAfter(
                        method=method, message="private error payload", retry_after=5
                    )
            return await super().make_request(bot, method, timeout)
        response = self.check_response(
            bot=bot,
            method=method,
            status_code=200,
            content=self.json_dumps({"ok": True, "result": result}),
        )
        assert response.result is not None
        return response.result


async def prepare_event(store: Store, *, photo: bool = False) -> None:
    await store.initialize()
    source = await store.save_source(
        SourceInput(
            chat_id=111,
            message_id=1,
            text="Название: <Jam>&",
            received_at=datetime(2026, 11, 9, tzinfo=UTC),
            telegram_file_id="available-file-id" if photo else None,
            is_photo=photo,
        )
    )
    if photo:
        path = Path("posters/1.jpg")
        (store.path.parent / path).write_bytes(b"poster")
        await store.set_source_poster_path(source.id, path)
    draft = await store.save_draft(
        source.id,
        EventData(title="<Jam>&", event_type="Джем", start_date=date(2026, 11, 10)),
        poster_source_id=source.id if photo else None,
    )
    await store.confirm_draft(draft.id, draft.version)


@pytest.mark.parametrize("photo", [False, True])
def test_publication_escapes_html_in_text_and_caption(tmp_path: Path, photo: bool) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await prepare_event(store, photo=photo)
        post = await build_weekly_post(date(2026, 11, 9), store)
        session = RegressionSession()
        async with Bot(TOKEN, session=session) as bot:
            result = await publish_week(post, GROUP_ID, store, bot)
        assert result.status == "delivered"
        body = session.photo_requests[0].caption if photo else session.messages[0]
        assert body is not None
        assert "&lt;Jam&gt;&amp;" in body
        assert "<Jam>" not in body

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["forbidden", "rate_limit"])
def test_explicit_telegram_refusal_is_retryable_not_unknown(tmp_path: Path, failure: str) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await prepare_event(store)
        post = await build_weekly_post(date(2026, 11, 9), store)
        session = RegressionSession(failure)
        async with Bot(TOKEN, session=session) as bot:
            result = await publish_week(post, GROUP_ID, store, bot)
        assert result.status == "partial"
        assert result.publication_id is not None
        publication = await store.get_publication(result.publication_id)
        assert publication is not None
        assert publication.parts[0].status == "failed"
        assert "private error payload" not in repr(result)

    asyncio.run(scenario())


def test_available_telegram_file_does_not_require_local_original(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await prepare_event(store, photo=True)
        post = await build_weekly_post(date(2026, 11, 9), store)
        (tmp_path / "posters/1.jpg").unlink()
        session = RegressionSession()
        async with Bot(TOKEN, session=session) as bot:
            result = await publish_week(post, GROUP_ID, store, bot)
        assert result.status == "delivered"
        assert len(session.photo_requests) == 1
        assert session.photo_requests[0].photo == "available-file-id"

    asyncio.run(scenario())


def test_cancelled_initial_claim_does_not_leave_publication_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await prepare_event(store)
        post = await build_weekly_post(date(2026, 11, 9), store)
        saved = asyncio.Event()
        release = asyncio.Event()
        original_claim = store.claim_publication

        async def pause_after_claim(
            post: WeeklyPost,
            group_id: int,
            active_token: str,
            *,
            automatic: bool = False,
            expected_publication_id: int | None = None,
            expected_preview_revision: int | None = None,
            owner_chat_id: int | None = None,
            owner_message_id: int | None = None,
        ) -> PublicationClaimResult:
            result = await original_claim(
                post,
                group_id,
                active_token,
                automatic=automatic,
                expected_publication_id=expected_publication_id,
                expected_preview_revision=expected_preview_revision,
                owner_chat_id=owner_chat_id,
                owner_message_id=owner_message_id,
            )
            saved.set()
            await release.wait()
            return result

        monkeypatch.setattr(store, "claim_publication", pause_after_claim)
        session = RegressionSession()
        async with Bot(TOKEN, session=session) as bot:
            task = asyncio.create_task(publish_week(post, GROUP_ID, store, bot))
            await asyncio.wait_for(saved.wait(), timeout=2)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        publication = await store.get_publication_for_week(GROUP_ID, post.week_start)
        assert publication is not None
        assert publication.active_token is None
        assert publication.status == "partial"
        assert publication.parts[0].status == "failed"
        assert session.messages == []

    asyncio.run(scenario())


def test_cancel_after_successful_response_saves_message_before_releasing_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await prepare_event(store)
        post = await build_weekly_post(date(2026, 11, 9), store)
        finish_started = threading.Event()
        allow_finish = threading.Event()
        finish_completed = threading.Event()
        original_finish = store._finish_publication_part_sync

        def pause_finish(
            publication_id: int,
            part_index: int,
            active_token: str,
            status: Literal["sent", "failed", "unknown"],
            message_id: int | None,
        ) -> None:
            finish_started.set()
            if not allow_finish.wait(timeout=5):
                raise TimeoutError("Не дождался разрешения завершить запись выпуска.")
            try:
                original_finish(publication_id, part_index, active_token, status, message_id)
            finally:
                finish_completed.set()

        monkeypatch.setattr(store, "_finish_publication_part_sync", pause_finish)
        release_started = asyncio.Event()
        release_completed = asyncio.Event()
        original_release = store.release_publication_claim

        async def track_release(publication_id: int, active_token: str) -> None:
            release_started.set()
            await original_release(publication_id, active_token)
            release_completed.set()

        monkeypatch.setattr(store, "release_publication_claim", track_release)
        session = RegressionSession()
        async with Bot(TOKEN, session=session) as bot:
            task = asyncio.create_task(publish_week(post, GROUP_ID, store, bot))
            try:
                assert await asyncio.wait_for(asyncio.to_thread(finish_started.wait, 3), 4)
                task.cancel()
                cancellation_processed = asyncio.Event()
                asyncio.get_running_loop().call_soon(cancellation_processed.set)
                await cancellation_processed.wait()
                released_before_finish = release_started.is_set()
                if released_before_finish:
                    await asyncio.wait_for(release_completed.wait(), 3)
            finally:
                allow_finish.set()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 4)
            assert await asyncio.wait_for(asyncio.to_thread(finish_completed.wait, 3), 4)
            repeated = await publish_week(post, GROUP_ID, store, bot)
            assert repeated.part_message_ids == (101,)

        publication = await store.get_publication_for_week(GROUP_ID, post.week_start)
        assert publication is not None
        assert not released_before_finish
        assert publication.status == "delivered"
        assert publication.active_token is None
        assert publication.parts[0].status == "sent"
        assert publication.parts[0].message_id == 101
        assert len(session.messages) == 1

    asyncio.run(scenario())


def test_automatic_rebuild_to_empty_week_does_not_send_or_raise(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await prepare_event(store)
        post = await build_weekly_post(date(2026, 11, 9), store)
        record = post.records[0]
        await store.cancel_record(record.id, record.revision)
        session = RegressionSession()
        async with Bot(TOKEN, session=session) as bot:
            result = await publish_week(post, GROUP_ID, store, bot, automatic=True)
        assert result.publication_id is None
        assert result.status == "rejected"
        assert session.messages == []

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_restart_releases_claim_after_outcome_saved_before_cleanup(
    tmp_path: Path, outcome: Literal["failed", "unknown"]
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await prepare_event(store)
        post = await build_weekly_post(date(2026, 11, 9), store)
        claim = await store.claim_publication(post, GROUP_ID, "interrupted")
        assert claim.publication is not None and claim.part is not None
        await store.finish_publication_part(
            claim.publication.id, claim.part.index, "interrupted", outcome
        )
        # Процесс завершился после сохранения результата, до release_publication_claim.
        restarted = Store(store.path)
        await restarted.initialize()
        await restarted.recover_publications_on_startup()
        publication = await restarted.get_publication(claim.publication.id)
        assert publication is not None
        assert publication.active_token is None
        assert publication.parts[claim.part.index].status == outcome
        assert publication.status == ("partial" if outcome == "failed" else "unknown")
        if outcome == "unknown":
            await restarted.resolve_publication_part(publication.id, claim.part.index, True)
        else:
            bound = await restarted.bind_publication_message(
                publication.id, publication.preview_revision, 111, 300
            )
            assert bound is not None
            retry = await restarted.retry_publication_part(
                publication.id,
                claim.part.index,
                GROUP_ID,
                "retry",
                claim.part.attempt_revision,
                bound.preview_revision,
                111,
                300,
            )
            assert retry.outcome == "claimed"

    asyncio.run(scenario())
