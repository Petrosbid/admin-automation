
import asyncio


async def terminate_process(proc, timeout: int = 5):
    """
    توقف امن یک subprocess.
    ابتدا terminate و در صورت پاسخ ندادن kill اجرا می‌شود.
    """
    if proc is None or proc.returncode is not None:
        return

    try:
        proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)

    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            pass

        try:
            await proc.wait()
        except Exception:
            pass


async def communicate_cancellable(proc):
    """
    دریافت خروجی subprocess با پشتیبانی از لغو عملیات.
    در صورت Cancel شدن task، پردازش فرزند نیز متوقف می‌شود.
    """
    try:
        return await proc.communicate()

    except asyncio.CancelledError:
        await terminate_process(proc)
        raise