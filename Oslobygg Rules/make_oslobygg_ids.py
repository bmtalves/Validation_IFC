"""Turns Oslobygg's BIM egenskapsmatrise (Excel) into IDS files the validation tool can read.

    python make_oslobygg_ids.py ["BIM egenskapsmatrise.xlsx"]

Writes one file per phase and discipline next to this script, e.g. oslobygg_2-forprosjekt_ark.ids.

The matrix is written for people, so a few things are decided here rather than read from it:
- OBJECTS says which IFC classes each row of the matrix ("Yttervegger", "Bjelker" ...) means.
- Exterior and interior walls, doors, windows and glass facades are told apart by IsExternal.
  An object without IsExternal would then be in neither group, so IsExternal gets its own
  requirement for all of them ("... (alle)").
- Property names are compared with IFC4's own property sets: misspellings ("HandicapAccesible")
  are corrected and the IFC value type is taken from there. Properties IFC4 doesn't have keep the
  matrix's name; "(True/False)" ones must be booleans, others can be any type.
- The common tab ("Felles") and the room numbering in the BIM manual (5.4.1) apply to every phase.
Run it again when Oslobygg publishes a new matrix, and check what it prints.
"""
import difflib
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import ifcopenshell.util.pset

HERE = Path(__file__).resolve().parent
SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
IDS_NS = "http://standards.buildingsmart.org/IDS"
XS_NS = "http://www.w3.org/2001/XMLSchema"
ET.register_namespace("ids", IDS_NS)
ET.register_namespace("xs", XS_NS)

# Matrix columns: (pset column, property column) per phase
PHASES = [("Konseptvalg", "D", "E"), ("Forprosjekt", "F", "G"), ("Detaljering", "H", "I"), ("Som bygget", "J", "K")]
EXTERNAL = {"IsExternal": "true"}
INTERNAL = {"IsExternal": "false"}

# (NS 3451 code, object in the matrix) -> (group name in the report, IFC classes, PredefinedType, property filter)
OBJECTS = {
    "ARK": {
        ("", "Room/Space"): ("Rom", ["IFCSPACE"], None, None),
        ("22", "Bjelker"): ("22 Bjelker", ["IFCBEAM", "IFCBEAMSTANDARDCASE"], None, None),
        ("22", "Søyler"): ("22 Søyler", ["IFCCOLUMN", "IFCCOLUMNSTANDARDCASE"], None, None),
        ("23", "Yttervegger"): ("23 Yttervegger", ["IFCWALL", "IFCWALLSTANDARDCASE", "IFCWALLELEMENTEDCASE"], None, EXTERNAL),
        ("23", "Ytterdører"): ("23 Ytterdører", ["IFCDOOR", "IFCDOORSTANDARDCASE"], None, EXTERNAL),
        ("23", "Glassfasader"): ("23 Glassfasader", ["IFCCURTAINWALL"], None, EXTERNAL),
        ("23", "Vinduer"): ("23 Vinduer", ["IFCWINDOW", "IFCWINDOWSTANDARDCASE"], None, EXTERNAL),
        ("24", "Innervegger"): ("24 Innervegger", ["IFCWALL", "IFCWALLSTANDARDCASE", "IFCWALLELEMENTEDCASE"], None, INTERNAL),
        ("24", "Innerdører"): ("24 Innerdører", ["IFCDOOR", "IFCDOORSTANDARDCASE"], None, INTERNAL),
        ("24", "Innvendige vinduer"): ("24 Innvendige vinduer", ["IFCWINDOW", "IFCWINDOWSTANDARDCASE"], None, INTERNAL),
        ("24", "Glassfasader"): ("24 Innvendige glassfasader", ["IFCCURTAINWALL"], None, INTERNAL),
        ("25", "Dekker og gulv"): ("25 Dekker og gulv", ["IFCSLAB", "IFCSLABSTANDARDCASE", "IFCSLABELEMENTEDCASE"], None, None),
        ("25", "System- og akustisk himling"): ("25 Himlinger", ["IFCCOVERING"], "CEILING", None),
        ("26", "Primærkonstruksjoner"): ("26 Yttertak", ["IFCROOF"], None, None),
        ("26", "Glasstak"): ("26 Glasstak", ["IFCWINDOW", "IFCWINDOWSTANDARDCASE"], "SKYLIGHT", None),
        ("28", "Trapper"): ("28 Trapper", ["IFCSTAIR"], None, None),
        ("28", "Rampe"): ("28 Ramper", ["IFCRAMP"], None, None),
        ("28", "Rekkverk"): ("28 Rekkverk", ["IFCRAILING"], None, None),
    },
}

