"""Opt-in live regression evaluation; fixture images stay outside the repository.

The manifest specifies image paths, expected names/prices and normalized photo
rectangles. Reports contain metrics only, never image bytes, tokens or replies.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx


def overlap(a, b):
    common = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - common
    return common / union if union > 0 else 0


def score(body, case):
    items, expected = body.get("items", []), case["expected"]
    result = {
        "selection": len(items) == len(expected) and not body.get("review_items"),
        "prices": len(items) == len(expected),
        "names": len(items) == len(expected),
        "photos": len(items) == len(expected),
    }
    photo_overlaps = []
    for item, target in zip(items, expected):
        result["prices"] &= item.get("price") == target["price"]
        name = item.get("name", "").replace(" ", "")
        result["names"] &= any(hint.replace(" ", "") in name for hint in target["name_hints"])
        photo = item.get("image_crop") or {}
        result["photos"] &= bool(photo.get("base64"))
        if "photo_box" in target:
            photo_overlap = overlap(photo.get("box", [0, 0, 0, 0]), target["photo_box"])
            photo_overlaps.append(round(photo_overlap, 3))
            result["photos"] &= photo_overlap >= 0.5
    summary = body.get("selection_summary") or {}
    if "excluded" in case:
        result["selection"] &= summary.get("excluded") == case["excluded"]
    result["pass"] = all(result.values())
    result["strategy"] = body.get("extraction_strategy")
    result["warnings"] = body.get("warnings", [])
    result["review_count"] = len(body.get("review_items", []))
    result["photo_overlaps"] = photo_overlaps
    result["verification_notes"] = sorted(
        {note for item in items + body.get("review_items", []) for note in item.get("verification_notes", [])}
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--vercel-cli", type=Path, help="Optional authenticated Vercel CLI JS entrypoint")
    parser.add_argument("--scope", default="mini-yujin")
    args = parser.parse_args()
    cases = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    base = args.base_url.rstrip("/")
    if not base.startswith("https://"):
        raise SystemExit("A HTTPS evaluation deployment is required")
    reports = []
    with httpx.Client(timeout=110) as http:

        def request(path, token=None, image=None):
            if args.vercel_cli:
                curl_args = ["--form", f"image=@{image};type=image/png"] if image else ["--request", "POST"]
                config = f'header = "Authorization: Bearer {token}"\n' if token else ""
                if token:
                    curl_args += ["--config", "-"]
                command = [
                    "node",
                    str(args.vercel_cli),
                    "curl",
                    path,
                    "--deployment",
                    base,
                    "--scope",
                    args.scope,
                    "--",
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--max-time",
                    "110",
                ] + curl_args
                response = subprocess.run(command, input=config.encode(), capture_output=True, timeout=150)
                if response.returncode:
                    raise RuntimeError("EVALUATION_HTTP_FAILED")
                return json.loads(response.stdout)
            headers = {"Authorization": "Bearer " + token} if token else {}
            if image:
                with image.open("rb") as source:
                    response = http.post(
                        base + path, headers=headers, files={"image": ("cart.png", source, "image/png")}
                    )
            else:
                response = http.post(base + path, headers=headers)
            response.raise_for_status()
            return response.json()

        token = request("/api/v1/guest-sessions")["access_token"]
        for run in range(args.runs):
            for case in cases:
                started = time.monotonic()
                try:
                    source = Path(case["image"])
                    if not source.is_absolute():
                        source = args.manifest.parent / source
                    body = request("/api/v1/cart-extractions", token, source)
                    report = score(body, case)
                except (httpx.HTTPError, RuntimeError, ValueError, OSError, subprocess.SubprocessError):
                    report = {"pass": False, "error": "EVALUATION_REQUEST_FAILED"}
                report.update(case=case["id"], run=run + 1, seconds=round(time.monotonic() - started, 2))
                reports.append(report)
                print(json.dumps(report, ensure_ascii=False), flush=True)
                args.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if all(r["pass"] for r in reports) else 1)


if __name__ == "__main__":
    main()
