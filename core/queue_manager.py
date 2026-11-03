import asyncio
import logging
import uuid

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable, Any

from aiogram import Bot

from core.config_loader import cfg


logger = logging.getLogger(__name__)


STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


STATUS_LABELS = {
    STATUS_QUEUED: "⏳ در انتظار",
    STATUS_RUNNING: "⚙️ در حال اجرا",
    STATUS_COMPLETED: "✅ تکمیل شده",
    STATUS_FAILED: "❌ دارای خطا",
    STATUS_CANCELLED: "🚫 لغو شده",
}


QUEUE_LABELS = {
    "download": "📥 دانلود",
    "mkvtoolnix": "📎 سافت‌ساب",
    "handbrake": "🔥 هاردساب",
    "upload": "☁️ آپلود",
}


def current_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class QueueTask:
    task_id: str
    queue_type: str

    title: str

    coro_func: Callable[..., Awaitable[Any]]

    args: tuple = field(default_factory=tuple)

    kwargs: dict = field(default_factory=dict)

    status: str = STATUS_QUEUED

    progress: str = "در انتظار شروع"

    result: Any = None

    error: str | None = None

    worker_id: int | None = None

    created_at: str = field(default_factory=current_time)

    started_at: str | None = None

    finished_at: str | None = None

    execution_task: asyncio.Task | None = field(
        default=None,
        repr=False
    )

    cancel_requested: bool = False

    notification_sent: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_CANCELLED
        )

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(
            self.status,
            self.status
        )

    @property
    def queue_label(self) -> str:
        return QUEUE_LABELS.get(
            self.queue_type,
            self.queue_type
        )

    @property
    def duration(self) -> str:
        if not self.started_at:
            return "-"

        try:
            start = datetime.strptime(
                self.started_at,
                "%Y-%m-%d %H:%M:%S"
            )

            end = (
                datetime.strptime(
                    self.finished_at,
                    "%Y-%m-%d %H:%M:%S"
                )
                if self.finished_at
                else datetime.now()
            )

            seconds = int((end - start).total_seconds())

            minutes, seconds = divmod(seconds, 60)

            hours, minutes = divmod(minutes, 60)

            if hours:
                return f"{hours}h {minutes}m {seconds}s"

            if minutes:
                return f"{minutes}m {seconds}s"

            return f"{seconds}s"

        except Exception:
            return "-"


