import os
import sys
import asyncio
from dataclasses import dataclass
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

# This module provides a simple in-memory FIFO queue for provisioning tasks
# to avoid concurrent provisioning collisions. It serializes tasks one-by-one.

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'bot.db')

@dataclass
class ProvisionTask:
    order_id: int
    user_id: int
    protocol: str  # e.g., 'xray', 'wg'

_queue: asyncio.Queue[ProvisionTask] = asyncio.Queue()
_busy: bool = False

async def _provision_xray(order_id: int) -> tuple[int, str]:
    """Run provision_xray.py for a given order id. Returns (rc, error_text)."""
    prov_path = os.path.join(BASE_DIR, 'provision_xray.py')
    if not os.path.exists(prov_path):
        return 2, 'provision_xray.py not found'

    def _run():
        import subprocess
        return subprocess.run(
            [sys.executable, prov_path, '--order-id', str(order_id), '--db', DB_PATH],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=600
        )

    res = await asyncio.to_thread(_run)
    if res.returncode != 0:
        err = res.stderr or res.stdout or 'Unknown error'
        return res.returncode, err[-2000:]
    return 0, ''

async def _process_task(app: Application, task: ProvisionTask) -> None:
    global _busy
    _busy = True
    try:
        # Send optional start notification
        try:
            await app.bot.send_message(
                chat_id=task.user_id,
                text=f"🔧 Начинаю настройку заказа #{task.order_id}…"
            )
        except Exception:
            pass

        rc = 0
        err: Optional[str] = ''
        if task.protocol == 'xray':
            rc, err = await _provision_xray(task.order_id)
        else:
            # Unknown protocol: for safety, just mark as error
            rc, err = 2, f'Unsupported protocol: {task.protocol}'

        if rc == 0:
            # Success: notify user with manage button
            try:
                kb_ready = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Показать конфиги", callback_data=f"order_manage:{task.order_id}")]])
                await app.bot.send_message(
                    chat_id=task.user_id,
                    text=(
                        f"✅ Готово! Заказ #{task.order_id}.") ,
                    reply_markup=kb_ready
                )
            except Exception:
                pass
        else:
            # Failure: notify user
            try:
                await app.bot.send_message(
                    chat_id=task.user_id,
                    text=(
                        f"❌ Не удалось выдать конфиг для заказа #{task.order_id}.\n"
                        "Если списание произошло, баланс уже уменьшен на сумму покупки."
                    )
                )
            except Exception:
                pass
    finally:
        _busy = False

async def _worker(app: Application) -> None:
    # Run forever, serializing tasks
    while True:
        task = await _queue.get()
        try:
            await _process_task(app, task)
        except Exception:
            # Swallow to keep worker alive
            pass
        finally:
            _queue.task_done()

# Public API

def start_worker_in_app_loop(app: Application) -> None:
    """Start the background worker using the running event loop (must be called from an async context)."""
    loop = asyncio.get_running_loop()
    loop.create_task(_worker(app))

def enqueue(order_id: int, user_id: int, protocol: str) -> int:
    """Enqueue a provisioning task. Returns estimated queue position (1-based)."""
    _queue.put_nowait(ProvisionTask(order_id=order_id, user_id=user_id, protocol=protocol))
    # Estimated position: tasks waiting + (1 if busy)
    pos = _queue.qsize() + (1 if _busy else 0)
    return pos
