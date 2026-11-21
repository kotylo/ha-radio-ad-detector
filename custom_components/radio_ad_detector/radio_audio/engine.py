"""Same-connection MP3 relay and low-latency speech/non-music detector."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
import logging
import hmac
import math
import time
import uuid
from yarl import URL

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, TCPConnector, web

from .detection import Detector
from .stream_urls import StreamResolver, stream_label, stream_signature, validate_stream_url

LOG = logging.getLogger("radio_detector")
SAMPLE_RATE = 16000
HOP = 7680
STARTUP_BUFFER_SECONDS = 15
RELAY_CHUNK_SIZE = 2048
OUTPUT_CONTENT_TYPES = {"mp3": "audio/mpeg", "wav": "audio/wav"}
STREAM_SLOTS = (1, 2, 3)
FADE_IN_SECONDS = range(11)


@dataclass
class Playback:
    stream: str
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started: float = field(default_factory=time.monotonic)
    last_audio: float = 0.0
    ad_detected: bool = False
    reason: str = "warming_up"
    error: str | None = None
    scores: dict = field(default_factory=dict)
    analyzed_seconds: float = 0.0


class Radio:
    def __init__(self, config, model, get_playback_buffer):
        self.config, self.model = config, model
        self.get_playback_buffer = get_playback_buffer
        self.sessions = {}
        self.slots = {}
        self.slot_locks = {slot: asyncio.Lock() for slot in STREAM_SLOTS}
        self.active = None
        self.last_error = None
        self.client = None
        self.custom_client = None
        self.watchers = set()
        self.state_callback = None
        self.closing = False

    def snapshot(self):
        return {
            **self.playback_state(self.active, self.last_error),
            "slots": {
                slot: self.playback_state(self.slots[slot][0] if slot in self.slots else None)
                for slot in STREAM_SLOTS
            },
        }

    @staticmethod
    def playback_state(p, last_error=None):
        age = time.monotonic() - p.last_audio if p and p.last_audio else None
        available = bool(p and not p.error and age is not None and age < 5)
        return {
            "available": available,
            "ad_detected": p.ad_detected if available else None,
            "reason": p.reason if p else "idle",
            "stream": p.stream if p else None,
            "session_id": p.session_id if p else None,
            "audio_age_seconds": round(age, 2) if age is not None else None,
            "analyzed_seconds": round(p.analyzed_seconds, 2) if p else 0,
            "error": p.error if p else last_error,
            **(p.scores if p else {}),
        }

    async def lifecycle(self, app):
        # Socket-idle timeouts also count deliberate relay backpressure. Apply
        # the read deadline only when our consumer actually needs more bytes.
        timeout = ClientTimeout(total=None, connect=15, sock_read=None)
        self.client = ClientSession(timeout=timeout, cookie_jar=DummyCookieJar(), auto_decompress=False)
        resolver = StreamResolver(self.config.get("allow_private_streams", False))
        connector = TCPConnector(resolver=resolver)
        self.custom_client = ClientSession(timeout=timeout, cookie_jar=DummyCookieJar(), auto_decompress=False, connector=connector)
        ticker = asyncio.create_task(self.tick())
        yield
        self.closing = True
        ticker.cancel()
        with suppress(asyncio.CancelledError):
            await ticker
        await self.client.close()
        await self.custom_client.close()
        await resolver.close()

    async def tick(self):
        while True:
            self.publish()
            await asyncio.sleep(1)

    def publish(self):
        state = self.snapshot()
        if self.state_callback is not None:
            self.state_callback(state)
        for queue in self.watchers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(state)

    async def events(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        queue = asyncio.Queue(maxsize=1)
        self.watchers.add(queue)
        queue.put_nowait(self.snapshot())

        async def send():
            while True:
                await ws.send_json(await queue.get())

        async def receive():
            async for _ in ws:
                pass

        workers = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for worker in done:
                worker.result()
        finally:
            self.watchers.discard(queue)
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await ws.close()
        return ws

    async def status(self, request):
        return web.json_response(self.snapshot(), headers={"Cache-Control": "no-store"})

    async def health(self, request):
        # Service readiness is distinct from playback availability.
        return web.json_response({"ready": True, "sessions": len(self.sessions), "api_version": 2, "custom_urls": True})

    async def open_custom(self, url):
        allow_private = self.config.get("allow_private_streams", False)
        for _ in range(6):
            url = validate_stream_url(str(url), allow_private)
            response = await self.custom_client.get(
                url, allow_redirects=False,
                headers={"Icy-MetaData": "0", "Accept-Encoding": "identity", "User-Agent": "RadioAdDetector/1.0"},
            )
            if response.status not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("Station redirect has no Location header")
            url = url.join(URL(location))
        raise ValueError("Too many station redirects")

    async def analyze(self, decoder, p):
        detector = Detector(self.config)
        size = self.model.samples * 2
        pcm = await decoder.stdout.readexactly(size)
        while True:
            began = time.monotonic()
            scores = await asyncio.to_thread(self.model.classify, pcm)
            top = scores.pop("top_classes")
            decision = detector.update(**scores)
            previous = p.ad_detected
            p.last_audio = time.monotonic()
            p.ad_detected, p.reason = decision.ad_detected, decision.reason
            p.scores = {k: round(v, 3) for k, v in scores.items()}
            p.scores.update(top_classes=top, inference_ms=round((p.last_audio - began) * 1000))
            p.analyzed_seconds += self.model.samples / SAMPLE_RATE if not p.analyzed_seconds else HOP / SAMPLE_RATE
            self.publish()
            if previous != p.ad_detected:
                LOG.info("%s session=%s ad=%s reason=%s scores=%s", p.stream, p.session_id, p.ad_detected, p.reason, p.scores)
            pcm = pcm[HOP * 2:] + await decoder.stdout.readexactly(HOP * 2)

    async def stream(self, request):
        output_format = request.query.get("format", "mp3")
        if output_format not in OUTPUT_CONTENT_TYPES:
            raise web.HTTPBadRequest(text="Output format must be mp3 or wav")
        fade_param = request.query.get("fade_in_seconds", "0")
        if fade_param not in map(str, FADE_IN_SECONDS):
            raise web.HTTPBadRequest(text="Fade-in duration must be a whole number from 0 to 10 seconds")
        fade_in = int(fade_param)
        slot = None
        if "slot" in request.query:
            slot_param = request.query["slot"]
            if slot_param not in map(str, STREAM_SLOTS):
                raise web.HTTPBadRequest(text="Slot must be 1, 2 or 3")
            slot = int(slot_param)
        custom = "station" not in request.match_info
        bitrate_override = None
        if custom:
            if not self.config.get("allow_custom_urls", True):
                raise web.HTTPForbidden(text="Custom station URLs are disabled")
            try:
                url = validate_stream_url(request.query.get("url"), self.config.get("allow_private_streams", False))
                if "bitrate" in request.query:
                    bitrate_override = float(request.query["bitrate"])
                    if not 32 <= bitrate_override <= 512:
                        raise ValueError("Bitrate must be between 32 and 512 kb/s")
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc
            station = stream_label(url)
            if secret := self.config.get("signing_secret"):
                expected = stream_signature(secret, url, bitrate_override, output_format, slot, fade_in)
                if not hmac.compare_digest(expected, request.query.get("sig", "")):
                    raise web.HTTPForbidden(text="Use the HA playback action to generate a signed proxy URL")
        else:
            station = request.match_info["station"]
            if station not in self.config.get("streams", {}):
                raise web.HTTPNotFound(text="Unknown station")
        headers = {"Content-Type": OUTPUT_CONTENT_TYPES[output_format], "Cache-Control": "no-store", "Access-Control-Allow-Origin": "*", "Accept-Ranges": "none"}
        if request.method == "HEAD":
            return web.Response(headers=headers)
        if slot is None:
            slot = next(
                (number for number in STREAM_SLOTS
                 if number not in self.slots and not self.slot_locks[number].locked()),
                None,
            )
            if slot is None:
                raise web.HTTPServiceUnavailable(text="Too many playback sessions")
            p = self.reserve(slot, station)
        else:
            # Reap the old connection before opening another, even at capacity.
            async with self.slot_locks[slot]:
                if slot in self.slots:
                    _, previous = self.slots[slot]
                    if not previous.cancelling():
                        previous.cancel()
                    await asyncio.wait({previous})
                p = self.reserve(slot, station)
        upstream = decoder = transcoder = response = None
        workers = []
        try:
            playback_buffer = self.get_playback_buffer()
            self.publish()
            if custom:
                upstream = await asyncio.wait_for(self.open_custom(url), timeout=20)
            else:
                upstream = await asyncio.wait_for(self.client.get(
                    self.config["streams"][station],
                    headers={"Icy-MetaData": "0", "Accept-Encoding": "identity", "User-Agent": "RadioAdDetector/1.0"},
                ), timeout=20)
            upstream.raise_for_status()
            media_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type not in ("audio/mpeg", "audio/mp3", "audio/x-mpeg", "audio/mpeg3"):
                raise ValueError("Upstream is not MP3 audio")
            # Some Icecast/StreamGuys servers explicitly advertise zero when
            # metadata is disabled. The string "0" is truthy, but is safe MP3.
            try:
                metadata_interval = int(upstream.headers.get("icy-metaint", "0"))
            except ValueError as exc:
                raise ValueError("Invalid upstream icy-metaint header") from exc
            if metadata_interval < 0:
                raise ValueError("Invalid upstream icy-metaint header")
            if metadata_interval > 0:
                raise ValueError("Upstream ignored Icy-MetaData: 0; cannot safely relay metadata")
            bitrate = bitrate_override
            if bitrate is None:
                # Icecast can report zero when the stream bitrate is unknown.
                reported_bitrate = float(upstream.headers.get("icy-br", "0"))
                bitrate = reported_bitrate or float(self.config.get("relay_bitrate_kbps", 128))
            if not 32 <= bitrate <= 512:
                raise ValueError("Unsupported MP3 bitrate")
            bytes_per_second = bitrate * 1000 / 8
            startup_bytes = bytes_per_second * float(self.config.get("startup_buffer_seconds", STARTUP_BUFFER_SECONDS))
            # Bound the speaker's lead and memory use while the detector consumes
            # the same bytes at real-time speed, including during the initial burst.
            pending = asyncio.Queue(maxsize=math.ceil(startup_bytes / RELAY_CHUNK_SIZE) + 1)
            playback_bytes = startup_bytes + playback_buffer * bytes_per_second
            playback = asyncio.Queue(maxsize=math.ceil(playback_bytes / RELAY_CHUNK_SIZE) + 1)
            decoder = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-probesize", "32768", "-analyzeduration", "0", "-f", "mp3", "-i", "pipe:0",
                "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if output_format == "wav" or fade_in:
                # Apply the fade only to playback. Analysis always gets the original audio.
                filters = ["-af", f"afade=t=in:st=0:d={fade_in}"] if fade_in else []
                if output_format == "wav":
                    codec = ["-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le"]
                else:
                    codec = ["-c:a", "libmp3lame", "-b:a", str(round(bitrate * 1000))]
                transcoder = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-probesize", "8192", "-analyzeduration", "0", "-f", "mp3", "-i", "pipe:0",
                    "-vn", *filters, *codec, "-flush_packets", "1", "-f", output_format, "pipe:1",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            response = web.StreamResponse(headers=headers)
            await response.prepare(request)
            self.active, self.last_error = p, None
            self.publish()
            LOG.info("Playback started slot=%s stream=%s session=%s", slot, station, p.session_id)

            async def pump():
                origin = time.monotonic()
                sent = 0
                while True:
                    # A source may pause after its initial burst. Allow time for
                    # the queued audio to play before counting that as starvation.
                    buffered_seconds = max(pending.qsize(), playback.qsize()) * RELAY_CHUNK_SIZE / bytes_per_second
                    chunk = await asyncio.wait_for(
                        upstream.content.read(RELAY_CHUNK_SIZE),
                        timeout=buffered_seconds + float(self.config.get("read_timeout_seconds", 12)),
                    )
                    if not chunk:
                        await asyncio.gather(pending.join(), playback.join())
                        if transcoded_sender is not None:
                            transcoder.stdin.close()
                            await transcoded_sender
                        raise ConnectionError("Upstream stream ended")
                    # Let the player fill its startup buffer immediately, then
                    # limit delivery to real time with a bounded lead.
                    position = max(0, sent - startup_bytes) / bytes_per_second
                    target = origin + position
                    now = time.monotonic()
                    if now - target > 2:
                        origin = now - position
                        target = now
                    if target > now:
                        await asyncio.sleep(target - now)
                    # Queue analysis immediately and shift the playback schedule, including its startup burst.
                    await pending.put(chunk)
                    await playback.put((time.monotonic() + playback_buffer, chunk))
                    sent += len(chunk)

            async def send_audio():
                while True:
                    release_at, chunk = await playback.get()
                    delay = release_at - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if transcoder is None:
                        await asyncio.wait_for(response.write(chunk), timeout=8)
                    else:
                        transcoder.stdin.write(chunk)
                        await asyncio.wait_for(transcoder.stdin.drain(), timeout=8)
                    playback.task_done()

            async def feed_decoder():
                origin = time.monotonic()
                sent = 0
                while True:
                    chunk = await pending.get()
                    # Never burst the classifier: otherwise opening speech could
                    # trigger and clear before the speaker plays that audio.
                    target = origin + sent / bytes_per_second
                    now = time.monotonic()
                    if now - target > 2:
                        origin = now - sent / bytes_per_second
                        target = now
                    if target > now:
                        await asyncio.sleep(target - now)
                    decoder.stdin.write(chunk)
                    await asyncio.wait_for(decoder.stdin.drain(), timeout=8)
                    sent += len(chunk)
                    pending.task_done()

            async def send_transcoded():
                while chunk := await transcoder.stdout.read(32768):
                    await asyncio.wait_for(response.write(chunk), timeout=8)
                raise ConnectionError("Playback transcoder ended")

            transcoded_sender = asyncio.create_task(send_transcoded()) if transcoder is not None else None
            workers = [
                asyncio.create_task(pump()), asyncio.create_task(feed_decoder()),
                asyncio.create_task(self.analyze(decoder, p)), asyncio.create_task(send_audio()),
            ]
            if transcoded_sender is not None:
                workers.append(transcoded_sender)
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for worker in done:
                worker.result()
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, BrokenPipeError):
            LOG.info("Playback client disconnected session=%s", p.session_id)
        except Exception as exc:
            # HTTP exceptions can include signed URL query strings. Keep them out
            # of status attributes, MQTT messages and journal logs.
            detail = str(exc) if isinstance(exc, ValueError) else "Upstream or decoder failed"
            p.error = type(exc).__name__ + ": " + detail
            p.reason = "error"
            self.last_error = p.error
            LOG.warning("Playback failed stream=%s session=%s error=%s", station, p.session_id, p.error)
            if response is None or not response.prepared:
                raise web.HTTPBadGateway(text="Radio connection failed; see service logs") from exc
        finally:
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            if upstream:
                upstream.close()
            for process in (decoder, transcoder):
                if process is None:
                    continue
                if process.stdin:
                    process.stdin.close()
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        # Drain output too: wait() can hang after termination
                        # when the WAV decoder's stdout pipe is full.
                        await asyncio.wait_for(process.communicate(), timeout=3)
                    except asyncio.TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.communicate()
            self.sessions.pop(p.session_id, None)
            del self.slots[slot]
            # A finishing old session must never reset a newer Chromecast connection.
            if self.active is p:
                self.active = None
            self.publish()
        return response

    def reserve(self, slot, station):
        if len(self.sessions) >= int(self.config.get("max_sessions", len(STREAM_SLOTS))):
            raise web.HTTPServiceUnavailable(text="Too many playback sessions")
        playback = Playback(station)
        self.sessions[playback.session_id] = playback
        self.slots[slot] = playback, asyncio.current_task()
        return playback


def create_app(config, model, *, get_playback_buffer: Callable[[], float] = lambda: 0.0):
    Detector(config)  # Validate thresholds before accepting playback.
    startup = float(config.get("startup_buffer_seconds", STARTUP_BUFFER_SECONDS))
    if not math.isfinite(startup) or not 0 <= startup <= 30:
        raise ValueError("Startup buffer must be between 0 and 30 seconds")
    radio = Radio(config, model, get_playback_buffer)
    app = web.Application()
    app["radio"] = radio
    app.cleanup_ctx.append(radio.lifecycle)
    app.router.add_get("/health", radio.health)
    app.router.add_get("/status", radio.status)
    app.router.add_get("/events", radio.events)
    app.router.add_get("/stream", radio.stream)
    app.router.add_get("/stream/{station}.mp3", radio.stream)
    return app
