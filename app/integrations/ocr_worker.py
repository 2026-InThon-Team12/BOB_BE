"""Portable Tesseract binding, isolated in a process so OCR can be timed out.

PNG bytes enter via stdin; TSV leaves via stdout. No screenshot is written to disk.
The Linux wheel includes the native engine; only language data is bundled separately.
"""

import io
import sys

from PIL import Image


def main() -> int:
    from tesserocr import PSM, PyTessBaseAPI

    try:
        with Image.open(io.BytesIO(sys.stdin.buffer.read())) as image:
            with PyTessBaseAPI(path=sys.argv[1], lang="kor+eng", psm=PSM.SPARSE_TEXT) as engine:
                engine.SetImage(image)
                result = engine.GetTSVText(0)
        sys.stdout.buffer.write(result.encode("utf-8"))
        return 0
    except Exception:
        # Native errors and input content must not become provider logs.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
