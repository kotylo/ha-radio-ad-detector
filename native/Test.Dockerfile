FROM python:3.14-alpine3.23
RUN apk add --no-cache ffmpeg && pip install --no-cache-dir aiohttp==3.13.5
WORKDIR /work
ENTRYPOINT ["python", "/work/tests/native_smoke.py", "/work/custom_components/radio_ad_detector", "/work/models", "/work/native-test/speech.pcm", "/work/native-test/speech.mp3"]
