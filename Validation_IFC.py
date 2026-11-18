import sys
import csv
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.api.pset


MVD = "{http://buildingsmart-tech.org/mvd/XML/1.1}"
IDS = "{http://standards.buildingsmart.org/IDS}"
XS = "{http://www.w3.org/2001/XMLSchema}"
TOOL_FOLDER = Path(__file__).resolve().parent
# Rules files are read from "Simba Rules" and from the tool's own folder, and from folders inside them.
# Oslobygg's rules (converted from their BIM egenskapsmatrise) are kept apart in "Oslobygg Rules".
OSLOBYGG_FOLDER = TOOL_FOLDER / "Oslobygg Rules"
RULES_FOLDERS = {"statsbygg": [TOOL_FOLDER / "Simba Rules", TOOL_FOLDER], "oslobygg": [OSLOBYGG_FOLDER]}
CLIENT_NAMES = {"statsbygg": "SIMBA 2.1", "oslobygg": "Oslobygg"}
DASHBOARD_TEMPLATE = TOOL_FOLDER / "dashboard_template.html"
ALLOWED_VALUES_FILE = TOOL_FOLDER / "allowed_values.csv"
CROSS_CHECKS_FILE = TOOL_FOLDER / "cross_checks.csv"
# Your own copies of the NS 3451 system codes and NS 3457-8 component codes (optional, not shared)
TFM_CODES_FOLDER = TOOL_FOLDER / "TFM codes"
# Statsbygg's published TFM lists (PA 0802 system/component codes, PA 0702 NS 3451 system codes)
STATSBYGG_TFM_FOLDER = TOOL_FOLDER / "Statsbygg TFM"
# The property holding the TFM code in SIMBA 2.1 (NONS10102)
TFM_PROPERTY = ("NONS_Reference", "RefString")
# Statsbygg's IDS files are named ..._b3-1-architectural.ids, ..._b3-1_environmental_material.ids
IDS_FILE_NAME = re.compile(r"_b(\d)-(\d)[-_]([a-z_]+)\.ids$", re.IGNORECASE)
# Oslobygg's are named oslobygg_2-forprosjekt_ark.ids (the number keeps the phases in order)
OSLOBYGG_FILE_NAME = re.compile(r"^oslobygg_\d+-([a-z_]+)_([a-z]+)\.ids$", re.IGNORECASE)

# Statsbygg fills free-text [Value] rules with example values ('1234567', 'Building name'),
# so for these attributes a [Value] rule only means "must be filled in"
PLACEHOLDER_ATTRIBUTES = {"Name", "LongName", "Description", "LandTitleNumber"}
REQUIRED_ATTRIBUTES = PLACEHOLDER_ATTRIBUTES | {"ObjectType"}
PROPERTY_KEYS = {"Set", "PropertyName", "Value"}
POSITIONING_KEYS = {"ContextType", "ContextIdentifier", "CRSName", "HasGlobalPosition"}

# One rule condition, e.g. T_Set[Value]='Pset_WallCommon' or CRSName[Value]=reg'^EPSG:(.*)'
ATOM = re.compile(r"(T_)?(\w+)\[(\w+)\]=(reg)?('[^']*'|\S+)|\b(AND|OR)\b")


@dataclass
class Atom:
    on_type: bool   # T_ prefix: check the type object instead of the element
    key: str        # attribute or Set/PropertyName/Value
    metric: str     # Exists, Value or Type
    value: str
    is_regex: bool = False


@dataclass(eq=False)
class Concept:
    group: str       # ConceptRoot / IDS specification name, e.g. "200 Wall"
    entity: str      # e.g. "IfcWall" (subclasses included); None when `entities` is used
    label: str       # e.g. "Pset_WallCommon.IsExternal"
    rules: tuple
    applicability: tuple | None
    required: tuple | None
    required_by: set = field(default_factory=set)
    entities: tuple = ()     # IDS: exact classes, without subclasses
    predefined: str = None   # IDS: only objects with this PredefinedType
    contains: tuple = ()     # cross-check: ((pset, property), ...) whose values must appear in the required property
    description: str = ""    # what the requirement means, from the rules file
    filters: tuple = ()      # IDS: only objects whose property has one of these values, ((pset, property, (VALUES)), ...)
    allowed: tuple = ()      # IDS: allowed values or patterns for an attribute, e.g. a room number pattern

    def scope(self):
        """Which objects the concept applies to; concepts with the same scope share the lookup"""
        return (self.group, self.entity, self.entities, self.predefined, self.filters,
                id(self.applicability) if self.applicability else None)


@dataclass
class RuleSet:
    concepts: list
    phase: str               # shown in the report, e.g. "B3.1.1 Outline conceptual design ..."
    short: str               # used in file names, e.g. "B3.1.1"
    allowed: dict = field(default_factory=dict)  # allowed values that come with the rules (IDS)
    client: str = "statsbygg"    # whose requirements: "statsbygg" or "oslobygg"


@dataclass
class Result:
    concept: Concept
    element: ifcopenshell.entity_instance
    passed: bool
    value: str | None = None   # what the model has for this requirement, as text; None if missing

    @property
    def status(self):
        """"passed", "missing" (no value at all) or "failed" (a value that isn't valid)"""
        if self.passed:
            return "passed"
        return "missing" if self.value is None else "failed"


def main():
    model_path, rules_path = get_arguments(sys.argv)
    model = load_model(model_path)
    client, rule_files = choose_client(rules_path)
    if client == "statsbygg" and model.schema != "IFC4":
        print(f"Warning: Statsbygg requires IFC4, this model is {model.schema}")

    rules = choose_rules(rule_files)
    rules.client = client
    own_allowed = load_allowed_values(ALLOWED_VALUES_FILE)
    if own_allowed:
        print(f"Checking allowed values for {len(own_allowed)} properties from {ALLOWED_VALUES_FILE.name}")
    # A list in allowed_values.csv replaces the one in the rules
    allowed = {**rules.allowed, **own_allowed}
    cross_checks = load_cross_checks(CROSS_CHECKS_FILE)
    concepts = rules.concepts + cross_check_concepts(rules.concepts, cross_checks)
    if client == "statsbygg":
        concepts += tfm_concepts(rules.concepts, tfm_code_lists())
    # Copying the old DesignedStatus is a SIMBA 2.0 -> 2.1 step
    changed = migrate_designed_status(model) if client == "statsbygg" else False

    results = validate(model, concepts, allowed)
    print_summary(results)
    phase = rules.phase

    report_name = f"validation {Path(model_path).stem} {rules.short}"
    report = ask_option("Export a report?", ["csv", "html dashboard", "both", "no"])
    if report in ("csv", "both"):
        export_csv(results, f"{report_name}.csv")
        print(f"File saved as {report_name}.csv")
    if report in ("html dashboard", "both"):
        export_dashboard(results, model, model_path, phase, f"{report_name}.html", allowed, client)

    if changed:
        save_file(model, model_path)
    else:
        print("Program finished.")


def get_arguments(argv):
    if len(argv) < 2:
        sys.exit("Usage: python Validation_IFC.py model.ifc [rules.mvdxml or rules.ids]")
    if len(argv) > 3:
        sys.exit("Too many arguments")
    if not argv[1].lower().endswith(".ifc"):
        sys.exit("Not an IFC file")
    if len(argv) == 3:
        if not argv[2].lower().endswith((".mvdxml", ".ids")):
            sys.exit("Not an mvdXML or IDS file")
        return argv[1], Path(argv[2])
    return argv[1], None


