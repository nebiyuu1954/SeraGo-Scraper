"""Throwaway: load any model fixture in committed chunks (resume-safe).

Usage: python scripts/load_fixture_chunked.py <fixture.json> <app.Model> [chunk]
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "serago.settings")
django.setup()

from django.apps import apps  # noqa: E402
from django.core import serializers  # noqa: E402
from django.db import transaction  # noqa: E402

fixture_path, model_label = sys.argv[1], sys.argv[2]
chunk = int(sys.argv[3]) if len(sys.argv) > 3 else 10

Model = apps.get_model(model_label)
print(f"model: {model_label}  fixture: {fixture_path}  chunk: {chunk}", flush=True)

with open(fixture_path, encoding="utf-8") as f:
    objects = list(serializers.deserialize("json", f, handle_forward_references=True))
print(f"deserialized {len(objects)} objects", flush=True)

# Resume-safe: drop objects whose pk already exists on Neon.
pks = [o.object.pk for o in objects]
existing = set(
    Model.objects.filter(pk__in=pks).values_list("pk", flat=True)
)
if existing:
    before = len(objects)
    objects = [o for o in objects if o.object.pk not in existing]
    print(f"skipping {len(existing)} already-present rows ({before} -> {len(objects)})", flush=True)
if not objects:
    print("nothing to do", flush=True)
    sys.exit(0)

total_start = time.time()
for start in range(0, len(objects), chunk):
    group = objects[start : start + chunk]
    t0 = time.time()
    with transaction.atomic():
        for obj in group:
            obj.save()
    print(
        f"committed rows {start+1}-{start+len(group)} in {time.time()-t0:.2f}s "
        f"(cum {time.time()-total_start:.1f}s)",
        flush=True,
    )
print(f"DONE {len(objects)} rows in {time.time()-total_start:.1f}s", flush=True)
sys.stdout.flush()
