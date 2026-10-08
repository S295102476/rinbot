"""Avatar networking is always mocked; no running bot or public QQ requests."""
import asyncio
import importlib
from io import BytesIO
from pathlib import Path
import sys
import threading
import types

import httpx
from PIL import Image
import pytest


package = types.ModuleType("_minigame_avatar_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
avatars = importlib.import_module(package.__name__ + ".avatars")
AvatarCache = avatars.AvatarCache


def picture(fmt="PNG", size=(80, 60), color=(30, 130, 220)):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=fmt)
    return output.getvalue()


class ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.yielded += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def test_constructed_without_running_loop():
    cache = AvatarCache()
    assert cache._client is None and cache._slots is None
    assert cache.ttl == 86400 and cache.max_users == 256
    assert cache.retry_seconds >= 60 and cache.timeout == 3
    asyncio.run(cache.close())


def test_fixed_url_valid_ids_and_png_output():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=picture("JPEG"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = AvatarCache(client=client)
            data = await cache.get_many([123, 123, True, 0, -3, "456", 2**80, None])
            assert set(data) == {123}
            assert str(requests[0].url) == "https://q1.qlogo.cn/g?b=qq&nk=123&s=640"
            with Image.open(BytesIO(data[123])) as image:
                assert image.size == (256, 256) and image.format == "PNG"
                assert not image.getexif()
            assert await cache.get_many([123]) == data
            assert len(requests) == 1
            await cache.close()
            assert not client.is_closed  # Borrowed clients retain their owner.

    asyncio.run(scenario())


def test_both_players_fetched_concurrently():
    async def scenario():
        seen = []
        both = asyncio.Event()

        async def handler(request):
            seen.append(int(request.url.params["nk"]))
            if len(seen) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), .5)
            return httpx.Response(200, content=picture())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = AvatarCache(client=client)
            assert set(await cache.get_many([11, 22])) == {11, 22}
            assert sorted(seen) == [11, 22]
            await cache.close()

    asyncio.run(scenario())


def test_same_user_shared_and_one_reader_cancel_does_not_cancel_download():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, content=picture())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = AvatarCache(client=client)
            first = asyncio.create_task(cache.get_many([123]))
            await entered.wait()
            second = asyncio.create_task(cache.get_many([123]))
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not cache._inflight[123].cancelled()
            release.set()
            assert set(await second) == {123}
            assert len(requests) == 1
            assert set(await cache.get_many([123])) == {123}
            await cache.close()

    asyncio.run(scenario())


def test_ttl_and_lru_capacity():
    calls = []
    now = [100.0]

    def handler(request):
        calls.append(int(request.url.params["nk"]))
        return httpx.Response(200, content=picture())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = AvatarCache(client=client, clock=lambda: now[0], max_users=2, ttl=10)
            await cache.get_many([1, 2])
            await cache.get_many([1])
            await cache.get_many([3])
            assert list(cache._entries) == [1, 3]
            await cache.get_many([2])
            assert calls.count(2) == 2 and len(cache._entries) == 2
            now[0] = 109.9
            await cache.get_many([2])
            assert calls.count(2) == 2
            now[0] = 110.0
            await cache.get_many([2])
            assert calls.count(2) == 3
            await cache.close()

    asyncio.run(scenario())


def test_failure_backoff_and_bounded_negative_cache():
    now = [0.0]
    calls = []

    def handler(request):
        calls.append(int(request.url.params["nk"]))
        return httpx.Response(503, content=b"unavailable")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = AvatarCache(client=client, clock=lambda: now[0], max_users=2,
                                retry_seconds=1)
            assert await cache.get_many([1]) == {}
            now[0] = 59.9
            assert await cache.get_many([1]) == {}
            assert calls == [1]
            now[0] = 60
            assert await cache.get_many([1]) == {}
            assert calls == [1, 1]
            await cache.get_many([2, 3])
            assert len(cache._entries) == 2
            assert all(entry.data is None for entry in cache._entries.values())
            await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [b"", b"not an image", picture("BMP")],
                         ids=["empty", "invalid", "unsupported-bmp"])
def test_invalid_images_fail_closed(body):
    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        async with httpx.AsyncClient(transport=transport) as client:
            cache = AvatarCache(client=client)
            assert await cache.get_many([1]) == {}
            await cache.close()
    asyncio.run(scenario())


def test_pixel_limit_before_expensive_decode():
    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=picture(size=(11, 10))))
        async with httpx.AsyncClient(transport=transport) as client:
            cache = AvatarCache(client=client, max_pixels=100)
            assert await cache.get_many([1]) == {}
            await cache.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("declared", [True, False])