def find_rule_files(folders, exclude=()):
    """mvdXML and IDS files in the folders and one level of folders inside them,
    leaving out those in `exclude` (another client's folder)"""
    files = []
    for folder in folders:
        for pattern in ("*.mvdxml", "*.ids", "*/*.mvdxml", "*/*.ids"):
            files += [p for p in sorted(folder.glob(pattern)) if p.parent not in exclude]
    # The same file in two places should only be listed once
    unique = {}
    for path in files:
        unique.setdefault(path.name, path)
    return list(unique.values())


def client_of(rules_path):
    """Whose rules a rules file given on the command line holds"""
    path = Path(rules_path).resolve()
    if OSLOBYGG_FOLDER in path.parents or path.name.lower().startswith("oslobygg"):
        return "oslobygg"
    return "statsbygg"


def choose_client(rules_path=None):
    """(client, rule files): SIMBA or Oslobygg, asking when both have rules"""
    if rules_path:
        return client_of(rules_path), [Path(rules_path)]
    found = {
        "statsbygg": find_rule_files(RULES_FOLDERS["statsbygg"], exclude=[OSLOBYGG_FOLDER]),
        "oslobygg": find_rule_files(RULES_FOLDERS["oslobygg"]),
    }
    found = {client: files for client, files in found.items() if files}
    if not found:
        sys.exit("No mvdXML or IDS rules found")
    if len(found) == 1:
        client = next(iter(found))
    else:
        client = ask_option("Rules for which client?", list(found))
    return client, found[client]


def load_model(path):
    try:
        return ifcopenshell.open(path)
    except (FileNotFoundError, OSError):
        sys.exit("IFC file doesn't exist")


# ---------- Reading the mvdXML ----------

def load_rules(path):
    """Return ({exchange requirement uuid: name}, [Concept])"""
    try:
        root = ET.parse(path).getroot()
    except (FileNotFoundError, ET.ParseError):
        sys.exit(f"Could not read rules file {path}")

    exchange_requirements = {er.get("uuid"): er.get("name") for er in root.iter(MVD + "ExchangeRequirement")}
    concepts = []
    for concept_root in root.iter(MVD + "ConceptRoot"):
        applicability = concept_root.find(f"{MVD}Applicability/{MVD}TemplateRules")
        if applicability is not None:
            applicability = parse_rules(applicability)

        for node in concept_root.iterfind(f"{MVD}Concepts/{MVD}Concept"):
            rules_node = node.find(MVD + "TemplateRules")
            if rules_node is None:
                continue
            rules = parse_rules(rules_node)
            required = find_required(rules)
            required_by = {
                r.get("exchangeRequirement")
                for r in node.iterfind(f"{MVD}Requirements/{MVD}Requirement")
                if r.get("requirement") == "mandatory"
            }
            concepts.append(Concept(
                concept_root.get("name"),
                concept_root.get("applicableRootEntity"),
                make_label(node.get("name"), required),
                rules,
                applicability,
                required,
                required_by,
                # "<what this requirement means> : <what the IFC class is>"
                description=clean_text(node.findtext(f"{MVD}Definitions/{MVD}Definition/{MVD}Body")).split(" : ")[0],
            ))
    return exchange_requirements, concepts


def parse_rules(node):
    """TemplateRules -> (operator, [children]), TemplateRule -> ("rule", [AND-groups joined by OR])"""
    if node.tag == MVD + "TemplateRule":
        return ("rule", parse_parameters(node.get("Parameters", "")))
    children = [parse_rules(child) for child in node if child.tag in (MVD + "TemplateRules", MVD + "TemplateRule")]
    return (node.get("operator", "and").lower(), children)


def parse_parameters(text):
    groups = [[]]
    for on_type, key, metric, regex, value, connector in ATOM.findall(text):
        if connector == "OR":
            groups.append([])
        elif not connector:
            groups[-1].append(Atom(bool(on_type), key, metric, value.strip("'"), bool(regex)))
    return [group for group in groups if group]


def iter_groups(node):
    kind, children = node
    if kind == "rule":
        yield from children
    else:
        for child in children:
            yield from iter_groups(child)


def find_required(rules):
    """What this concept asks for: ("property", pset, name, type), ("attribute", name), or None for
    checks such as PredefinedType, type object and georeferencing"""
    for group in iter_groups(rules):
        own = {(a.key, a.metric): a.value for a in group if not a.on_type}
        if ("Set", "Value") in own and ("PropertyName", "Value") in own and ("Value", "Exists") in own:
            return ("property", own[("Set", "Value")], own[("PropertyName", "Value")], own.get(("Value", "Type"), "IFCLABEL"))
    for group in iter_groups(rules):
        for atom in group:
            if not atom.on_type and atom.key in REQUIRED_ATTRIBUTES and atom.metric in ("Exists", "Value"):
                return ("attribute", atom.key)
    return None


def make_label(name, required):
    if required and required[0] == "property":
        return f"{required[1]}.{required[2]}"
    # "0001-003 Name : 200 Wall : 01 Architectural (ARK)" -> "Name"
    return re.sub(r"^\S+\s+", "", (name or "").split(" : ")[0])


# ---------- Reading IDS ----------
# Statsbygg's IDS files (IDS 0.9.6, exported from BIMQ) use a small part of IDS: one entity per
# specification (sometimes with a PredefinedType), then attribute and property requirements.
# Each requirement becomes a Concept with the same kind of rules as the mvdXML, so both are checked the same way.

def load_ids(path, fallback=None):
    """Return (info, [Concept], {(pset, property): [allowed values]}).
    `fallback` maps specification names to an applicability found in other IDS files, for
    specifications exported with an empty one"""
    try:
        root = ET.parse(path).getroot()
    except (FileNotFoundError, ET.ParseError):
        sys.exit(f"Could not read rules file {path}")
    info = {key: (root.findtext(f"{IDS}info/{IDS}{key}") or "").strip() for key in ("title", "milestone", "purpose")}

    concepts, allowed, skipped = [], {}, []
    for spec in root.iter(IDS + "specification"):
        group = spec.get("name") or ""
        applies = read_ids_applicability(spec.find(IDS + "applicability"))
        if applies is None:
            applies = (fallback or {}).get(group)
        if applies is None:
            skipped.append(group)
            continue
        entities, predefined, filters = applies

        requirements = spec.find(IDS + "requirements")
        for facet in requirements if requirements is not None else ():
            name = facet.findtext(f"{IDS}name/{IDS}simpleValue")
            if facet.tag == IDS + "property":
                pset = facet.findtext(f"{IDS}propertySet/{IDS}simpleValue")
                ifc_type = (facet.get("datatype") or "").upper() or None  # no datatype: any value type
                if not (pset and name):
                    continue
                required = ("property", pset, name, ifc_type)
                rules = property_rules(pset, name, ifc_type)
                values = ids_allowed_values(facet.find(IDS + "value"))
                if values:
                    allowed.setdefault((pset, name), [])
                    allowed[(pset, name)] += [v for v in values if v not in allowed[(pset, name)]]
                label = f"{pset}.{name}"
            elif facet.tag == IDS + "attribute" and name:
                required = ("attribute", name)
                rules = ("rule", [[Atom(False, name, "Exists", "TRUE")]])
                label = ids_label(facet.get("instructions"), name)
                # e.g. a room number pattern; checked in validate()
                attribute_allowed = tuple(ids_allowed_values(facet.find(IDS + "value")))
            else:
                continue  # classification, material and partOf requirements aren't used by Statsbygg
            # "0001- : ProjectNumber || Statsbygg official Project Number (seven digits)"
            description = clean_text(facet.get("instructions")).split("||", 1)[-1].strip()
            concepts.append(Concept(group, None, label, rules, None, required,
                                    entities=entities, predefined=predefined, description=description,
                                    filters=filters, allowed=attribute_allowed if required[0] == "attribute" else ()))
    if skipped:
        print(f"Note: {Path(path).name}: {len(skipped)} specification(s) apply to nothing and were skipped: {', '.join(skipped)}")
    return info, concepts, allowed