class QueueEngine:

    def __init__(self):

        self.queues: dict[str, asyncio.Queue] = {
            "mkvtoolnix": asyncio.Queue(),
            "handbrake": asyncio.Queue(),
            "download": asyncio.Queue(),
            "upload": asyncio.Queue(),
        }

        self.tasks_registry: dict[str, QueueTask] = {}

        self._workers: list[asyncio.Task] = []

        self._worker_counts: dict[str, int] = {}

        self._bot: Bot | None = None

        self._started = False
        self._stopping = False
    # ==================================================
    # BOT / NOTIFICATION
    # ==================================================

    def set_bot(self, bot: Bot):
        """
        ثبت Bot برای ارسال اعلان اتمام پردازش‌ها.
        """

        self._bot = bot

    async def _notify_admins(self, task: QueueTask):

        if task.notification_sent:
            return

        if not self._bot:
            logger.warning(
                "Cannot notify task %s: bot is not configured",
                task.task_id
            )
            return

        admin_ids = cfg.get(
            "telegram",
            "admin_ids",
            []
        )

        if not admin_ids:
            return

        if task.status == STATUS_COMPLETED:

            icon = "✅"

            heading = "پردازش با موفقیت تمام شد"

        elif task.status == STATUS_CANCELLED:

            icon = "🚫"

            heading = "پردازش لغو شد"

        else:

            icon = "❌"

            heading = "پردازش با خطا مواجه شد"

        text = (
            f"{icon} <b>{heading}</b>\n\n"
            f"📌 <b>عنوان:</b> {self._escape_html(task.title)}\n"
            f"🆔 <b>شناسه:</b> <code>{task.task_id}</code>\n"
            f"📂 <b>نوع عملیات:</b> {task.queue_label}\n"
            f"📊 <b>وضعیت:</b> {task.status_label}\n"
            f"📈 <b>آخرین پیشرفت:</b> {self._escape_html(task.progress)}\n"
            f"⏱ <b>مدت اجرا:</b> {task.duration}"
        )

        if task.error:
            text += (
                "\n\n⚠️ <b>جزئیات خطا:</b>\n"
                f"<code>{self._escape_html(task.error[:1500])}</code>"
            )

        if task.result and isinstance(task.result, list):

            links = task.result[:8]

            if links:

                text += "\n\n🔗 <b>لینک‌های خروجی:</b>\n"

                text += "\n".join(
                    self._escape_html(str(link))
                    for link in links
                )

        sent_to_all = True

        for admin_id in admin_ids:

            try:

                await self._bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="HTML"
                )

            except Exception as exc:

                sent_to_all = False

                logger.error(
                    "Failed to notify admin %s about task %s: %s",
                    admin_id,
                    task.task_id,
                    exc
                )

        task.notification_sent = sent_to_all

    @staticmethod
    def _escape_html(value: Any) -> str:

        text = str(value)

        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    # ==================================================
    # WORKERS
    # ==================================================

    def start_workers(self):
        self._stopping = False
        if self._started:
            logger.warning("Queue workers are already running.")
            return

        concurrency_cfg = cfg.get("concurrency") or {}

        worker_counts = {
            "handbrake": int(
                concurrency_cfg.get("handbrake_workers", 2)
            ),

            "mkvtoolnix": int(
                concurrency_cfg.get("mkvtoolnix_workers", 1)
            ),

            "download": int(
                concurrency_cfg.get("download_workers", 3)
            ),

            "upload": int(
                concurrency_cfg.get("upload_workers", 1)
            ),
        }

        self._worker_counts = worker_counts

        for queue_name, count in worker_counts.items():

            count = max(1, count)

            for worker_id in range(1, count + 1):

                worker = asyncio.create_task(
                    self._worker_loop(
                        queue_name,
                        worker_id
                    )
                )

                self._workers.append(worker)

        self._started = True

        logger.info(
            "Queue workers started: %s",
            worker_counts
        )

    async def stop_workers(self):

        self._stopping = True

        # ابتدا پردازش‌های فعال متوقف شوند.
        for task in self.tasks_registry.values():

            if task.execution_task and not task.execution_task.done():
                task.execution_task.cancel()

        # سپس Workerها متوقف شوند.
        for worker in self._workers:
            worker.cancel()

        await asyncio.gather(
            *self._workers,
            return_exceptions=True
        )

        self._workers.clear()

        self._started = False

        self._stopping = False

    # ==================================================
    # ADD TASK
    # ==================================================

    async def add_task(
        self,
        queue_type: str,
        title: str,
        coro_func: Callable[..., Awaitable[Any]],
        *args,
        **kwargs
    ) -> QueueTask:

        if queue_type not in self.queues:

            raise ValueError(
                f"Unknown queue type: {queue_type}"
            )

        task_id = uuid.uuid4().hex[:8]

        while task_id in self.tasks_registry:

            task_id = uuid.uuid4().hex[:8]

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

        logger.info(
            "Task added: %s | %s | %s",
            task.task_id,
            queue_type,
            title
        )

        return task

    # ==================================================
    # PROGRESS
    # ==================================================

    @staticmethod
    def _make_progress_callback(task: QueueTask):

        def update_progress(progress_text: str):

            if task.status == STATUS_RUNNING:

                task.progress = str(progress_text)

        return update_progress

    # ==================================================
    # WORKER LOOP
    # ==================================================

    async def _worker_loop(
        self,
        queue_name: str,
        worker_id: int
    ):

        queue = self.queues[queue_name]

        logger.info(
            "Worker #%s started for %s",
            worker_id,
            queue_name
        )

        while True:

            task: QueueTask = await queue.get()

            try:

                # اگر تسک پیش از رسیدن نوبت لغو شده باشد
                if task.status == STATUS_CANCELLED:

                    continue

                task.worker_id = worker_id

                task.status = STATUS_RUNNING

                task.started_at = current_time()

                task.progress = "شروع پردازش..."

                logger.info(
                    "Task started: %s | Worker #%s",
                    task.task_id,
                    worker_id
                )

                # اجرای پردازش اصلی در یک Task مستقل
                # تا بتوان آن را لغو کرد، بدون اینکه Worker از بین برود.

                kwargs = dict(task.kwargs)

                kwargs["progress_callback"] = (
                    self._make_progress_callback(task)
                )

                execution = asyncio.create_task(
                    task.coro_func(
                        *task.args,
                        **kwargs
                    )
                )

                task.execution_task = execution

                try:

                    task.result = await execution

                    if task.cancel_requested:

                        task.status = STATUS_CANCELLED

                    else:

                        task.status = STATUS_COMPLETED

                        task.progress = "100%"

                except asyncio.CancelledError:

                    task.status = STATUS_CANCELLED

                    task.progress = "پردازش متوقف شد."

                    logger.info(
                        "Task cancelled: %s",
                        task.task_id
                    )

                except Exception as exc:

                    task.status = STATUS_FAILED

                    task.error = str(exc)

                    task.progress = "پردازش ناموفق بود."

                    logger.exception(
                        "Task failed: %s",
                        task.task_id
                    )

                finally:

                    task.execution_task = None

                    task.finished_at = current_time()

                    await self._notify_admins(task)

            finally:

                queue.task_done()

            if self._stopping:
                break

    # ==================================================
    # CANCEL TASK
    # ==================================================

    async def cancel_task(self, task_id: str) -> bool:

        task = self.tasks_registry.get(task_id)

        if not task:

            return False

        if task.is_terminal:

            return False

        task.cancel_requested = True

        if task.status == STATUS_QUEUED:

            task.status = STATUS_CANCELLED

            task.progress = "پیش از شروع لغو شد."

            task.finished_at = current_time()

            logger.info(
                "Queued task cancelled: %s",
                task_id
            )

            await self._notify_admins(task)

            return True

        if task.status == STATUS_RUNNING:

            execution = task.execution_task

            if execution and not execution.done():

                execution.cancel()

                logger.info(
                    "Running task cancellation requested: %s",
                    task_id
                )

                return True

        return False

    # ==================================================
    # GET TASKS
    # ==================================================

    def get_task(self, task_id: str) -> QueueTask | None:

        return self.tasks_registry.get(task_id)

    def get_all_tasks(self) -> list[QueueTask]:

        return list(self.tasks_registry.values())

    def get_queue_summary(
        self,
        limit: int = 30
    ) -> list[QueueTask]:

        tasks = list(
            self.tasks_registry.values()
        )

        return tasks[-limit:]

    def get_statistics(self) -> dict:

        stats = {
            "total": 0,
            STATUS_QUEUED: 0,
            STATUS_RUNNING: 0,
            STATUS_COMPLETED: 0,
            STATUS_FAILED: 0,
            STATUS_CANCELLED: 0,
        }

        for task in self.tasks_registry.values():

            stats["total"] += 1

            if task.status in stats:

                stats[task.status] += 1

        return stats

    def get_worker_counts(self) -> dict:

        return dict(self._worker_counts)


queue_engine = QueueEngine()