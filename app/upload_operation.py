"""Wait for owned upload work before releasing resources on cancellation."""
import asyncio


async def await_upload_operation(operation, *, on_cancel=None, prefer_cancellation=False):
    """Finish admitted I/O before reporting cancellation to its owner."""
    task = asyncio.create_task(operation)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = error
            if on_cancel is not None:
                on_cancel()
        except Exception:
            break
    try:
        result = task.result()
    except BaseException as error:
        if cancelled is not None:
            if prefer_cancellation:
                raise cancelled
            raise error from cancelled
        raise
    return result, cancelled
