"""Small, independent QQ-avatar cache for game illustrations, never Agent input.

Network work is asynchronous; image decoding is moved off the event loop.  The
cache is deliberately memory-only and may be constructed before a loop exists.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from itertools import islice
import logging
import time
from typing import Callable, Iterable

import httpx
from PIL import Image, ImageOps


logger = logging.getLogger(__name__)
AVATAR_TTL = 24 * 60 * 60
MAX_USERS = 256
RETRY_SECONDS = 60
DOWNLOAD_TIMEOUT = 3.0
MAX_BYTES = 3 * 1024 * 1024
MAX_PIXELS = 16_000_000
AVATAR_SIZE = 256


@dataclass(frozen=True)
class _Entry:
    data: bytes | None
    expires_at: float


def _decode_avatar(data: bytes, max_pixels: int = MAX_PIXELS) -> bytes:
    """Validate, orient and center-crop a first frame to a metadata-free PNG."""
    with Image.open(BytesIO(data)) as source:
        if source.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
            raise ValueError("Unsupported avatar format")
        if source.width <= 0 or source.height <= 0 or source.width * source.height > max_pixels:
            raise ValueError("Avatar pixel limit exceeded")
        source.seek(0)
        oriented = ImageOps.exif_transpose(source)
        scaled = ImageOps.fit(oriented.convert("RGBA"), (AVATAR_SIZE, AVATAR_SIZE),
                              method=Image.Resampling.LANCZOS)
        # Copy pixels only: do not carry EXIF or arbitrary upstream PNG metadata.
        output_image = Image.new("RGBA", scaled.size)
        output_image.paste(scaled)
        output = BytesIO()
        output_image.save(output, format="PNG")
        return output.getvalue()


class AvatarCache:
    """Bounded LRU with shared in-flight fetches and negative-result backoff.

    ``client`` and ``clock`` are optional injection points for offline tests. An
    injected client stays owned by its caller; a lazily-created client is closed
    by ``close``. Cancellation of one reader never cancels another reader's fetch.
    """

    def __init__(self, *, client: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 ttl: float = AVATAR_TTL, max_users: int = MAX_USERS,
                 retry_seconds: float = RETRY_SECONDS,
                 timeout: float = DOWNLOAD_TIMEOUT,
                 max_bytes: int = MAX_BYTES, max_pixels: int = MAX_PIXELS):
        self._client = client
        self._owns_client = client is None
        self._clock = clock
        self.ttl = max(1.0, float(ttl))
        self.max_users = max(1, min(MAX_USERS, int(max_users)))
        self.retry_seconds = max(RETRY_SECONDS, float(retry_seconds))
        self.timeout = max(0.001, min(DOWNLOAD_TIMEOUT, float(timeout)))
        self.max_bytes = max(1, min(MAX_BYTES, int(max_bytes)))
        self.max_pixels = max(1, min(MAX_PIXELS, int(max_pixels)))
        self._entries: OrderedDict[int, _Entry] = OrderedDict()
        self._inflight: dict[int, asyncio.Task[bytes | None]] = {}
        self._slots: asyncio.Semaphore | None = None
        self._closed = False

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False, trust_env=False,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                headers={"Accept": "image/*", "Accept-Encoding": "identity"},
            )
        return self._client

    async def _download(self, user_id: int) -> bytes:
        # The host/path are constants and the identifier has been integer-checked.
        url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        async with self._http_client().stream("GET", url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError("Avatar HTTP request failed")
            declared_size = response.headers.get("Content-Length")
            if declared_size is not None and int(declared_size) > self.max_bytes:
                raise ValueError("Avatar byte limit exceeded")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                if len(body) + len(chunk) > self.max_bytes:
                    raise ValueError("Avatar byte limit exceeded")
                body.extend(chunk)
            if not body:
                raise ValueError("Empty avatar response")
            return bytes(body)

    def _remember(self, user_id: int, data: bytes | None) -> None:
        if self._closed:
            return
        lifetime = self.ttl if data is not None else self.retry_seconds
        self._entries[user_id] = _Entry(data, self._clock() + lifetime)
        self._entries.move_to_end(user_id)
        while len(self._entries) > self.max_users:
            self._entries.popitem(last=False)

    async def _fetch(self, user_id: int) -> bytes | None:
        acquired = False
        try:
            if self._slots is None:
                self._slots = asyncio.Semaphore(4)
            # Queueing and all download chunks share one deadline. Startup
            # recovery in many groups must not queue an unbounded UI wait.
            async with asyncio.timeout(self.timeout):
                await self._slots.acquire()
                acquired = True
                body = await self._download(user_id)
            # Keep the permit while decoding, but do not try to interrupt Pillow
            # in another thread merely because the network deadline has elapsed.
            data = await asyncio.to_thread(_decode_avatar, body, self.max_pixels)
            self._remember(user_id, data)
            return data
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not log upstream response bodies, URLs or credentials.
            logger.debug("[minigame_avatar] user=%s status=unavailable error=%s",
                         user_id, type(error).__name__)
            self._remember(user_id, None)
            return None
        finally:
            if acquired:
                self._slots.release()

    def _finished(self, user_id: int, task: asyncio.Task) -> None:
        if self._inflight.get(user_id) is task:
            self._inflight.pop(user_id, None)
        # Fetches can outlive their only caller; consume any unexpected exception.
        if not task.cancelled():
            task.exception()

    async def _get(self, user_id: int) -> bytes | None:
        if self._closed:
            return None
        cached = self._entries.get(user_id)
        if cached is not None:
            if cached.expires_at > self._clock():
                self._entries.move_to_end(user_id)
                return cached.data
            self._entries.pop(user_id, None)
        task = self._inflight.get(user_id)
        if task is None:
            if len(self._inflight) >= self.max_users:
                return None
            task = asyncio.create_task(self._fetch(user_id))
            self._inflight[user_id] = task
            task.add_done_callback(lambda finished: self._finished(user_id, finished))
        return await asyncio.shield(task)

    async def get_many(self, user_ids: Iterable[int]) -> dict[int, bytes]:
        """Return only successfully loaded users; missing entries mean fallback.

        Games normally supply at most two players. Bound even an accidental large
        or infinite iterable rather than spawning an unbounded number of fetches.
        """
        if self._closed:
            return {}
        unique: dict[int, None] = {}
        try:
            for user_id in islice(user_ids, self.max_users * 2):
                if type(user_id) is int and 0 < user_id <= 2**63 - 1:
                    unique[user_id] = None
                    if len(unique) == self.max_users:
                        break
        except TypeError:
            return {}
        ids = list(unique)
        results = await asyncio.gather(*(self._get(user_id) for user_id in ids))
        return {user_id: data for user_id, data in zip(ids, results) if data is not None}

    async def close(self) -> None:
        """Stop pending network work and release owned sockets and image bytes."""
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._entries.clear()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
