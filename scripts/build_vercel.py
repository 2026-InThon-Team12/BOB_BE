"""Fetch pinned official OCR data at build time, never at request time."""

import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFont

REVISION = "87416418657359cb625c412a48b6e1d6d41c29bd"
HASHES = {
    "eng": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "kor": "6b85e11d9bbf07863b97b3523b1b112844c43e713df8b66418a081fd1060b3b2",
}
ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    target = ROOT / "vendor" / "tessdata"
    target.mkdir(parents=True, exist_ok=True)
    for language, expected in HASHES.items():
        with urlopen(
            f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{REVISION}/{language}.traineddata",
            timeout=60,
        ) as response:
            data = response.read(20_000_001)
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"OCR model checksum mismatch: {language}")
        (target / f"{language}.traineddata").write_bytes(data)
    with urlopen(
        f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{REVISION}/LICENSE", timeout=30
    ) as response:
        (target / "LICENSE").write_bytes(response.read(100_000))

    # Exercise the same subprocess used at runtime, with both language models loaded.
    with Image.new("RGB", (500, 150), "white") as image:
        ImageDraw.Draw(image).text((30, 40), "BOB 12345", font=ImageFont.load_default(size=48), fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    result = subprocess.run(
        [sys.executable, "-m", "app.integrations.ocr_worker", str(target)],
        input=buffer.getvalue(),
        capture_output=True,
        timeout=30,
        cwd=ROOT,
        check=True,
    )
    if "12345" not in result.stdout.decode("utf-8"):
        raise RuntimeError("Bundled OCR smoke check failed")
    print("OCR build check passed (kor+eng).")
    # Opt-in diagnostic runs where the server key already lives. Only a random
    # digit image is sent; the probe prints safe booleans/codes, never credentials.
    if os.environ.get("BOB_VERIFY_LABLUP") == "true":
        subprocess.run([sys.executable, "-m", "scripts.check_lablup"], cwd=ROOT, timeout=90, check=True)


if __name__ == "__main__":
    main()
