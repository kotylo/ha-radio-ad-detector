"""Install the uploaded ZIP on the Home Assistant host via copy-to-server.ps1."""
import fcntl
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def install(components, package):
    destination = components / "radio_ad_detector"
    deployments = package.parent
    backup = deployments / f"backup-{package.stem}"
    with TemporaryDirectory(dir=deployments) as temporary:
        staged = Path(temporary) / destination.name
        with ZipFile(package) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest["domain"] != destination.name:
                raise ValueError("Unexpected integration domain in the package")
            version = manifest["version"]
            archive.getinfo("__init__.py")
            archive.getinfo("native/SHA256SUMS.json")
            archive.extractall(staged)

        # Keep staged manifests and previous installations outside HA discovery.
        with (deployments / ".install.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            had_previous = destination.exists()
            if had_previous:
                destination.rename(backup)
            try:
                staged.rename(destination)
            except OSError:
                if had_previous:
                    backup.rename(destination)
                raise
    print(f"Installed Radio Ad Detector {version} at {destination}")
    if had_previous:
        print(f"Previous installation saved at {backup}")


if __name__ == "__main__":
    install(Path(sys.argv[1]), Path(sys.argv[2]))
