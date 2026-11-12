# Native runtime builds

Home Assistant OS/Container use musl and change Python versions over time. The
integration calls TensorFlow Lite's stable C API through `ctypes`, so it does not
need a Python-version-specific inference wheel, another container, or a daemon.
The model and inference run inside the HA integration. FFmpeg is a child process
launched only during playback.

`Dockerfile` is a development/build tool only. Build on each target architecture:

```sh
docker buildx build -f native/Dockerfile --target artifact \
  --output type=local,dest=custom_components/radio_ad_detector/native/linux-aarch64-musl .
```

Use `linux-x86_64-musl` on an x86-64 builder. The GitHub workflow builds both
architectures and combines their files in the installable ZIP. It does not publish
a release automatically. A complete public release should include both builds.

Only the 14 builtin operations in the pinned YAMNet model are registered and
compiled; this runtime is not a general-purpose Lite model runner.
The C++ runtime is linked statically to reduce dependencies on the HA image's GCC
version. TensorFlow Lite 2.14 source is compiled with two jobs; small ARM machines
can take a while. Include the generated TensorFlow and dependency license notices
in redistributed ZIPs. The audio model is downloaded separately and verified with
the SHA-256 pinned in `assets.py`.

Run `python tools/package_integration.py` after building both architectures.
For GitHub releases, run the **Build native integration ZIP** workflow or push a
`v*` tag. Download the workflow artifact, then attach the
`radio_ad_detector.zip` inside it to the GitHub release.

## Tests

- Run `python -m unittest discover -s tests` with aiohttp and FFmpeg installed
  to exercise detection and the relay.
- Run `python tests/ha_smoke.py custom_components/radio_ad_detector` in Home
  Assistant's Python environment to test setup, actions, entities, and unload.
- Run `tests/native_smoke.py` in the target Linux environment with FFmpeg and
  aiohttp. Pass the component directory, a directory containing the two files
  listed in `assets.py`, and matching speech samples in 16 kHz mono signed
  16-bit little-endian PCM and constant-bitrate MP3. The test also accepts
  `wav` as its final argument to exercise WAV playback.
