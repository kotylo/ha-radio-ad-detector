# Radio Ad Detector for Home Assistant

<img src="custom_components/radio_ad_detector/brand/icon.png" alt="Radio with a red line through Ad" width="96">

Play internet radio through Home Assistant and flag likely ad breaks. The
integration runs YAMNet locally on the same stream it sends to your speaker.
Speech and sustained non-music can trigger a detection. DJ chatter can trigger
too, and music-heavy ads can slip through.

## Install

Requires Home Assistant **2026.4 or newer** on **Home Assistant OS or Container**
for **ARM64 or x86-64**.

1. Download `radio_ad_detector.zip` from the repository's GitHub Releases page.
   Extract it into `/config/custom_components/radio_ad_detector/`, with
   `manifest.json` directly inside that folder.
2. Restart Home Assistant. Go to **Settings → Devices & services → Add
   integration → Radio Ad Detector**.
3. Enter the Home Assistant host's LAN IP address and a free proxy port
   (default `8098`). The speaker must be able to reach this address. With Docker
   bridge networking, publish the proxy port too (for example, `8098:8098`).

First setup downloads the verified YAMNet model (about 16 MB).

## Play a station

Run this action in **Developer Tools → Actions**, or use it in a script or
automation. Replace the example media player and station URL.

```yaml
action: radio_ad_detector.play
data:
  entity_id: media_player.radio_speaker
  stream_url: https://radio.example/live.mp3
  slot: 1
```

Use the same action for up to three simultaneous streams. Give each player a
different slot (`1`, `2`, or `3`). Starting playback on an occupied slot
replaces the stream in that slot. Omit `slot` to use the first free slot.

Use a direct, public HTTP(S) stream carrying fixed-bitrate MP3. Set `bitrate`
in kb/s if the station's reported bitrate is missing or inaccurate. Use
`radio_ad_detector.get_proxy_url` if your automation needs the signed URL and
media content type without starting playback.

To change the default playback format, go to **Settings → Devices & services →
Radio Ad Detector → Configure**. WAV remains the default and starts faster on
Cast speakers, using about 1.4 Mb/s on the LAN. Choose **Original MP3** to use
the station's lower bitrate. Some Cast streams may take 15 seconds or more to
start with MP3. Both formats support ad detection. The setting applies to new
playback and proxy URLs. An explicit `output_format` in an action overrides it.

Each slot has its own entities, so an automation can react to ads on one player
while the others keep playing.

| Slot | Ad detection | Audio status |
| --- | --- | --- |
| 1 | `binary_sensor.radio_ad_detector_stream_1_ad_detected` | `sensor.radio_ad_detector_stream_1_audio_status` |
| 2 | `binary_sensor.radio_ad_detector_stream_2_ad_detected` | `sensor.radio_ad_detector_stream_2_audio_status` |
| 3 | `binary_sensor.radio_ad_detector_stream_3_ad_detected` | `sensor.radio_ad_detector_stream_3_audio_status` |

Each pair is unavailable until its audio has been analyzed, when playback stops,
or when analysis becomes stale. The original **Ad detected** and **Audio status**
entities still follow the most recently started relay playback.

See the [native build instructions](native/README.md) to build a release ZIP.
