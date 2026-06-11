import asyncio
import tempfile
import unittest
from pathlib import Path

import alltuu_downloader as app


ALBUM_ID = "0123456789abcdef0123456789abcdef"


class DummyProgress:
    def __init__(self):
        self.advanced = 0

    def advance(self, _task_id):
        self.advanced += 1


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.headers = headers or {"Content-Type": "image/jpeg"}
        self.content = FakeContent([body])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, _url, timeout=None):
        self.calls += 1
        return self.response


class UrlValidationTests(unittest.TestCase):
    def test_accepts_alltuu_https_url(self):
        url = f"https://m.alltuu.com/album/{ALBUM_ID}/?menu=live"
        self.assertEqual(app.parse_alltuu_url(url), ALBUM_ID)

    def test_accepts_bare_album_id(self):
        self.assertEqual(app.parse_alltuu_url(ALBUM_ID.upper()), ALBUM_ID)

    def test_rejects_non_alltuu_host(self):
        url = f"https://example.com/album/{ALBUM_ID}/"
        with self.assertRaises(ValueError):
            app.parse_alltuu_url(url)

    def test_rejects_insecure_url(self):
        url = f"http://m.alltuu.com/album/{ALBUM_ID}/"
        with self.assertRaises(ValueError):
            app.parse_alltuu_url(url)


class FilesystemTests(unittest.TestCase):
    def test_sanitizes_traversal_and_reserved_names(self):
        self.assertEqual(app.sanitize_component("..", "fallback"), "fallback")
        self.assertEqual(app.sanitize_component("CON.txt", "fallback"), "_CON.txt")
        self.assertEqual(app.sanitize_component("a/b:c.jpg", "fallback"), "a_b_c.jpg")

    def test_output_directory_stays_below_base(self):
        with tempfile.TemporaryDirectory() as directory:
            output = app.resolve_output_directory(directory, "..", ALBUM_ID)
            self.assertEqual(output.parent, Path(directory).resolve())
            self.assertNotEqual(output, Path(directory).resolve().parent)

    def test_duplicate_names_are_deterministic(self):
        photos = [
            {"name": "same.jpg", "id": 10},
            {"name": "same.jpg", "id": 20},
            {"name": "unique.jpg", "id": 30},
        ]
        names = [name for _, name in app.prepare_download_items(photos)]
        self.assertEqual(names, ["same_10.jpg", "same_20.jpg", "unique.jpg"])


class RetryTests(unittest.TestCase):
    def test_retry_after_seconds(self):
        self.assertEqual(app.retry_after_seconds("3"), 3.0)
        self.assertIsNone(app.retry_after_seconds("not-a-date"))


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_and_finalizes_download(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            session = FakeSession(FakeResponse(body=b"\xff\xd8" + b"x" * 200))
            progress = DummyProgress()
            result = await app.download_single(
                session=session,
                photo={"originalUrl": "https://cdn.example/image.jpg"},
                target_name="image.jpg",
                output_dir=output,
                progress=progress,
                task_id=1,
                semaphore=asyncio.Semaphore(1),
                max_retries=1,
                request_timeout=10,
                request_delay=0,
            )
            self.assertEqual(result["status"], "downloaded")
            self.assertTrue((output / "image.jpg").exists())
            self.assertFalse((output / "image.jpg.part").exists())
            self.assertEqual(progress.advanced, 1)

    async def test_resume_skips_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "image.jpg").write_bytes(b"x" * 200)
            session = FakeSession(FakeResponse(body=b"unused"))
            progress = DummyProgress()
            result = await app.download_single(
                session=session,
                photo={"originalUrl": "https://cdn.example/image.jpg"},
                target_name="image.jpg",
                output_dir=output,
                progress=progress,
                task_id=1,
                semaphore=asyncio.Semaphore(1),
                max_retries=1,
                request_timeout=10,
                request_delay=0,
            )
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(session.calls, 0)


if __name__ == "__main__":
    unittest.main()
