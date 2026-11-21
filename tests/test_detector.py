import asyncio
from array import array
import io
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import wave
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "radio_ad_detector"))
from radio_audio.detection import Detector


class Decisions(unittest.TestCase):
    def test_speech_with_music_triggers_after_confirmation(self):
        d = Detector({})
        self.assertFalse(d.update(0.9, 0.8, 0.1, 0.1).ad_detected)
        self.assertTrue(d.update(0.9, 0.8, 0.1, 0.1).ad_detected)

    def test_singing_and_rap_do_not_trigger(self):
        d = Detector({})
        for _ in range(20):
            self.assertFalse(d.update(0.65, 0.8, 0.85, 0.1).ad_detected)

    def test_one_speech_spike_does_not_trigger(self):
        d = Detector({})
        for _ in range(10):
            self.assertFalse(d.update(0.9, 0.1, 0.1, 0.1).ad_detected)
            self.assertFalse(d.update(0.1, 0.9, 0.1, 0.1).ad_detected)

    def test_non_music_requires_longer_confirmation(self):
        d = Detector({})
        for _ in range(5):
            self.assertFalse(d.update(0.1, 0.01, 0.01, 0.1).ad_detected)
        self.assertTrue(d.update(0.1, 0.01, 0.01, 0.1).ad_detected)

    def test_silence_never_triggers(self):
        d = Detector({})
        for _ in range(20):
            self.assertFalse(d.update(0, 0, 0, 0).ad_detected)

    def test_alarm_holds_until_music_is_confirmed(self):
        d = Detector({})
        for _ in range(2):
            d.update(0.9, 0.1, 0.1, 0.1)
        for _ in range(3):
            self.assertTrue(d.update(0.05, 0.95, 0.1, 0.1).ad_detected)
        self.assertFalse(d.update(0.05, 0.95, 0.1, 0.1).ad_detected)

    def test_invalid_settings_and_scores(self):
        with self.assertRaises(ValueError):
            Detector({"speech_windows": 0})
        with self.assertRaises(ValueError):
            Detector({}).update(float("nan"), 0.5, 0, 0.1)


try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from radio_audio.engine import Playback, create_app
    from radio_audio.stream_urls import StreamResolver, validate_stream_url, stream_label, stream_signature
    from yarl import URL
    HAS_SERVER = True
except ImportError:
    HAS_SERVER = False


class FakeModel:
    samples = 15600

    def __init__(self):
        self.calls = 0
        self.first_pcm = None

    def classify(self, pcm):
        self.calls += 1
        if self.first_pcm is None:
            self.first_pcm = pcm
        assert len(pcm) == self.samples * 2
        return {"speech": 0.95, "music": 0.6, "singing": 0.01, "rms": 0.1, "top_classes": []}