# Requirements in the common tab and the BIM manual: (group, IFC class, attribute, label, description, pattern)
COMMON = [
    ("Prosjekt", "IFCPROJECT", "Name", "Forvaltnings-ID", "Oslobyggs forvaltnings-ID: 6 siffer, f.eks. 200635 (BIM-manual, begreper)", r"\d{6}"),
    ("Tomt", "IFCSITE", "Name", "Tomtenavn", "Navn på tomt (gatenavn)", None),
    ("Bygning", "IFCBUILDING", "Name", "Bygg-ID", "Oslobyggs bygg-ID: 2 siffer, 01-99 (BIM-manual, begreper)", r"0[1-9]|[1-9]\d"),
    ("Etasje", "IFCBUILDINGSTOREY", "Name", "Etasjenavn", "Navn på etasje", None),
    ("Rom", "IFCSPACE", "Name", "Romnummer",
     "Romnummer med ni tegn: bygg-ID, bindestrek, etasje (K, U, E, M, L eller T + to siffer) og tre siffer, f.eks. 01-E01001 (BIM-manual 5.4.1)",
     r"\d{2}-[KUEMLT]\d{2}\d{3}"),
    ("Rom", "IFCSPACE", "LongName", "Romnavn", "Romnavn (BIM-manual 5.4.1)", None),
]

NOT_REQUIRED = re.compile(r"^ikke kravsatt", re.IGNORECASE)
PSET_NAME = re.compile(r"^(Pset_|NOOBF_|Qto_)\w+$")
# "Category (f.eks: Korridor)", "FireExit (True/False)", "ThermalTransmittance(f.eks: 0,2W/m²K)"
PROPERTY = re.compile(r"^\s*([A-Za-z_][\w]*)\s*(?:\((.*)\))?\s*$")


def read_sheet(path, name):
    """{row number: {column letter: text}} for one tab of the workbook (no extra libraries needed)"""
    with zipfile.ZipFile(path) as book:
        strings = ["".join(t.text or "" for t in si.iter(SHEET + "t"))
                   for si in ET.fromstring(book.read("xl/sharedStrings.xml")).iter(SHEET + "si")]
        names = [s.get("name") for s in ET.fromstring(book.read("xl/workbook.xml")).iter(SHEET + "sheet")]
        if name not in names:
            sys.exit(f"{path.name} has no tab '{name}' (tabs: {', '.join(names)})")
        root = ET.fromstring(book.read(f"xl/worksheets/sheet{names.index(name) + 1}.xml"))
    rows = {}
    for row in root.iter(SHEET + "row"):
        cells = {}
        for cell in row.iter(SHEET + "c"):
            value = cell.find(SHEET + "v")
            text = cell.find(SHEET + "is")
            if value is not None:
                cells[re.sub(r"\d", "", cell.get("r"))] = strings[int(value.text)] if cell.get("t") == "s" else value.text
            elif text is not None:
                cells[re.sub(r"\d", "", cell.get("r"))] = "".join(t.text or "" for t in text.iter(SHEET + "t"))
        cells = {k: v.strip() for k, v in cells.items() if v and v.strip()}
        if cells:
            rows[int(row.get("r"))] = cells
    return rows


