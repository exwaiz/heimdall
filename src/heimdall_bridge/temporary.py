"""A bounded operational override; restarting after expiry always loads defaults."""
import asyncio
import time


class TemporaryAccessExpired(RuntimeError):
    pass


async def expiry_guard(deadline, expired):
    # asyncio sleep uses a monotonic clock: wall-clock rollback cannot extend
    # the already-running service's privilege window.
    await asyncio.sleep(max(0, deadline - time.time()))
    expired()
    raise TemporaryAccessExpired('temporary_access_expired_restart_required')
