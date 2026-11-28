import hashlib
import io
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "infra/containers/prepare_tonkatsu_ollama_context.py"
SPEC = importlib.util.spec_from_file_location("prepare_tonkatsu_ollama_context", SCRIPT)
assert SPEC and SPEC.loader
context = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(context)


class TonkatsuOllamaContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = self.root / "store"
        self.output = self.root / "context"
        self.base_image = f"ollama/ollama@sha256:{'a' * 64}"
        config = self.write_blob(b"config")
        model = self.write_blob(b"model")
        self.model_blob = self.store / "blobs" / model["digest"].replace(":", "-")
        self.manifest = self.store / context.MANIFEST
        self.manifest.parent.mkdir(parents=True)
        manifest = json.dumps({"config": config, "layers": [model]}).encode()
        self.manifest.write_bytes(manifest)
        pin = patch.object(context, "MANIFEST_SHA256", hashlib.sha256(manifest).hexdigest())
        pin.start()
        self.addCleanup(pin.stop)

    def write_blob(self, content):
        digest = hashlib.sha256(content).hexdigest()
        blobs = self.store / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        (blobs / f"sha256-{digest}").write_bytes(content)
        return {"digest": f"sha256:{digest}", "size": len(content)}

    def test_only_verified_model_files_are_copied(self):
        self.write_blob(b"unreferenced blob")
        (self.store / "id_ed25519").write_text("unrelated file")
        context.prepare_context(self.store, self.output, self.base_image)
        self.assertEqual((self.output / "models" / context.MANIFEST).read_bytes(),
                         self.manifest.read_bytes())
        self.assertTrue((self.output / "models" / "manifests-v2").is_dir())
        self.assertEqual(len(list((self.output / "models" / "blobs").iterdir())), 2)
        self.assertEqual({path.name for path in self.output.iterdir()}, {"Dockerfile", "models"})
        dockerfile = (self.output / "Dockerfile").read_text()
        self.assertIn(f"FROM {self.base_image}\n", dockerfile)
        self.assertIn("OLLAMA_HOST=127.0.0.1:11434", dockerfile)
        self.assertIn("OLLAMA_NO_CLOUD=1", dockerfile)
        self.assertIn('VOLUME ["/run/foodtech-ollama"]', dockerfile)
        self.assertIn("TMPDIR=/run/foodtech-ollama", dockerfile)
        self.assertIn("USER 65532:65532", dockerfile)

    def test_registry_reconstructs_same_reviewed_manifest_and_verifies_blobs(self):
        upstream = self.manifest.read_bytes()
        digest = hashlib.sha256(upstream).hexdigest()
        expected_manifest = json.loads(upstream)
        expected_manifest["layers"].append({
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "digest": f"sha256:{digest}",
            "size": len(upstream),
        })
        expected_bytes = json.dumps(expected_manifest, separators=(",", ":")).encode()
        paths = {
            "manifests/8b": upstream,
            **{f"blobs/{path.name.replace('sha256-', 'sha256:')}": path.read_bytes()
               for path in (self.store / "blobs").iterdir()},
        }

        def registry(path):
            return io.BytesIO(paths[path])

        with (
            patch.object(context, "REGISTRY_MANIFEST_SHA256", digest),
            patch.object(context, "MANIFEST_SHA256", hashlib.sha256(expected_bytes).hexdigest()),
            patch.object(context, "registry_open", side_effect=registry),
        ):
            context.prepare_context(None, self.output, self.base_image)
        store = self.output / "models"
        self.assertEqual((store / context.MANIFEST).read_bytes(), expected_bytes)
        self.assertEqual((store / "blobs" / f"sha256-{digest}").read_bytes(), upstream)

    def test_changed_public_manifest_is_rejected_before_blob_requests(self):
        with patch.object(context, "registry_open", return_value=io.BytesIO(b"changed")) as request:
            with self.assertRaisesRegex(ValueError, "public manifest differs"):
                context.prepare_context(None, self.output, self.base_image)
        request.assert_called_once_with("manifests/8b")
        self.assertFalse(self.output.exists())

    def test_rejects_changed_blob_and_removes_partial_output(self):
        self.model_blob.write_bytes(b"other")
        with self.assertRaisesRegex(ValueError, "blob digest or size mismatch"):
            context.prepare_context(self.store, self.output, self.base_image)
        self.assertFalse(self.output.exists())

    def test_rejects_symlinked_blob(self):
        replacement = self.model_blob.rename(self.root / "replacement")
        self.model_blob.symlink_to(replacement)
        with self.assertRaises(OSError):
            context.prepare_context(self.store, self.output, self.base_image)
        self.assertFalse(self.output.exists())

    def test_rejects_changed_manifest_before_creating_context(self):
        self.manifest.write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            context.prepare_context(self.store, self.output, self.base_image)
        self.assertFalse(self.output.exists())

    def test_rejects_mutable_base_and_preserves_existing_directory(self):
        with self.assertRaisesRegex(ValueError, "pinned"):
            context.prepare_context(self.store, self.output, "ollama/ollama:latest")
        self.output.mkdir()
        marker = self.output / "keep"
        marker.touch()
        with self.assertRaises(FileExistsError):
            context.prepare_context(self.store, self.output, self.base_image)
        self.assertTrue(marker.exists())
