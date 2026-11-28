"""Prepare a local Ollama build context from the reviewed tonkatsu model files."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import build_opener, HTTPRedirectHandler, Request

MANIFEST = Path("manifests/registry.ollama.ai/library/llama3.1/8b")
MANIFEST_SHA256 = "3fc95d284915704cf140b825abf5150af0d32fde95f55c081c9602d49d4547c0"
REGISTRY = "https://registry.ollama.ai/v2/library/llama3.1"
REGISTRY_MANIFEST_SHA256 = "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e"
OLLAMA_AMD64_IMAGE = "ollama/ollama@sha256:a717bfc1c56acfc2e85d3109bd40a7646258ec9062c40cea83b9ed0927eb4223"
BASE_IMAGE = re.compile(r"ollama/ollama@sha256:[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class HttpsRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise ValueError("model registry redirected outside HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def registry_open(path):
    request = Request(f"{REGISTRY}/{path}", headers={
        "Accept": "application/vnd.docker.distribution.manifest.v2+json",
    })
    return build_opener(HttpsRedirects()).open(request, timeout=60)


def registry_manifest():
    with registry_open("manifests/8b") as source:
        payload = source.read(64 * 1024)
    if hashlib.sha256(payload).hexdigest() != REGISTRY_MANIFEST_SHA256:
        raise ValueError("public manifest differs from the reviewed llama3.1:8b digest")
    descriptor = {
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "digest": f"sha256:{REGISTRY_MANIFEST_SHA256}",
        "size": len(payload),
    }
    manifest = json.loads(payload)
    manifest["layers"].append(descriptor)
    return json.dumps(manifest, separators=(",", ":")).encode(), payload


@contextmanager
def open_local_blob(path: Path, size: int):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError(f"invalid model blob file: {path.name}")
        yield stream


def copy_blob(source, destination: Path, digest: str, size: int) -> None:
    checksum = hashlib.sha256()
    copied = 0
    with destination.open("xb") as output_file:
        os.fchmod(output_file.fileno(), 0o644)
        while chunk := source.read(1024 * 1024):
            copied += len(chunk)
            if copied > size:
                raise ValueError(f"model blob grew during copy: {destination.name}")
            checksum.update(chunk)
            output_file.write(chunk)
    if copied != size or f"sha256:{checksum.hexdigest()}" != digest:
        raise ValueError(f"model blob digest or size mismatch: {destination.name}")


def prepare_context(model_store: Path | None, output: Path, base_image: str) -> None:
    if not BASE_IMAGE.fullmatch(base_image):
        raise ValueError("base image must be an Ollama image pinned by SHA-256")
    if model_store is not None:
        if output.resolve().is_relative_to(model_store.resolve()):
            raise ValueError("build context must be outside the Ollama model store")
        with (model_store / MANIFEST).open("rb") as source:
            manifest = source.read(64 * 1024)
        registry_payload = None
    else:
        manifest, registry_payload = registry_manifest()
    if hashlib.sha256(manifest).hexdigest() != MANIFEST_SHA256:
        raise ValueError("manifest differs from the reviewed llama3.1:8b digest")

    descriptors = json.loads(manifest)
    output.mkdir(mode=0o700)
    try:
        model_output = output / "models"
        blob_output = model_output / "blobs"
        blob_output.mkdir(parents=True)
        # Ollama opens both manifest layouts, including for a legacy-only store.
        (model_output / "manifests-v2").mkdir()
        for item in [descriptors["config"], *descriptors["layers"]]:
            digest, size = item["digest"], item["size"]
            if not DIGEST.fullmatch(digest) or type(size) is not int or size <= 0:
                raise ValueError("invalid model blob descriptor")
            name = digest.replace(":", "-", 1)
            if model_store is not None:
                source = open_local_blob(model_store / "blobs" / name, size)
            elif digest == f"sha256:{REGISTRY_MANIFEST_SHA256}":
                source = io.BytesIO(registry_payload)
            else:
                source = registry_open(f"blobs/{digest}")
            with source as stream:
                copy_blob(stream, blob_output / name, digest, size)

        manifest_output = model_output / MANIFEST
        manifest_output.parent.mkdir(parents=True)
        manifest_output.write_bytes(manifest)
        manifest_output.chmod(0o644)
        model_output.chmod(0o755)
        for directory in (path for path in model_output.rglob("*") if path.is_dir()):
            directory.chmod(0o755)
        (output / "Dockerfile").write_text(
            f"""FROM {base_image}
COPY models/ /opt/foodtech/ollama-models/
USER 0:0
RUN install -d -m 0700 -o 65532 -g 65532 /run/foodtech-ollama
VOLUME ["/run/foodtech-ollama"]
ENV OLLAMA_MODELS=/opt/foodtech/ollama-models \\
    OLLAMA_HOST=127.0.0.1:11434 \\
    OLLAMA_NO_CLOUD=1 \\
    OLLAMA_NOPRUNE=1 \\
    HOME=/run/foodtech-ollama \\
    TMPDIR=/run/foodtech-ollama
USER 65532:65532
ENTRYPOINT ["/bin/ollama"]
CMD ["serve"]
""",
            encoding="utf-8",
        )
    except BaseException:
        shutil.rmtree(output)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--model-store", type=Path)
    source.add_argument("--from-registry", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-image", default=OLLAMA_AMD64_IMAGE, help="reviewed ollama/ollama@sha256:...")
    args = parser.parse_args()
    try:
        prepare_context(args.model_store, args.output, args.base_image)
    except URLError:
        parser.exit(1, "could not retrieve verified data from the model registry\n")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