def read_matrix(rows):
    """[(code, object, {phase: [(pset, property text)]})] in matrix order, plus notes on what was left out"""
    objects, notes = [], []
    code, section = "", ""
    current = None
    for number in sorted(rows):
        cells = rows[number]
        if number <= 2:
            continue  # headings
        if "B" in cells:
            code = cells.get("A", "")
            section = f"{code} {cells['B']}".strip()
        if "C" in cells:
            current = (code, cells["C"], {phase: [] for phase, _, _ in PHASES}, {})
            objects.append(current)
        elif "B" in cells:
            current = None
            continue
        if current is None:
            text = next((cells[c] for _, p, q in PHASES for c in (p, q) if c in cells), None)
            if text:
                notes.append(f"{section}: only general text, nothing to check (\"{text[:70]}...\")")
            continue
        _, name, properties, psets = current
        for phase, pset_column, property_column in PHASES:
            pset = cells.get(pset_column)
            if pset:
                psets[phase] = None if NOT_REQUIRED.match(pset) else pset
            text = cells.get(property_column)
            if not text or NOT_REQUIRED.match(text):
                continue
            if PSET_NAME.match(text):
                continue  # a property set name in the property column (Vinduer, Konseptvalg)
            if psets.get(phase) is None:
                notes.append(f"{code} {name}, {phase}: '{text}' is listed, but the property set says 'Ikke kravsatt'; left out")
                continue
            properties[phase].append((psets[phase], text))
    return objects, notes


class PropertyTypes:
    """IFC4's own property sets: correct names and value types"""

    def __init__(self):
        self.template = ifcopenshell.util.pset.get_template("IFC4")
        self.fixes = []

    def resolve(self, pset, name, hint):
        """(property name, IFC type or None)"""
        definition = self.template.get_by_name(pset) if pset.startswith("Pset_") else None
        boolean = "IFCBOOLEAN" if hint and hint.lower().replace(" ", "") == "true/false" else None
        if definition is None:
            return name, boolean
        known = {p.Name: p for p in definition.HasPropertyTemplates}
        if name not in known:
            close = difflib.get_close_matches(name, known, n=1, cutoff=0.85)
            if not close:
                return name, boolean  # not in IFC4: keep the matrix's name
            self.fixes.append(f"{pset}.{name} -> {close[0]}")
            name = close[0]
        measure = getattr(known[name], "PrimaryMeasureType", None)
        return name, (measure.upper() if measure else boolean)


def element(parent, tag, text=None, ns=IDS_NS, **attributes):
    node = ET.SubElement(parent, f"{{{ns}}}{tag}", {k: v for k, v in attributes.items() if v is not None})
    if text is not None:
        node.text = text
    return node


def simple(parent, tag, value):
    element(element(parent, tag), "simpleValue", value)


def value_list(parent, tag, values):
    """One value as simpleValue, several as an enumeration"""
    if len(values) == 1:
        simple(parent, tag, values[0])
        return
    restriction = element(element(parent, tag), "restriction", ns=XS_NS, base="xs:string")
    for value in values:
        element(restriction, "enumeration", ns=XS_NS, value=value)


def specification(specs, name, entities, predefined=None, filters=None):
    spec = element(specs, "specification", name=name, ifcVersion="IFC4", minOccurs="0", maxOccurs="unbounded")
    applicability = element(spec, "applicability")
    entity = element(applicability, "entity")
    value_list(entity, "name", entities)
    if predefined:
        simple(entity, "predefinedType", predefined)
    for (pset, prop), value in (filters or {}).items():
        facet = element(applicability, "property", datatype="IFCBOOLEAN")
        simple(facet, "propertySet", pset)
        simple(facet, "name", prop)
        simple(facet, "value", value)
    return element(spec, "requirements")


def add_property(requirements, pset, name, ifc_type, description):
    facet = element(requirements, "property", instructions=description, datatype=ifc_type,
                    minOccurs="1", maxOccurs="unbounded")
    simple(facet, "propertySet", pset)
    simple(facet, "name", name)


def add_attribute(requirements, name, label, description, pattern):
    facet = element(requirements, "attribute", instructions=f": {label} || {description}")
    simple(facet, "name", name)
    if pattern:
        restriction = element(element(facet, "value"), "restriction", ns=XS_NS, base="xs:string")
        element(restriction, "pattern", ns=XS_NS, value=pattern)


def describe(hint):
    """The matrix's note in brackets, as the requirement's description"""
    if not hint:
        return ""
    if hint.lower().replace(" ", "") == "true/false":
        return "True/False"
    return re.sub(r"^f\.?\s*eks\.?\s*:?\s*", "Eksempel: ", hint.strip())


