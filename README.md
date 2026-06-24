# oras-conan

A Conan 2.x custom command that uses any **OCI registry** (GHCR, Docker Hub, ACR,
ECR, Harbor, zot, a local `registry:2` …) as a Conan binary package repository, via
[ORAS](https://oras.land). It mirrors `conan upload` / `conan download`:

```sh
conan oci push  "zlib/1.3.1:*"  ghcr.io/me/conan
conan oci pull  "zlib/1.3.1"    ghcr.io/me/conan
```

**One OCI artifact = one Conan package.** Each package (recipe + binary) is
serialized by Conan's own `cache.save` (manifests and integrity preserved), pushed
as the single layer of an OCI artifact, and restored on pull with `cache.restore` —
so a pulled package is byte-identical and passes `conan cache check-integrity`.

## Install

```sh
uv pip install "oras==0.2.42"       # required; install into the SAME interpreter Conan runs on
conan config install https://github.com/ta7mid/oras-conan.git
# or, from a local clone:  conan config install .
```

> The ORAS python client must be importable by the Python that runs `conan`. With a
> Homebrew/pipx Conan that's its bundled interpreter — point `uv` at it:
> ```sh
> uv pip install --python "$(head -1 "$(which conan)" | sed 's/^#!//')" "oras==0.2.42"
> ```
> (Plain `pip` works too: `… -m pip install oras==0.2.42`.)

## Usage

```
conan oci push <reference-pattern> <oci-base> [--user U --password P] [--insecure]
conan oci pull <reference>         <oci-base> [--package-id ID] [--user U --password P] [--insecure]
conan oci pull --list <pkglist.json> <oci-base> [--user U --password P] [--insecure]
```

- `<oci-base>` is `<registry>/<namespace>`, e.g. `ghcr.io/me/conan`. The Conan package
  name is appended as a sub-repository: `zlib` → `ghcr.io/me/conan/zlib`.
- `push` reads matching packages from the **local cache** (build something with
  `conan create` first). `push "zlib/*"` expands like `conan upload`.
- `pull` lists tags in the registry, matches by reference, and restores into the cache.
- `pull --list <pkglist.json>` pulls an explicit set from a Conan package-list JSON
  (the format `conan list … --format=json` emits) instead of a pattern — exact tags are
  computed directly, no tag listing needed. Packages are pulled **in parallel**
  (worker count from Conan's `core.download:parallel`, default 8). Example:
  ```sh
  conan list "zlib/*#*:*#*" --format=json > pkglist.json
  conan oci pull --list pkglist.json ghcr.io/me/conan
  ```
- `--insecure` allows http / skips TLS verify — for local/test registries.
- Auth: pass `--user/--password`, or rely on an existing `docker login` /
  `~/.docker/config.json`. For GHCR the password is a PAT with `write:packages`.

### Naming scheme

- **Repo:** `<base>/<name>` (lowercased).
- **Tag:** `<version>-<recipe_rev[:8]>-<package_id>` (recipe-only artifacts use `…-recipe`).
  Unique per (version, recipe revision, package_id); a newer package revision
  overwrites the tag (latest wins, like any registry tag).
- **Manifest annotations** carry the authoritative data: `conan.reference`,
  `conan.recipe_revision`, `conan.package_id`, `conan.package_revision`.

## Test

```sh
python test_cmd_oci.py        # pure-logic self-test (no Conan/ORAS needed)
```

End-to-end against a throwaway registry:

```sh
docker run -d -p 5001:5000 --name reg registry:2
conan create .   # in some package dir, to populate the cache
conan oci push "yourpkg/*" localhost:5001/conan --insecure
conan remove "yourpkg/*" -c
conan oci pull "yourpkg/*" localhost:5001/conan --insecure   # back in cache, integrity ok
```

## Troubleshooting

- **Push hangs on macOS / Docker Desktop:** ORAS reads `~/.docker/config.json` and can
  stall on the `docker-credential-desktop` helper. Point it at a clean config for the
  command: `DOCKER_CONFIG=$(mktemp -d) conan oci push … --user … --password …`.
- **GHCR `UNSUPPORTED` / `DENIED` on push:** the token lacks `write:packages`. With the
  GitHub CLI: `gh auth refresh -h github.com -s write:packages,read:packages`, then
  `--password "$(gh auth token)"`. Or use a classic PAT with `write:packages`.

## Limitations

- No referrers/`oras discover` query (oras-py lacks the API); discovery is tag-based.
- No tag deletion (`oci remove`), no parallel transfer, no delta/skip-existing.
- `oras` 0.2.x is pre-1.0 and pinned; all ORAS calls live in one adapter in
  `extensions/commands/cmd_oci.py`, so a future bump touches one place.
