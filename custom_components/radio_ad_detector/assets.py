"""Fetch and verify the fixed audio model without blocking HA's event loop."""
from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.helpers.aiohttp_client import async_get_clientsession

FILES = {
    "yamnet.tflite": (
        "https://storage.googleapis.com/audioset/yamnet.tflite",
        "e193da5676a2fdb7a3702c10fba62aa27d76f64e4534467443811c1a2ff1667d",
    ),
    "yamnet_class_map.csv": (
        "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv",
        "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2",
    ),
}


def valid(path, digest):
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest


def store(path, data, digest):
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Audio model checksum does not match the trusted release")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


async def async_ensure_models(hass):
    root = Path(hass.config.path(".storage", DOMAIN_FOLDER, "models"))
    session = async_get_clientsession(hass)
    for name, (url, digest) in FILES.items():
        path = root / name
        if await hass.async_add_executor_job(valid, path, digest):
            continue
        async with session.get(url, timeout=90) as response:
            response.raise_for_status()
            data = await response.read()
        await hass.async_add_executor_job(store, path, data, digest)
    return {"model_path": str(root / "yamnet.tflite"), "labels_path": str(root / "yamnet_class_map.csv")}


DOMAIN_FOLDER = "radio_ad_detector"
