# kolbicz repo

Package repo for my tweaks, for rootless and roothide jailbreaks.

**Add to Sileo or Zebra:** `https://kolbicz.github.io/repo/`

| Package | Source |
|---|---|
| SBTweaker | [kolbicz/SBTweaker](https://github.com/kolbicz/SBTweaker) |
| Cylinder Reborn | [kolbicz/Cylinder-Reborn](https://github.com/kolbicz/Cylinder-Reborn) |
| OTA Toggle | [kolbicz/OTAToggle](https://github.com/kolbicz/OTAToggle) |
| AppEnumGuard | [kolbicz/AppEnumGuard](https://github.com/kolbicz/AppEnumGuard) |

## How it works

Every hour, the [Update repo](.github/workflows/update.yml) workflow runs
`scripts/build.py --fetch`. It downloads the `.deb` files of each package's
latest GitHub release into `debs/`, regenerates `Packages`, `Release`, the
Sileo and HTML depictions and the landing page, commits whatever changed and
publishes the site to GitHub Pages.

To publish a release right away instead of within the hour:

```sh
gh workflow run update.yml -R kolbicz/repo
```

## Adding a package

Add an entry to [packages.json](packages.json) with the package ID, its
GitHub repo and the text for its depiction, and put a square PNG icon at
`icons/<package id>.png`. Pushing the change runs the workflow.

Only release assets whose `Package:` matches the configured ID are imported.
Package managers only offer an update when the version is higher, so every
published build needs a new version number; a release asset replaced under
the same file name is not picked up again.

## Building locally

```sh
python3 scripts/build.py --fetch
```

Needs Python 3 and `dpkg-deb` (`brew install dpkg`); `zstd` is used when
installed.
