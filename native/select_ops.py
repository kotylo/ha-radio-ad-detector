"""Build only the builtin operations in the checksum-pinned YAMNet model."""
from pathlib import Path
import re

root = Path("/src/tensorflow-2.14.0/tensorflow/lite")
allowed = {"ADD", "CONV_2D", "DEPTHWISE_CONV_2D", "FULLY_CONNECTED", "GATHER", "LOG", "LOGISTIC", "MEAN", "MUL", "PAD", "RESHAPE", "SLICE", "SPLIT", "SQRT"}
register = root / "core/kernels/register.cc"
source = register.read_text()
seen = set()


def keep(match):
    name = match.group(1)
    if name in allowed:
        seen.add(name)
        return match.group(0)
    return ""


source, count = re.subn(r"  AddBuiltin\(BuiltinOperator_(\w+),[\s\S]*?\);", keep, source)
if count < 100 or seen != allowed:
    raise RuntimeError(f"Unexpected upstream registrations: {count}, missing {allowed-seen}")
source = re.sub(r"  AddCustom\([\s\S]*?\);", "", source)
register.write_text(source)
cmake = root / "CMakeLists.txt"
text = cmake.read_text()
marker = 'populate_tflite_source_vars("kernels/internal" TFLITE_KERNEL_INTERNAL_SRCS)'
names = "activations|add|conv|depthwise_conv|fully_connected|gather|elementwise|reduce|mul|pad|reshape|slice|split|kernel_util|cpu_backend_context"
assert text.count(marker) == 1
text = text.replace(marker, f'list(FILTER TFLITE_KERNEL_SRCS INCLUDE REGEX "/({names})\\\\.cc$")\n' + marker)
cmake.write_text(text)
print("Selected YAMNet operators:", sorted(allowed))
