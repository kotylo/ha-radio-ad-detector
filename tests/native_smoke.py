"""Exercise the actual bundled C runtime and relay in HA's Python environment.

python native_smoke.py COMPONENT_DIR MODEL_DIR SPEECH_PCM SPEECH_MP3 [mp3|wav]
"""
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, sys.argv[1])
from radio_audio.native_model import NativeYamnet
from radio_audio.detection import Detector
from radio_audio.engine import create_app
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient


async def run():
    output_format = sys.argv[5] if len(sys.argv) > 5 else "mp3"
    directory = Path(sys.argv[2])
    model = await asyncio.to_thread(NativeYamnet, {
        "model_path": str(directory / "yamnet.tflite"),
        "labels_path": str(directory / "yamnet_class_map.csv"),
    })
    try:
        silence = await asyncio.to_thread(model.classify, bytes(model.samples * 2))
        assert silence["speech"] < 0.1 and silence["music"] < 0.1, silence
        pcm = Path(sys.argv[3]).read_bytes()
        detector = Detector({})
        latencies = []
        detected = False
        for start in range(0, len(pcm)-model.samples*2, 7680*2):
            began = time.monotonic()
            scores = await asyncio.to_thread(model.classify, pcm[start:start+model.samples*2])
            latencies.append((time.monotonic()-began)*1000)
            scores.pop("top_classes")
            detected |= detector.update(**scores).ad_detected
        assert detected, "Native runtime did not detect the speech fixture"

        audio = Path(sys.argv[4]).read_bytes()
        async def source(request): return web.Response(body=audio, content_type="audio/mpeg")
        upstream_app = web.Application()
        upstream_app.router.add_get("/speech", source)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        app = create_app({"streams": {"test": str(upstream.make_url("/speech"))}}, model)
        client = TestClient(TestServer(app, handler_cancellation=True))
        await client.start_server()
        try:
            began = time.monotonic()
            response = await client.get("/stream/test.mp3", params={"format": output_format})
            assert response.status == 200, response.status
            assert response.headers["Content-Type"] == {"mp3": "audio/mpeg", "wav": "audio/wav"}[output_format]
            async def consume():
                async for _ in response.content.iter_chunked(2048): pass
            reader = asyncio.create_task(consume())
            try:
                for _ in range(200):
                    if app["radio"].snapshot()["ad_detected"] is True:
                        elapsed = time.monotonic()-began
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("Native paced relay did not detect speech within 10 seconds")
            finally:
                response.close()
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        finally:
            await client.close()
            await upstream.close()
        print(json.dumps({"native_runtime": "passed", "output_format": output_format, "silence": silence,
                          "speech_windows": len(latencies), "inference_ms_mean": round(sum(latencies)/len(latencies), 2),
                          "inference_ms_max": round(max(latencies), 2), "relay_alert_seconds": round(elapsed, 2)}))
    finally:
        await asyncio.to_thread(model.close)


if __name__ == "__main__":
    asyncio.run(run())
