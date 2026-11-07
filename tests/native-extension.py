# SPDX-License-Identifier: MIT
"""Exercise the actual locked librt extension used by Flask's typing group."""
from importlib.machinery import ExtensionFileLoader
from importlib.metadata import version
from pathlib import Path
import librt.base64

assert version('librt') == '0.13.0'
assert isinstance(librt.base64.__loader__, ExtensionFileLoader)
assert Path(librt.base64.__file__).suffix in ('.so', '.pyd')
assert librt.base64.b64encode(b'purified native C') == b'cHVyaWZpZWQgbmF0aXZlIEM='
assert librt.base64.b64decode(b'cHVyaWZpZWQgbmF0aXZlIEM=') == b'purified native C'
print(librt.base64.__file__)
