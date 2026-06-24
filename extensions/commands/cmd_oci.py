"""Use an OCI registry as a Conan binary repository, via ORAS.

`conan oci push` / `conan oci pull` mirror `conan upload` / `conan download`,
but the remote is any OCI-conformant registry (GHCR, Docker Hub, ACR, zot, ...).
One OCI artifact == one Conan package: the package (recipe + binary, with
manifests and integrity preserved) is serialized by Conan's own
`cache.save`/`cache.restore` into a .tgz, which is the single layer of the artifact.

Requires the ORAS python client:  pip install oras==0.2.42
"""

import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from conan.api.model import ListPattern, PackagesList, MultiPackagesList
from conan.api.output import ConanOutput
from conan.cli import make_abs_path
from conan.cli.command import conan_command, conan_subcommand, OnceArgument
from conan.errors import ConanException


# ---- pure helpers (covered by test_cmd_oci.py) ----------------------------

_TAG_BAD = re.compile(r"[^A-Za-z0-9_.-]")


def _sanitize(s):
    """Make a string legal as the variable part of an OCI tag."""
    return _TAG_BAD.sub("_", s)


def _tag_for(version, recipe_revision, package_id):
    """name is encoded in the repo path; the tag identifies a binary within it.

    <version>-<rrev8>-<package_id>, or <version>-<rrev8>-recipe for a recipe-only
    artifact. Unique per (version, recipe-rev, package_id); a newer package-rev
    overwrites the tag (latest wins, like any registry tag).
    """
    rrev8 = (recipe_revision or "norev")[:8]
    return f"{_sanitize(version)}-{rrev8}-{package_id or 'recipe'}"


def _split_target(base):
    """'localhost:5000/conan' -> ('localhost:5000', 'conan'). Registry host kept,
    namespace path lowercased (OCI repositories must be lowercase)."""
    base = base.rstrip("/")
    if "/" not in base:
        raise ConanException(f"OCI target '{base}' must be <registry>/<namespace>, "
                             "e.g. ghcr.io/me/conan or localhost:5000/conan")
    host, _, ns = base.partition("/")
    return host, ns.lower()


# ---- ORAS adapter ---------------------------------------------------------
# ponytail: oras-py is pre-1.0 and churns across 0.2.x — version is pinned in
# README and every oras call lives here, so a future bump touches one place only.

def _oras_client(args):
    try:
        import oras.client
    except ImportError:
        raise ConanException("ORAS client not found - run: pip install oras==0.2.42")
    client = oras.client.OrasClient(insecure=args.insecure)
    if args.user and args.password:
        host = _split_target(args.target)[0]
        client.login(username=args.user, password=args.password, hostname=host,
                     tls_verify=not args.insecure)
    return client


def _auth_args(subparser):
    subparser.add_argument("target", help="OCI base, e.g. ghcr.io/me/conan")
    subparser.add_argument("--user", action=OnceArgument, help="Registry username")
    subparser.add_argument("--password", action=OnceArgument, help="Registry password/token")
    subparser.add_argument("--insecure", action="store_true",
                           help="Allow http / skip TLS verify (local registries)")


# ---- commands -------------------------------------------------------------

@conan_command(group="Custom commands")
def oci(conan_api, parser, *args):
    """Push/pull Conan packages to/from an OCI registry using ORAS."""


@conan_subcommand()
def oci_push(conan_api, parser, subparser, *args):
    """Push matching packages from the local cache to an OCI registry."""
    subparser.add_argument("reference", help="Conan reference/pattern, e.g. 'zlib/1.3.1' or 'zlib/*'")
    _auth_args(subparser)
    args = parser.parse_args(*args)
    out = ConanOutput()

    # package_id="*" makes the pattern include binaries (mirrors `conan upload`).
    pkg_list = conan_api.list.select(ListPattern(args.reference, package_id="*"), remote=None)
    _split_target(args.target)  # validate target shape early
    client = _oras_client(args)

    pushed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for ref, packages in pkg_list.items():
            entries = list(packages.items()) or [(None, None)]  # recipe-only if no binaries
            for pref, info in entries:
                full = pref.repr_notime() if pref else ref.repr_notime()
                single = PackagesList()
                single.add_ref(ref)
                if pref is not None:
                    single.add_pref(pref, info)
                tgz = os.path.join(tmp, "pkg.tgz")
                if os.path.exists(tgz):
                    os.remove(tgz)
                conan_api.cache.save(single, tgz)

                tag = _tag_for(str(ref.version), ref.revision,
                               pref.package_id if pref else None)
                target = f"{args.target.rstrip('/')}/{ref.name}:{tag}"
                annotations = {
                    "conan.reference": full,
                    "conan.recipe_revision": ref.revision or "",
                    "conan.package_id": pref.package_id if pref else "",
                    "conan.package_revision": (pref.revision or "") if pref else "",
                }
                client.push(target=target, files=[tgz], manifest_annotations=annotations,
                            disable_path_validation=True)  # tgz lives in a temp dir, not cwd
                out.info(f"Pushed {full} -> {target}")
                pushed += 1
    out.success(f"Pushed {pushed} artifact(s) to {args.target}")


