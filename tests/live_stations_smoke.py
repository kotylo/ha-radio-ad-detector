"""Check live stations through the local relay with python tests/live_stations_smoke.py."""

import asyncio
from pathlib import Path
import subprocess
import sys
import unittest

from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "radio_ad_detector"))
from const import DEFAULT_PLAYBACK_BUFFER
from radio_audio.engine import create_app
from radio_audio.stream_urls import stream_signature
from test_detector import FakeModel


STATIONS = {
    "All Classical Radio": "https://allclassical.streamguys1.com/ac128kmp3",
    "WRTI Classical": "https://wrti-live.streamguys1.com/classical-mp3",
    "WWFM The Classical Network": "https://wwfm.streamguys1.com/live-mp3",
    "Iowa Public Radio Classical": "https://classical-stream.iowapublicradio.org/Classical.mp3",
}


class LiveStations(unittest.IsolatedAsyncioTestCase):
    async def test_station_playback(self):
        for station, url in STATIONS.items():
            for output_format, content_type in (("mp3", "audio/mpeg"), ("wav", "audio/wav")):
                with self.subTest(station=station, output_format=output_format):
                    await self.check_stream(URL(url), output_format, content_type)
                    print(f"{station} ({output_format.upper()}): OK", flush=True)

    async def check_stream(self, url, output_format, content_type):
        secret = "live-stations-test"
        slot = 1
        app = create_app(
            {"signing_secret": secret, "allow_custom_urls": True},
            FakeModel(),
            get_playback_buffer=lambda: DEFAULT_PLAYBACK_BUFFER,
        )
        radio = app["radio"]
        params = {
            "url": str(url),
            "format": output_format,
            "slot": slot,
            "sig": stream_signature(secret, url, output_format=output_format, slot=slot),
        }

        async with TestClient(TestServer(app, handler_cancellation=True)) as client:
            try:
                async with asyncio.timeout(30):
                    async with client.get("/stream", params=params) as response:
                        self.assertEqual(response.status, 200, radio.last_error)
                        self.assertEqual(response.headers.get("Content-Type"), content_type)
                        audio = await response.content.readexactly(32768)
                        while not radio.snapshot()["slots"][slot]["available"]:
                            self.assertIsNone(radio.last_error)
                            await asyncio.sleep(0.05)
            except TimeoutError:
                self.fail(f"Timed out waiting for relay audio and analysis from {url}")

        decoded = await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", output_format,
                "-i", "pipe:0", "-t", "0.25", "-ac", "1", "-ar", "16000",
                "-f", "s16le", "pipe:1",
            ],
            input=audio,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode(errors="replace"))
        self.assertGreaterEqual(len(decoded.stdout), 3200, "Expected at least 0.1 seconds of decoded audio")


if __name__ == "__main__":
    unittest.main(verbosity=2)
