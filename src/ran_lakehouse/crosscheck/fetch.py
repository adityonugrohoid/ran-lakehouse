"""Download the real-data set for the cross-check (rule E2), never committed.

"Performance Management Counters from Live 5G, 4G and 2G Radio Access
Network", Zenodo 10.5281/zenodo.17815388, CC BY 4.0. Only the files the
cross-check reads are fetched: the README and Dataset_03 (GSM 900 and LTE
1800). The request is plain: no account, name, email or other identifier in
headers, URL or parameters. Checksums are pinned to the record's published
MD5, so a changed file fails loudly.

    uv run python -m ran_lakehouse.crosscheck.fetch
"""

import hashlib
import logging
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

RECORD = "17815388"
DOI = "10.5281/zenodo.17815388"
BASE_URL = f"https://zenodo.org/api/records/{RECORD}/files"
DEST = Path(__file__).resolve().parents[3] / "data" / "external" / f"zenodo-{RECORD}"
# MD5 as published by the record (Zenodo API "checksum").
FILES = {
    "README.md": "092277c2bb7bd59a6635185456f3f406",
    "Dataset_03.zip": "f9e3517853f6a21f3b49ad089f1b8da0",
}
MEMBERS = (
    "Dataset_03/Baseband_02/Dataset_03_LTE_1800.csv",
    "Dataset_03/Baseband_01/Dataset_03_GSM_900.csv",
)
CHUNK = 1 << 20


def request(name: str) -> urllib.request.Request:
    """The download request of one file: the URL only, no added headers.

    Args:
        name: File name in the record.

    Returns:
        The request.
    """
    return urllib.request.Request(f"{BASE_URL}/{name}/content")


def md5(path: Path) -> str:
    """MD5 of a file.

    Args:
        path: File.

    Returns:
        Hex digest.
    """
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def verified(path: Path, expected: str) -> None:
    """Check a file against its published MD5.

    Args:
        path: File.
        expected: Published hex digest.

    Raises:
        RuntimeError: On a mismatch.
    """
    found = md5(path)
    if found != expected:
        raise RuntimeError(f"{path.name}: MD5 {found}, the record publishes {expected}")


def download(name: str, dest: Path) -> Path:
    """Download one file of the record, unless a verified copy is there.

    Args:
        name: File name in the record.
        dest: Folder.

    Returns:
        The local file.
    """
    path = dest / name
    if path.exists():
        verified(path, FILES[name])
        logger.info("%s already here and verified", name)
        return path
    part = path.with_suffix(path.suffix + ".part")
    with urllib.request.urlopen(request(name), timeout=120) as response, part.open("wb") as out:
        while block := response.read(CHUNK):
            out.write(block)
    verified(part, FILES[name])
    part.rename(path)
    logger.info("downloaded %s", name)
    return path


def fetch(dest: Path) -> list[Path]:
    """Download and verify the files, and extract the CSVs the cross-check reads.

    Args:
        dest: Folder (under data/, which git ignores).

    Returns:
        The extracted CSV files.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        download(name, dest)
    with zipfile.ZipFile(dest / "Dataset_03.zip") as archive:
        for member in MEMBERS:
            if not (dest / member).exists():
                archive.extract(member, dest)
    return [dest / member for member in MEMBERS]


def main() -> None:
    """Fetch into data/external/."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for path in fetch(DEST):
        print(path)


if __name__ == "__main__":
    main()