@conan_subcommand()
def oci_pull(conan_api, parser, subparser, *args):
    """Pull packages from an OCI registry into the local cache.

    Give a reference/pattern, or --list with a Conan package-list JSON
    (as produced by `conan list ... --format=json`) to pull an explicit set.
    """
    subparser.add_argument("reference", nargs="?",
                           help="Conan reference/pattern, e.g. 'zlib/1.3.1' or 'zlib/*'")
    _auth_args(subparser)
    subparser.add_argument("--package-id", action=OnceArgument,
                           help="Only this package_id (pattern mode only)")
    subparser.add_argument("-l", "--list", action=OnceArgument,
                           help="Pull the packages in this Conan package-list JSON file")
    args = parser.parse_args(*args)
    out = ConanOutput()

    if bool(args.reference) == bool(args.list):
        raise ConanException("Specify exactly one of a reference/pattern or --list")

    _split_target(args.target)  # validate target shape early
    client = _oras_client(args)
    base = args.target.rstrip("/")

    if args.list:
        targets = _targets_from_list(base, make_abs_path(args.list))
        if not targets:
            out.warning(f"No packages in '{args.list}'")
            return
    else:
        targets = _targets_from_pattern(client, base, args.reference, args.package_id)
        if not targets:
            out.warning(f"No artifacts matched '{args.reference}' in {base}")
            return

    workers = conan_api.config.get("core.download:parallel", default=8, check_type=int)
    workers = max(1, min(workers, len(targets)))
    pulled = _pull_many(conan_api, client, targets, workers, out)
    out.success(f"Pulled {pulled} artifact(s) from {args.target}")


# ---- internals ------------------------------------------------------------

def _targets_from_pattern(client, base, reference, package_id):
    """List registry tags and match against a reference/pattern -> [(target, label)]."""
    name, _, rest = reference.partition("/")
    version_pat = rest.split("#")[0].split(":")[0] or "*"
    repo = f"{base}/{name}"
    try:
        tags = client.get_tags(repo)
    except Exception as e:
        raise ConanException(f"Could not list tags for {repo}: {e}")
    targets = []
    for tag in tags:
        target = f"{repo}:{tag}"
        ann = (client.get_manifest(target) or {}).get("annotations", {})
        full = ann.get("conan.reference", "")
        if not full.startswith(name + "/"):
            continue
        ver = full.split("/", 1)[1].split("#")[0].split(":")[0]
        if not _version_matches(version_pat, ver):
            continue
        if package_id and ann.get("conan.package_id") != package_id:
            continue
        targets.append((target, full))
    return targets


def _targets_from_list(base, listfile):
    """Read a Conan package-list JSON -> [(target, label)], computing tags directly."""
    ml = MultiPackagesList.load(listfile)  # validates JSON / rejects graph files
    targets = []
    for name, version, rrev, pkgid in _pkglist_entries(ml.serialize()):
        tag = _tag_for(version, rrev, pkgid)
        label = f"{name}/{version}#{rrev}" + (f":{pkgid}" if pkgid else "")
        targets.append((f"{base}/{name}:{tag}", label))
    return targets


def _pull_many(conan_api, client, targets, workers, out):
    """Pull each (target, label) and restore into the cache. Returns count pulled.

    Parallel over the network pull; restore is serialized.
    # ponytail: cache.restore writes the cache sqlite db, so it runs under a lock;
    #           parallelize restore only if it ever proves the bottleneck.
    # ponytail: one shared OrasClient across threads (requests handles concurrent
    #           distinct GETs); switch to per-worker clients if it ever races.
    """
    restore_lock = threading.Lock()
    with tempfile.TemporaryDirectory() as tmp:
        def fetch(i_target_label):
            i, (target, label) = i_target_label
            outdir = os.path.join(tmp, str(i))  # unique: every artifact is named pkg.tgz
            os.makedirs(outdir, exist_ok=True)
            files = client.pull(target=target, outdir=outdir)
            for f in files:
                if f.endswith(".tgz"):
                    with restore_lock:
                        conan_api.cache.restore(f)
                    os.remove(f)
            out.info(f"Pulled {label} <- {target}")
            return 1

        with ThreadPoolExecutor(max_workers=workers) as ex:
            return sum(ex.map(fetch, enumerate(targets)))


def _pkglist_entries(serialized):
    """Yield (name, version, recipe_revision, package_id_or_None) from a
    MultiPackagesList.serialize() dict, flattened across all remote keys.
    Recipe-only refs yield package_id=None. Entries with an 'error' are skipped."""
    for _remote, plist in serialized.items():
        if not isinstance(plist, dict) or "error" in plist:
            continue
        for ref, refdata in plist.items():
            head = ref.split("@", 1)[0]  # drop user/channel
            name, _, version = head.partition("/")
            for rrev, rdata in (refdata.get("revisions") or {}).items():
                packages = (rdata or {}).get("packages") or {}
                if not packages:
                    yield name, version, rrev, None
                for pkgid in packages:
                    yield name, version, rrev, pkgid


def _version_matches(pattern, version):
    """fnmatch-style match supporting Conan's '*' in the version slot."""
    import fnmatch
    return pattern in ("", "*") or fnmatch.fnmatch(version, pattern)
