"""Portable Tesseract binding, isolated in a process so OCR can be timed out.

PNG bytes enter via stdin; TSV leaves via stdout. No screenshot is written to disk.
The Linux wheel includes the native engine; only language data is bundled separately.
"""

import io
import base64
import json
import sys

from PIL import Image


def main() -> int:
    from tesserocr import PSM, PyTessBaseAPI

    try:
        if len(sys.argv) > 2 and sys.argv[2] == "prices":
            payload = json.loads(sys.stdin.buffer.read())
            if not isinstance(payload, list) or len(payload) > 20:
                return 1
            results = []
            with PyTessBaseAPI(path=sys.argv[1], lang="kor+eng", psm=PSM.SINGLE_LINE) as engine:
                for encoded in payload:
                    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                        engine.SetImage(image)
                        results.append(engine.GetUTF8Text())
                        engine.Clear()
            sys.stdout.buffer.write(json.dumps(results, ensure_ascii=False).encode("utf-8"))
            return 0
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