def test_oversized_declared_and_streamed_payloads(declared):
    async def scenario():
        stream = ChunkedBody([b"x" * 65536] * 8)
        headers = {"Content-Length": str(8 * 65536)} if declared else {}
        transport = httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=stream))
        async with httpx.AsyncClient(transport=transport) as client:
            cache = AvatarCache(client=client, max_bytes=70000)
            assert await cache.get_many([1]) == {}
            assert stream.closed
            assert stream.yielded == (0 if declared else 2)
            await cache.close()
    asyncio.run(scenario())


def test_timeout_covers_total_stream_not_only_individual_reads():
    async def scenario():
        stream = ChunkedBody([b"x"] * 20, delay=.01)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        async with httpx.AsyncClient(transport=transport) as client:
            cache = AvatarCache(client=client, timeout=.04)
            assert await asyncio.wait_for(cache.get_many([1]), .5) == {}
            assert stream.closed and stream.yielded < 20
            assert cache._entries[1].data is None
            assert cache._slots._value == 4
            await cache.close()
    asyncio.run(scenario())


def test_timeout_includes_waiting_for_all_four_occupied_slots():
    calls = []
    now = [0.0]

    def handler(request):
        calls.append(int(request.url.params["nk"]))
        return httpx.Response(200, content=picture())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = AvatarCache(client=client, clock=lambda: now[0], timeout=.04)
            cache._slots = asyncio.Semaphore(4)
            for _ in range(4):
                await cache._slots.acquire()
            assert await asyncio.wait_for(cache.get_many([1]), .5) == {}
            assert calls == []
            assert cache._entries[1].data is None and not cache._inflight
            # A waiter timing out must not release somebody else's permit.
            assert cache._slots._value == 0
            cache._slots.release()
            assert await cache.get_many([1]) == {}  # Negative cache still applies.
            assert calls == []
            now[0] = 60.0
            assert set(await cache.get_many([1])) == {1}
            assert calls == [1] and cache._slots._value == 1
            for _ in range(3):
                cache._slots.release()
            await cache.close()

    asyncio.run(scenario())


def test_redirect_not_followed_even_with_borrowed_client_redirects_enabled():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            cache = AvatarCache(client=client)
            assert await cache.get_many([1]) == {}
            assert len(requests) == 1
            await cache.close()
    asyncio.run(scenario())


def test_image_decoding_is_off_event_loop(monkeypatch):
    event_thread = threading.get_ident()
    actual_decode = avatars._decode_avatar
    threads = []

    def recording_decode(*args):
        threads.append(threading.get_ident())
        return actual_decode(*args)

    monkeypatch.setattr(avatars, "_decode_avatar", recording_decode)

    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=picture()))
        async with httpx.AsyncClient(transport=transport) as client:
            cache = AvatarCache(client=client)
            assert set(await cache.get_many([1])) == {1}
            assert threads and all(thread != event_thread for thread in threads)
            await cache.close()
    asyncio.run(scenario())


def test_first_frame_and_exif_orientation():
    output = BytesIO()
    red = Image.new("RGB", (20, 20), "red")
    blue = Image.new("RGB", (20, 20), "blue")
    red.save(output, format="GIF", save_all=True, append_images=[blue], duration=100, loop=0)
    with Image.open(BytesIO(avatars._decode_avatar(output.getvalue()))) as decoded:
        assert decoded.getpixel((128, 128))[:3] == (255, 0, 0)

    output = BytesIO()
    split = Image.new("RGB", (80, 40), "red")
    split.paste("blue", (40, 0, 80, 40))
    exif = Image.Exif()
    exif[274] = 6  # 90 degrees clockwise: left red half becomes upper half.
    split.save(output, format="JPEG", exif=exif, quality=95)
    with Image.open(BytesIO(avatars._decode_avatar(output.getvalue()))) as decoded:
        assert decoded.getpixel((128, 20))[0] > 200
        assert decoded.getpixel((128, 236))[2] > 200
        assert not decoded.getexif()


def test_inflight_limit_and_large_iterable_are_bounded():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def handler(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, content=picture())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = AvatarCache(client=client, max_users=1)
            pending = asyncio.create_task(cache.get_many([1]))
            await entered.wait()
            assert await cache.get_many(range(2, 10**9)) == {}
            assert len(cache._inflight) == 1 and len(calls) == 1
            release.set()
            await pending
            await cache.close()
    asyncio.run(scenario())


def test_close_cancels_pending_fetch_and_closes_owned_client():
    async def scenario():
        entered = asyncio.Event()

        async def handler(request):
            entered.set()
            await asyncio.Event().wait()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cache = AvatarCache()
        cache._client = client  # Exercise the lazy-owned client's cleanup path.
        pending = asyncio.create_task(cache.get_many([1]))
        await entered.wait()
        await cache.close()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert client.is_closed
        assert not cache._entries and not cache._inflight
        assert await cache.get_many([1]) == {}
        await cache.close()
    asyncio.run(scenario())
