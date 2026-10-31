"""Run blocking watch work off-loop, settling it before cancellation cleanup."""
import asyncio


async def call(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A worker cannot be cancelled. In particular, mint must finish before
        # its caller revokes the turn, otherwise a late mint can leak a token.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # consume an error after the caller was cancelled
        raise