@unittest.skipUnless(HAS_SERVER and shutil.which("ffmpeg"), "Requires aiohttp and ffmpeg")
class Relay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.audio = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=4", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1",
        ])
        self.upstream_calls = 0
        self.headers = []
        self.queries = []

        async def upstream(request):
            self.upstream_calls += 1
            self.headers.append(dict(request.headers))
            self.queries.append(dict(request.query))
            headers = {"Content-Type": "audio/mpeg", "icy-br": "128"}
            if "metaint" in request.query:
                headers["icy-metaint"] = request.query["metaint"]
            response = web.StreamResponse(headers=headers)
            await response.prepare(request)
            try:
                while True:
                    for i in range(0, len(self.audio), 4096):
                        await response.write(self.audio[i:i+4096])
                        await asyncio.sleep(0.02)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            return response

        async def bad(request):
            return web.Response(text="not audio", content_type=request.query.get("type", "text/plain"))

        async def burst(request):
            response = web.StreamResponse(headers={"Content-Type": "audio/mpeg"})
            await response.prepare(request)
            await response.write(self.audio)
            await asyncio.sleep(1)
            return response

        async def long_burst(request):
            return web.Response(body=self.audio * 8, content_type="audio/mpeg")

        source = web.Application()
        source.router.add_get("/radio", upstream)
        source.router.add_get("/bad", bad)
        source.router.add_get("/burst", burst)
        source.router.add_get("/long_burst", long_burst)
        self.source = TestServer(source)
        await self.source.start_server()
        self.model = FakeModel()
        self.playback_buffer = 0.0
        self.app = create_app(
            {"allow_private_streams": True, "streams": {"a": str(self.source.make_url("/radio")), "bad": str(self.source.make_url("/bad")), "burst": str(self.source.make_url("/burst"))}},
            self.model,
            get_playback_buffer=lambda: self.playback_buffer,
        )
        self.client = TestClient(TestServer(self.app, handler_cancellation=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.source.close()

    async def wait_for(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0.03)
        self.fail("Timed out waiting for relay state")

    async def test_exact_bytes_one_connection_and_detection(self):
        self.app["radio"].config["startup_buffer_seconds"] = 0.5
        began = time.monotonic()
        response = await self.client.get("/stream/a.mp3")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.content.readexactly(len(self.audio)), self.audio)
        # After the startup credit, delivery must settle at 128 kb/s.
        self.assertGreater(time.monotonic() - began, len(self.audio) / 16000 - 0.5 - 0.3)
        await self.wait_for(lambda: self.app["radio"].snapshot()["ad_detected"] is True)
        self.assertEqual(self.upstream_calls, 1)
        self.assertEqual(self.headers[0]["Icy-MetaData"], "0")
        self.assertGreaterEqual(self.model.calls, 2)
        response.close()
        await self.wait_for(lambda: not self.app["radio"].sessions)
        state = self.app["radio"].snapshot()
        self.assertFalse(state["available"])
        self.assertIsNone(state["ad_detected"])

    async def test_startup_fills_player_buffer_without_rushing_analysis(self):
        began = time.monotonic()
        response = await self.client.get("/stream/a.mp3")
        self.assertEqual(await response.content.readexactly(len(self.audio)), self.audio)
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertLessEqual(self.model.calls, 1)
        await self.wait_for(lambda: self.app["radio"].snapshot()["ad_detected"] is True)
        self.assertGreater(time.monotonic() - began, 1)
        self.assertGreaterEqual(self.model.calls, 2)
        self.assertEqual(self.upstream_calls, 1)
        response.close()
        await self.wait_for(lambda: not self.app["radio"].sessions)

    async def test_zero_startup_buffer_keeps_realtime_delivery(self):
        self.app["radio"].config["startup_buffer_seconds"] = 0
        began = time.monotonic()
        response = await self.client.get("/stream/a.mp3")
        self.assertEqual(await response.content.readexactly(16384), self.audio[:16384])
        self.assertGreater(time.monotonic() - began, 0.8)
        response.close()

    async def test_playback_buffer_gives_analysis_a_head_start_in_both_formats(self):
        self.playback_buffer = 2.0
        radio = self.app["radio"]
        for output_format, expected in (("mp3", self.audio[:4096]), ("wav", b"RIFF")):
            with self.subTest(output_format=output_format):
                began = time.monotonic()
                async with self.client.get("/stream/a.mp3", params={"format": output_format}) as response:
                    self.assertEqual(response.status, 200)
                    await self.wait_for(lambda: radio.snapshot()["ad_detected"] is True)
                    self.assertEqual(response.content.read_nowait(), b"")
                    self.assertEqual(await response.content.readexactly(len(expected)), expected)
                    self.assertGreaterEqual(time.monotonic() - began, 2)
                await self.wait_for(lambda: not radio.sessions)
        self.assertEqual(self.upstream_calls, 2)

    async def test_playback_buffer_persists_beyond_the_startup_burst(self):
        self.playback_buffer = 0.75
        self.app["radio"].config["startup_buffer_seconds"] = 0.5
        began = time.monotonic()
        async with self.client.get("/stream/a.mp3") as response:
            self.assertEqual(await response.content.readexactly(len(self.audio)), self.audio)
            # Allow for the last chunk being released at the start of its audio.
            self.assertGreater(time.monotonic() - began, len(self.audio) / 16000 - 0.5 + 0.75 - 0.25)

    async def test_default_startup_burst_is_bounded(self):
        url = str(self.source.make_url("/long_burst"))
        began = time.monotonic()
        response = await self.client.get("/stream", params={"url": url})
        # Fill a 15-second receiver buffer immediately, but never send the
        # station's entire burst or run the classifier at network speed.
        audio = self.audio * 8
        self.assertEqual(await response.content.readexactly(240000), audio[:240000])
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertLessEqual(self.model.calls, 1)
        await response.content.readexactly(16000)
        self.assertGreater(time.monotonic() - began, 0.7)
        response.close()
        await self.wait_for(lambda: not self.app["radio"].sessions)

    async def test_invalid_startup_buffer_is_rejected(self):
        for value in (-1, 31, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                create_app({"startup_buffer_seconds": value}, self.model)

    async def test_head_and_unknown_station_do_not_open_source(self):
        response = await self.client.head("/stream/a.mp3")
        self.assertEqual(response.status, 200)
        response = await self.client.get("/stream/unknown.mp3")
        self.assertEqual(response.status, 404)
        self.assertEqual(self.upstream_calls, 0)

    async def test_old_disconnect_cannot_clear_newer_session(self):
        first = await self.client.get("/stream/a.mp3")
        first_id = self.app["radio"].active.session_id
        second = await self.client.get("/stream/a.mp3")
        second_id = self.app["radio"].active.session_id
        self.assertNotEqual(first_id, second_id)
        first.close()
        await self.wait_for(lambda: first_id not in self.app["radio"].sessions)
        self.assertEqual(self.app["radio"].snapshot()["session_id"], second_id)
        second.close()

    async def test_three_slots_can_play_and_replace_independently(self):
        url = self.source.make_url("/radio")
        radio = self.app["radio"]
        first = await asyncio.gather(*(
            self.client.get("/stream", params={"url": str(url.with_query(station=slot)), "slot": slot})
            for slot in (1, 2, 3)
        ))
        try:
            for response in first:
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.content.readexactly(2048), self.audio[:2048])
            await self.wait_for(lambda: all(s["ad_detected"] for s in radio.snapshot()["slots"].values()))
            sessions = {slot: state["session_id"] for slot, state in radio.snapshot()["slots"].items()}
            self.assertEqual(len(set(sessions.values())), 3)

            async with self.client.get("/stream", params={"url": str(url)}) as overflow:
                self.assertEqual(overflow.status, 503)
            async with self.client.get("/stream", params={"url": str(url), "slot": 2}) as replacement:
                self.assertEqual(replacement.status, 200)
                self.assertEqual(await replacement.content.readexactly(2048), self.audio[:2048])
                slots = radio.snapshot()["slots"]
                self.assertNotEqual(slots[2]["session_id"], sessions[2])
                for slot in (1, 3):
                    self.assertEqual(slots[slot]["session_id"], sessions[slot])
                    self.assertTrue(slots[slot]["ad_detected"])
                self.assertEqual(len(radio.sessions), 3)
                self.assertNotIn(sessions[2], radio.sessions)
                first[1].close()
                first[0].close()
                await self.wait_for(lambda: not radio.snapshot()["slots"][1]["available"])
                self.assertEqual(radio.snapshot()["slots"][3]["session_id"], sessions[3])
                self.assertTrue(radio.snapshot()["slots"][3]["available"])
        finally:
            for response in first:
                response.close()

    async def test_bad_upstream_and_stale_audio_are_unavailable(self):
        response = await self.client.get("/stream/bad.mp3")
        self.assertEqual(response.status, 502)
        self.assertFalse(self.app["radio"].sessions)
        # audio/mpegurl is a playlist, despite beginning with "audio/mpeg".
        self.app["radio"].config["streams"]["hls"] = str(self.source.make_url("/bad")) + "?type=audio%2Fmpegurl"
        response = await self.client.get("/stream/hls.mp3")
        self.assertEqual(response.status, 502)
        self.app["radio"].active = Playback("a", last_audio=time.monotonic()-6)
        self.assertFalse(self.app["radio"].snapshot()["available"])
        self.assertIsNone(self.app["radio"].snapshot()["ad_detected"])

    async def test_idle_upstream_socket_does_not_expire_buffered_audio(self):
        # Source finishes its network write immediately. Consuming already
        # buffered audio takes longer than a read deadline and must still work.
        self.app["radio"].config["read_timeout_seconds"] = 0.2
        response = await self.client.get("/stream/burst.mp3")
        self.assertEqual(await response.content.readexactly(45056), self.audio[:45056])
        await self.wait_for(lambda: self.model.calls >= 2)
        self.assertTrue(self.app["radio"].snapshot()["available"])
        response.close()

    async def test_full_station_url_preserves_query_and_streams_identical_audio(self):
        url = str(self.source.make_url("/radio")) + "?token=a%2Bb%26c&quality=128"
        response = await self.client.get("/stream", params={"url": url})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.content.readexactly(4096), self.audio[:4096])
        self.assertEqual(self.queries, [{"token": "a+b&c", "quality": "128"}])
        state = self.app["radio"].snapshot()
        self.assertTrue(state["stream"].startswith("custom:"))
        self.assertNotIn("token", state["stream"])
        response.close()

    async def test_invalid_custom_urls_are_rejected_before_connection(self):
        self.app["radio"].config["allow_private_streams"] = False
        for url in ("", "file:///etc/passwd", "http://127.0.0.1/admin", "http://[::1]/", "http://localhost/", "https://user:password@radio.example/"):
            response = await self.client.get("/stream", params={"url": url})
            self.assertEqual(response.status, 400, url)
        self.assertEqual(self.upstream_calls, 0)

    async def test_wav_preserves_audio_and_detection_on_one_connection(self):
        url = str(self.source.make_url("/radio")) + "?metaint=0"
        began = time.monotonic()
        response = await self.client.get("/stream", params={"url": url, "format": "wav"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "audio/wav")
        # Five seconds of stereo PCM arrive in the startup burst, without
        # letting classification race ahead of the speaker's buffered audio.
        audio = await response.content.readexactly(882000)
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertLessEqual(self.model.calls, 1)
        with wave.open(io.BytesIO(audio)) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (2, 2, 44100))
            pcm = wav.readframes(44100)
        expected = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "mp3", "-i", "pipe:0",
            "-ac", "2", "-ar", "44100", "-f", "s16le", "pipe:1",
        ], input=self.audio)
        self.assertEqual(pcm, expected[:len(pcm)])
        await self.wait_for(lambda: self.app["radio"].snapshot()["ad_detected"] is True)
        expected_analysis = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "mp3", "-i", "pipe:0",
            "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
        ], input=self.audio)
        self.assertEqual(self.model.first_pcm, expected_analysis[:self.model.samples * 2])
        self.assertEqual(self.upstream_calls, 1)
        response.close()
        await self.wait_for(lambda: not self.app["radio"].sessions)

    async def test_wav_disconnect_reaps_both_ffmpeg_processes(self):
        original_spawn = asyncio.create_subprocess_exec
        processes = []
        async def spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            return process
        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            response = await self.client.get("/stream/a.mp3", params={"format": "wav"})
            self.assertEqual(response.status, 200)
            await response.content.readexactly(32768)
            response.close()
            await self.wait_for(lambda: not self.app["radio"].sessions)
        self.assertEqual(len(processes), 2)
        self.assertTrue(all(process.returncode is not None for process in processes))

    async def test_fade_in_ramps_playback_in_both_formats(self):
        for fmt, bytes_per_second in (("wav", 176400), ("mp3", 16000)):
            with self.subTest(fmt=fmt):
                params = {"format": fmt, "fade_in_seconds": 2}
                async with self.client.get("/stream/a.mp3", params=params) as response:
                    self.assertEqual(response.status, 200)
                    audio = await response.content.readexactly(3 * bytes_per_second)
                await self.wait_for(lambda: not self.app["radio"].sessions)
                pcm = subprocess.check_output([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", fmt, "-i", "pipe:0",
                    "-t", "2.75", "-ar", "16000", "-ac", "1", "-f", "s16le", "pipe:1",
                ], input=audio)
                samples = array("h", pcm)

                def peak(start):
                    return max(samples[start:start + 800])

                full_volume = peak(40000)
                self.assertGreater(full_volume, 1000)
                # Sample 50 ms of the steady tone at the start, midpoint and end.
                self.assertLess(peak(800), full_volume * 0.06)
                self.assertAlmostEqual(peak(16000) / full_volume, 0.5, delta=0.06)
                self.assertAlmostEqual(peak(33600) / full_volume, 1.0, delta=0.02)

    async def test_wav_zero_startup_credit_keeps_realtime_delivery(self):
        self.app["radio"].config["startup_buffer_seconds"] = 0
        began = time.monotonic()
        response = await self.client.get("/stream/a.mp3", params={"format": "wav"})
        self.assertEqual(response.status, 200)
        await response.content.readexactly(44100 * 2 * 2 * 3)
        self.assertGreater(time.monotonic() - began, 2.5)
        self.assertTrue(self.app["radio"].snapshot()["available"])
        response.close()

    async def test_output_format_is_validated_and_head_opens_no_source(self):
        response = await self.client.get("/stream/a.mp3", params={"format": "invalid"})
        self.assertEqual(response.status, 400)
        response = await self.client.head("/stream/a.mp3", params={"format": "wav"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "audio/wav")
        self.assertEqual(self.upstream_calls, 0)

    async def test_signed_output_format_cannot_be_changed(self):
        self.app["radio"].config["signing_secret"] = "test-secret"
        url = str(self.source.make_url("/radio"))
        for signed_format, requested_format in (("mp3", "wav"), ("wav", "mp3")):
            signature = stream_signature("test-secret", URL(url), output_format=signed_format)
            response = await self.client.get("/stream", params={"url": url, "format": requested_format, "sig": signature})
            self.assertEqual(response.status, 403)
        self.assertEqual(self.upstream_calls, 0)
        signature = stream_signature("test-secret", URL(url), output_format="wav")
        response = await self.client.get("/stream", params={"url": url, "format": "wav", "sig": signature})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.content.readexactly(4), b"RIFF")
        response.close()

    async def test_signed_slot_cannot_be_changed_added_or_removed(self):
        radio = self.app["radio"]
        radio.config["signing_secret"] = "test-secret"
        url = self.source.make_url("/radio")
        for signed, requested, expected in ((2, 2, 200), (2, 3, 403), (2, None, 403), (None, 2, 403), (None, 4, 400)):
            params = {"url": str(url), "sig": stream_signature("test-secret", url, slot=signed)}
            if requested is not None:
                params["slot"] = requested
            async with self.client.head("/stream", params=params) as response:
                self.assertEqual(response.status, expected)
        self.assertEqual(self.upstream_calls, 0)
        self.assertFalse(radio.sessions)

    async def test_signed_fade_duration_is_validated_before_playback(self):
        self.app["radio"].config["signing_secret"] = "test-secret"
        url = self.source.make_url("/radio")
        params = {"url": str(url), "sig": stream_signature("test-secret", url, fade_in_seconds=10)}
        for fade, status in (("10", 200), ("0", 403), ("9", 403), ("-1", 400), ("11", 400), ("nan", 400)):
            async with self.client.head("/stream", params={**params, "fade_in_seconds": fade}) as response:
                self.assertEqual(response.status, status)
        self.assertEqual(self.upstream_calls, 0)

    async def test_explicit_zero_icy_metadata_relays_audio_and_detects(self):
        # StreamGuys (including All Classical's ac128kmp3) sends this header
        # even when honoring our request to disable ICY metadata.
        url = str(self.source.make_url("/radio")) + "?metaint=0"
        response = await self.client.get("/stream", params={"url": url})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.content.readexactly(len(self.audio)), self.audio)
        await self.wait_for(lambda: self.app["radio"].snapshot()["ad_detected"] is True)
        self.assertEqual(self.upstream_calls, 1)
        self.assertEqual(self.headers[0]["Icy-MetaData"], "0")
        response.close()
        await self.wait_for(lambda: not self.app["radio"].sessions)

    async def test_configured_station_accepts_explicit_zero_icy_metadata(self):
        self.app["radio"].config["streams"]["zero"] = str(self.source.make_url("/radio")) + "?metaint=0"
        response = await self.client.get("/stream/zero.mp3")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.content.readexactly(4096), self.audio[:4096])
        response.close()

    async def test_active_or_invalid_icy_metadata_is_rejected(self):
        url = str(self.source.make_url("/radio"))
        for interval in ("16000", "-1", "invalid", ""):
            with self.subTest(interval=interval):
                response = await self.client.get("/stream", params={"url": url + "?metaint=" + interval})
                self.assertEqual(response.status, 502)
                self.assertFalse(self.app["radio"].sessions)
                expected = "cannot safely relay metadata" if interval == "16000" else "Invalid upstream icy-metaint"
                self.assertIn(expected, self.app["radio"].last_error)

    async def test_redirect_to_private_network_is_rejected(self):
        self.app["radio"].config["allow_private_streams"] = False
        class Redirect:
            status = 302
            headers = {"Location": "http://127.0.0.1/private"}
            closed = False
            def close(self): self.closed = True
        redirect = Redirect()
        with patch.object(self.app["radio"].custom_client, "get", new=AsyncMock(return_value=redirect)) as get:
            with self.assertRaises(ValueError):
                await self.app["radio"].open_custom(URL("https://radio.example/"))
            self.assertEqual(get.await_count, 1)
        self.assertTrue(redirect.closed)

    async def test_dns_private_answers_are_rejected(self):
        resolver = StreamResolver()
        try:
            with patch("aiohttp.resolver.DefaultResolver.resolve", new=AsyncMock(return_value=[{"host": "192.168.1.2"}])):
                with self.assertRaises(ValueError):
                    await resolver.resolve("radio.example", 80)
        finally:
            await resolver.close()

    async def test_websocket_pushes_detection_without_polling(self):
        ws = await self.client.ws_connect("/events")
        self.assertFalse((await ws.receive_json())["available"])
        response = await self.client.get("/stream/a.mp3")
        async def consume():
            async for _ in response.content.iter_chunked(2048): pass
        reader = asyncio.create_task(consume())
        try:
            for _ in range(30):
                state = await ws.receive_json(timeout=5)
                if state["ad_detected"] is True:
                    break
            else:
                self.fail("No speech detection pushed over WebSocket")
        finally:
            response.close()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await ws.close()

    async def test_ha_signed_urls_cannot_be_retargeted(self):
        self.app["radio"].config["signing_secret"] = "test-secret"
        url = str(self.source.make_url("/radio"))
        response = await self.client.get("/stream", params={"url": url})
        self.assertEqual(response.status, 403)
        signature = stream_signature("test-secret", URL(url), 128)
        response = await self.client.get("/stream", params={"url": url+"?changed=yes", "bitrate": "128", "sig": signature})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.upstream_calls, 0)
        response = await self.client.get("/stream", params={"url": url, "bitrate": "128", "sig": signature})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.content.readexactly(2048), self.audio[:2048])
        response.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
