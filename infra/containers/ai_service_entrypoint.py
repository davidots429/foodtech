"""Materialize ECS-injected TLS material as private files, then start one service."""

import os
import tempfile


SETTINGS = {
    "credential-registration": (
        "foodtech_credential_registration.serve",
        "FOODTECH_REGISTRATION_SERVER_CERT",
        "FOODTECH_REGISTRATION_SERVER_KEY",
        "FOODTECH_REGISTRATION_BUSINESS_CA",
    ),
    "ai-gateway": (
        "foodtech_ai_gateway.serve",
        "FOODTECH_GATEWAY_SERVER_CERT",
        "FOODTECH_GATEWAY_SERVER_KEY",
        "FOODTECH_GATEWAY_BUSINESS_CA",
    ),
}


def main() -> None:
    service = os.environ["FOODTECH_SERVICE"]
    module, *paths = SETTINGS[service]
    directory = tempfile.mkdtemp(prefix="foodtech-tls-")
    os.chmod(directory, 0o700)
    for secret_name, path_name in zip(
        ("FOODTECH_TLS_SERVER_CERT_PEM", "FOODTECH_TLS_SERVER_KEY_PEM", "FOODTECH_TLS_BUSINESS_CA_PEM"), paths,
        strict=True,
    ):
        material = os.environ.pop(secret_name).encode("utf-8")
        if not material.startswith(b"-----BEGIN "):
            raise ValueError(f"Invalid TLS material: {secret_name}")
        path = os.path.join(directory, path_name.lower())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(material)
        os.environ[path_name] = path
    os.execvpe("python", ["python", "-m", module], os.environ)


if __name__ == "__main__":
    main()
