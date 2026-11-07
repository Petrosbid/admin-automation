import asyncio
import html
import inspect
import logging
import re
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
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

    QUEUE_LABELS = {
        "mkvtoolnix": "🎬 سافت‌ساب (MKVToolNix)",
        "handbrake": "🔥 هاردساب / انکود (HandBrake)",
        "download": "📥 دانلود فایل",
        "upload": "☁️ آپلود به سرور (CuteFTP)",
    }

    def __init__(self):
        self.tasks: dict[str, QueueTask] = {}
        self.pending_lists: dict[str, list[str]] = {q: [] for q in self.QUEUE_TYPES}
        self._events: dict[str, asyncio.Event] = {q: asyncio.Event() for q in self.QUEUE_TYPES}
        self._locks: dict[str, asyncio.Lock] = {q: asyncio.Lock() for q in self.QUEUE_TYPES}
        self._workers: list[asyncio.Task] = []
        self._bot: Bot | None = None
        self._known_chats: set[int] = set()

    def set_bot(self, bot: Bot):
        self._bot = bot

    def register_chat(self, chat_id: int):
        if chat_id:
            self._known_chats.add(int(chat_id))

    def _get_target_chat_ids(self) -> list[int]:
        raw_admins = cfg.get("telegram", "admin_ids", [])
        chat_ids: set[int] = set(getattr(self, "_known_chats", set()))

        if isinstance(raw_admins, (int, str)):
            try:
                chat_ids.add(int(str(raw_admins).strip()))
            except ValueError:
                pass
        elif isinstance(raw_admins, list):
            for item in raw_admins:
                try:
                    chat_ids.add(int(str(item).strip()))
                except ValueError:
                    pass

        return list(chat_ids)

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

    async def _notify_admins(self, html_text: str):
        """ارسال تضمینی نوتیفیکیشن به تلگرام برای تمام انواع پراسس‌ها"""
        if not self._bot:
            logging.error("QueueManager._bot is None!")
            return

        target_ids = self._get_target_chat_ids()
        for admin_id in target_ids:
            try:
                await self._bot.send_message(
                    chat_id=admin_id,
                    text=html_text[:4000],
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except Exception as e:
                logging.warning(f"HTML notification fallback for {admin_id}: {e}")
                try:
                    plain = re.sub(r"<[^>]+>", "", html_text)
                    await self._bot.send_message(
                        chat_id=admin_id,
                        text=plain[:4000],
                        disable_web_page_preview=True
                    )
                except Exception as e2:
                    logging.error(f"Failed to send notification to {admin_id}: {e2}")

    async def _send_completion_notification(self, task: QueueTask, res: Any):
        """ساخت و ارسال پیام تکمیل شدن برای دانلود، سافت‌ساب، هاردساب/انکود و آپلود"""
        section_label = html.escape(self.QUEUE_LABELS.get(task.queue_type, task.queue_type.upper()))
        safe_title = html.escape(str(task.title))
        safe_id = html.escape(str(task.task_id))

        # ۱. اگر پراسس آپلود بود:
        if task.queue_type == "upload":
            if isinstance(res, list) and res:
                links_block = "\n".join([f"🔗 <code>{html.escape(str(u))}</code>" for u in res[:25]])
                await self._notify_admins(
                    f"☁️ <b>آپلود با موفقیت به پایان رسید!</b>\n"
                    f"🆔 کد: <code>{safe_id}</code>\n"
                    f"📌 عنوان: <code>{safe_title}</code>\n\n"
                    f"📥 <b>لینک‌های مستقیم دانلود:</b>\n{links_block}"
                )
            else:
                await self._notify_admins(
                    f"☁️ <b>آپلود با موفقیت انجام شد!</b>\n"
                    f"🆔 کد: <code>{safe_id}</code>\n"
                    f"📌 عنوان: <code>{safe_title}</code>"
                )
            return

        # ۲. برای دانلود، سافت‌ساب (mkvtoolnix) و هاردساب/انکود (handbrake):
        out_path: Path | None = None
        if isinstance(res, Path):
            out_path = res
        elif isinstance(res, str) and res.strip():
            try:
                p = Path(res)
                if p.exists():
                    out_path = p
            except Exception:
                pass
        elif isinstance(res, list) and len(res) > 0:
            try:
                p = Path(str(res[0]))
                if p.exists():
                    out_path = p
            except Exception:
                pass

        if out_path is None and task.args:
            for arg in reversed(task.args):
                if isinstance(arg, Path) and arg.exists():
                    out_path = arg
                    break

        details = ""
        if out_path and out_path.exists():
            if out_path.is_file():
                size_mb = out_path.stat().st_size / (1024 * 1024)
                details = (
                    f"\n📄 <b>فایل خروجی:</b>\n<code>{html.escape(out_path.name)}</code>\n"
                    f"📂 <b>مسیر ذخیره:</b>\n<code>{html.escape(str(out_path.parent))}</code>\n"
                    f"📦 <b>حجم فایل:</b> <code>{size_mb:.2f} MB</code>"
                )
            elif out_path.is_dir():
                files_in_dir = sorted(
                    [
                        f for f in out_path.rglob("*")
                        if f.is_file() and not f.name.startswith("_") and not f.name.endswith(".aria2")
                    ],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                if files_in_dir:
                    newest = files_in_dir[0]
                    size_mb = newest.stat().st_size / (1024 * 1024)
                    extra_count = f" (+ {len(files_in_dir) - 1} فایل دیگر)" if len(files_in_dir) > 1 else ""
                    details = (
                        f"\n📄 <b>فایل دانلود شده:</b>\n<code>{html.escape(newest.name)}</code>{extra_count}\n"
                        f"📂 <b>مسیر پوشه:</b>\n<code>{html.escape(str(out_path))}</code>\n"
                        f"📦 <b>حجم فایل:</b> <code>{size_mb:.2f} MB</code>"
                    )
                else:
                    details = f"\n📂 <b>مسیر پوشه:</b>\n<code>{html.escape(str(out_path))}</code>"

        if task.queue_type == "download":
            header = "📥 <b>دانلود فایل با موفقیت به پایان رسید!</b>"
        elif task.queue_type == "mkvtoolnix":
            header = "🎬 <b>عملیات سافت‌ساب (SoftSub) تکمیل شد!</b>"
        elif task.queue_type == "handbrake":
            header = "🔥 <b>عملیات هاردساب / انکود (HandBrake) تکمیل شد!</b>"
        else:
            header = f"✅ <b>عملیات {section_label} تکمیل شد!</b>"

        msg = (
            f"{header}\n"
            f"🆔 کد تسک: <code>{safe_id}</code>\n"
            f"📌 عنوان: <code>{safe_title}</code>"
            f"{details}"
        )
        logging.info(f"🔔 Sending completion notification for task #{task.task_id} ({task.queue_type})")
        await self._notify_admins(msg)

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

                await self._send_completion_notification(task, res)

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
                    f"❌ <b>خطا در پردازش!</b>\n"
                    f"🆔 کد: <code>{html.escape(str(task.task_id))}</code>\n"
                    f"⚙️ بخش: <code>{html.escape(task.queue_type.upper())}</code>\n"
                    f"📌 عنوان: <code>{html.escape(str(task.title))}</code>\n"
                    f"⚠️ جزئیات خطا:\n<code>{html.escape(str(e)[:400])}</code>"
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
            "upload": 1,
        }
        for q_type, num in counts.items():
            for idx in range(1, max(1, num) + 1):
                w = asyncio.create_task(self._worker_loop(q_type, idx))
                self._workers.append(w)

    def restart_workers_for_hot_reload(self):
        """
        جایگزینی تسک‌های _worker_loop قدیمی با کد جدید هنگام زدن کلید R:
        اگر تسکی در این لحظه در حال اجرا نباشد، ورکرهای قدیمی را بسته و ورکرهای جدید را با کد آپدیت‌شده استارت می‌زند.
        """
        any_running = any(
            t.running_asyncio_task is not None and not t.running_asyncio_task.done()
            for t in self.tasks.values()
        )
        if not any_running:
            for w in self._workers:
                if not w.done():
                    w.cancel()
            self._workers.clear()
            self.start_workers()
            logging.info("🔄 Queue workers restarted with updated notification logic.")

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