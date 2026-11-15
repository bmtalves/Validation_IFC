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
# Rules files are read from "Simba Rules" and from the tool's own folder, and from folders inside them
RULES_FOLDERS = [TOOL_FOLDER / "Simba Rules", TOOL_FOLDER]
DASHBOARD_TEMPLATE = TOOL_FOLDER / "dashboard_template.html"
ALLOWED_VALUES_FILE = TOOL_FOLDER / "allowed_values.csv"
CROSS_CHECKS_FILE = TOOL_FOLDER / "cross_checks.csv"
# Statsbygg's IDS files are named ..._b3-1-architectural.ids, ..._b3-1_environmental_material.ids
IDS_FILE_NAME = re.compile(r"_b(\d)-(\d)[-_]([a-z_]+)\.ids$", re.IGNORECASE)

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

    def scope(self):
        """Which objects the concept applies to; concepts with the same scope share the lookup"""
        return (self.group, self.entity, self.entities, self.predefined, id(self.applicability) if self.applicability else None)


@dataclass
class RuleSet:
    concepts: list
    phase: str               # shown in the report, e.g. "B3.1.1 Outline conceptual design ..."
    short: str               # used in file names, e.g. "B3.1.1"
    allowed: dict = field(default_factory=dict)  # allowed values that come with the rules (IDS)


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
    if model.schema != "IFC4":
        print(f"Warning: Statsbygg requires IFC4, this model is {model.schema}")

    rules = choose_rules([rules_path] if rules_path else find_rule_files(RULES_FOLDERS))
    own_allowed = load_allowed_values(ALLOWED_VALUES_FILE)
    if own_allowed:
        print(f"Checking allowed values for {len(own_allowed)} properties from {ALLOWED_VALUES_FILE.name}")
    # A list in allowed_values.csv replaces the one in the rules
    allowed = {**rules.allowed, **own_allowed}
    cross_checks = load_cross_checks(CROSS_CHECKS_FILE)
    concepts = rules.concepts + cross_check_concepts(rules.concepts, cross_checks)
    changed = migrate_designed_status(model)

    results = validate(model, concepts, allowed)
    print_summary(results)
    phase = rules.phase

    report_name = f"validation {Path(model_path).stem} {rules.short}"
    report = ask_option("Export a report?", ["csv", "html dashboard", "both", "no"])
    if report in ("csv", "both"):
        export_csv(results, f"{report_name}.csv")
        print(f"File saved as {report_name}.csv")
    if report in ("html dashboard", "both"):
        export_dashboard(results, model, model_path, phase, f"{report_name}.html", allowed)

    if changed:
        save_file(model, model_path)
    else:
        print("Program finished.")


def get_arguments(argv):
    if len(argv) < 2:
        sys.exit("Usage: python Validation_Simba.py model.ifc [rules.mvdxml or rules.ids]")
    if len(argv) > 3:
        sys.exit("Too many arguments")
    if not argv[1].lower().endswith(".ifc"):
        sys.exit("Not an IFC file")
    if len(argv) == 3:
        if not argv[2].lower().endswith((".mvdxml", ".ids")):
            sys.exit("Not an mvdXML or IDS file")
        return argv[1], Path(argv[2])
    return argv[1], None


def find_rule_files(folders):
    """mvdXML and IDS files in the folders and one level of folders inside them"""
    files = []
    for folder in folders:
        for pattern in ("*.mvdxml", "*.ids", "*/*.mvdxml", "*/*.ids"):
            files += sorted(folder.glob(pattern))
    # The same file in two places should only be listed once
    unique = {}
    for path in files:
        unique.setdefault(path.name, path)
    return list(unique.values())


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
        entities, predefined = applies

        requirements = spec.find(IDS + "requirements")
        for facet in requirements if requirements is not None else ():
            name = facet.findtext(f"{IDS}name/{IDS}simpleValue")
            if facet.tag == IDS + "property":
                pset = facet.findtext(f"{IDS}propertySet/{IDS}simpleValue")
                ifc_type = (facet.get("datatype") or "IFCLABEL").upper()
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
            else:
                continue  # classification, material and partOf requirements aren't used by Statsbygg
            # "0001- : ProjectNumber || Statsbygg official Project Number (seven digits)"
            description = clean_text(facet.get("instructions")).split("||", 1)[-1].strip()
            concepts.append(Concept(group, None, label, rules, None, required,
                                    entities=entities, predefined=predefined, description=description))
    if skipped:
        print(f"Note: {Path(path).name}: {len(skipped)} specification(s) apply to nothing and were skipped: {', '.join(skipped)}")
    return info, concepts, allowed


