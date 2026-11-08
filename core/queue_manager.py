import asyncio
import html
import inspect
import logging
import re
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg

# کش موقت مسیرها برای دکمه‌های شیشه‌ای نوتیفیکیشن‌ها (حفظ در برابر Hot-Reload)
NOTIF_PATH_REGISTRY: dict[str, str] = getattr(
    sys.modules.get(__name__), "NOTIF_PATH_REGISTRY", {}
)


def register_notif_path(path_obj: Path | str) -> str:
    token = uuid.uuid4().hex[:8]
    NOTIF_PATH_REGISTRY[token] = str(Path(path_obj).resolve())
    if len(NOTIF_PATH_REGISTRY) > 500:
        oldest_key = next(iter(NOTIF_PATH_REGISTRY))
        NOTIF_PATH_REGISTRY.pop(oldest_key, None)
    return token


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
    started_ts: float = 0.0
    finished_ts: float = 0.0
    error: str | None = None
    result: Any = None
    is_finished: bool = False
    is_cancelled: bool = False
    running_asyncio_task: asyncio.Task | None = None

    @property
    def elapsed_str(self) -> str:
        if self.started_ts <= 0:
            return "00:00"
        end_t = self.finished_ts if self.finished_ts > 0 else time.time()
        total_sec = max(0, int(end_t - self.started_ts))
        m, s = divmod(total_sec, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"


class QueueManager:
    QUEUE_TYPES = ("download", "mkvtoolnix", "handbrake", "upload")

    QUEUE_LABELS = {
        "mkvtoolnix": "🎬 سافت‌ساب (MKVToolNix)",
        "handbrake": "🔥 هاردساب / انکود (HandBrake)",
        "download": "📥 دانلود فایل",
        "upload": "☁️ آپلود به سرور (FTP)",
    }

    def __init__(self):
        self.tasks: dict[str, QueueTask] = {}
        self.pending_lists: dict[str, list[str]] = {q: [] for q in self.QUEUE_TYPES}
        self._events: dict[str, asyncio.Event] = {q: asyncio.Event() for q in self.QUEUE_TYPES}
        self._locks: dict[str, asyncio.Lock] = {q: asyncio.Lock() for q in self.QUEUE_TYPES}
        self._workers: list[asyncio.Task] = []
        self._worker_meta: dict[asyncio.Task, tuple[str, int]] = {}
        self._bot: Bot | None = None
        self._known_chats: set[int] = set()

    def _ensure_internal_structures(self):
        """تضمین وجود ساختارهای داخلی در صورت ارتقای کلاس هنگام Hot-Reload"""
        if not hasattr(self, "_worker_meta") or self._worker_meta is None:
            self._worker_meta = {}
        if not hasattr(self, "_known_chats") or self._known_chats is None:
            self._known_chats = set()
        for q in self.QUEUE_TYPES:
            if q not in self.pending_lists:
                self.pending_lists[q] = []
            if q not in self._events:
                self._events[q] = asyncio.Event()
            if q not in self._locks:
                self._locks[q] = asyncio.Lock()

    def set_bot(self, bot: Bot):
        self._bot = bot

    def register_chat(self, chat_id: int):
        self._ensure_internal_structures()
        if chat_id:
            self._known_chats.add(int(chat_id))

    def _get_target_chat_ids(self) -> list[int]:
        self._ensure_internal_structures()
        raw_admins = cfg.get("telegram", "admin_ids", [])
        chat_ids: set[int] = set(self._known_chats)

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
        self._ensure_internal_structures()
        if queue_type not in self.QUEUE_TYPES:
            queue_type = "download"

        # تضمین زنده بودن ورکرها قبل از افزودن تسک
        self.start_workers()

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
            self._events[task.queue_type].set()
            return True
        elif direction == "up" and idx > 0:
            p_list[idx - 1], p_list[idx] = p_list[idx], p_list[idx - 1]
            self._events[task.queue_type].set()
            return True
        elif direction == "down" and idx < len(p_list) - 1:
            p_list[idx + 1], p_list[idx] = p_list[idx], p_list[idx + 1]
            self._events[task.queue_type].set()
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

    def _rebuild_orphaned_pending_lists(self):
        """
        اگر به هر دلیلی تسکی وضعیت «در انتظار» دارد اما آیدی آن در pending_lists جا افتاده است،
        آن را خودکار به لیست نوبت برمی‌گرداند تا هرگز تسکی در حالت انتظار قفل نشود.
        """
        for tid, t in list(self.tasks.items()):
            if not t.is_finished and not t.is_cancelled and "انتظار" in str(t.status):
                q_type = t.queue_type if t.queue_type in self.QUEUE_TYPES else "download"
                if tid not in self.pending_lists[q_type]:
                    self.pending_lists[q_type].append(tid)
                    self._events[q_type].set()

    async def _pop_next_pending(self, queue_type: str) -> QueueTask | None:
        while True:
            self._ensure_internal_structures()
            async with self._locks[queue_type]:
                while self.pending_lists[queue_type]:
                    tid = self.pending_lists[queue_type].pop(0)
                    t = self.tasks.get(tid)
                    if t and not t.is_cancelled and not t.is_finished and t.running_asyncio_task is None:
                        # اگر هنوز تسک دیگری در لیست هست، Event را روشن نگه دار تا ورکر موازی بعدی هم بلافاصله بردارد
                        if self.pending_lists[queue_type]:
                            self._events[queue_type].set()
                        else:
                            self._events[queue_type].clear()
                        return t
                self._events[queue_type].clear()

            # به جای خوابیدن ابدی، هر ۱.۵ ثانیه بیدار شو و لیست انتظار را چک کن (ضد قفل شدن پس از Hot-Reload)
            try:
                await asyncio.wait_for(self._events[queue_type].wait(), timeout=1.5)
            except asyncio.TimeoutError:
                self._rebuild_orphaned_pending_lists()

    @staticmethod
    def _build_notification_kb(
        queue_type: str,
        target_file: Path | None = None,
        target_dir: Path | None = None
    ) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()

        folder_to_open = target_dir
        if folder_to_open is None and target_file is not None:
            folder_to_open = target_file.parent if target_file.is_file() else target_file

        if folder_to_open and folder_to_open.exists():
            dir_token = register_notif_path(folder_to_open)
            builder.row(
                InlineKeyboardButton(
                    text=f"📂 باز کردن پوشه ({folder_to_open.name[:22]})",
                    callback_data=f"ntf:open:{dir_token}"
                )
            )
            if queue_type == "download":
                auto_dir = folder_to_open.parent if re.match(r"^[Ss]\d{1,2}$", folder_to_open.parent.name) else folder_to_open
                auto_token = register_notif_path(auto_dir)
                builder.row(
                    InlineKeyboardButton(
                        text="🚀 اتوماسیون هوشمند روی این پوشه",
                        callback_data=f"ntf:sauto:{auto_token}"
                    )
                )

        if queue_type in ("mkvtoolnix", "handbrake") and target_file and target_file.is_file():
            file_token = register_notif_path(target_file)
            builder.row(
                InlineKeyboardButton(
                    text="☁️ آپلود سریع این فایل به سرور",
                    callback_data=f"ntf:up:{file_token}"
                )
            )

        builder.row(
            InlineKeyboardButton(
                text="📊 وضعیت صف‌ها",
                callback_data="menu:queues"
            ),
            InlineKeyboardButton(
                text="🗑 حذف این پیام",
                callback_data="ntf:del_msg"
            )
        )
        return builder.as_markup()

    async def _notify_admins(self, html_text: str, reply_markup: InlineKeyboardMarkup | None = None):
        if not self._bot:
            return

        if reply_markup is None:
            kb = InlineKeyboardBuilder()
            kb.row(
                InlineKeyboardButton(text="📊 وضعیت صف‌ها", callback_data="menu:queues"),
                InlineKeyboardButton(text="🗑 حذف این پیام", callback_data="ntf:del_msg"),
            )
            reply_markup = kb.as_markup()

        target_ids = self._get_target_chat_ids()
        for admin_id in target_ids:
            try:
                await self._bot.send_message(
                    chat_id=admin_id,
                    text=html_text[:4000],
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            except Exception as e:
                logging.warning(f"HTML notification fallback for {admin_id}: {e}")
                try:
                    plain = re.sub(r"<[^>]+>", "", html_text)
                    await self._bot.send_message(
                        chat_id=admin_id,
                        text=plain[:4000],
                        reply_markup=reply_markup,
                        disable_web_page_preview=True
                    )
                except Exception as e2:
                    logging.error(f"Failed to send notification to {admin_id}: {e2}")

    async def _send_completion_notification(self, task: QueueTask, res: Any):
        section_label = html.escape(self.QUEUE_LABELS.get(task.queue_type, task.queue_type.upper()))
        safe_title = html.escape(str(task.title))
        safe_id = html.escape(str(task.task_id))
        elapsed = task.elapsed_str

        local_ref_path: Path | None = None
        for arg in task.args:
            if isinstance(arg, Path) and arg.exists():
                local_ref_path = arg
            elif isinstance(arg, list) and arg and isinstance(arg[0], Path) and arg[0].exists():
                local_ref_path = arg[0]

        if task.queue_type == "upload":
            target_dir = local_ref_path.parent if (local_ref_path and local_ref_path.is_file()) else local_ref_path
            kb = self._build_notification_kb("upload", target_file=local_ref_path, target_dir=target_dir)

            if isinstance(res, list) and res:
                links_block = "\n".join([f"🔗 <code>{html.escape(str(u))}</code>" for u in res[:20]])
                extra = f"\n<i>... و {len(res) - 20} لینک دیگر</i>" if len(res) > 20 else ""
                await self._notify_admins(
                    f"☁️ <b>آپلود به سرور با موفقیت تکمیل شد!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🆔 کد تسک: <code>{safe_id}</code>  ▏  ⏱ زمان: <code>{elapsed}</code>\n"
                    f"📌 عنوان: <code>{safe_title}</code>\n\n"
                    f"📥 <b>لینک‌های مستقیم دانلود ({len(res)} فایل):</b>\n{links_block}{extra}",
                    reply_markup=kb
                )
            else:
                await self._notify_admins(
                    f"☁️ <b>آپلود با موفقیت انجام شد!</b>\n"
                    f"🆔 کد: <code>{safe_id}</code>  ▏  ⏱ زمان: <code>{elapsed}</code>\n"
                    f"📌 عنوان: <code>{safe_title}</code>",
                    reply_markup=kb
                )
            return

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

        if out_path is None:
            out_path = local_ref_path

        target_file_for_kb: Path | None = None
        target_dir_for_kb: Path | None = None
        details = ""

        if out_path and out_path.exists():
            if out_path.is_file():
                target_file_for_kb = out_path
                target_dir_for_kb = out_path.parent
                size_mb = out_path.stat().st_size / (1024 * 1024)
                details = (
                    f"\n📄 <b>نام فایل:</b>\n<code>{html.escape(out_path.name)}</code>\n"
                    f"📂 <b>محل ذخیره:</b>\n<code>{html.escape(str(out_path.parent))}</code>\n"
                    f"📦 <b>حجم نهایی:</b> <code>{size_mb:.2f} MB</code>"
                )
            elif out_path.is_dir():
                target_dir_for_kb = out_path
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
                    target_file_for_kb = newest
                    size_mb = newest.stat().st_size / (1024 * 1024)
                    extra_count = f" (+ {len(files_in_dir) - 1} فایل دیگر)" if len(files_in_dir) > 1 else ""
                    details = (
                        f"\n📄 <b>فایل دانلود شده:</b>\n<code>{html.escape(newest.name)}</code>{extra_count}\n"
                        f"📂 <b>پوشه مقصد:</b>\n<code>{html.escape(str(out_path))}</code>\n"
                        f"📦 <b>حجم فایل:</b> <code>{size_mb:.2f} MB</code>"
                    )
                else:
                    details = f"\n📂 <b>پوشه مقصد:</b>\n<code>{html.escape(str(out_path))}</code>"

        if task.queue_type == "download":
            header = "📥 <b>دانلود فایل با موفقیت به پایان رسید!</b>"
        elif task.queue_type == "mkvtoolnix":
            header = "🎬 <b>عملیات سافت‌ساب (SoftSub) تکمیل شد!</b>"
        elif task.queue_type == "handbrake":
            header = "🔥 <b>عملیات هاردساب / انکود (HandBrake) تکمیل شد!</b>"
        else:
            header = f"✅ <b>عملیات {section_label} تکمیل شد!</b>"

        kb = self._build_notification_kb(
            task.queue_type,
            target_file=target_file_for_kb,
            target_dir=target_dir_for_kb
        )
        msg = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 کد تسک: <code>{safe_id}</code>  ▏  ⏱ زمان کل: <code>{elapsed}</code>\n"
            f"📌 عنوان: <code>{safe_title}</code>"
            f"{details}"
        )
        await self._notify_admins(msg, reply_markup=kb)

    async def _worker_loop(self, queue_type: str, worker_idx: int):
        while True:
            task = await self._pop_next_pending(queue_type)
            if not task:
                continue

            task.started_ts = time.time()
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

                task.finished_ts = time.time()
                task.result = res
                task.status = "✅ تکمیل شد"
                task.progress = "100%"
                task.is_finished = True

                await self._send_completion_notification(task, res)

            except asyncio.CancelledError:
                task.finished_ts = time.time()
                task.status = "🛑 لغو شد"
                task.progress = "متوقف شده توسط کاربر"
                task.is_finished = True
            except Exception as e:
                task.finished_ts = time.time()
                logging.error(f"Task {task.task_id} failed:\n{traceback.format_exc()}")
                task.error = str(e)
                task.status = "❌ خطا"
                task.progress = "متوقف شده به دلیل خطا"
                task.is_finished = True
                await self._notify_admins(
                    f"❌ <b>خطا در پردازش!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🆔 کد: <code>{html.escape(str(task.task_id))}</code>  ▏  ⏱ زمان: <code>{task.elapsed_str}</code>\n"
                    f"⚙️ بخش: <code>{html.escape(task.queue_type.upper())}</code>\n"
                    f"📌 عنوان: <code>{html.escape(str(task.title))}</code>\n"
                    f"⚠️ جزئیات خطا:\n<code>{html.escape(str(e)[:400])}</code>"
                )
            finally:
                task.running_asyncio_task = None

    async def _worker_supervisor(self, queue_type: str, worker_idx: int):
        while True:
            try:
                await self._worker_loop(queue_type, worker_idx)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Worker %s/%s crashed; restarting in 1 second.", queue_type, worker_idx)
                await asyncio.sleep(1)

    def start_workers(self):
        """استارت تضمینی ورکرهای هر ۴ صف و بیدار کردن فوری تسک‌های در انتظار"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        self._ensure_internal_structures()
        self._workers = [w for w in self._workers if not w.done()]
        self._worker_meta = {w: meta for w, meta in self._worker_meta.items() if not w.done()}

        # اگر از قبلِ Hot-Reload ورکری بدون متادیتا مانده و هیچ تسکی در حال اجرا نیست، آن را پاک کن تا ورکر تمیز ساخته شود
        untracked = [w for w in self._workers if w not in self._worker_meta]
        any_running = any(
            t.running_asyncio_task is not None and not t.running_asyncio_task.done()
            for t in self.tasks.values()
        )
        if untracked and not any_running:
            for w in untracked:
                w.cancel()
            self._workers = [w for w in self._workers if w in self._worker_meta]

        conc = cfg.get("concurrency") or {}

        def _count(key: str, default: int) -> int:
            try:
                return max(1, int(conc.get(key, default)))
            except (TypeError, ValueError):
                return default

        counts = {
            "handbrake": _count("handbrake_workers", 2),
            "mkvtoolnix": _count("mkvtoolnix_workers", 1),
            "download": _count("download_workers", 1),
            "upload": _count("upload_workers", 1),
        }

        for q_type, num in counts.items():
            active = sum(
                1 for w in self._workers
                if self._worker_meta.get(w, (None, None))[0] == q_type and not w.done()
            )
            for idx in range(active + 1, num + 1):
                worker = loop.create_task(self._worker_supervisor(q_type, idx))
                self._workers.append(worker)
                self._worker_meta[worker] = (q_type, idx)
                logging.info("🚀 Started queue worker %s/%s", q_type, idx)

        # بیدار کردن فوری تمام صف‌هایی که تسک منتظر دارند
        self._rebuild_orphaned_pending_lists()
        for q_type in self.QUEUE_TYPES:
            if self.pending_lists.get(q_type):
                self._events[q_type].set()

    def restart_workers_for_hot_reload(self):
        self._ensure_internal_structures()
        any_running = any(
            t.running_asyncio_task is not None and not t.running_asyncio_task.done()
            for t in self.tasks.values()
        )
        if not any_running:
            for worker in self._workers:
                if not worker.done():
                    worker.cancel()
            self._workers.clear()
            self._worker_meta.clear()
            logging.info("🔄 Queue workers restarted cleanly with updated code.")

        self.start_workers()

    def get_queue_summary(self) -> list[QueueTask]:
        # هر بار که کاربر صفحه صف را باز می‌کند، از زنده بودن ورکرها و بیدار بودن صف‌ها مطمئن شو
        self.start_workers()
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


# حفظ تک‌نمونه (Singleton) هنگام Hot-Reload:
# اگر از قبل queue_engine در حافظه وجود دارد، همان شیء قبلی را نگه دار و فقط کلاسش را به کلاس جدید ارتقا بده!
_prev_mod = sys.modules.get(__name__)
_existing_engine = getattr(_prev_mod, "queue_engine", None) if _prev_mod else None

if _existing_engine is not None:
    _existing_engine.__class__ = QueueManager
    _existing_engine._ensure_internal_structures()
    queue_engine = _existing_engine
else:
    queue_engine = QueueManager()