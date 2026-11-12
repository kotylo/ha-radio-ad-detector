"""Create the HACS/manual-install ZIP, requiring complete native runtimes."""
import argparse
import hashlib
import json
import struct
from pathlib import Path
import zipfile

NATIVE_FILES = (
    "libtensorflowlite_c.so",
    "TENSORFLOW-LICENSE.txt",
    "THIRD_PARTY_NOTICES.txt",
    "GCC-COPYING3.txt",
    "GCC-COPYING.RUNTIME.txt",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=["aarch64", "x86_64"], action="append")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    component = root / "custom_components" / "radio_ad_detector"
    architectures = args.arch or ["aarch64", "x86_64"]
    hashes = {}
    native_files = []
    for arch in architectures:
        directory = component / "native" / f"linux-{arch}-musl"
        artifacts = [directory / name for name in NATIVE_FILES]
        for artifact in artifacts:
            if not artifact.is_file():
                raise SystemExit(f"Missing native artifact: {artifact}. Build native/Dockerfile for {arch} first.")
        path = artifacts[0]
        binary = path.read_bytes()
        if len(binary) < 64 or binary[:4] != b"\x7fELF" or struct.unpack_from("<H", binary, 18)[0] != {"aarch64": 183, "x86_64": 62}[arch]:
            raise SystemExit(f"Native runtime has the wrong architecture: {path}")
        hashes[path.relative_to(component).as_posix()] = hashlib.sha256(binary).hexdigest()
        native_files.extend(artifacts)

    source_files = [
        *component.glob("*.py"),
        *component.joinpath("radio_audio").glob("*.py"),
        *component.joinpath("brand").glob("*.png"),
        component / "manifest.json",
        component / "services.yaml",
        component / "strings.json",
    ]
    destination = root / "dist"
    destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination / "radio_ad_detector.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_files + native_files):
            archive.write(path, path.relative_to(component).as_posix())
        archive.write(component / "strings.json", "translations/en.json")
        archive.writestr("native/SHA256SUMS.json", json.dumps(hashes, indent=2) + "\n")
    print(destination / "radio_ad_detector.zip")


if __name__ == "__main__":
    main()
