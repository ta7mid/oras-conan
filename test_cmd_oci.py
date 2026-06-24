"""Self-test for the pure logic in cmd_oci.py (no Conan/ORAS needed).

Run:  python test_cmd_oci.py
"""
import importlib.util
import os
import re
import sys

# Load cmd_oci.py without importing conan (it only imports conan at module top,
# so we exec just the helper functions we need by stubbing the conan imports).
HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "extensions", "commands", "cmd_oci.py")

# Stub the conan modules so the import at the top of cmd_oci.py succeeds.
import types
for name in ("conan", "conan.api", "conan.api.model", "conan.api.output",
             "conan.cli", "conan.cli.command", "conan.errors"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["conan.api.model"].ListPattern = object
sys.modules["conan.api.model"].PackagesList = object
sys.modules["conan.api.model"].MultiPackagesList = object
sys.modules["conan.api.output"].ConanOutput = object
sys.modules["conan.cli"].make_abs_path = lambda p: p
sys.modules["conan.cli.command"].conan_command = lambda *a, **k: (lambda f: f)
sys.modules["conan.cli.command"].conan_subcommand = lambda *a, **k: (lambda f: f)
sys.modules["conan.cli.command"].OnceArgument = object
class _CE(Exception):
    pass
sys.modules["conan.errors"].ConanException = _CE

spec = importlib.util.spec_from_file_location("cmd_oci", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def test_sanitize():
    assert m._sanitize("1.2.3") == "1.2.3"
    assert m._sanitize("1.0+build/2") == "1.0_build_2"
    assert m._sanitize("a:b#c") == "a_b_c"


def test_tag_is_legal_and_deterministic():
    pid = "d62d3d2f3b1c4e5a6b7c8d9e0f1a2b3c4d5e6f7a"
    t1 = m._tag_for("1.2.3", "abcdef1234567890", pid)
    t2 = m._tag_for("1.2.3", "abcdef1234567890", pid)
    assert t1 == t2, "tag must be deterministic"
    assert t1 == f"1.2.3-abcdef12-{pid}"
    assert TAG_RE.match(t1), f"illegal OCI tag: {t1}"
    # weird version still produces a legal tag
    weird = m._tag_for("1.0+x/y", "rrev", pid)
    assert TAG_RE.match(weird), weird
    # recipe-only
    rec = m._tag_for("1.2.3", "abcdef12", None)
    assert rec.endswith("-recipe") and TAG_RE.match(rec)
    # missing rrev
    assert m._tag_for("1.2.3", None, pid) == f"1.2.3-norev-{pid}"


def test_split_target():
    assert m._split_target("localhost:5000/conan") == ("localhost:5000", "conan")
    assert m._split_target("ghcr.io/Me/Conan/") == ("ghcr.io", "me/conan")  # lowercased
    try:
        m._split_target("ghcr.io")  # no namespace
        assert False, "expected ConanException"
    except _CE:
        pass


def test_pkglist_entries():
    serialized = {
        "Local Cache": {
            "hello/1.0": {"revisions": {"53321bba8793db6f": {
                "packages": {"461f120128f0af7a": {"info": {}}}}}},
            "header/2.0@me/stable": {"revisions": {"abcdef0123456789": {
                "packages": {}}}},  # recipe-only (no binaries)
        },
        "myremote": {
            "zlib/1.3.1": {"revisions": {"r1": {"packages": {"pidA": {}, "pidB": {}}}}},
        },
        "broken": {"error": "remote down"},  # must be skipped
    }
    got = sorted(m._pkglist_entries(serialized))
    assert got == sorted([
        ("hello", "1.0", "53321bba8793db6f", "461f120128f0af7a"),
        ("header", "2.0", "abcdef0123456789", None),     # user/channel dropped, recipe-only
        ("zlib", "1.3.1", "r1", "pidA"),
        ("zlib", "1.3.1", "r1", "pidB"),
    ]), got
    # derived tags are legal and carry the package_id (or 'recipe')
    tags = [m._tag_for(v, rr, pid) for _, v, rr, pid in got]
    assert all(TAG_RE.match(t) for t in tags), tags
    assert any(t.endswith("-recipe") for t in tags)


def test_version_matches():
    assert m._version_matches("*", "1.2.3")
    assert m._version_matches("", "1.2.3")
    assert m._version_matches("1.2.3", "1.2.3")
    assert m._version_matches("1.*", "1.2.3")
    assert not m._version_matches("2.*", "1.2.3")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
