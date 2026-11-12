"""Collect upstream license texts into the distributable native artifact."""
from pathlib import Path
import urllib.request

out = Path("/out")
out.mkdir(exist_ok=True)
roots = [Path("/src/tensorflow-2.14.0"), Path("/build")]
texts = []
seen = set()
for root in roots:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name.upper() in ("LICENSE", "LICENSE.TXT", "LICENSE.MD", "COPYING", "COPYRIGHT"):
            text = path.read_text(errors="replace")
            if text not in seen:
                seen.add(text)
                texts.append(f"\n{'=' * 70}\n{path}\n{'=' * 70}\n{text}\n")
(out / "THIRD_PARTY_NOTICES.txt").write_text("".join(texts))
(out / "TENSORFLOW-LICENSE.txt").write_text(Path("/src/tensorflow-2.14.0/LICENSE").read_text())
for name in ("COPYING3", "COPYING.RUNTIME"):
    url = f"https://raw.githubusercontent.com/gcc-mirror/gcc/releases/gcc-15/{name}"
    with urllib.request.urlopen(url, timeout=60) as response:
        (out / f"GCC-{name}.txt").write_bytes(response.read())
