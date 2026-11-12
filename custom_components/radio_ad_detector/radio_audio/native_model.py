"""YAMNet through Lite's stable C API; no Python-specific ML wheel required."""
from __future__ import annotations

from array import array
import csv
import ctypes as ct
import math
from pathlib import Path
import platform
import sys
import threading


def library_path():
    machine = platform.machine().lower()
    machine = {"arm64": "aarch64", "amd64": "x86_64"}.get(machine, machine)
    if sys.platform != "linux" or machine not in ("aarch64", "x86_64"):
        raise RuntimeError("Supported platforms: Linux ARM64 and x86-64 running Home Assistant OS/Container")
    path = Path(__file__).resolve().parents[1] / "native" / f"linux-{machine}-musl" / "libtensorflowlite_c.so"
    if not path.is_file():
        raise RuntimeError(f"The release is missing its native runtime for {machine}; install a complete release ZIP")
    return path


class NativeYamnet:
    """One interpreter, serialized across playback sessions and shutdown."""

    def __init__(self, config):
        self.lock = threading.Lock()
        self.model = self.interpreter = None
        self.lib = ct.CDLL(str(library_path()))
        ptr, integer = ct.c_void_p, ct.c_int
        declarations = {
            "TfLiteModelCreateFromFile": (ptr, [ct.c_char_p]),
            "TfLiteModelDelete": (None, [ptr]),
            "TfLiteInterpreterOptionsCreate": (ptr, []),
            "TfLiteInterpreterOptionsDelete": (None, [ptr]),
            "TfLiteInterpreterOptionsSetNumThreads": (None, [ptr, integer]),
            "TfLiteInterpreterCreate": (ptr, [ptr, ptr]),
            "TfLiteInterpreterDelete": (None, [ptr]),
            "TfLiteInterpreterAllocateTensors": (integer, [ptr]),
            "TfLiteInterpreterGetInputTensor": (ptr, [ptr, integer]),
            "TfLiteInterpreterGetOutputTensorCount": (integer, [ptr]),
            "TfLiteInterpreterGetOutputTensor": (ptr, [ptr, integer]),
            "TfLiteInterpreterInvoke": (integer, [ptr]),
            "TfLiteTensorType": (integer, [ptr]),
            "TfLiteTensorByteSize": (ct.c_size_t, [ptr]),
            "TfLiteTensorCopyFromBuffer": (integer, [ptr, ptr, ct.c_size_t]),
            "TfLiteTensorCopyToBuffer": (integer, [ptr, ptr, ct.c_size_t]),
        }
        for name, (result, args) in declarations.items():
            function = getattr(self.lib, name)
            function.restype, function.argtypes = result, args
        try:
            self.model = self.lib.TfLiteModelCreateFromFile(str(config["model_path"]).encode())
            if not self.model:
                raise RuntimeError("Cannot load the YAMNet model")
            options = self.lib.TfLiteInterpreterOptionsCreate()
            if not options:
                raise RuntimeError("Cannot allocate interpreter options")
            try:
                self.lib.TfLiteInterpreterOptionsSetNumThreads(options, 2)
                self.interpreter = self.lib.TfLiteInterpreterCreate(self.model, options)
            finally:
                self.lib.TfLiteInterpreterOptionsDelete(options)
            if not self.interpreter:
                raise RuntimeError("Cannot create the YAMNet interpreter")
            self.check(self.lib.TfLiteInterpreterAllocateTensors(self.interpreter))
            self.input = self.lib.TfLiteInterpreterGetInputTensor(self.interpreter, 0)
            if self.lib.TfLiteTensorType(self.input) != 1:
                raise RuntimeError("YAMNet must have float32 waveform input")
            self.samples = self.lib.TfLiteTensorByteSize(self.input) // 4
            if not 15000 <= self.samples <= 17000:
                raise RuntimeError("Unexpected YAMNet input length")
            self.output = None
            for i in range(self.lib.TfLiteInterpreterGetOutputTensorCount(self.interpreter)):
                tensor = self.lib.TfLiteInterpreterGetOutputTensor(self.interpreter, i)
                if self.lib.TfLiteTensorType(tensor) == 1 and self.lib.TfLiteTensorByteSize(tensor) == 521 * 4:
                    self.output = tensor
                    break
            if self.output is None:
                raise RuntimeError("Unexpected YAMNet scores output")
            with open(config["labels_path"], newline="") as source:
                self.labels = [r["display_name"] for r in csv.DictReader(source)]
            if len(self.labels) != 521 or self.labels[0] != "Speech" or self.labels[132] != "Music":
                raise RuntimeError("Unexpected YAMNet class map")
        except Exception:
            self.close()
            raise

    @staticmethod
    def check(status):
        if status != 0:
            raise RuntimeError(f"Lite inference failed (status {status})")

    def classify(self, pcm):
        integers = array("h")
        integers.frombytes(pcm)
        if sys.byteorder != "little":
            integers.byteswap()
        waveform = array("f", (v / 32768.0 for v in integers))
        if len(waveform) != self.samples:
            raise ValueError("Unexpected audio window size")
        input_buffer = (ct.c_float * self.samples).from_buffer(waveform)
        output = (ct.c_float * 521)()
        with self.lock:
            if not self.interpreter:
                raise RuntimeError("Interpreter is closed")
            self.check(self.lib.TfLiteTensorCopyFromBuffer(self.input, input_buffer, ct.sizeof(input_buffer)))
            self.check(self.lib.TfLiteInterpreterInvoke(self.interpreter))
            self.check(self.lib.TfLiteTensorCopyToBuffer(self.output, output, ct.sizeof(output)))
        top = sorted(range(521), key=lambda i: output[i], reverse=True)[:3]
        return {
            "speech": max(output[i] for i in (0, 1, 2, 3, 5, 12)),
            "music": output[132],
            "singing": max(output[i] for i in range(24, 33)),
            "rms": math.sqrt(sum(v * v for v in waveform) / len(waveform)),
            "top_classes": [{"name": self.labels[i], "score": round(output[i], 3)} for i in top],
        }

    def close(self):
        with self.lock:
            if self.interpreter:
                self.lib.TfLiteInterpreterDelete(self.interpreter)
                self.interpreter = None
            if self.model:
                self.lib.TfLiteModelDelete(self.model)
                self.model = None
