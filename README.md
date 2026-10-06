# Arc2 Mission Hill Photo Map

Every photo from the Architecture Studio 2 site visit, mapped by where it was taken.

**Live site:** https://samueld-owen.github.io/site-visit-map/

The build script reads the original photos from `../site visit photos/`, makes web-sized copies with all camera metadata removed, and writes their locations to `data/photos.json`. The originals are never changed, moved or uploaded.

---

## One-time setup (already done on Sam's Mac)

```bash
brew install exiftool
cd ~/Documents/Claude/Projects/"Teaching tools"/site-visit-map
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`python3 build.py` automatically uses the `.venv` folder, so there's nothing to activate.

---

## Adding a new batch of photos

1. Put the student's folder inside `Teaching tools/site visit photos/`. **The folder name becomes the student's name.** A trailing " Photos" is dropped, so `J Smith Photos` shows as **J Smith**. Subfolders inside it count as that student's photos.
2. In Terminal:
   ```bash
   cd ~/Documents/Claude/Projects/"Teaching tools"/site-visit-map
   python3 build.py --publish
   ```
3. Read the report it prints (a copy is saved to `.cache/last-report.txt`).
4. The live site updates about a minute after it says "Pushed". Hard-refresh the page (Cmd-Shift-R) if you still see the old version.

Only new or changed photos are processed, so reruns are quick. If you run it without `--publish`, it builds locally and doesn't upload anything. To preview locally:

```bash
python3 -m http.server 8000
```

then open http://localhost:8000.

**Removing photos:** delete them, or the whole folder, from `site visit photos/` and run `python3 build.py --publish` again. Their web copies are removed.

**Formats:** HEIC, JPG, PNG, TIFF, WebP, AVIF, DNG, and camera RAW (CR2, CR3, NEF, ARW, RAF, ORF, RW2). Videos, Live Photo `.MOV` files, `.AAE` files and anything else are skipped and listed in the report. If a student exports both a HEIC and a JPG of the same shot, only one is kept.

---

## Fixing a student's name or color, or hiding a folder

Edit `students.json`. The keys are the folder names (without trailing spaces):

```json
{
  "students": {
    "J Smith Photos": { "name": "Jordan S.", "color": "#2f6fe4" },
    "Ethan": { "name": "Ethan" }
  },
  "hidden": ["Old test folder"]
}
```

- `name` changes the label shown on the page.
- `color` is optional. Otherwise each student is given the next unused color, and it stays fixed once assigned.
- Folders listed in `hidden` are left off the site entirely.

Then run `python3 build.py --publish`. Name and color changes don't reprocess any photos.

---

## Photos with "no location"

These photos still appear in the grid with a small **no location** badge, but they have no dot on the map. The report lists them by student under **NO LOCATION**.

They usually come from a transfer that stripped the GPS: AirDrop with "All Photos Data" turned off, Messages, WhatsApp, Google Photos downloads, screenshots, or edited exports. Ask the student to send the **original** files instead:

- **iPhone → Mac:** AirDrop, then tap **Options** at the top of the share sheet and turn on **All Photos Data**. Or import with the Photos app or Image Capture.
- **Google Drive / OneDrive upload:** upload from the Files app or a computer, not from inside Messages.
- Check that **Location Services → Camera** was on when the photos were taken. If it was off, the location can't be recovered.

When a student sends originals, drop them in to replace the old files and rebuild. The build recognizes them as changed.

---

## Settings (`config.json`)

| Setting | Meaning |
|---|---|
| `title` | Page title |
| `site` | The site location (25 Calumet St) used for the black diamond on the map and the "Distance from site" sort. If you change the address, also update `lat`/`lon` (right-click the spot in Google Maps to copy them). |
| `source_dir` | Where the originals are, relative to this folder |
| `full.long_edge`, `full.quality` | Size and quality of the image that opens when you click a photo (2400 px, 82) |
| `thumb.long_edge`, `thumb.quality` | Grid thumbnail (480 px WebP, 75) |
| `workers` | Number of parallel decoders (`null` = automatic) |

Changing a size or quality setting reprocesses every photo on the next build.

**Site size:** GitHub Pages allows 1 GB. Each photo adds about 1 MB, so 350 photos is roughly 360 MB. The build warns you above 800 MB. If you get close, lower `full.long_edge` to 2000 or `full.quality` to 75.

---

## What's in this repo

| Path | |
|---|---|
| `build.py` | The build script |
| `index.html`, `app.js`, `style.css` | The page: plain JavaScript and Leaflet 1.9.4 |
| `data/photos.json` | Generated photo list with locations |
| `photos/full/`, `photos/thumb/` | Generated web images, named by a hash of the original file |
| `.cache/manifest.json` | Build cache, so reruns only process new photos |

Map tiles: © OpenStreetMap contributors (street) and Esri World Imagery (satellite).