def property_rules(pset, name, ifc_type):
    """The element's own property, or else its type's, must have a value of the right type
    (the same rule shape Statsbygg's mvdXML uses)"""
    own = [Atom(False, "Set", "Value", pset), Atom(False, "PropertyName", "Value", name),
           Atom(False, "Value", "Exists", "TRUE"), Atom(False, "Value", "Type", ifc_type)]
    on_type = [Atom(True, "Set", "Value", pset), Atom(True, "PropertyName", "Value", name),
               Atom(True, "Value", "Exists", "TRUE"), Atom(True, "Value", "Type", ifc_type)]
    has_own = [Atom(False, "Set", "Value", pset), Atom(False, "PropertyName", "Value", name)]
    return ("or", [("rule", [own]), ("and", [("nor", [("rule", [has_own])]), ("rule", [on_type])])])


def read_ids_applicability(node):
    """(entity names, predefined type or None), or None when the specification applies to nothing"""
    entity = node.find(IDS + "entity") if node is not None else None
    if entity is None:
        return None
    names = ids_values(entity.find(IDS + "name"))
    if not names:
        return None
    predefined = ids_values(entity.find(IDS + "predefinedType"))
    return tuple(names), (predefined[0] if predefined else None)


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
            entities=concept.entities, predefined=concept.predefined, contains=parts,
            description=f"The value must contain the values of {', '.join(part[1] for part in parts)} (from {CROSS_CHECKS_FILE.name})."))
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
                    if nominal.is_a().upper() != ifc_type.upper():
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
            if concept.contains:
                outcome = checker.check_contains(concept, element)
                if outcome is not None:
                    results.append(Result(concept, element, *outcome))
            else:
                results.append(Result(concept, element, checker.evaluate(concept.rules, element), checker.value_of(concept, element)))
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


def dashboard_data(results, model, model_path, phase, allowed=None):
    requirements = {}
    objects = {}
    values = {}
    # One row per check, as indexes into the lists above to keep the file small:
    # [object, requirement, status (1 passed, 0 failed, 2 missing), value (-1 if missing)]
    checks = []
    for result in results:
        concept, element = result.concept, result.element
        required = concept.required
        is_property = required is not None and required[0] in ("property", "cross")
        requirement = requirements.setdefault(concept, {
            "group": concept.group,
            "label": concept.label,
            "index": len(requirements),
            "pset": required[1] if is_property else None,
            "type": required[3] if is_property and required[0] == "property" else None,
            "info": concept.description,
            "allowed": allowed_text((allowed or {}).get((required[1], required[2]), []))
                       if is_property and required[0] == "property" else [],
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
        "schema": model.schema,
        "phase": phase,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "requirements": list(requirements.values()),
        "objects": list(objects.values()),
        "values": list(values),
        "checks": checks,
    }


def export_dashboard(results, model, model_path, phase, filename, allowed=None):
    if not DASHBOARD_TEMPLATE.exists():
        print(f"Dashboard not saved: {DASHBOARD_TEMPLATE.name} must be in the same folder as this script")
        return
    data = json.dumps(dashboard_data(results, model, model_path, phase, allowed))
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
    files = {}  # phase -> {discipline: (path, info)}
    for path in rule_files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            sys.exit(f"Could not read rules file {path}")
        info = {"milestone": (root.findtext(f"{IDS}info/{IDS}milestone") or "").strip()}
        phase, discipline = ids_phase(path, info)
        files.setdefault(phase, {})[discipline] = (path, info)

    phases = sorted(files)
    print("Phases (IDS):")
    phase = phases[ask_number("Validate against", [f"{p} {next(iter(files[p].values()))[1]['milestone']}".strip() for p in phases])]
    disciplines = sorted(files[phase])
    discipline = disciplines[ask_number("Discipline", disciplines)] if len(disciplines) > 1 else disciplines[0]

    path, info = files[phase][discipline]
    # Specifications exported without an applicability borrow it from the same name in the other IDS files
    neighbours = set(rule_files) | {p for f in rule_files for p in Path(f).parent.glob("*.ids")}
    _, concepts, allowed = load_ids(path, ids_fallback(sorted(neighbours)))
    name = f"{phase} {discipline} (IDS) {info['milestone']}".strip()
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
