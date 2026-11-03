import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable, Any


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
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


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

    def start_workers(self):
        """راه‌اندازی ورکرهای پس‌زمینه برای هر صف"""
        for q_name in self.queues:
            worker = asyncio.create_task(self._worker_loop(q_name))
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

    async def _worker_loop(self, queue_name: str):
        queue = self.queues[queue_name]
        while True:
            task: QueueTask = await queue.get()
            if task.status == "لغو شده ❌":
                queue.task_done()
                continue

            task.status = "در حال اجرا ⚙️"
            try:
                # ارسال تابع آپدیت درصد پیشرفت به داخل سرویس‌ها
                def update_progress(pct_text: str):
                    task.progress = pct_text

                task.kwargs["progress_callback"] = update_progress
                task.result = await task.coro_func(*task.args, **task.kwargs)
                task.status = "تکمیل شد ✅"
                task.progress = "100%"
            except Exception as e:
                task.status = "خطا ❌"
                task.error = str(e)
            finally:
                queue.task_done()

    def get_queue_summary(self) -> list[QueueTask]:
        """برگرداندن لیست تمام تسک‌ها برای نمایش در تلگرام"""
        return list(self.tasks_registry.values())[-15:]

    def cancel_task(self, task_id: str) -> bool:
        task = self.tasks_registry.get(task_id)
        if task and task.status.startswith("در صف"):
            task.status = "لغو شده ❌"
            return True
        return False


queue_engine = SequentialQueueEngine()