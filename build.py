#!/usr/bin/env python3
"""Build the site visit photo map.

    python3 build.py            # process new/changed photos, write data/photos.json
    python3 build.py --publish  # ...then commit the generated files and push

Originals are read from the folder named in config.json and never modified.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
VENV_PY = REPO / ".venv" / "bin" / "python"

# Use the repo's virtualenv automatically, so plain `python3 build.py` works.
try:
    import pillow_heif  # noqa: F401
    import rawpy  # noqa: F401
    from PIL import Image  # noqa: F401
    from tqdm import tqdm  # noqa: F401
except ImportError:
    if VENV_PY.exists() and Path(sys.prefix).resolve() != VENV_PY.parent.parent.resolve():
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve())] + sys.argv[1:])
    sys.exit(
        "Missing Python packages. One-time setup (see README):\n"
        "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    )

import argparse
import hashlib
import io
import json
import math
import shutil
import subprocess
import tempfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed

import pillow_heif
import rawpy
from PIL import Image, ImageCms, ImageOps
from tqdm import tqdm

pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = 300_000_000

PHOTO_EXTS = {
    ".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".avif",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2",
}
RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2"}
KNOWN_SKIP = {".mov": "video", ".mp4": "video", ".m4v": "video", ".aae": "iPhone edit sidecar",
              ".ds_store": "macOS metadata", ".xmp": "metadata sidecar", ".gif": "animated/graphic"}
# Distinct on both light street tiles and dark satellite imagery (dots get a white outline).
PALETTE = ["#e6194b", "#2f6fe4", "#f5b700", "#8e3fd8", "#00b3c7", "#ff7a00",
           "#2fb24c", "#ff4fa0", "#8d5a2b", "#5a6b7d", "#b5d000", "#00366e"]
MANIFEST = REPO / ".cache" / "manifest.json"
HASHCACHE = REPO / ".cache" / "hashes.json"     # size/mtime -> sha1 speedup; not committed
REPORT = REPO / ".cache" / "last-report.txt"
FULL_DIR = REPO / "photos" / "full"
THUMB_DIR = REPO / "photos" / "thumb"
DATA = REPO / "data" / "photos.json"
PUBLISHED = ["index.html", "app.js", "style.css", "data", "photos", ".nojekyll"]
GENERATED = ["data", "photos", ".cache/manifest.json"]
EXIF_TAGS = ["GPSLatitude", "GPSLongitude", "GPSHPositioningError", "GPSImgDirection",
             "DateTimeOriginal", "OffsetTimeOriginal", "CreateDate", "OffsetTime", "Orientation"]


# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_if_changed(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def display_name(folder):
    name = folder.strip()
    if name.lower().endswith(" photos"):
        name = name[: -len(" photos")].strip()
    return name or folder.strip()


def human(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def dir_size(p):
    p = REPO / p
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


# ---------------------------------------------------------------- metadata

def run_exiftool(paths):
    """One exiftool call for the whole batch. Returns {abs path: tag dict}."""
    if not paths:
        return {}
    if not shutil.which("exiftool"):
        sys.exit("exiftool is not installed. Run:  brew install exiftool")
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
        f.write("\n".join(str(p) for p in paths) + "\n")
        argfile = f.name
    try:
        cmd = ["exiftool", "-json", "-n", "-q", "-q", "-charset", "filename=utf8"]
        cmd += [f"-{t}" for t in EXIF_TAGS] + ["-@", argfile]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
    finally:
        os.unlink(argfile)
    result = {}
    for rec in json.loads(out or "[]"):
        result[os.path.normpath(rec["SourceFile"])] = rec
    return result


def parse_meta(rec):
    def num(key):
        v = rec.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    lat, lon = num("GPSLatitude"), num("GPSLongitude")
    if lat is None or lon is None or (abs(lat) < 1e-6 and abs(lon) < 1e-6) \
            or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    acc, bearing = num("GPSHPositioningError"), num("GPSImgDirection")

    taken = None
    for dt_key, off_key in (("DateTimeOriginal", "OffsetTimeOriginal"), ("CreateDate", "OffsetTime")):
        dt = rec.get(dt_key)
        if isinstance(dt, str) and len(dt) >= 19 and not dt.startswith("0000"):
            taken = dt[:10].replace(":", "-") + "T" + dt[11:19]
            off = rec.get(off_key)
            if isinstance(off, str) and len(off) == 6 and off[0] in "+-":
                taken += off
            break

    return {
        "lat": round(lat, 7) if lat is not None else None,
        "lon": round(lon, 7) if lon is not None else None,
        "gps_accuracy_m": round(acc, 1) if acc is not None and lat is not None else None,
        "bearing_deg": round(bearing % 360, 1) if bearing is not None and lat is not None else None,
        "taken_at": taken,
        "orientation": int(rec["Orientation"]) if isinstance(rec.get("Orientation"), (int, float)) else 1,
    }


# ---------------------------------------------------------------- decoding (runs in worker processes)

SRGB = ImageCms.createProfile("sRGB")


def to_srgb(im):
    """Flatten alpha, normalise bit depth and convert embedded colour profiles to sRGB."""
    icc = im.info.get("icc_profile")
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        im = im.point(lambda v: v / 256).convert("L")
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    elif im.mode == "CMYK" and not icc:
        im = im.convert("RGB")
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            im = ImageCms.profileToProfile(im, src, SRGB, outputMode="RGB",
                                           renderingIntent=ImageCms.Intent.PERCEPTUAL)
        except Exception:
            pass
    return im.convert("RGB") if im.mode != "RGB" else im


def open_raw(path, orientation, min_preview):
    with rawpy.imread(str(path)) as raw:
        try:
            thumb = raw.extract_thumb()
        except Exception:
            thumb = None
        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            im = Image.open(io.BytesIO(thumb.data))
            if max(im.size) >= min_preview:
                im.load()
                if im.getexif().get(0x0112, 1) != 1:
                    return ImageOps.exif_transpose(im)
                return apply_orientation(im, orientation)
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)  # applies the raw's own flip
    return Image.fromarray(rgb)


def apply_orientation(im, orientation):
    ops = {2: [Image.Transpose.FLIP_LEFT_RIGHT], 3: [Image.Transpose.ROTATE_180],
           4: [Image.Transpose.FLIP_TOP_BOTTOM], 5: [Image.Transpose.TRANSPOSE],
           6: [Image.Transpose.ROTATE_270], 7: [Image.Transpose.TRANSVERSE],
           8: [Image.Transpose.ROTATE_90]}
    for op in ops.get(orientation, []):
        im = im.transpose(op)
    return im


def resized(im, long_edge):
    w, h = im.size
    scale = long_edge / max(w, h)
    if scale >= 1:
        return im.copy()
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return im.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)


def save_atomic(im, dest, fmt, **kw):
    tmp = dest.with_name(dest.name + ".tmp")
    im.save(tmp, fmt, **kw)
    os.replace(tmp, dest)


def process_photo(job):
    """Decode one original and write its two EXIF-free derivatives. Never raises."""
    try:
        path = Path(job["path"])
        if path.suffix.lower() in RAW_EXTS:
            im = open_raw(path, job["orientation"], job["raw_min_preview"])
        else:
            im = Image.open(path)
            if im.format == "JPEG":
                im.draft("RGB", (job["full"]["long_edge"],) * 2)
            im.load()
            im = ImageOps.exif_transpose(im)
        im = to_srgb(im)
        im.info = {}  # drop EXIF/XMP/ICC so nothing leaks into the derivatives

        full = resized(im, job["full"]["long_edge"])
        save_atomic(full, Path(job["full_out"]), "JPEG", quality=job["full"]["quality"],
                    optimize=True, progressive=True, subsampling="4:2:0")
        thumb = resized(full, job["thumb"]["long_edge"])
        save_atomic(thumb, Path(job["thumb_out"]), "WEBP", quality=job["thumb"]["quality"], method=6)
        return {"sha": job["sha"], "width": thumb.size[0], "height": thumb.size[1]}
    except Exception as e:
        msg = str(e).replace(f"'{job['path']}'", "").replace(job["path"], "").strip(" :")
        return {"sha": job["sha"], "error": f"{type(e).__name__}: {msg}" if msg else type(e).__name__}


# ---------------------------------------------------------------- main build

def scan(source, hidden):
    """Walk the source folder. Returns (photos, skipped, unsupported, hidden_found)."""
    photos, skipped, unsupported, hidden_found = [], [], [], []
    for top in sorted(source.iterdir(), key=lambda p: p.name):
        if top.name.startswith("."):
            continue
        if not top.is_dir():
            unsupported.append((str(top.relative_to(source)), "not inside a student folder"))
            continue
        key = top.name.strip()
        if key in hidden:
            hidden_found.append(key)
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                rel = str(p.relative_to(source))
                ext = p.suffix.lower() if p.suffix else fn.lower()
                if fn.lower() == ".ds_store":
                    continue  # silent: always present on macOS
                if fn.startswith("._"):
                    skipped.append((rel, "macOS resource fork"))
                elif ext in PHOTO_EXTS:
                    photos.append({"path": p, "rel": rel, "key": key, "ext": ext})
                elif ext in KNOWN_SKIP:
                    skipped.append((rel, KNOWN_SKIP[ext]))
                else:
                    unsupported.append((rel, "not a supported photo format"))
    return photos, skipped, unsupported, hidden_found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--publish", action="store_true", help="commit generated files and push to GitHub")
    ap.add_argument("--rebuild", action="store_true", help="regenerate every derivative from scratch")
    args = ap.parse_args()

    config = load_json(REPO / "config.json", {})
    source = (REPO / config.get("source_dir", "../site visit photos")).resolve()
    if not source.is_dir():
        sys.exit(f"Source folder not found: {source}\nEdit source_dir in config.json.")
    full_cfg = {"long_edge": 2400, "quality": 82, **config.get("full", {})}
    thumb_cfg = {"long_edge": 480, "quality": 75, **config.get("thumb", {})}
    raw_min_preview = config.get("raw_min_preview_px", 2000)
    overrides = load_json(REPO / "students.json", {})
    student_over = overrides.get("students", {})
    hidden = {h.strip() for h in overrides.get("hidden", [])}

    manifest = load_json(MANIFEST, {})
    settings = {"full": full_cfg, "thumb": thumb_cfg, "raw_min_preview_px": raw_min_preview, "v": 1}
    known = manifest.get("photos", {}) if manifest.get("settings") == settings and not args.rebuild else {}
    colors = dict(manifest.get("colors", {}))
    hashcache = load_json(HASHCACHE, {})

    print(f"Source: {source}")
    photos, skipped, unsupported, hidden_found = scan(source, hidden)

    # 1. Hash every original (cached by size + mtime).
    new_hashcache = {}
    for ph in tqdm(photos, desc="Hashing", unit="file", leave=False, disable=None):
        st = ph["path"].stat()
        sig = f"{st.st_size}:{st.st_mtime_ns}"
        cached = hashcache.get(ph["rel"])
        ph["sha"] = cached["sha"] if cached and cached["sig"] == sig else sha1_file(ph["path"])
        new_hashcache[ph["rel"]] = {"sig": sig, "sha": ph["sha"]}

    # 2. Metadata: one exiftool call for every file we have not seen before.
    need_meta = [ph for ph in photos if ph["sha"] not in known]
    exif = run_exiftool([os.path.normpath(ph["path"]) for ph in need_meta])
    for ph in photos:
        if ph["sha"] in known:
            ph["meta"] = {k: known[ph["sha"]].get(k) for k in
                          ("lat", "lon", "gps_accuracy_m", "bearing_deg", "taken_at", "orientation")}
        else:
            ph["meta"] = parse_meta(exif.get(os.path.normpath(ph["path"]), {}))

    # 3. Duplicates: identical bytes, then HEIC/JPG export pairs (same folder, name and capture time).
    duplicates, by_sha, kept = [], {}, []
    for ph in photos:
        if ph["sha"] in by_sha:
            duplicates.append((ph["rel"], f"identical to {by_sha[ph['sha']]['rel']}"))
        else:
            by_sha[ph["sha"]] = ph
            kept.append(ph)
    pairs = defaultdict(list)
    for ph in kept:
        pairs[(str(ph["path"].parent), ph["path"].stem.lower())].append(ph)
    drop = set()
    for group in pairs.values():
        if len(group) < 2:
            continue
        times = {g["meta"]["taken_at"] for g in group}
        if len(times) != 1 or None in times:
            continue
        rank = lambda g: (g["meta"]["lat"] is None, g["ext"] not in (".heic", ".heif"), g["rel"])
        group = sorted(group, key=rank)
        for g in group[1:]:
            drop.add(g["sha"])
            duplicates.append((g["rel"], f"same shot as {group[0]['rel']}"))
    kept = [ph for ph in kept if ph["sha"] not in drop]

    # 4. Decode new photos in a process pool.
    FULL_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    for ph in kept:
        sha = ph["sha"]
        full_out, thumb_out = FULL_DIR / f"{sha}.jpg", THUMB_DIR / f"{sha}.webp"
        if sha in known and full_out.exists() and thumb_out.exists():
            continue
        jobs.append({"sha": sha, "path": str(ph["path"]), "orientation": ph["meta"]["orientation"],
                     "full": full_cfg, "thumb": thumb_cfg, "raw_min_preview": raw_min_preview,
                     "full_out": str(full_out), "thumb_out": str(thumb_out)})

    failed, results = [], {}
    if jobs:
        workers = config.get("workers") or max(1, (os.cpu_count() or 2) - 1)
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            futures = [pool.submit(process_photo, j) for j in jobs]
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Processing", unit="photo"):
                r = fut.result()
                results[r["sha"]] = r
    new_shas = set()
    for ph in kept:
        r = results.get(ph["sha"])
        if r and "error" in r:
            failed.append((ph["rel"], r["error"]))
            for d in (FULL_DIR / f"{ph['sha']}.jpg", THUMB_DIR / f"{ph['sha']}.webp"):
                d.unlink(missing_ok=True)
        elif r:
            new_shas.add(ph["sha"])
    failed_shas = {ph["sha"] for ph in kept if ph["rel"] in {f[0] for f in failed}}
    kept = [ph for ph in kept if ph["sha"] not in failed_shas]

    # 5. New manifest; remove derivatives of photos that disappeared.
    photos_manifest = {}
    for ph in kept:
        sha = ph["sha"]
        dims = results.get(sha) or known[sha]
        photos_manifest[sha] = {**ph["meta"], "width": dims["width"], "height": dims["height"]}
    old_all = set(manifest.get("photos", {}))
    removed = sorted(old_all - set(photos_manifest))
    valid = set(photos_manifest)
    for d, ext in ((FULL_DIR, ".jpg"), (THUMB_DIR, ".webp")):
        for f in d.iterdir():
            if f.name.startswith("."):
                continue
            if f.suffix != ext or f.stem not in valid:
                f.unlink()

    # 6. Students: stable colours (first unused palette colour, remembered in the manifest).
    present_keys = sorted({ph["key"] for ph in kept} | {p.name.strip() for p in source.iterdir()
                                                         if p.is_dir() and not p.name.startswith(".")
                                                         and p.name.strip() not in hidden})
    for key in present_keys:
        if key not in colors:
            used = set(colors.values())
            colors[key] = next((c for c in PALETTE if c not in used), PALETTE[len(colors) % len(PALETTE)])
    colors = {k: v for k, v in colors.items() if k in present_keys}

    def student_name(key):
        return student_over.get(key, {}).get("name") or display_name(key)

    counts = defaultdict(int)
    for ph in kept:
        counts[ph["key"]] += 1
    students = [{"key": k, "name": student_name(k), "color": student_over.get(k, {}).get("color") or colors[k],
                 "count": counts[k]} for k in present_keys]
    students.sort(key=lambda s: s["name"].lower())

    # 7. data/photos.json — stable order, one photo per line for readable diffs.
    records = []
    for ph in kept:
        m = photos_manifest[ph["sha"]]
        rec = {"id": ph["sha"][:16], "student": student_name(ph["key"]),
               "original_filename": ph["path"].name,
               "lat": m["lat"], "lon": m["lon"]}
        if m["gps_accuracy_m"] is not None:
            rec["gps_accuracy_m"] = m["gps_accuracy_m"]
        if m["bearing_deg"] is not None:
            rec["bearing_deg"] = m["bearing_deg"]
        rec.update({"taken_at": m["taken_at"], "width": m["width"], "height": m["height"],
                    "thumb": f"photos/thumb/{ph['sha']}.webp", "full": f"photos/full/{ph['sha']}.jpg"})
        records.append(rec)
    records.sort(key=lambda r: (r["taken_at"] or "~", r["student"].lower(), r["original_filename"], r["id"]))

    head = {"title": config.get("title", "Site Visit Photo Map"), "students": students}
    lines = ["{"]
    lines.append(f' "title": {json.dumps(head["title"], ensure_ascii=False)},')
    if config.get("site"):
        lines.append(f' "site": {json.dumps(config["site"], ensure_ascii=False)},')
    lines.append(' "students": [')
    lines.append(",\n".join("  " + json.dumps(s, ensure_ascii=False) for s in students))
    lines.append(" ],")
    lines.append(' "photos": [')
    lines.append(",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in records))
    lines.append(" ]")
    lines.append("}")
    write_if_changed(DATA, "\n".join(lines) + "\n")
    write_if_changed(MANIFEST, json.dumps({"settings": settings, "colors": colors, "photos": photos_manifest},
                                          indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    write_if_changed(HASHCACHE, json.dumps(new_hashcache, indent=0, sort_keys=True, ensure_ascii=False) + "\n")

    # 8. Report.
    new_by_student = defaultdict(int)
    for ph in kept:
        if ph["sha"] in new_shas and ph["sha"] not in old_all:
            new_by_student[student_name(ph["key"])] += 1
    n_new = sum(new_by_student.values())
    n_reprocessed = len(new_shas) - n_new
    out = []
    p = out.append
    p("")
    p("=" * 64)
    p(f" BUILD REPORT — {head['title']}")
    p("=" * 64)
    p(f" Photos on the map site: {len(kept)}"
      f"   ({sum(1 for r in records if r['lat'] is not None)} with location)")
    p(f" New: {n_new}   Unchanged: {len(kept) - len(new_shas)}   Removed: {len(removed)}   Failed: {len(failed)}"
      + (f"   Re-processed: {n_reprocessed}" if n_reprocessed else ""))
    p("")
    p(" Per student:")
    for s in students:
        nolo = sorted(ph["path"].name for ph in kept
                      if ph["key"] == s["key"] and photos_manifest[ph["sha"]]["lat"] is None)
        extra = f"  (+{new_by_student[s['name']]} new)" if new_by_student.get(s["name"]) else ""
        p(f"   {s['name']:<24} {s['count']:>4} photos{extra}")
        if nolo:
            p(f"      NO LOCATION ({len(nolo)}) — ask for the original files:")
            for n in nolo:
                p(f"        - {n}")
    if hidden_found:
        p(f"\n Hidden by students.json: {', '.join(hidden_found)}")
    for title, items in (("FAILED (could not be read — check the file)", failed),
                         ("Duplicates (skipped)", duplicates),
                         ("Skipped (not photos)", skipped),
                         ("Unsupported files", unsupported)):
        if items:
            p(f"\n {title}: {len(items)}")
            for rel, why in items:
                p(f"   - {rel}  [{why}]")
    site_bytes = sum(dir_size(x) for x in PUBLISHED if (REPO / x).exists())
    p(f"\n Published site size: {human(site_bytes)}")
    if site_bytes > 800 * 1024 * 1024:
        p(" WARNING: the site is over 800 MB. GitHub Pages stops at 1 GB —"
          " lower full.long_edge or full.quality in config.json, or hide a folder.")
    p("=" * 64)
    report = "\n".join(out)
    print(report)
    REPORT.write_text(report + "\n", encoding="utf-8")

    if args.publish:
        publish(new_by_student, len(removed))


def publish(new_by_student, n_removed):
    def git(*a, check=True):
        return subprocess.run(["git", "-C", str(REPO), *a], check=check, capture_output=True, text=True)

    git("add", "-A", "--", *GENERATED)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("\nNothing new to publish — the live site is already up to date.")
        return
    parts = []
    n_new = sum(new_by_student.values())
    if n_new:
        who = ", ".join(sorted(new_by_student))
        parts.append(f"Add {n_new} photo{'s' if n_new != 1 else ''} ({who})")
    if n_removed:
        parts.append(f"{'remove' if parts else 'Remove'} {n_removed} photo{'s' if n_removed != 1 else ''}")
    msg = ", ".join(parts) or "Update photo data"
    git("commit", "-m", msg)
    print(f"\nCommitted: {msg}\nPushing to GitHub…")
    r = git("push", check=False)
    if r.returncode != 0:
        print(r.stderr)
        sys.exit("Push failed — see the message above. Your commit is saved locally; run `git push` to retry.")
    print("Pushed. The live site updates in about a minute.")


if __name__ == "__main__":
    main()
