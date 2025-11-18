import threading
from typing import Optional

# 간단한 런타임 레지스트리: FastAPI에서 discord.Bot 인스턴스에 접근하기 위함
_lock = threading.RLock()
_bot = None


def set_bot(bot) -> None:
    global _bot
    with _lock:
        _bot = bot


def get_bot():
    with _lock:
        return _bot
