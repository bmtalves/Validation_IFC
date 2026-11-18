"""Reads Statsbygg's published TFM code lists (PDF) into the CSV files the validation tool uses.

    python make_tfm_lists.py [folder with the PDFs]

Needs `pdftotext` (from xpdf or poppler) on the PATH. Looks for the PDFs in ../PDF_statsbygg by default:

  PA 0802 (Statsbygg's TFM: +AAAAAA=NNN.nnn-BBnnn)
    PA-0802-Vedlegg-9.1-Systemkodeliste-*.pdf    -> pa0802_system_codes.csv     (code, name, use in TFM)
    PA-0802-vedlegg-9.2-Komponentkodeliste*.pdf  -> pa0802_component_codes.csv  (code, name)
  PA 0805 (NS-TFM: ++AAAAAA=NNNN.nnn-BBBnnn%BBB.nnn.nnn)
    PA-0702-Vedlegg-1-2-Dokumentasjonskrav-*.pdf -> pa0805_system_codes.csv     (code, name, use in TFM)
      Statsbygg's documentation requirements per NS 3451:2022 system code, which mark the codes that
      "Skal ikke benyttes!". The NS 3457-8 component codes aren't published by Statsbygg.

Run it again when Statsbygg publishes a new revision, and check the counts it prints.
"""
import csv
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE_FURNITURE = re.compile(r"^(Godkjent|Saksnr|Revisjon|\s*SYSTEMKODELISTE|\s*DOKUMENTASJONSKRAV|\s*PA 0[78]0\d|\s*Vedlegg|Bygningsdelsnummer|Indeks)")


def pdf_text(path):
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "out.txt"
        try:
            subprocess.run(["pdftotext", "-table", "-enc", "UTF-8", str(path), str(out)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            sys.exit("pdftotext was not found. Install xpdf or poppler, or use the CSV files as they are.")
        return out.read_text(encoding="utf-8")


def pa0802_system_codes(text):
    """[(code, name, "yes"/"no")] for the 3-digit codes. The TFM comment column says
    "Systemkode i TFM" / "Systemkode for 36 ... i TFM" / "Benyttes for ..." or "Skal ikke benyttes i TFM"."""
    codes, current = [], None
    for line in text.splitlines():
        if PAGE_FURNITURE.match(line):
            continue
        match = re.match(r"^(\d{2,3})(?=\s|$)\s*(.*)$", line)
        if match:
            rest = match.group(2).strip()
            name = re.split(r"\s{2,}|\(", rest)[0].strip() if rest else ""
            current = {"code": match.group(1), "name": re.sub(r"\s+", " ", name), "comment": [], "text": rest}
            codes.append(current)
        if current is not None:
            # The comment column starts far to the right
            current["comment"] += [m.group() for m in re.finditer(r"\S+(?: \S+)*", line) if m.start() >= 130]
    result, unclear = [], []
    for c in codes:
        if len(c["code"]) != 3:
            continue  # 2-digit headings; PA 0802 uses 3 digits
        comment = " ".join(" ".join(c["comment"]).split())
        if re.search(r"Skal\s+ikke\s+benyttes", comment) or (not comment and "Skal ikke benyttes" in c["text"]):
            use = "no"
        elif re.search(r"Systemkode|Benyttes", comment):
            use = "yes"
        else:
            unclear.append(c["code"])
            continue
        name = "" if c["name"].startswith("Skal ikke benyttes") else c["name"]
        result.append((c["code"], name, use))
    if unclear:
        print(f"  ! No TFM comment found for: {', '.join(unclear)} (left out; check the PDF)")
    return result


def pa0802_component_codes(text):
    """[(code, name)] for the 2-letter codes that have a name (the others are free)"""
    found = {}
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}([A-ZÆØÅ]{2})(?:\s{2,}(\S.*?))?\s*$", line)
        if match and match.group(2):
            name = re.split(r"\s{2,}", match.group(2))[0].strip().rstrip(" -")
            if name and not re.match(r"(?i)ikke i bruk", name):
                found.setdefault(match.group(1), name)
    return sorted(found.items())


def pa0805_system_codes(text):
    """[(code, name, "yes"/"no")] from PA 0702 vedlegg 1-2: one row per document required for a system.
    "Skal ikke benyttes!" marks the codes Statsbygg doesn't use. 1xxx are document groups, not systems."""
    found, current = {}, None
    for line in text.splitlines():
        if PAGE_FURNITURE.match(line):
            continue
        match = re.match(r"^\s{0,4}\d{1,4}\s{2,}(\d{4})(?:\s{2,}(.*))?$", line.rstrip())
        if match:
            rest = match.group(2) or ""
            current = found.setdefault(match.group(1), {"names": [], "banned": False})
            name = re.split(r"\s{2,}", rest.strip())[0] if rest.strip() else ""
            if name:
                current["names"].append(name)
            line = rest
        if current is not None and "Skal ikke benyttes" in line:
            current["banned"] = True

    def clean(names):
        # Some names come out of the PDF with two lines of text interleaved; take the cleanest
        return min(names, key=lambda n: (len(re.findall(r"[a-zæøå][A-ZÆØÅ]", n)), -len(n))) if names else ""

    return [(code, clean(d["names"]), "no" if d["banned"] else "yes")
            for code, d in sorted(found.items()) if not code.startswith("1")]


def write(path, header, rows, source):
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(header)
        writer.writerow([f"# From {source} (Statsbygg). Made by make_tfm_lists.py"] + [""] * (len(header) - 1))
        writer.writerows(rows)


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "PDF_statsbygg"
    jobs = [
        ("PA-0802-*9.1-Systemkodeliste*.pdf", pa0802_system_codes, "pa0802_system_codes.csv", ["Code", "Name", "Use in TFM"]),
        ("PA-0802-*9.2-Komponentkodeliste*.pdf", pa0802_component_codes, "pa0802_component_codes.csv", ["Code", "Name"]),
        ("PA-0702-*Dokumentasjonskrav*0805*.pdf", pa0805_system_codes, "pa0805_system_codes.csv", ["Code", "Name", "Use in TFM"]),
    ]
    for pattern, reader, target, header in jobs:
        sources = sorted(folder.glob(pattern))
        if not sources:
            print(f"{target}: no {pattern} in {folder}; kept as it is")
            continue
        rows = reader(pdf_text(sources[-1]))
        write(HERE / target, header, rows, sources[-1].name)
        used = sum(r[2] == "yes" for r in rows) if len(header) == 3 else len(rows)
        print(f"{target}: {len(rows)} codes, {used} in use (from {sources[-1].name})")


if __name__ == "__main__":
    main()
