import asyncio
import inspect
import logging
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine
from aiogram import Bot

from core.config_loader import cfg


@dataclass
class QueueTask:
    task_id: str
    queue_type: str          # 'download' | 'mkvtoolnix' | 'handbrake' | 'upload'
    title: str
    func: Callable[..., Coroutine[Any, Any, Any]]
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    status: str = "⏳ در انتظار"
    progress: str = "0%"
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    error: str | None = None
    result: Any = None
    is_finished: bool = False
    is_cancelled: bool = False
    running_asyncio_task: asyncio.Task | None = None


class QueueManager:
    QUEUE_TYPES = ("download", "mkvtoolnix", "handbrake", "upload")

    def __init__(self):
        self.tasks: dict[str, QueueTask] = {}
        self.pending_lists: dict[str, list[str]] = {q: [] for q in self.QUEUE_TYPES}
        self._events: dict[str, asyncio.Event] = {q: asyncio.Event() for q in self.QUEUE_TYPES}
        self._locks: dict[str, asyncio.Lock] = {q: asyncio.Lock() for q in self.QUEUE_TYPES}
        self._workers: list[asyncio.Task] = []
        self._bot: Bot | None = None

    def set_bot(self, bot: Bot):
        self._bot = bot

    async def add_task(
        self,
        queue_type: str,
        title: str,
        func: Callable,
        *args,
        priority_top: bool = False,
        **kwargs
    ) -> QueueTask:
        if queue_type not in self.QUEUE_TYPES:
            queue_type = "download"

        task_id = str(uuid.uuid4())[:6]
        task = QueueTask(
            task_id=task_id,
            queue_type=queue_type,
            title=title,
            func=func,
            args=args,
            kwargs=kwargs
        )
        self.tasks[task_id] = task

        async with self._locks[queue_type]:
            if priority_top:
                self.pending_lists[queue_type].insert(0, task_id)
            else:
                self.pending_lists[queue_type].append(task_id)
            self._events[queue_type].set()

        return task

    def move_pending_task(self, task_id: str, direction: str) -> bool:
        """
        جابجایی نوبت و اولویت پراسس‌های در انتظار:
        direction: 'up' (یک پله بالاتر) | 'down' (یک پله پایین‌تر) | 'top' (انتقال به صدر صف)
        """
        task = self.tasks.get(task_id)
        if not task or "انتظار" not in task.status:
            return False

        p_list = self.pending_lists.get(task.queue_type, [])
        if task_id not in p_list:
            return False

        idx = p_list.index(task_id)
        if direction == "top" and idx > 0:
            p_list.pop(idx)
            p_list.insert(0, task_id)
            return True
        elif direction == "up" and idx > 0:
            p_list[idx - 1], p_list[idx] = p_list[idx], p_list[idx - 1]
            return True
        elif direction == "down" and idx < len(p_list) - 1:
            p_list[idx + 1], p_list[idx] = p_list[idx], p_list[idx + 1]
            return True
        return False

    def get_pending_position(self, task_id: str) -> tuple[int, int]:
        """برگرداندن (جایگاه فعلی در صف انتظار، کل تعداد در انتظار همان صف)"""
        task = self.tasks.get(task_id)
        if not task:
            return 0, 0
        p_list = self.pending_lists.get(task.queue_type, [])
        if task_id not in p_list:
            return 0, len(p_list)
        return p_list.index(task_id) + 1, len(p_list)

    async def _pop_next_pending(self, queue_type: str) -> QueueTask | None:
        while True:
            async with self._locks[queue_type]:
                while self.pending_lists[queue_type]:
                    tid = self.pending_lists[queue_type].pop(0)
                    t = self.tasks.get(tid)
                    if t and not t.is_cancelled and not t.is_finished:
                        return t
                self._events[queue_type].clear()
            await self._events[queue_type].wait()

    async def _notify_admins(self, text: str):
        if not self._bot:
            return
        for admin_id in cfg.get("telegram", "admin_ids", []):
            try:
                await self._bot.send_message(
                    chat_id=admin_id,
                    text=text[:4000],
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
            except Exception:
                try:
                    await self._bot.send_message(
                        chat_id=admin_id,
                        text=text.replace("`", "").replace("*", "")[:4000],
                        disable_web_page_preview=True
                    )
                except Exception as e:
                    logging.error(f"Queue notification error: {e}")

    async def _worker_loop(self, queue_type: str, worker_idx: int):
        while True:
            task = await self._pop_next_pending(queue_type)
            if not task:
                continue

            task.status = f"⚙️ در حال اجرا (W{worker_idx})"
            task.progress = "شروع پردازش..."

            def _prog_cb(msg: str, t=task):
                t.progress = str(msg)

            kwargs = dict(task.kwargs)
            try:
                sig = inspect.signature(task.func)
                if "progress_callback" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    kwargs["progress_callback"] = _prog_cb
            except Exception:
                kwargs["progress_callback"] = _prog_cb

            try:
                coro = task.func(*task.args, **kwargs)
                run_task = asyncio.create_task(coro)
                task.running_asyncio_task = run_task
                res = await run_task

                task.result = res
                task.status = "✅ تکمیل شد"
                task.progress = "100%"
                task.is_finished = True

                # اگر تسک آپلود بوده و لینک‌های دانلود تولید کرده، لینک‌ها را در تلگرام بفرست
                if queue_type == "upload" and isinstance(res, list) and res:
                    links_block = "\n".join([f"🔗 `{u}`" for u in res[:25]])
                    await self._notify_admins(
                        f"☁️ **آپلود با موفقیت به پایان رسید!**\n"
                        f"📌 عنوان: `{task.title}`\n\n"
                        f"📥 **لینک‌های مستقیم دانلود:**\n{links_block}"
                    )

            except asyncio.CancelledError:
                task.status = "🛑 لغو شد"
                task.progress = "متوقف شده توسط کاربر"
                task.is_finished = True
            except Exception as e:
                logging.error(f"Task {task.task_id} failed:\n{traceback.format_exc()}")
                task.error = str(e)
                task.status = "❌ خطا"
                task.progress = "متوقف شده به دلیل خطا"
                task.is_finished = True
                await self._notify_admins(
                    f"❌ **خطا در پردازش!**\n"
                    f"🆔 کد: `{task.task_id}`\n"
                    f"⚙️ بخش: `{task.queue_type.upper()}`\n"
                    f"📌 عنوان: `{task.title}`\n"
                    f"⚠️ جزئیات خطا:\n`{str(e)[:400]}`"
                )
            finally:
                task.running_asyncio_task = None

    def start_workers(self):
        if self._workers:
            return
        conc = cfg.get("concurrency") or {}
        counts = {
            "handbrake": int(conc.get("handbrake_workers", 2)),
            "mkvtoolnix": int(conc.get("mkvtoolnix_workers", 1)),
            "download": int(conc.get("download_workers", 1)),
            "upload": 1,  # همیشه ۱ ورکر برای CuteFTP تا آپلودها به صورت ترتیبی و بدون فریز شدن انجام شوند
        }
        for q_type, num in counts.items():
            for idx in range(1, max(1, num) + 1):
                w = asyncio.create_task(self._worker_loop(q_type, idx))
                self._workers.append(w)

    def get_queue_summary(self) -> list[QueueTask]:
        return list(self.tasks.values())

    def cancel_task(self, task_id: str) -> bool:
        t = self.tasks.get(task_id)
        if not t:
            return False
        t.is_cancelled = True
        if t.queue_type in self.pending_lists and task_id in self.pending_lists[t.queue_type]:
            self.pending_lists[t.queue_type].remove(task_id)
        if t.running_asyncio_task and not t.running_asyncio_task.done():
            t.running_asyncio_task.cancel()
        t.status = "🛑 لغو شد"
        t.is_finished = True
        return True

    def remove_task(self, task_id: str) -> bool:
        if task_id in self.tasks:
            t = self.tasks[task_id]
            if t.queue_type in self.pending_lists and task_id in self.pending_lists[t.queue_type]:
                self.pending_lists[t.queue_type].remove(task_id)
            del self.tasks[task_id]
            return True
        return False

    def clear_finished_tasks(self) -> int:
        to_del = [
            tid for tid, t in self.tasks.items()
            if t.is_finished or any(k in str(t.status) for k in ("تکمیل", "خطا", "لغو"))
        ]
        for tid in to_del:
            del self.tasks[tid]
        return len(to_del)


queue_engine = QueueManager()