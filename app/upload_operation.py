"""Wait for owned upload work before releasing resources on cancellation."""
import asyncio


async def await_upload_operation(operation, *, on_cancel=None):
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
    except BaseException:
        if cancelled is not None:
            raise cancelled
        raise
    return result, cancelled
