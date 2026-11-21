# Repository instructions

## Stream changes

After changing stream-related code, run `python tests/live_stations_smoke.py`
from the repository root before reporting the change complete. This applies to
the play action, station URL handling, upstream connections, relay buffering and
pacing, audio decoding and transcoding, and playback formats.

The test checks all four live stations in MP3 and WAV. Report any failing
station and format, or explain what blocked the test from running.
