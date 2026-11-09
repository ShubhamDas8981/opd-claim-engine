"""Put at tests/test_storage.py.  Run: python tests/test_storage.py   (local mode, no AWS needed)"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import storage

def test_local_roundtrip():
    old = os.getcwd(); tmp = tempfile.mkdtemp(); os.chdir(tmp)
    try:
        ref = storage.save_upload("abc", "bill.pdf", b"%PDF-1.7 x")
        assert ref == "uploads/abc_bill.pdf" and open(ref, "rb").read() == b"%PDF-1.7 x"
        with storage.local_copy(ref) as p:
            assert p == ref and os.path.exists(p)
        storage.delete(ref)
        assert not os.path.exists(ref)
        storage.delete("uploads/does-not-exist.pdf")      # must not raise
        with storage.local_copy("no_file") as p:
            assert p == "no_file"
    finally:
        os.chdir(old)

if __name__ == "__main__":
    test_local_roundtrip(); print("PASS test_local_roundtrip")
