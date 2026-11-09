"""
Put this file in claim-ops/ and run:   python apply_s3_patch.py
It edits app/main.py, app/worker/claims_worker.py and app/services/claim_service.py.
Each edit must match exactly once, otherwise nothing is changed in that file and it tells you which one failed.
Safe to run twice (it says "already patched").
"""
import sys

PATCHES = {
    "app/main.py": [
        ("from fastapi.responses import JSONResponse, FileResponse\n",
         "from fastapi.responses import JSONResponse, FileResponse, RedirectResponse   # NEW (S3)\n"),
        ("from app.core.cache import cache\n",
         "from app.core.cache import cache\nfrom app.core import storage                      # NEW (S3)\n"),
        ('# Mount uploads directory to serve files statically\n'
         'if not os.path.exists("uploads"):\n'
         '    os.makedirs("uploads")\n'
         'app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")\n',
         '# NEW (S3): bills are served from the local folder, or (STORAGE_BACKEND=s3) through a short-lived S3 link\n'
         'storage.check()\n'
         'if storage.USE_S3:\n'
         '    @app.get("/uploads/{filename}", include_in_schema=False)\n'
         '    def open_uploaded_bill(filename: str):\n'
         '        if "/" in filename or "\\\\" in filename or ".." in filename:\n'
         '            raise HTTPException(status_code=404, detail="Not found")\n'
         '        return RedirectResponse(storage.presigned_url(filename))\n'
         'else:\n'
         '    if not os.path.exists("uploads"):\n'
         '        os.makedirs("uploads")\n'
         '    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")\n'),
        ('        file_path = f"uploads/{claim_id}_{safe_name}"\n'
         '        with open(file_path, "wb") as buffer:\n'
         '            buffer.write(content)\n'
         '        file_data = file_path\n',
         '        file_data = storage.save_upload(claim_id, safe_name, content)   # NEW (S3): local folder or S3\n'),
    ],
    "app/worker/claims_worker.py": [
        ("from app.services.extraction_service import extract_bill_data\n",
         "from app.services.extraction_service import extract_bill_data\nfrom app.core import storage                      # NEW (S3)\n"),
        ("                    extracted_data = extract_bill_data(file_path, llm_client=orchestrator.client)\n",
         "                    with storage.local_copy(file_path) as local_path:      # NEW (S3): temporary local copy of the bill\n"
         "                        extracted_data = extract_bill_data(local_path, llm_client=orchestrator.client)\n"),
    ],
    "app/services/claim_service.py": [
        ("from app.worker.claims_worker import SessionLocal\n",
         "from app.worker.claims_worker import SessionLocal\nfrom app.core import storage                      # NEW (S3)\n"),
        ("        if file_path:\n            try:\n                os.remove(file_path)\n            except OSError:\n                pass\n",
         "        storage.delete(file_path)                          # NEW (S3): removes the local file or the S3 object\n"),
    ],
}

bad = False
for rel, items in PATCHES.items():
    try:
        src = open(rel, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"✗ {rel}: file not found (run this from the claim-ops folder)")
        bad = True
        continue
    new_src, problems = src, []
    for old, new in items:
        if new in new_src:                      # this edit is already in the file
            continue
        if new_src.count(old) != 1:
            problems.append((new_src.count(old), old.strip().splitlines()[0][:70]))
            continue
        new_src = new_src.replace(old, new)
    if problems:
        bad = True
        print(f"✗ {rel}: NOT changed. These lines were not found exactly once:")
        for n, line in problems:
            print(f"    found {n} times: {line}")
    elif new_src == src:
        print(f"= {rel}: already patched")
    else:
        open(rel, "w", encoding="utf-8").write(new_src)
        print(f"✓ {rel}: patched")
sys.exit(1 if bad else 0)
