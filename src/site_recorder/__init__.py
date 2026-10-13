"""SITE Recorder — standalone data-collection recorder for SITE-Bench."""

__version__ = "0.1.0"

# The native-messaging host name the SITE Collector extension connects to.
# Fixed by the extension code (background.js NATIVE_HOST); the Chrome Web Store
# build uses the same name, so either extension works with this recorder.
NATIVE_HOST_NAME = "com.sitebench.collector"

# Extension ids allowed to talk to the host. The bundled extension carries the
# Web Store item's public key, so loading it unpacked yields the SAME id as the
# store install. The second id is the older pinned-unpacked build
# (manifest key from the attach-mode branch) — accepted for compatibility.
EXTENSION_ID = "gbhaoocbgjdighlaaflhgfbmaahgmjaa"
ALLOWED_EXTENSION_IDS = (EXTENSION_ID, "dobcejaanjflmobjanngcpchnmagpgdc")
WEBSTORE_URL = "https://chromewebstore.google.com/detail/" + EXTENSION_ID
