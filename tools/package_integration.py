"""Create the HACS/manual-install ZIP, requiring complete native runtimes."""
import argparse
import hashlib
import json
import re
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
    parser.add_argument("--keep-version", action="store_true", help="Rebuild the version already in manifest.json (used by CI)")
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
        component / "services.yaml",
        component / "strings.json",
    ]
    destination = root / "dist"
    destination.mkdir(exist_ok=True)
    output = destination / "radio_ad_detector.zip"
    manifest = component / "manifest.json"
    # Serialize version selection and publication across local packaging runs.
    lock = destination / ".packaging"
    try:
        lock.mkdir()
    except FileExistsError:
        raise SystemExit(f"Packaging is busy. If a previous run was interrupted, remove {lock} and retry.") from None
    staged_zip = lock / output.name
    staged_manifest = lock / manifest.name
    previous_manifest = lock / "previous.json"
    try:
        original = manifest.read_bytes()
        version = json.loads(original)["version"]
        contents = original
        if not args.keep_version:
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise SystemExit(f"Expected a major.minor.patch manifest version, found {version!r}")
            prefix, patch = version.rsplit(".", 1)
            version = f"{prefix}.{int(patch) + 1}"
            # Keep the manifest's compact arrays and existing indentation.
            contents = re.sub(
                rb'("version"\s*:\s*)"[^"]+"', rb'\g<1>"' + version.encode() + b'"',
                original, count=1,
            )
        with zipfile.ZipFile(staged_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source_files + native_files):
                archive.write(path, path.relative_to(component).as_posix())
            archive.writestr("manifest.json", contents)
            archive.write(component / "strings.json", "translations/en.json")
            archive.writestr("native/SHA256SUMS.json", json.dumps(hashes, indent=2) + "\n")
        if not args.keep_version:
            staged_manifest.write_bytes(contents)
            previous_manifest.write_bytes(original)
            staged_manifest.replace(manifest)
        try:
            staged_zip.replace(output)
        except OSError:
            if not args.keep_version:
                previous_manifest.replace(manifest)
            raise
    finally:
        for staged in (staged_zip, staged_manifest, previous_manifest):
            staged.unlink(missing_ok=True)
        lock.rmdir()
    print(f"Packaged Radio Ad Detector {version} at {output}")


if __name__ == "__main__":
    main()