def property_rules(pset, name, ifc_type):
    """The element's own property, or else its type's, must have a value of the right type
    (the same rule shape Statsbygg's mvdXML uses)"""
    def side(on_type):
        atoms = [Atom(on_type, "Set", "Value", pset), Atom(on_type, "PropertyName", "Value", name),
                 Atom(on_type, "Value", "Exists", "TRUE")]
        if ifc_type:  # without a datatype any value type is accepted
            atoms.append(Atom(on_type, "Value", "Type", ifc_type))
        return atoms

    own, on_type = side(False), side(True)
    has_own = [Atom(False, "Set", "Value", pset), Atom(False, "PropertyName", "Value", name)]
    return ("or", [("rule", [own]), ("and", [("nor", [("rule", [has_own])]), ("rule", [on_type])])])


def read_ids_applicability(node):
    """(entity names, predefined type or None, property filters), or None when the specification applies
    to nothing. A filter (pset, property, (VALUES)) limits it to objects with that value, e.g. only
    external walls (Pset_WallCommon.IsExternal = TRUE)"""
    entity = node.find(IDS + "entity") if node is not None else None
    if entity is None:
        return None
    names = ids_values(entity.find(IDS + "name"))
    if not names:
        return None
    predefined = ids_values(entity.find(IDS + "predefinedType"))
    filters = []
    for facet in node.findall(IDS + "property"):
        pset = facet.findtext(f"{IDS}propertySet/{IDS}simpleValue")
        name = facet.findtext(f"{IDS}name/{IDS}simpleValue")
        values = tuple(v.upper() for v in ids_values(facet.find(IDS + "value")))
        if pset and name and values:
            filters.append((pset, name, values))
    return tuple(names), (predefined[0] if predefined else None), tuple(filters)


def ids_values(node):
    """A simpleValue, an enumeration, or a pattern that is just a list of names ("IFCWALL|IFCSLAB")"""
    if node is None:
        return []
    simple = node.findtext(IDS + "simpleValue")
    if simple:
        return [simple.strip()]
    values = [e.get("value") for e in node.iter(XS + "enumeration")]
    for pattern in node.iter(XS + "pattern"):
        values += pattern.get("value").split("|")
    return [v.strip() for v in values if v and v.strip()]


def ids_allowed_values(node):
    """Allowed values for a property. A list with a single value is an example in Statsbygg's files
    ("Matrikkelens bygningsnummer" for BuildingID), so it only means "must have a value"."""
    if node is None:
        return []
    values = [e.get("value") for e in node.iter(XS + "enumeration")]
    values = [v.strip().rstrip(".") for v in values if v and v.strip()]  # "UNSET." is a typo for UNSET
    patterns = [re.compile(p.get("value")) for p in node.iter(XS + "pattern")]
    if len(values) < 2:
        values = []
    return values + patterns


def clean_text(text):
    """Rule text without the broken characters some Statsbygg exports have (æøå that didn't survive)"""
    return " ".join((text or "").replace("�", "").split())


def ids_label(instructions, attribute):
    """'0001- : ProjectNumber || Statsbygg official ...' -> 'ProjectNumber'"""
    match = re.search(r":\s*([^|:]+?)\s*\|\|", instructions or "")
    return match.group(1) if match else attribute


def ids_fallback(paths):
    """{specification name: applicability} from every IDS file, to fill in empty applicabilities"""
    found = {}
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for spec in root.iter(IDS + "specification"):
            applies = read_ids_applicability(spec.find(IDS + "applicability"))
            if applies is not None:
                found.setdefault(spec.get("name"), applies)
    return found


def ids_phase(path, info):
    """('B3.1', 'architectural') from the file name, or the milestone and file name for other IDS files"""
    match = IDS_FILE_NAME.search(Path(path).name)
    if match:
        return f"B{match.group(1)}.{match.group(2)}", match.group(3).replace("_", " ").strip()
    match = OSLOBYGG_FILE_NAME.search(Path(path).name)
    if match:
        return match.group(1).replace("_", " ").capitalize(), match.group(2).upper()
    return info.get("milestone") or Path(path).stem, Path(path).stem


# ---------- Cross-checks between properties ----------
# e.g. NONS_Reference.RefString must contain the values of RefLocStorey, RefPriSysClass, ...