def build(discipline, phase_number, phase, objects, types, source):
    root = ET.Element(f"{{{IDS_NS}}}ids")
    info = element(root, "info")
    element(info, "title", f"Oslobygg BIM egenskapsmatrise – {discipline} – {phase}")
    element(info, "description", f"Laget fra {source} med make_oslobygg_ids.py. Ikke en offisiell Oslobygg-fil.")
    element(info, "purpose", discipline)
    element(info, "milestone", phase)
    specs = element(root, "specifications")

    # Common requirements, grouped by object
    common_groups = {}
    for group, entity, attribute, label, description, pattern in COMMON:
        common_groups.setdefault((group, entity), []).append((attribute, label, description, pattern))
    space_extra = common_groups.pop(("Rom", "IFCSPACE"), [])
    for (group, entity), attributes in common_groups.items():
        requirements = specification(specs, group, [entity])
        for attribute, label, description, pattern in attributes:
            add_attribute(requirements, attribute, label, description, pattern)

    shared = {}  # IsExternal for both exterior and interior objects: (classes) -> {(pset, prop)}
    count = 0
    for code, name, properties, _ in objects:
        mapping = OBJECTS[discipline].get((code, name))
        if mapping is None:
            continue
        group, entities, predefined, flt = mapping
        wanted = properties[phase]
        if not wanted and not (group == "Rom" and space_extra):
            continue
        rows = []
        for pset, text in wanted:
            match = PROPERTY.match(text)
            if not match:
                print(f"  ! {group}, {phase}: could not read '{text}'")
                continue
            prop, ifc_type = types.resolve(pset, match.group(1), match.group(2))
            if flt and prop in flt:
                # Exterior and interior both require IsExternal: one requirement for all of them
                shared.setdefault(tuple(entities), set()).add((pset, prop, ifc_type))
                continue
            rows.append((pset, prop, ifc_type, describe(match.group(2))))
        filters = {(rows[0][0] if rows else wanted[0][0], key): value for key, value in (flt or {}).items()}
        if not rows and not (group == "Rom" and space_extra):
            continue
        requirements = specification(specs, group, entities, predefined, filters)
        if group == "Rom":
            for attribute, label, description, pattern in space_extra:
                add_attribute(requirements, attribute, label, description, pattern)
        seen = set()
        for pset, prop, ifc_type, description in rows:
            if (pset, prop) not in seen:
                seen.add((pset, prop))
                add_property(requirements, pset, prop, ifc_type, description)
                count += 1

    names = {"IFCWALL": "Vegger", "IFCDOOR": "Dører", "IFCWINDOW": "Vinduer", "IFCCURTAINWALL": "Glassfasader"}
    for entities, props in shared.items():
        requirements = specification(specs, f"{names.get(entities[0], entities[0])} (alle)", list(entities))
        for pset, prop, ifc_type in sorted(props):
            add_property(requirements, pset, prop, ifc_type,
                         "True/False. Skiller utvendige og innvendige objekter, som har ulike krav.")
            count += 1

    ET.indent(root)
    path = HERE / f"oslobygg_{phase_number}-{phase.lower().replace(' ', '_')}_{discipline.lower()}.ids"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path, count


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "BIM egenskapsmatrise.xlsx"
    if not source.exists():
        sys.exit(f"Not found: {source}")
    types = PropertyTypes()
    for discipline in OBJECTS:
        objects, notes = read_matrix(read_sheet(source, discipline))
        unmapped = [f"{code} {name}" for code, name, props, _ in objects
                    if (code, name) not in OBJECTS[discipline] and any(props.values())]
        print(f"{discipline}:")
        for number, (phase, _, _) in enumerate(PHASES, 1):
            path, count = build(discipline, number, phase, objects, types, source.name)
            print(f"  {path.name}: {count} property requirements")
        for note in notes:
            print(f"  - {note}")
        for name in unmapped:
            print(f"  ! No IFC class given for '{name}' in OBJECTS; it was left out")
    for fix in sorted(set(types.fixes)):
        print(f"Corrected to the IFC4 name: {fix}")


if __name__ == "__main__":
    main()
