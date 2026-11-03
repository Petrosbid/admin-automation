import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable, Any
from core.config_loader import cfg


@dataclass
class QueueTask:
    task_id: str
    queue_type: str          # 'mkvtoolnix' | 'handbrake' | 'download' | 'upload'
    title: str
    coro_func: Callable[..., Awaitable[Any]]
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    status: str = "در صف انتظار ⏳"
    progress: str = "0%"
    result: Any = None
    error: str | None = None
    worker_id: int = 1
    async_task: asyncio.Task | None = None
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))

    @property
    def is_finished(self) -> bool:
        return any(k in self.status for k in ("تکمیل", "خطا", "لغو"))


class SequentialQueueEngine:
    def __init__(self):
        self.queues: dict[str, asyncio.Queue[QueueTask]] = {
            "mkvtoolnix": asyncio.Queue(),
            "handbrake": asyncio.Queue(),
            "download": asyncio.Queue(),
            "upload": asyncio.Queue(),
        }
        self.tasks_registry: dict[str, QueueTask] = {}
        self._workers: list[asyncio.Task] = []
        self.bot: Any = None

    def set_bot(self, bot: Any):
        """تنظیم نمونه ربات تلگرام برای ارسال اعلان خودکار پایان یا خطای پردازش‌ها"""
        self.bot = bot

    async def _notify_admins(self, text: str):
        """ارسال پیام وضعیت به ادمین‌ها در تلگرام"""
        if not self.bot:
            return
        admin_ids = cfg.get("telegram", "admin_ids", [])
        for admin_id in admin_ids:
            try:
                await self.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.warning(f"Failed to send queue notification to {admin_id}: {e}")

    def start_workers(self):
        """راه‌اندازی ورکرهای پس‌زمینه (۲ ورکر همزمان برای HandBrake)"""
        if self._workers:
            return
        concurrency_cfg = cfg.get("concurrency") or {}
        worker_counts = {
            "handbrake": int(concurrency_cfg.get("handbrake_workers", 2)),
            "mkvtoolnix": int(concurrency_cfg.get("mkvtoolnix_workers", 1)),
            "download": int(concurrency_cfg.get("download_workers", 1)),
            "upload": int(concurrency_cfg.get("upload_workers", 1)),
        }

        for q_name, count in worker_counts.items():
            for w_idx in range(1, max(1, count) + 1):
                worker = asyncio.create_task(self._worker_loop(q_name, w_idx))
                self._workers.append(worker)

    async def add_task(
        self,
        queue_type: str,
        title: str,
        coro_func: Callable[..., Awaitable[Any]],
        *args,
        **kwargs
    ) -> QueueTask:
        task_id = str(uuid.uuid4())[:6]
        task = QueueTask(
            task_id=task_id,
            queue_type=queue_type,
            title=title,
            coro_func=coro_func,
            args=args,
            kwargs=kwargs
        )
        self.tasks_registry[task_id] = task
        await self.queues[queue_type].put(task)
        return task

    async def _worker_loop(self, queue_name: str, worker_id: int):
        queue = self.queues[queue_name]
        while True:
            task: QueueTask = await queue.get()
            if task.task_id not in self.tasks_registry or task.status == "لغو شده ❌":
                queue.task_done()
                continue

            task.worker_id = worker_id
            task.status = f"در حال اجرا (ورکر #{worker_id}) ⚙️"
            try:
                def update_progress(pct_text: str):
                    task.progress = pct_text

                task.kwargs["progress_callback"] = update_progress
                task.async_task = asyncio.current_task()
                task.result = await task.coro_func(*task.args, **task.kwargs)
                task.status = "تکمیل شد ✅"
                task.progress = "100%"

                # ارسال اعلان اتمام موفقیت‌آمیز به تلگرام
                msg = (
                    f"✅ **پردازش تکمیل شد!**\n"
                    f"🆔 کد: `{task.task_id}`\n"
                    f"⚙️ بخش: `{task.queue_type.upper()}`\n"
                    f"📌 عنوان: `{task.title}`"
                )
                if isinstance(task.result, list) and task.result:
                    links = "\n".join([f"🔗 {u}" for u in task.result[:10]])
                    msg += f"\n\n📥 **لینک‌های تولید شده:**\n{links}"
                elif task.result:
                    msg += f"\n📂 خروجی: `{task.result}`"
                await self._notify_admins(msg)

            except asyncio.CancelledError:
                task.status = "لغو شده ❌"
                task.error = "توسط کاربر متوقف شد."
            except Exception as e:
                task.status = "خطا ❌"
                task.error = str(e)
                await self._notify_admins(
                    f"❌ **خطا در پردازش!**\n"
                    f"🆔 کد: `{task.task_id}`\n"
                    f"⚙️ بخش: `{task.queue_type.upper()}`\n"
                    f"📌 عنوان: `{task.title}`\n"
                    f"⚠️ جزئیات خطا:\n`{str(e)[:300]}`"
                )
            finally:
                task.async_task = None
                queue.task_done()

    def get_queue_summary(self) -> list[QueueTask]:
        """برگرداندن لیست تسک‌ها برای نمایش در تلگرام"""
        return list(self.tasks_registry.values())[-20:]

    def cancel_task(self, task_id: str) -> bool:
        """لغو تسک در صف انتظار یا در حال اجرا"""
        task = self.tasks_registry.get(task_id)
        if not task:
            return False
        if task.status.startswith("در صف"):
            task.status = "لغو شده ❌"
            return True
        if task.async_task and not task.async_task.done():
            task.async_task.cancel()
            task.status = "لغو شده ❌"
            return True
        return False

    def remove_task(self, task_id: str) -> bool:
        """حذف یک تسک از لیست"""
        task = self.tasks_registry.get(task_id)
        if not task:
            return False
        if not task.is_finished:
            self.cancel_task(task_id)
        self.tasks_registry.pop(task_id, None)
        return True

    def clear_finished_tasks(self) -> int:
        """حذف یکجای تمامی پراسس‌های تمام‌شده، خطادار یا لغوشده"""
        finished_ids = [
            tid for tid, t in self.tasks_registry.items()
            if t.is_finished
        ]
        for tid in finished_ids:
            self.tasks_registry.pop(tid, None)
        return len(finished_ids)


queue_engine = SequentialQueueEngine()