def load_cross_checks(path):
    """{(pset, property): ((pset, property), ...)} from cross_checks.csv; {} if there is no file"""
    checks = {}
    if not path.exists():
        return checks
    with open(path, newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            name = (row.get("Property") or "").strip()
            if name.startswith("#") or "." not in name:
                continue
            parts = [p.strip() for p in (row.get("Must contain") or "").split(";") if "." in p]
            if parts:
                checks[tuple(name.split(".", 1))] = tuple(tuple(p.split(".", 1)) for p in parts)
    return checks


def cross_check_concepts(concepts, checks):
    """An extra requirement for every object group that requires a property with a cross-check"""
    extra, seen = [], set()
    for concept in concepts:
        required = concept.required
        if not (required and required[0] == "property" and (required[1], required[2]) in checks):
            continue
        key = (concept.scope(), required[1], required[2])
        if key in seen:
            continue
        seen.add(key)
        parts = checks[(required[1], required[2])]
        extra.append(Concept(
            concept.group, concept.entity, f"{required[1]}.{required[2]} contents", None, concept.applicability,
            ("cross", required[1], required[2], parts), concept.required_by,
            entities=concept.entities, predefined=concept.predefined, contains=parts, filters=concept.filters,
            description=f"The value must contain the values of {', '.join(part[1] for part in parts)} (from {CROSS_CHECKS_FILE.name})."))
    return extra


# ---------- TFM codes (Statsbygg PA 0805 and PA 0802) ----------
# Statsbygg has two TFM formats, told apart by the location sign:
#   ++  PA 0805 Bruk av NS-TFM i Statsbygg (rev. 04, 13.03.2025), "Normal TFM-ID med komponenttype":
#       ++AAAAAA=NNNN.nnn.nn(n)-BBBnnn%BBB.nnn.nnn
#   +   PA 0802 Tverrfaglig merkesystem (rev. 3; code lists rev. 5, 17.02.2026), component and component type:
#       +AAAAAA=NNN.nnn-BBnnn and +AAAAAA=NNN.nnn-BBnnnT

TFM_FORMATS = {"0805": "++AAAAAA=NNNN.nnn-BBBnnn%BBB.nnn.nnn", "0802": "+AAAAAA=NNN.nnn-BBnnn(T)"}
TFM_PARTS = re.compile(r"^\+\+(?P<location>[^=]*)=(?P<system>[^-%]*)-(?P<component>[^%]*)%(?P<type>.*)$")
TFM_SYSTEM = re.compile(r"^(\d{4})\.(\d{3})(?:\.(\d+))?$")
TFM_COMPONENT = re.compile(r"^([A-ZÆØÅ]{3})(\d{3})$")
TFM_TYPE = re.compile(r"^([A-ZÆØÅ]{3})\.(\d{3})(?:\.(\d{2,3}))?$")  # PA 0805 shows .nn in 2.3.3, .nnn in its examples
TFM_0802_PARTS = re.compile(r"^\+(?P<location>[^=]*)=(?P<system>[^-]*)-(?P<component>.*)$")
TFM_0802_SYSTEM = re.compile(r"^(\d{3})\.(\d{3})$")
TFM_0802_COMPONENT = re.compile(r"^([A-ZÆØÅ]{2})(\d{3})(T?)$")
# Overall system codes Statsbygg doesn't allow, and what to use instead (PA 0805 2.2.1.1)
TFM_BANNED_SYSTEMS = {
    "2300": "2310, 2320, 2330 or 2340", "2400": "a 24x0 code", "2500": "a 25x0 code", "2600": "a 26x0 code",
    "2700": "a 27x0 code", "3100": "a 31x0 code", "3300": "a 33x0 code", "3400": "a 34x0 code",
    "3500": "a 35x0 code", "4600": "a 46x0 code", "5210": "5201-5204", "5230": "5201-5204", "5240": "5201-5204",
    "7600": "a 76x0 code", "7700": "a 77x0 code",
    **{f"360{i}": "3600 (air handling only uses 3600)" for i in range(1, 8)},
}


@dataclass(frozen=True, eq=False)
class TfmCodes:
    """The code lists TFM codes are checked against; None where there is no list"""
    ns3451: frozenset = None            # your NS 3451 system codes ("TFM codes" folder)
    ns3457: frozenset = None            # your NS 3457-8 component codes
    pa0805: dict = None                 # Statsbygg's NS 3451 system codes from PA 0702: {code: "yes"/"no"}
    pa0802_systems: dict = None         # PA 0802 system codes: {code: "yes"/"no"}
    pa0802_components: frozenset = None  # PA 0802 component codes

    def names(self):
        """The lists in use, for messages"""
        return [name for name, found in (
            ("NS 3451", self.ns3451), ("NS 3457-8", self.ns3457), ("PA 0702 system codes", self.pa0805),
            ("PA 0802 system codes", self.pa0802_systems), ("PA 0802 component codes", self.pa0802_components)) if found]


def tfm_format(code):
    """"0805" or "0802": by the location sign, or without one by the system code's digits"""
    code = code.strip()
    if code.startswith("++"):
        return "0805"
    if code.startswith("+"):
        return "0802"
    system = re.search(r"=(\d+)", code)
    if "%" in code or (system and len(system.group(1)) == 4):
        return "0805"
    if system and len(system.group(1)) == 3:
        return "0802"
    return None


def tfm_problems(code, codes=None):
    """What is wrong with a Statsbygg TFM code (PA 0805 or PA 0802), as short sentences; [] if nothing"""
    codes = codes or TfmCodes()
    code, problems = code.strip(), []
    if re.search("[–—]", code):
        problems.append("use - (hyphen), not – (dash)")
        code = re.sub("[–—]", "-", code)
    kind = tfm_format(code)
    if kind == "0805":
        return problems + pa0805_problems(code, codes)
    if kind == "0802":
        return problems + pa0802_problems(code, codes)
    return problems + [f"not a TFM code; expected {TFM_FORMATS['0805']} (PA 0805) or {TFM_FORMATS['0802']} (PA 0802)"]


def tfm_location_problems(location):
    if re.fullmatch(r"\d{6}", location):
        return []
    return [f"location {location or '(empty)'} should be the 6-digit building number"]


def pa0805_problems(code, codes):
    """NS-TFM as Statsbygg uses it (PA 0805)"""
    for sign, name in (("<", "owner's overall ID <...>"), ("==", "special functional role =="), ("(", "additional information (...)")):
        if sign in code:
            return [f"{name} is not used by Statsbygg"]
    expected = TFM_FORMATS["0805"]
    parts = TFM_PARTS.match(code)
    if not parts:
        missing = [name for sign, name in (("++", "++location"), ("=", "=system"), ("-", "-component"), ("%", "%type"))
                   if sign not in code]
        if missing:
            return [f"missing {', '.join(missing)}; expected {expected}"]
        return [f"parts in the wrong order; expected {expected}"]

    problems = tfm_location_problems(parts["location"])
    system = TFM_SYSTEM.match(parts["system"])
    if not system:
        problems.append(f"system ={parts['system']} should be NNNN.nnn (4-digit code, 3-digit number)")
    else:
        number, sub = system.group(1), system.group(3)
        if number in TFM_BANNED_SYSTEMS:
            problems.append(f"system code {number} is not used by Statsbygg; use {TFM_BANNED_SYSTEMS[number]}")
        elif codes.pa0805 and codes.pa0805.get(number) == "no":
            problems.append(f"system code {number} is not used by Statsbygg (PA 0702)")
        elif codes.ns3451 and number not in codes.ns3451:
            problems.append(f"system code {number} is not in NS 3451")
        family = number[:2]
        if sub is not None:
            if number[0] == "2":
                problems.append(f"building systems (2xxx) have no sub-number (.{sub})")
            elif family in ("32", "35", "37") and sub not in ("04", "05"):
                problems.append(f"sub-number .{sub} should be .04 (supply) or .05 (return)")
            elif family == "36" and sub not in {f"0{i}" for i in range(1, 9)}:
                problems.append(f"sub-number .{sub} should be .01-.08 for air handling")
            elif number[0] in ("4", "5") and len(sub) != 3:
                problems.append(f"sub-number .{sub} should have 3 digits for {number[0]}xxx systems")

    component = TFM_COMPONENT.match(parts["component"])
    if not component:
        problems.append(f"component -{parts['component']} should be 3 letters and 3 digits, e.g. -RTA001")
    elif codes.ns3457 and component.group(1) not in codes.ns3457:
        problems.append(f"component code {component.group(1)} is not in NS 3457-8")

    kind = TFM_TYPE.match(parts["type"])
    if not kind:
        problems.append(f"type %{parts['type']} should be BBB.nnn.nnn, e.g. %RTA.001.002")
    elif codes.ns3457 and kind.group(1) not in codes.ns3457 and (not component or kind.group(1) != component.group(1)):
        problems.append(f"type code {kind.group(1)} is not in NS 3457-8")
    return problems


def pa0802_problems(code, codes):
    """Statsbygg's own TFM (PA 0802): +building=system.number-component, with T at the end for a type"""
    expected = TFM_FORMATS["0802"]
    parts = TFM_0802_PARTS.match(code)
    if not parts:
        missing = [name for sign, name in (("+", "+location"), ("=", "=system"), ("-", "-component")) if sign not in code]
        if missing:
            return [f"missing {', '.join(missing)}; expected {expected}"]
        return [f"parts in the wrong order; expected {expected}"]

    problems = tfm_location_problems(parts["location"])
    system = TFM_0802_SYSTEM.match(parts["system"])
    if not system:
        problems.append(f"system ={parts['system']} should be NNN.nnn (3-digit code, 3-digit number)")
    elif codes.pa0802_systems:
        number = system.group(1)
        use = codes.pa0802_systems.get(number)
        if use is None:
            problems.append(f"system code {number} is not in the PA 0802 system list")
        elif use == "no":
            family = number[:2] + "0"
            instead = f"; use {family}" if family != number and codes.pa0802_systems.get(family) == "yes" else ""
            problems.append(f"system code {number} is not used in TFM (PA 0802){instead}")

    component = TFM_0802_COMPONENT.match(parts["component"])
    if not component:
        problems.append(f"component -{parts['component']} should be 2 letters and 3 digits, "
                        "with T for a component type, e.g. -DI001 or -DI001T")
    elif codes.pa0802_components and component.group(1) not in codes.pa0802_components:
        problems.append(f"component code {component.group(1)} is not in the PA 0802 component list")
    return problems


def read_code_file(path, pattern):
    """Codes from an Excel or CSV file: every cell that starts with a code, e.g. "2310 Bærende ..." """
    texts = []
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as file:
            sample = file.read(4096)
            file.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
            texts = [cell for row in csv.reader(file, dialect) for cell in row]
    elif path.suffix.lower() in (".xlsx", ".xlsm"):
        texts = xlsx_texts(path)
    alone, leading = set(), set()
    for text in texts:
        text = str(text).strip()
        match = pattern.match(text)
        if match:
            (alone if match.group(1) == text else leading).add(match.group(1))
    # With a column of codes, cells that only start with one are names ("UPS inkl. batteri"), not codes
    return alone or leading


def xlsx_texts(path):
    """All cell texts in an Excel workbook, without extra libraries"""
    import zipfile
    sheet = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    texts = []
    with zipfile.ZipFile(path) as book:
        names = book.namelist()
        strings = []
        if "xl/sharedStrings.xml" in names:
            strings = ["".join(t.text or "" for t in si.iter(sheet + "t"))
                       for si in ET.fromstring(book.read("xl/sharedStrings.xml")).iter(sheet + "si")]
        for name in names:
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                for cell in ET.fromstring(book.read(name)).iter(sheet + "c"):
                    value, inline = cell.find(sheet + "v"), cell.find(sheet + "is")
                    if value is not None and value.text is not None:
                        text = strings[int(value.text)] if cell.get("t") == "s" else value.text
                        texts.append(text[:-2] if text.endswith(".0") else text)  # 2310 stored as a number
                    elif inline is not None:
                        texts.append("".join(t.text or "" for t in inline.iter(sheet + "t")))
    return texts


def load_tfm_codes(folder):
    """(NS 3451 system codes or None, NS 3457-8 component codes or None) from files in `folder` whose
    names contain 3451 / 3457. Without them only the format is checked."""
    system_codes, component_codes = set(), set()
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in (".csv", ".xlsx", ".xlsm"):
                continue
            try:
                if "3451" in path.name:
                    system_codes |= read_code_file(path, re.compile(r"^(\d{4})(?!\d)"))
                elif "3457" in path.name:
                    component_codes |= read_code_file(path, re.compile(r"^([A-ZÆØÅ]{3})(?![A-Za-zÆØÅæøå])"))
            except (OSError, ValueError, KeyError, csv.Error, ET.ParseError) as error:
                print(f"Could not read {path.name}: {error}")
    for name, codes in (("NS 3451 system", system_codes), ("NS 3457-8 component", component_codes)):
        if codes:
            print(f"Checking TFM codes against {len(codes)} {name} codes from {folder.name}")
    return system_codes or None, component_codes or None


def read_statsbygg_list(path):
    """{code: (name, use)} from a CSV made by make_tfm_lists.py; None if the file isn't there"""
    if not path.is_file():
        return None
    try:
        with open(path, newline="", encoding="utf-8-sig") as file:
            rows = [row for row in csv.reader(file) if row and row[0] and not row[0].startswith("#")][1:]
    except (OSError, csv.Error) as error:
        print(f"Could not read {path.name}: {error}")
        return None
    return {row[0]: (row[1] if len(row) > 1 else "", row[2] if len(row) > 2 else "yes") for row in rows}


def tfm_code_lists(own_folder=TFM_CODES_FOLDER, statsbygg_folder=STATSBYGG_TFM_FOLDER):
    """Your NS 3451 / NS 3457-8 lists plus Statsbygg's published PA 0802 and PA 0702 lists"""
    ns3451, ns3457 = load_tfm_codes(own_folder)
    pa0805 = read_statsbygg_list(statsbygg_folder / "pa0805_system_codes.csv")
    systems = read_statsbygg_list(statsbygg_folder / "pa0802_system_codes.csv")
    components = read_statsbygg_list(statsbygg_folder / "pa0802_component_codes.csv")
    codes = TfmCodes(
        ns3451=frozenset(ns3451) if ns3451 else None, ns3457=frozenset(ns3457) if ns3457 else None,
        pa0805={code: use for code, (_, use) in pa0805.items()} if pa0805 else None,
        pa0802_systems={code: use for code, (_, use) in systems.items()} if systems else None,
        pa0802_components=frozenset(components) if components else None)
    found = [f"{name} ({len(lst)})" for name, lst in (
        ("PA 0702 system codes", codes.pa0805), ("PA 0802 system codes", codes.pa0802_systems),
        ("PA 0802 component codes", codes.pa0802_components)) if lst]
    if found:
        print(f"Checking TFM codes against Statsbygg's lists: {', '.join(found)}")
    return codes


def tfm_concepts(concepts, codes=None):
    """A TFM format check for every object group that requires the TFM property"""
    extra, seen = [], set()
    for concept in concepts:
        required = concept.required
        if not (required and required[0] == "property" and (required[1], required[2]) == TFM_PROPERTY):
            continue
        if concept.scope() in seen:
            continue
        seen.add(concept.scope())
        codes = codes or TfmCodes()
        lists = ", ".join(codes.names())
        extra.append(Concept(
            concept.group, concept.entity, f"{TFM_PROPERTY[0]}.{TFM_PROPERTY[1]} TFM format", None, concept.applicability,
            ("tfm", *TFM_PROPERTY, codes), concept.required_by,
            entities=concept.entities, predefined=concept.predefined, filters=concept.filters,
            description="TFM code in one of Statsbygg's formats. PA 0805 (NS-TFM): "
                        "++building number=system code.number(.sub)-component%type, e.g. ++123456=3600.001.04-RTA001%RTA.001.002. "
                        "PA 0802: +building number=system code.number-component, with T for a type, e.g. +123456=244.001-DI001T"
                        + (f". Codes are checked against {lists}." if lists else ".")))
    return extra


# ---------- Checking the model ----------

class Checker:
    def __init__(self, model, allowed=None):
        self.model = model
        self.allowed = allowed or {}
        self.positioning = positioning_facts(model)
        self._properties = {}
        self._applicable = {}

    def applicable_elements(self, concept):
        key = concept.scope()
        if key not in self._applicable:
            if concept.entities:
                # IDS: the class itself, not its subclasses
                elements = []
                for name in concept.entities:
                    elements += self.by_type(name, include_subtypes=False)
            else:
                elements = self.by_type(concept.entity)
            if concept.applicability:
                elements = [e for e in elements if self.evaluate(concept.applicability, e)]
            if concept.predefined:
                elements = [e for e in elements if self.predefined_type(e) == concept.predefined.upper()]
            for pset, prop, values in concept.filters:
                elements = [e for e in elements if (self.property_text(e, pset, prop) or "").upper() in values]
            self._applicable[key] = elements
        return self._applicable[key]

    def by_type(self, name, include_subtypes=True):
        try:
            return list(self.model.by_type(name, include_subtypes))
        except RuntimeError:
            return []  # entity doesn't exist in this schema

    def predefined_type(self, element):
        """The element's PredefinedType, or its type's when the element's is NOTDEFINED or empty"""
        value = attribute_value(element, "PredefinedType") or attribute_value(self.target(element, True), "PredefinedType")
        return str(value).upper() if value else None

    def property_text(self, element, pset, prop):
        """A property's value as text, from the element or else its type; None if there is none"""
        for target in (element, self.target(element, True)):
            properties = self.properties(target).get(pset, {}) if target is not None else {}
            if prop in properties:
                nominal = properties[prop]
                return None if nominal is None else display_value(nominal.wrappedValue)
        return None

    def check_contains(self, concept, element):
        """Cross-check: (passed, value), or None when the object doesn't have the property at all
        (the property's own requirement already reports that)"""
        _, pset, prop, parts = concept.required
        whole = self.property_text(element, pset, prop)
        if whole is None:
            return None
        problems = []
        for part_pset, part_prop in parts:
            part = self.property_text(element, part_pset, part_prop)
            if part is None:
                problems.append(f"no {part_prop}")
            elif part not in whole:
                problems.append(f"{part_prop} {part} not in it")
        return (not problems, whole + (f" ({'; '.join(problems)})" if problems else ""))

    def check_tfm(self, concept, element):
        """TFM format: (passed, value with what is wrong), or None when the object has no TFM code
        (the property's own requirement already reports that)"""
        _, pset, prop, codes = concept.required
        code = self.property_text(element, pset, prop)
        if code is None:
            return None
        problems = tfm_problems(code, codes)
        return (not problems, code + (f" ({'; '.join(problems)})" if problems else ""))

    def evaluate(self, node, element):
        kind, children = node
        if kind == "rule":
            return any(self.check_group(group, element) for group in children)
        results = (self.evaluate(child, element) for child in children)
        if kind == "or":
            return any(results)
        if kind == "nor":
            return not any(results)
        return all(results)

    def check_group(self, atoms, element):
        property_atoms = {False: [], True: []}
        positioning_atoms = []
        for atom in atoms:
            if atom.key in PROPERTY_KEYS:
                property_atoms[atom.on_type].append(atom)
            elif atom.key in POSITIONING_KEYS:
                positioning_atoms.append(atom)
            elif not self.check_attribute(atom, element):
                return False

        for on_type, atoms_for_side in property_atoms.items():
            if atoms_for_side and not self.check_property(atoms_for_side, element, on_type):
                return False

        if positioning_atoms:
            return any(all(value_matches(facts[a.key], a) for a in positioning_atoms) for facts in self.positioning)
        return True

    def check_attribute(self, atom, element):
        if atom.key == "TypeObject":
            value = self.target(element, True)
        else:
            value = attribute_value(self.target(element, atom.on_type), atom.key)
        if atom.metric == "Value" and atom.key in PLACEHOLDER_ATTRIBUTES:
            return value is not None
        return value_matches(value, atom)

    def check_property(self, atoms, element, on_type):
        target = self.target(element, on_type)
        if target is None:
            return False
        names = {a.key: a.value for a in atoms if a.metric == "Value" and a.key in ("Set", "PropertyName")}
        properties = self.properties(target).get(names.get("Set"), {})
        if names.get("PropertyName") not in properties:
            return False

        nominal = properties[names["PropertyName"]]
        for atom in atoms:
            if atom.key != "Value":
                continue
            if atom.metric == "Exists" and (nominal is not None) != (atom.value.upper() == "TRUE"):
                return False
            if atom.metric == "Type" and (nominal is None or nominal.is_a().upper() != atom.value.upper()):
                return False
            if atom.metric == "Value" and (nominal is None or not value_matches(nominal.wrappedValue, atom)):
                return False

        # Where the rule requires a value, it must also be a valid one
        requires_value = any(a.key == "Value" and a.metric == "Exists" and a.value.upper() == "TRUE" for a in atoms)
        if requires_value and nominal is not None:
            if not in_range(nominal):
                return False
            allowed = self.allowed.get((names["Set"], names["PropertyName"]))
            if allowed and not is_allowed(nominal.wrappedValue, allowed):
                return False
        return True

    def value_of(self, concept, element):
        """The value the requirement looks at, as text, or None if there is none"""
        required = concept.required
        if required and required[0] == "property":
            _, pset, prop, ifc_type = required
            # The element's own value wins over its type's, as in the rules
            for target in (element, self.target(element, True)):
                properties = self.properties(target).get(pset, {}) if target is not None else {}
                if prop in properties:
                    nominal = properties[prop]
                    if nominal is None:
                        return None
                    text = display_value(nominal.wrappedValue)
                    if ifc_type and nominal.is_a().upper() != ifc_type.upper():
                        text += f" ({nominal.is_a()})"
                    return text
            return None

        keys = {atom.key for group in iter_groups(concept.rules) for atom in group}
        if required:
            return display_value(attribute_value(element, required[1]))
        if "PredefinedType" in keys:
            value = attribute_value(element, "PredefinedType")
            object_type = self.target(element, True)
            if value is None:
                value = attribute_value(object_type, "PredefinedType")
            if value is None and object_type is not None and "TypeObject" in keys:
                # The rule also accepts just having a type object
                value = f"type {object_type.Name or object_type.is_a()}"
            return display_value(value)
        if "TypeObject" in keys:
            object_type = self.target(element, True)
            return display_value(object_type.Name or object_type.is_a()) if object_type else None
        if keys & POSITIONING_KEYS:
            return next((facts["CRSName"] for facts in self.positioning if facts["CRSName"]), None)
        return None

    def target(self, element, on_type):
        if not on_type:
            return element
        try:
            return ifcopenshell.util.element.get_type(element)
        except AttributeError:
            return None

    def properties(self, target):
        """{pset name: {property name: nominal value entity or None}}, read straight from the file"""
        if target.id() not in self._properties:
            if target.is_a("IfcTypeObject"):
                definitions = target.HasPropertySets or ()
            else:
                definitions = []
                for rel in getattr(target, "IsDefinedBy", ()):
                    if rel.is_a("IfcRelDefinesByProperties"):
                        definition = rel.RelatingPropertyDefinition
                        definitions += definition if isinstance(definition, tuple) else [definition]

            psets = {}
            for pset in definitions:
                if pset.is_a("IfcPropertySet"):
                    values = psets.setdefault(pset.Name, {})
                    for prop in pset.HasProperties:
                        values[prop.Name] = nominal_value(prop)
            self._properties[target.id()] = psets
        return self._properties[target.id()]


def nominal_value(prop):
    if prop.is_a("IfcPropertySingleValue"):
        value = prop.NominalValue
    elif prop.is_a("IfcPropertyEnumeratedValue") and prop.EnumerationValues:
        value = prop.EnumerationValues[0]
    else:
        return None
    if value is None or value.wrappedValue in (None, ""):
        return None
    return value


def attribute_value(target, key):
    if target is None:
        return None
    try:
        value = getattr(target, key)
    except AttributeError:
        return None
    # NOTDEFINED on an element means "use the type's PredefinedType"
    if value in ("", "NOTDEFINED"):
        return None
    return value


def display_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def value_matches(value, atom):
    if atom.metric == "Exists":
        return (value is not None) == (atom.value.upper() == "TRUE")
    if value is None:
        return False
    if atom.is_regex:
        return re.search(atom.value, str(value)) is not None
    return str(value) == atom.value


def positioning_facts(model):
    """One dict per representation context, used by the georeferencing rules"""
    facts = []
    for context in model.by_type("IfcGeometricRepresentationContext"):
        parent = getattr(context, "ParentContext", None)
        operations = getattr(context, "HasCoordinateOperation", None) or getattr(parent, "HasCoordinateOperation", None) or ()
        operation = operations[0] if operations else None
        crs = operation.TargetCRS if operation else None
        facts.append({
            "ContextType": context.ContextType,
            "ContextIdentifier": context.ContextIdentifier,
            "HasGlobalPosition": operation,
            "CRSName": crs.Name if crs else None,
        })
    return facts


def in_range(nominal):
    """IFC's own limits on measure types"""
    ifc_type = nominal.is_a().upper()
    value = nominal.wrappedValue
    if "POSITIVE" in ifc_type:
        return value > 0
    if "NORMALISEDRATIO" in ifc_type:
        return 0 <= value <= 1
    return True


def load_allowed_values(path):
    """{(pset, property): [allowed text or re.Pattern]} from allowed_values.csv; {} if there is no file"""
    allowed = {}
    if not path.exists():
        return allowed
    with open(path, newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            name = (row.get("Property") or "").strip()
            if name.startswith("#") or "." not in name:
                continue
            values = []
            for value in (row.get("Allowed values") or "").split(";"):
                value = value.strip()
                if value.startswith("re:"):
                    try:
                        values.append(re.compile(value[3:]))
                    except re.error as error:
                        sys.exit(f"{path.name}: invalid pattern '{value[3:]}' for {name} ({error})")
                elif value:
                    values.append(value)
            if values:
                allowed[tuple(name.split(".", 1))] = values
    return allowed


def is_allowed(value, allowed):
    text = str(value)
    return any(a.fullmatch(text) if isinstance(a, re.Pattern) else text == a for a in allowed)


def validate(model, concepts, allowed=None):
    checker = Checker(model, allowed)
    results = []
    for concept in concepts:
        for element in checker.applicable_elements(concept):
            if concept.required and concept.required[0] == "tfm":
                outcome = checker.check_tfm(concept, element)
                if outcome is not None:
                    results.append(Result(concept, element, *outcome))
            elif concept.contains:
                outcome = checker.check_contains(concept, element)
                if outcome is not None:
                    results.append(Result(concept, element, *outcome))
            else:
                passed, value = checker.evaluate(concept.rules, element), checker.value_of(concept, element)
                if passed and concept.allowed and value is not None and not is_allowed(value, concept.allowed):
                    passed = False
                results.append(Result(concept, element, passed, value))
    return results


# ---------- Output ----------

def count_statuses(results):
    counts = {"passed": 0, "failed": 0, "missing": 0}
    for result in results:
        counts[result.status] += 1
    return counts


def print_summary(results):
    by_concept = {}
    for result in results:
        by_concept.setdefault(result.concept, []).append(result)

    print()
    for concept, concept_results in by_concept.items():
        counts = count_statuses(concept_results)
        status = "OK  " if counts["passed"] == len(concept_results) else "FAIL"
        print(f"{status} {concept.group:<30} {concept.label:<50} "
              f"{counts['failed']} failed, {counts['missing']} missing of {len(concept_results)}")

    counts = count_statuses(results)
    print(f"\n{counts['passed']} of {len(results)} checks passed, {counts['failed']} failed, {counts['missing']} missing "
          f"({len(by_concept)} requirements on objects in the model)")


def export_csv(results, filename):
    with open(filename, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["GlobalId", "IfcType", "Name", "Group", "Requirement", "Value", "Validation"])
        writer.writeheader()
        for result in results:
            writer.writerow({
                "GlobalId": getattr(result.element, "GlobalId", ""),
                "IfcType": result.element.is_a(),
                "Name": getattr(result.element, "Name", ""),
                "Group": result.concept.group,
                "Requirement": result.concept.label,
                "Value": "" if result.value is None else result.value,
                "Validation": result.status.upper(),
            })


STATUS_CODES = {"failed": 0, "passed": 1, "missing": 2}


def allowed_text(values):
    """Allowed values as the dashboard shows them; patterns start with "re:" as in allowed_values.csv"""
    return [f"re:{v.pattern}" if isinstance(v, re.Pattern) else v for v in values]


def storeys(model):
    """[{"name", "elevation", "steps": [file numbers of the objects on it]}], lowest first, for the 3D
    viewer's floor filter. Covers every object, also those without requirements, and the parts of
    objects (stair flights, curtain wall panels) that sit on a storey through their whole."""
    found = {}

    def storey_of(spatial):
        # A space or zone belongs to the storey it is part of
        for _ in range(10):
            if spatial is None or spatial.is_a("IfcBuildingStorey"):
                return spatial
            spatial = ifcopenshell.util.element.get_aggregate(spatial)
        return None

    def add(storey, element, depth=0):
        found.setdefault(storey, []).append(element.id())
        if depth < 5:
            for rel in getattr(element, "IsDecomposedBy", ()):
                for part in rel.RelatedObjects:
                    add(storey, part, depth + 1)

    for rel in model.by_type("IfcRelContainedInSpatialStructure"):
        storey = storey_of(rel.RelatingStructure)
        if storey is not None:
            for element in rel.RelatedElements:
                add(storey, element)

    def elevation(storey):
        try:
            return float(storey.Elevation) if storey.Elevation is not None else None
        except (TypeError, ValueError):
            return None

    ordered = sorted(found, key=lambda s: (elevation(s) is None, elevation(s) or 0, s.id()))
    return [{"name": s.Name or s.LongName or f"Storey #{s.id()}", "elevation": elevation(s), "steps": sorted(set(found[s]))}
            for s in ordered]


def dashboard_data(results, model, model_path, phase, allowed=None, client="statsbygg"):
    requirements = {}
    objects = {}
    values = {}
    # One row per check, as indexes into the lists above to keep the file small:
    # [object, requirement, status (1 passed, 0 failed, 2 missing), value (-1 if missing)]
    checks = []
    for result in results:
        concept, element = result.concept, result.element
        required = concept.required
        is_property = required is not None and required[0] in ("property", "cross", "tfm")
        requirement = requirements.setdefault(concept, {
            "group": concept.group,
            "label": concept.label,
            "index": len(requirements),
            "pset": required[1] if is_property else None,
            "type": required[3] if is_property and required[0] == "property" else None,
            "info": concept.description,
            "allowed": allowed_text(concept.allowed or ((allowed or {}).get((required[1], required[2]), [])
                                                        if is_property and required[0] == "property" else [])),
            "passed": 0,
            "failed": 0,
            "missing": 0,
        })
        requirement[result.status] += 1

        obj = objects.setdefault(element.id(), {
            "id": getattr(element, "GlobalId", ""),
            # The number in the IFC file (#123=...), which the 3D viewer uses to find the object
            "step": element.id(),
            "cls": element.is_a(),
            "name": getattr(element, "Name", None) or "",
            "index": len(objects),
            "total": 0,
            "failed": [],
        })
        obj["total"] += 1
        if not result.passed and concept.label not in obj["failed"]:
            obj["failed"].append(concept.label)

        value = -1 if result.value is None else values.setdefault(result.value, len(values))
        checks.append([obj["index"], requirement["index"], STATUS_CODES[result.status], value])

    return {
        "model": Path(model_path).name,
        "client": client,
        "title": f"{CLIENT_NAMES.get(client, client)} validation",
        "schema": model.schema,
        "phase": phase,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "requirements": list(requirements.values()),
        "objects": list(objects.values()),
        "values": list(values),
        "checks": checks,
        "storeys": storeys(model),
    }


def export_dashboard(results, model, model_path, phase, filename, allowed=None, client="statsbygg"):
    if not DASHBOARD_TEMPLATE.exists():
        print(f"Dashboard not saved: {DASHBOARD_TEMPLATE.name} must be in the same folder as this script")
        return
    data = json.dumps(dashboard_data(results, model, model_path, phase, allowed, client))
    # "</" would end the <script> block early if an object name contained "</script>"
    html = DASHBOARD_TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", data.replace("</", "<\\/"))
    Path(filename).write_text(html, encoding="utf-8")
    print(f"Dashboard saved as {filename}")


# ---------- Copying old status values ----------

def find_old_status(model):
    """Objects with the pre-SIMBA 2.1 NOSSB_Process.DesignedStatus but no NONS_Process.ProcessStatus.
    Returns [(element, value)]; values from the element's type count too"""
    found = []
    for element in model.by_type("IfcObject"):
        old = ifcopenshell.util.element.get_pset(element, "NOSSB_Process", "DesignedStatus")
        new = ifcopenshell.util.element.get_pset(element, "NONS_Process", "ProcessStatus")
        if old not in (None, "") and new in (None, ""):
            found.append((element, str(old)))
    return found


def migrate_designed_status(model):
    to_copy = find_old_status(model)
    if not to_copy:
        return False
    print(f"\n{len(to_copy)} object(s) have the old NOSSB_Process.DesignedStatus but no NONS_Process.ProcessStatus")
    if not ask_yes_no("Copy DesignedStatus into ProcessStatus?"):
        return False
    for element, value in to_copy:
        write_property(model, element, "NONS_Process", "ProcessStatus", "IFCLABEL", value)
    print(f"Copied on {len(to_copy)} object(s). The old NOSSB_Process values are kept.")
    return True


def write_property(model, element, pset_name, prop_name, ifc_type, value):
    # Always written on the element itself, so other objects sharing its type aren't changed
    pset = ifcopenshell.api.pset.add_pset(model, product=element, name=pset_name)
    ifcopenshell.api.pset.edit_pset(model, pset=pset, properties={prop_name: model.create_entity(ifc_type, value)})


# ---------- Prompts ----------

def choose_rules(rule_files):
    """Pick the rules: mvdXML or IDS when both are there, then the phase (and for IDS the discipline)"""
    mvdxml = [p for p in rule_files if p.suffix.lower() == ".mvdxml"]
    ids = [p for p in rule_files if p.suffix.lower() == ".ids"]
    if not mvdxml and not ids:
        sys.exit("No mvdXML or IDS rules found")
    if mvdxml and ids:
        use_ids = ask_option("Rules format? mvdXML has the .1/.5/.9 steps, IDS covers every discipline", ["mvdxml", "ids"]) == "ids"
    else:
        use_ids = bool(ids)
    return choose_ids(ids) if use_ids else choose_exchange_requirement(mvdxml)


def choose_exchange_requirement(rule_files):
    """Pick a phase across all mvdXML files"""
    options = []
    for path in rule_files:
        exchange_requirements, concepts = load_rules(path)
        for uuid, name in exchange_requirements.items():
            options.append((name, uuid, concepts))
    if not options:
        sys.exit("No mvdXML rules found")

    print("Exchange requirements:")
    name, uuid, concepts = options[ask_number("Validate against", [o[0] for o in options])]
    return RuleSet([c for c in concepts if uuid in c.required_by], name, name.split()[0])


def choose_ids(rule_files):
    """Pick a phase, then a discipline, among the IDS files"""
    # phase -> {discipline: (path, info)}, in file name order (b3-1 before b4-1, 1-konseptvalg before 2-forprosjekt)
    files = {}
    for path in sorted(rule_files, key=lambda p: p.name.lower()):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            sys.exit(f"Could not read rules file {path}")
        info = {"milestone": (root.findtext(f"{IDS}info/{IDS}milestone") or "").strip()}
        phase, discipline = ids_phase(path, info)
        files.setdefault(phase, {})[discipline] = (path, info)

    phases = list(files)
    print("Phases (IDS):")

    def phase_name(p):
        milestone = next(iter(files[p].values()))[1]["milestone"]
        return p if milestone in ("", p) else f"{p} {milestone}"

    phase = phases[ask_number("Validate against", [phase_name(p) for p in phases])]
    disciplines = sorted(files[phase])
    discipline = disciplines[ask_number("Discipline", disciplines)] if len(disciplines) > 1 else disciplines[0]

    path, info = files[phase][discipline]
    # Specifications exported without an applicability borrow it from the same name in the other IDS files
    neighbours = set(rule_files) | {p for f in rule_files for p in Path(f).parent.glob("*.ids")}
    _, concepts, allowed = load_ids(path, ids_fallback(sorted(neighbours)))
    milestone = info["milestone"] if info["milestone"] != phase else ""
    name = f"{phase} {discipline} (IDS) {milestone}".strip()
    return RuleSet(concepts, name, f"{phase} {discipline}", allowed)


def ask_number(prompt, options):
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}")
    while True:
        answer = input(f"{prompt} (1-{len(options)}): ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1


def ask_option(prompt, options):
    while True:
        answer = input(f"{prompt} ({'/'.join(options)}) ").strip().lower()
        for option in options:
            if answer and option.startswith(answer):
                return option


def ask_yes_no(prompt):
    return ask_option(prompt, ["yes", "no"]) == "yes"


def save_file(model, model_path):
    if ask_yes_no("Save as new file?"):
        default = f"{Path(model_path).stem}_updated"
        new_name = input(f"Input new name [{default}]: ").strip().removesuffix(".ifc") or default
        model.write(f"{new_name}.ifc")
        print(f"File saved as {new_name}.ifc")
    print("Thank you for using the program")


if __name__ == "__main__":
    main()
