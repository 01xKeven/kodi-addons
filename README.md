# 01xKeven Repository


## Install in Kodi

### Option 1 — Repository (recommended)

1. Download the repository zip: [repository.01xkeven-1.1.0.zip](https://raw.githubusercontent.com/01xKeven/kodi-addons/main/zips/repository.01xkeven/repository.01xkeven-1.1.0.zip)
2. In Kodi: **Settings → Add-ons → Install from zip file** → select the downloaded zip.
3. Go to **Install from repository → 01xKeven Repository** and install the add-ons you want.

### Option 2 — Repository URL (Kodi 19+)

1. In Kodi go to **Settings → Add-ons → Install from repository → Install from URL** (if your version supports it).
2. Use the URL: `https://01xkeven.github.io/kodi-addons/`

## Maintenance — publishing an add-on (please do not break the repo)

Kodi reads **one** file, `addons.xml`, to list the whole repository. If that file
stops being valid XML, every Kodi shows *"could not connect to the repository"*.
That is what happened once: a duplicated `<?xml ...?>` declaration was injected in
the middle of the file. These guards exist so it cannot happen again:

1. **Never edit `addons.xml` by hand.** Publish with:

   ```bash
   python tools/publish_addon.py --id service.guiaparental --message "what changed"
   ```

   It mirrors the add-on from Kodi's `addons` folder, rebuilds the zip, rewrites
   the `<addon>` block (BOM and `<?xml ...?>` stripped), regenerates
   `addons.xml.md5`, validates everything, commits, pushes, and finally checks what
   the repository URL really serves (GitHub's CDN caches for up to 5 minutes).
   Bump the version inside `addon.xml` first whenever the add-on content changes.

2. **Validate at any time:** `python tools/validate_repo.py` (add `--net` to also
   check the URL Kodi uses). It verifies: no stray XML declaration, valid XML,
   `addons.xml.md5` matches, every zip exists and contains the published version,
   and no user secret leaked into what is published.

3. **Local safety net:** `python tools/install_hooks.py` installs the versioned
   `pre-commit` hook, which runs the validator and **blocks the commit** if the
   repository would break.

4. **Cloud safety net:** `.github/workflows/validate-repo.yml` runs the same checks
   on every push to `main`, plus the URL/CDN check.

