#!/usr/bin/env python3
"""Cria um pacote de distribuição com lista explícita de arquivos sem segredos."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "migrate.py", "validate_migration.py", "migration_common.py",
    "requirements.txt", "requirements-validation.txt", "README.md",
    "docs/validacao.md", "docs/revisao-2026-09-25.md", "docs/desenvolvimento.md",
    "config/examples/.env.example", "config/examples/.env.validacao.example",
    "config/examples/validation-mapping.example.json",
    "tests/__init__.py", "tests/test_migrate.py", "tests/test_validation.py",
)


def main():
    destination = ROOT / "dist" / "migracao-atualizada.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".zip.tmp")
    with ZipFile(temporary, "w", ZIP_DEFLATED) as bundle:
        for relative in FILES:
            bundle.write(ROOT / relative, relative)
    with ZipFile(temporary) as bundle:
        if bundle.testzip() is not None or set(bundle.namelist()) != set(FILES):
            raise RuntimeError("Falha ao verificar o pacote gerado")
    temporary.replace(destination)
    print(f"Pacote verificado: {destination} ({len(FILES)} arquivos)")


if __name__ == "__main__":
    main()
