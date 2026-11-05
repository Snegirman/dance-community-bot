"""Проверки владельца и приватного чата."""


def is_owner_private(user_id: int | None, chat_type: str, owner_id: int) -> bool:
    """Разрешить действие только владельцу в личном чате."""
    return user_id == owner_id and chat_type == "private"
