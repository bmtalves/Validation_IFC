import csv
from pathlib import Path

import pytest
import ifcopenshell
import ifcopenshell.api

import Validation_IFC
from Validation_IFC import Atom, Checker, find_required, get_arguments, make_label, parse_parameters, write_property
from Validation_IFC import Concept, validate, dashboard_data, find_rule_files, load_allowed_values

PROCESS_STATUS = ("rule", parse_parameters(
    "Set[Value]='NONS_Process' AND PropertyName[Value]='ProcessStatus' AND Value[Exists]=TRUE AND Value[Type]='IFCLABEL'"))
TILE_WIDTH = ("rule", parse_parameters(
    "Set[Value]='Pset_CoveringCeiling' AND PropertyName[Value]='TileWidth' AND Value[Exists]=TRUE AND Value[Type]='IFCPOSITIVELENGTHMEASURE'"))


def make_model():
    model = ifcopenshell.file(schema="IFC4")
    run = ifcopenshell.api.run
    wall_type = run("root.create_entity", model, ifc_class="IfcWallType", name="AVA.001", predefined_type="SOLIDWALL")
    pset = run("pset.add_pset", model, product=wall_type, name="Pset_WallCommon")
    run("pset.edit_pset", model, pset=pset, properties={"IsExternal": True})
    typed_wall = run("root.create_entity", model, ifc_class="IfcWall", name="AVA.001")
    run("type.assign_type", model, related_objects=[typed_wall], relating_type=wall_type)
    lone_wall = run("root.create_entity", model, ifc_class="IfcWall", name="AVA.002")
    return model, typed_wall, lone_wall


# Same shape Statsbygg uses for every property requirement
IS_EXTERNAL = ("or", [
    ("rule", parse_parameters("Set[Value]='Pset_WallCommon' AND PropertyName[Value]='IsExternal' AND Value[Exists]=TRUE AND Value[Type]='IFCBOOLEAN'")),
    ("and", [
        ("nor", [("rule", parse_parameters("Set[Value]='Pset_WallCommon' AND PropertyName[Value]='IsExternal'"))]),
        ("rule", parse_parameters("T_Set[Value]='Pset_WallCommon' AND T_PropertyName[Value]='IsExternal' AND T_Value[Exists]=TRUE AND T_Value[Type]='IFCBOOLEAN'")),
    ]),
])
PREDEFINED_TYPE = ("rule", parse_parameters("PredefinedType[Exists]=TRUE OR T_PredefinedType[Exists]=TRUE"))


def test_parse_parameters():
    assert parse_parameters("Name[Exists]=TRUE") == [[Atom(False, "Name", "Exists", "TRUE")]]
    assert parse_parameters("PredefinedType[Exists]=TRUE OR T_PredefinedType[Exists]=TRUE") == [
        [Atom(False, "PredefinedType", "Exists", "TRUE")],
        [Atom(True, "PredefinedType", "Exists", "TRUE")],
    ]
    assert parse_parameters("LongName[Value]='Statsbyggs Byggnummer'") == [[Atom(False, "LongName", "Value", "Statsbyggs Byggnummer")]]
    assert parse_parameters("CRSName[Value]=reg'^EPSG:(.*)'") == [[Atom(False, "CRSName", "Value", "^EPSG:(.*)", True)]]


def test_find_required_and_label():
    required = find_required(IS_EXTERNAL)
    assert required == ("property", "Pset_WallCommon", "IsExternal", "IFCBOOLEAN")
    assert make_label("e2db IsExternal : 9f80 Pset_WallCommon : 200 Wall", required) == "Pset_WallCommon.IsExternal"
    assert find_required(("rule", parse_parameters("LongName[Exists]=TRUE"))) == ("attribute", "LongName")
    assert find_required(PREDEFINED_TYPE) is None
    assert make_label("0004 Predefined type : 200 Wall : 01 Architectural (ARK)", None) == "Predefined type"


def test_property_inherited_from_type():
    model, typed_wall, lone_wall = make_model()
    checker = Checker(model)
    assert checker.evaluate(IS_EXTERNAL, typed_wall) == True
    assert checker.evaluate(IS_EXTERNAL, lone_wall) == False


def test_wrong_value_type_on_element_fails():
    model, typed_wall, _ = make_model()
    pset = ifcopenshell.api.run("pset.add_pset", model, product=typed_wall, name="Pset_WallCommon")
    ifcopenshell.api.run("pset.edit_pset", model, pset=pset, properties={"IsExternal": model.create_entity("IfcLabel", "yes")})
    # The element's own (wrong) value hides the type's value
    assert Checker(model).evaluate(IS_EXTERNAL, typed_wall) == False


def test_text_types_count_as_the_same():
    model, typed_wall, lone_wall = make_model()
    write_property(model, typed_wall, "NONS_Process", "ProcessStatus", "IFCTEXT", "300")
    write_property(model, lone_wall, "NONS_Process", "ProcessStatus", "IFCREAL", 300.0)
    checker = Checker(model)
    assert checker.evaluate(PROCESS_STATUS, typed_wall) == True   # IfcText where IfcLabel is asked
    assert checker.evaluate(PROCESS_STATUS, lone_wall) == False   # a number is still the wrong type
    concept = Concept("200 Wall", "IfcWall", "NONS_Process.ProcessStatus", PROCESS_STATUS, None,
                      ("property", "NONS_Process", "ProcessStatus", "IFCLABEL"))
    results = {r.element.Name: (r.status, r.value) for r in validate(model, [concept])}
    assert results["AVA.001"] == ("passed", "300 (IfcText)")
    assert results["AVA.002"][0] == "failed"


def test_designed_status_date_as_text():
    model, typed_wall, lone_wall = make_model()
    rule = ("rule", parse_parameters("Set[Value]='NOSSB_Process' AND PropertyName[Value]='DesignedStatusDate' "
                                     "AND Value[Exists]=TRUE AND Value[Type]='IFCTIMEMEASURE'"))
    concept = Concept("200 Wall", "IfcWall", "NOSSB_Process.DesignedStatusDate", rule, None,
                      ("property", "NOSSB_Process", "DesignedStatusDate", "IFCTIMEMEASURE"))

    def result(value):
        write_property(model, typed_wall, "NOSSB_Process", "DesignedStatusDate", "IFCTEXT", value)
        return next((r.status, r.value) for r in validate(model, [concept]) if r.element == typed_wall)

    assert result("2023.11.17") == ("passed", "2023.11.17 (IfcText)")
    assert result("2023-05-12") == ("failed", "2023-05-12 (IfcText) (should be a date as YYYY.MM.DD)")
    assert result("10.11.2023") == ("failed", "10.11.2023 (IfcText) (should be a date as YYYY.MM.DD)")
    assert result("2023.13.01") == ("failed", "2023.13.01 (IfcText) (not a real date)")
    # A number is still checked as the rules say
    write_property(model, lone_wall, "NOSSB_Process", "DesignedStatusDate", "IFCREAL", 5.0)
    assert Checker(model).evaluate(rule, lone_wall) == False


def test_predefined_type():
    model, typed_wall, lone_wall = make_model()
    checker = Checker(model)
    assert checker.evaluate(PREDEFINED_TYPE, typed_wall) == True
    assert checker.evaluate(PREDEFINED_TYPE, lone_wall) == False


# ---------- SIMBA versions ----------

def test_simba_version_of():
    version = Validation_IFC.simba_version_of
    assert version(Path("statsbygg-project-1011301-vtm-simba-1-3_ifc-2x3-tc1_b4-1_architect-ark.mvdxml")) == "1.3"
    assert version(Path("SIMBA-2.1-kravsett-ALLE-FAG-IDS-096/statsbygg-template_b3-1-architectural.ids")) == "2.1"
    assert version(Path("my own rules.ids")) == "2.1"


def test_choose_simba_version(monkeypatch):
    model, typed_wall, _ = make_model()
    files = [Path("x_simba-1-3_b4-1.mvdxml"), Path("x_simba-2-1_b4-1.mvdxml"), Path("y_simba-2-1_b5-1.ids")]
    labels = []
    monkeypatch.setattr(Validation_IFC, "ask_number", lambda prompt, options: labels.extend(options) or 0)
    assert Validation_IFC.choose_simba_version(model, files) == ("1.3", [files[0]])
    assert labels == ["SIMBA 1.3 (IFC2X3)", "SIMBA 2.1 (IFC4)"]
    # Only one version: no question
    assert Validation_IFC.choose_simba_version(model, files[1:]) == ("2.1", files[1:])
    assert len(labels) == 2


def test_simba_13_rules_shipped():
    path = Validation_IFC.TOOL_FOLDER / "statsbygg-project-1011301-vtm-simba-1-3_ifc-2x3-tc1_b4-1_architect-ark.mvdxml"
    if not path.exists():
        pytest.skip("SIMBA 1.3 rules not in the folder")
    exchange_requirements, concepts = Validation_IFC.load_rules(path)
    assert list(exchange_requirements.values()) == ["B4.1.9 Coordinated design (B4.1 Detaljprosjekt) .9"]
    labels = {c.label for c in concepts}
    assert {"NOSSB_Process.DesignedStatus", "NOSSB_Reference.RefString"} <= labels
    assert Validation_IFC.tfm_concepts(concepts, prop=("NOSSB_Reference", "RefString"))


def test_dashboard_data():
    model, typed_wall, lone_wall = make_model()
    concept = Concept("200 Wall", "IfcWall", "Pset_WallCommon.IsExternal", IS_EXTERNAL, None, find_required(IS_EXTERNAL))
    results = validate(model, [concept])
    data = dashboard_data(results, model, "folder/model.ifc", "B5.1.1 Handover")
    assert data["model"] == "model.ifc"
    assert data["requirements"] == [{"group": "200 Wall", "label": "Pset_WallCommon.IsExternal", "index": 0,
                                     "pset": "Pset_WallCommon", "type": "IFCBOOLEAN", "info": "", "allowed": [],
                                     "passed": 1, "failed": 0, "missing": 1}]
    # Allowed values and descriptions go to the dashboard too
    concept.description = "Is the wall external"
    import re
    data = dashboard_data(results, model, "model.ifc", "B5.1.1", {("Pset_WallCommon", "IsExternal"): ["TRUE", re.compile("T.*")]})
    assert data["requirements"][0]["info"] == "Is the wall external"
    assert data["requirements"][0]["allowed"] == ["TRUE", "re:T.*"]
    assert {o["step"] for o in data["objects"]} == {typed_wall.id(), lone_wall.id()}
    failed = {o["name"]: o["failed"] for o in data["objects"]}
    assert failed == {"AVA.001": [], "AVA.002": ["Pset_WallCommon.IsExternal"]}
    # [object, requirement, status (1 passed, 0 failed, 2 missing), value index (-1 = missing)]
    assert data["values"] == ["TRUE"]
    checks = {data["objects"][obj]["name"]: (passed, value) for obj, req, passed, value in data["checks"]}
    assert checks == {"AVA.001": (1, 0), "AVA.002": (2, -1)}


def test_values_recorded():
    model, typed_wall, lone_wall = make_model()
    # Element's own wrong-typed value is shown with its type, and wins over the type's TRUE
    write_property(model, typed_wall, "Pset_WallCommon", "IsExternal", "IFCLABEL", "yes")
    write_property(model, lone_wall, "Pset_CoveringCeiling", "TileWidth", "IFCPOSITIVELENGTHMEASURE", 600.0)
    checker = Checker(model)
    is_external = Concept("200 Wall", "IfcWall", "Pset_WallCommon.IsExternal", IS_EXTERNAL, None, find_required(IS_EXTERNAL))
    tile_width = Concept("200 Wall", "IfcWall", "Pset_CoveringCeiling.TileWidth", TILE_WIDTH, None, find_required(TILE_WIDTH))
    predefined = Concept("200 Wall", "IfcWall", "Predefined type", PREDEFINED_TYPE, None, find_required(PREDEFINED_TYPE))
    assert checker.value_of(is_external, typed_wall) == "yes (IfcLabel)"
    assert checker.value_of(is_external, lone_wall) is None
    assert checker.value_of(tile_width, lone_wall) == "600"
    assert checker.value_of(predefined, typed_wall) == "SOLIDWALL"
    assert checker.value_of(predefined, lone_wall) is None



def test_status_missing_or_failed(tmp_path):
    model, typed_wall, lone_wall = make_model()
    other_wall = ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcWall", name="AVA.003")
    write_property(model, other_wall, "Pset_WallCommon", "IsExternal", "IFCLABEL", "yes")
    concept = Concept("200 Wall", "IfcWall", "Pset_WallCommon.IsExternal", IS_EXTERNAL, None, find_required(IS_EXTERNAL))
    results = validate(model, [concept])
    statuses = {r.element.Name: r.status for r in results}
    # Inherited TRUE passes, no value is missing, a value of the wrong type fails
    assert statuses == {"AVA.001": "passed", "AVA.002": "missing", "AVA.003": "failed"}

    path = tmp_path / "report.csv"
    Validation_IFC.export_csv(results, path)
    rows = {row["Name"]: (row["Value"], row["Validation"]) for row in csv.DictReader(open(path, encoding="utf-8-sig"))}
    assert rows == {"AVA.001": ("TRUE", "PASSED"), "AVA.002": ("", "MISSING"), "AVA.003": ("yes (IfcLabel)", "FAILED")}


def test_load_allowed_values(tmp_path):
    path = tmp_path / "allowed_values.csv"
    path.write_text(
        "Property,Allowed values,Source\n"
        "# NONS_Process.DuplicateOwnedBy,ARK;RIB,commented out\n"
        "NONS_Process.ProcessStatus, A ; B ,\n"
        "NONS_Thermal.ThermalTransmittanceReq,\"re:\\d+([.,]\\d+)?\",\n"
        "NONS_Process.ConstructedStatus,,empty list is ignored\n",
        encoding="utf-8")
    allowed = load_allowed_values(path)
    assert allowed[("NONS_Process", "ProcessStatus")] == ["A", "B"]
    assert allowed[("NONS_Thermal", "ThermalTransmittanceReq")][0].pattern == r"\d+([.,]\d+)?"
    assert len(allowed) == 2
    assert load_allowed_values(tmp_path / "missing.csv") == {}


def test_allowed_values_checked():
    model, typed_wall, lone_wall = make_model()
    write_property(model, typed_wall, "NONS_Process", "ProcessStatus", "IFCLABEL", "Godkjent")
    write_property(model, lone_wall, "NONS_Process", "ProcessStatus", "IFCLABEL", "350")
    allowed = {("NONS_Process", "ProcessStatus"): ["Godkjent", "Til godkjenning"]}
    assert Checker(model, allowed).evaluate(PROCESS_STATUS, typed_wall) == True
    assert Checker(model, allowed).evaluate(PROCESS_STATUS, lone_wall) == False
    # Without a list any filled-in text passes
    assert Checker(model).evaluate(PROCESS_STATUS, lone_wall) == True


def test_measure_range_checked():
    model, typed_wall, lone_wall = make_model()
    write_property(model, typed_wall, "Pset_CoveringCeiling", "TileWidth", "IFCPOSITIVELENGTHMEASURE", 600.0)
    write_property(model, lone_wall, "Pset_CoveringCeiling", "TileWidth", "IFCPOSITIVELENGTHMEASURE", -5.0)
    checker = Checker(model)
    assert checker.evaluate(TILE_WIDTH, typed_wall) == True
    assert checker.evaluate(TILE_WIDTH, lone_wall) == False


def test_find_rule_files(tmp_path):
    rules = tmp_path / "Simba Rules"
    rules.mkdir()
    (rules / "b3-1.mvdxml").write_text("")
    (tmp_path / "b3-1.mvdxml").write_text("")
    (tmp_path / "b5-1.mvdxml").write_text("")
    (tmp_path / "notes.txt").write_text("")
    found = find_rule_files([rules, tmp_path, tmp_path / "missing"])
    assert [p.name for p in found] == ["b3-1.mvdxml", "b5-1.mvdxml"]
    assert found[0].parent == rules


def test_get_arguments():
    assert get_arguments(["project.py", "model.ifc"]) == ("model.ifc", None)
    assert get_arguments(["project.py", "MODEL.IFC", "rules.mvdxml"])[1].name == "rules.mvdxml"
    assert get_arguments(["project.py", "model.ifc", "b5-1.ids"])[1].name == "b5-1.ids"
    for argv in (["project.py"], ["project.py", "model.txt"], ["project.py", "model.ifc", "rules.xml"], ["project.py", "a.ifc", "b", "c"]):
        with pytest.raises(SystemExit):
            get_arguments(argv)



IDS_FILE = """<?xml version="1.0" encoding="UTF-8"?>
<ids:ids xmlns:ids="http://standards.buildingsmart.org/IDS" xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <ids:info><ids:title>Test</ids:title><ids:milestone>Handover (B5.1 Ferdigstillelse)</ids:milestone></ids:info>
  <ids:specifications>
    <ids:specification name="200 Wall" ifcVersion="IFC4">
      <ids:applicability><ids:entity><ids:name><ids:simpleValue>IFCWALL</ids:simpleValue></ids:name></ids:entity></ids:applicability>
      <ids:requirements>
        <ids:attribute instructions="0001-003 : Name || Component code"><ids:name><ids:simpleValue>Name</ids:simpleValue></ids:name></ids:attribute>
        <ids:property datatype="IFCBOOLEAN" minOccurs="1" maxOccurs="unbounded">
          <ids:propertySet><ids:simpleValue>Pset_WallCommon</ids:simpleValue></ids:propertySet>
          <ids:name><ids:simpleValue>IsExternal</ids:simpleValue></ids:name>
        </ids:property>
        <ids:property datatype="IFCLABEL" minOccurs="1" maxOccurs="unbounded">
          <ids:propertySet><ids:simpleValue>NOSSB_Security</ids:simpleValue></ids:propertySet>
          <ids:name><ids:simpleValue>Class</ids:simpleValue></ids:name>
          <ids:value><xs:restriction base="xs:string"><xs:enumeration value="LOW"/><xs:enumeration value="UNSET."/></xs:restriction></ids:value>
        </ids:property>
        <ids:property datatype="IFCIDENTIFIER" minOccurs="1" maxOccurs="unbounded">
          <ids:propertySet><ids:simpleValue>Pset_BuildingCommon</ids:simpleValue></ids:propertySet>
          <ids:name><ids:simpleValue>BuildingID</ids:simpleValue></ids:name>
          <ids:value><xs:restriction base="xs:string"><xs:enumeration value="Matrikkelens bygningsnummer"/></xs:restriction></ids:value>
        </ids:property>
      </ids:requirements>
    </ids:specification>
    <ids:specification name="200 Ramp" ifcVersion="IFC4">
      <ids:applicability />
      <ids:requirements><ids:attribute><ids:name><ids:simpleValue>Name</ids:simpleValue></ids:name></ids:attribute></ids:requirements>
    </ids:specification>
    <ids:specification name="430 Thermostat" ifcVersion="IFC4">
      <ids:applicability />
      <ids:requirements><ids:attribute><ids:name><ids:simpleValue>Name</ids:simpleValue></ids:name></ids:attribute></ids:requirements>
    </ids:specification>
    <ids:specification name="200 Covering Ceiling" ifcVersion="IFC4">
      <ids:applicability><ids:entity>
        <ids:name><ids:value><xs:restriction base="xs:string"><xs:pattern value="IFCCOVERING|IFCSLAB"/></xs:restriction></ids:value></ids:name>
        <ids:predefinedType><ids:simpleValue>CEILING</ids:simpleValue></ids:predefinedType>
      </ids:entity></ids:applicability>
      <ids:requirements><ids:attribute><ids:name><ids:simpleValue>Description</ids:simpleValue></ids:name></ids:attribute></ids:requirements>
    </ids:specification>
  </ids:specifications>
</ids:ids>"""


def test_load_ids(tmp_path, capsys):
    path = tmp_path / "x_ifc-4-add2_b5-1-architectural.ids"
    path.write_text(IDS_FILE, encoding="utf-8")
    info, concepts, allowed = Validation_IFC.load_ids(path, {"200 Ramp": (("IFCRAMP",), None, ())})
    assert "430 Thermostat" in capsys.readouterr().out  # no applicability anywhere: skipped, with a note
    assert [(c.group, c.label) for c in concepts] == [
        ("200 Wall", "Name"), ("200 Wall", "Pset_WallCommon.IsExternal"), ("200 Wall", "NOSSB_Security.Class"),
        ("200 Wall", "Pset_BuildingCommon.BuildingID"), ("200 Ramp", "Name"), ("200 Covering Ceiling", "Description")]
    assert concepts[1].required == ("property", "Pset_WallCommon", "IsExternal", "IFCBOOLEAN")
    assert concepts[0].description == "Component code"
    assert concepts[4].entities == ("IFCRAMP",)
    assert concepts[5].entities == ("IFCCOVERING", "IFCSLAB") and concepts[5].predefined == "CEILING"
    # "UNSET." is cleaned up; a one-value list is an example, not a rule
    assert allowed == {("NOSSB_Security", "Class"): ["LOW", "UNSET"]}
    assert Validation_IFC.ids_phase(path, info) == ("B5.1", "architectural")


def test_ids_rules_checked(tmp_path):
    path = tmp_path / "rules.ids"
    path.write_text(IDS_FILE, encoding="utf-8")
    _, concepts, allowed = Validation_IFC.load_ids(path)
    model, typed_wall, lone_wall = make_model()
    standard_case = ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcWallStandardCase", name="AVA.003")
    write_property(model, typed_wall, "NOSSB_Security", "Class", "IFCLABEL", "LOW")
    write_property(model, lone_wall, "NOSSB_Security", "Class", "IFCLABEL", "HIGH")
    results = validate(model, concepts, allowed)
    by_check = {(r.element.Name, r.concept.label): r.status for r in results}
    # IDS entities don't include subclasses
    assert not any(name == "AVA.003" for name, _ in by_check)
    # IsExternal comes from AVA.001's type
    assert by_check[("AVA.001", "Pset_WallCommon.IsExternal")] == "passed"
    assert by_check[("AVA.002", "Pset_WallCommon.IsExternal")] == "missing"
    assert by_check[("AVA.001", "NOSSB_Security.Class")] == "passed"
    assert by_check[("AVA.002", "NOSSB_Security.Class")] == "failed"


def test_cross_check(tmp_path):
    csv_file = tmp_path / "cross_checks.csv"
    csv_file.write_text("Property,Must contain\n# comment,\n"
                        "NONS_Reference.RefString,NONS_Reference.RefLocStorey;NONS_Reference.RefCompOccNo\n", encoding="utf-8")
    checks = Validation_IFC.load_cross_checks(csv_file)
    assert checks == {("NONS_Reference", "RefString"): (("NONS_Reference", "RefLocStorey"), ("NONS_Reference", "RefCompOccNo"))}

    rule = ("rule", parse_parameters("Set[Value]='NONS_Reference' AND PropertyName[Value]='RefString' AND Value[Exists]=TRUE"))
    base = Concept("200 Wall", "IfcWall", "NONS_Reference.RefString", rule, None, ("property", "NONS_Reference", "RefString", "IFCLABEL"))
    extra = Validation_IFC.cross_check_concepts([base, base], checks)
    assert len(extra) == 1 and extra[0].label == "NONS_Reference.RefString contents"

    model, typed_wall, lone_wall = make_model()
    third = ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcWall", name="AVA.003")
    for wall, ref, storey, number in ((typed_wall, "+B01=360.001-01", "B01", "01"), (lone_wall, "+B01=360.001-02", "B01", "07")):
        write_property(model, wall, "NONS_Reference", "RefString", "IFCLABEL", ref)
        write_property(model, wall, "NONS_Reference", "RefLocStorey", "IFCLABEL", storey)
        write_property(model, wall, "NONS_Reference", "RefCompOccNo", "IFCLABEL", number)
    results = {r.element.Name: r for r in validate(model, extra)}
    assert results["AVA.001"].status == "passed"
    assert results["AVA.002"].status == "failed" and "RefCompOccNo 07 not in it" in results["AVA.002"].value
    assert "AVA.003" not in results  # no RefString: its own requirement reports that


# ---------- Oslobygg ----------

OSLOBYGG_IDS = r"""<?xml version="1.0" encoding="UTF-8"?>
<ids:ids xmlns:ids="http://standards.buildingsmart.org/IDS" xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <ids:info><ids:title>Oslobygg test</ids:title><ids:milestone>Forprosjekt</ids:milestone></ids:info>
  <ids:specifications>
    <ids:specification name="23 Yttervegger">
      <ids:applicability>
        <ids:entity><ids:name><ids:simpleValue>IFCWALL</ids:simpleValue></ids:name></ids:entity>
        <ids:property datatype="IFCBOOLEAN">
          <ids:propertySet><ids:simpleValue>Pset_WallCommon</ids:simpleValue></ids:propertySet>
          <ids:name><ids:simpleValue>IsExternal</ids:simpleValue></ids:name>
          <ids:value><ids:simpleValue>true</ids:simpleValue></ids:value>
        </ids:property>
      </ids:applicability>
      <ids:requirements>
        <ids:property>
          <ids:propertySet><ids:simpleValue>NOOBF_Wall</ids:simpleValue></ids:propertySet>
          <ids:name><ids:simpleValue>Load</ids:simpleValue></ids:name>
        </ids:property>
      </ids:requirements>
    </ids:specification>
    <ids:specification name="Rom">
      <ids:applicability>
        <ids:entity><ids:name><ids:simpleValue>IFCSPACE</ids:simpleValue></ids:name></ids:entity>
      </ids:applicability>
      <ids:requirements>
        <ids:attribute instructions=": Romnummer || Romnummer med ni tegn">
          <ids:name><ids:simpleValue>Name</ids:simpleValue></ids:name>
          <ids:value><xs:restriction base="xs:string"><xs:pattern value="\d{2}-[KUEMLT]\d{5}"/></xs:restriction></ids:value>
        </ids:attribute>
      </ids:requirements>
    </ids:specification>
  </ids:specifications>
</ids:ids>
"""


def test_oslobygg_filters_patterns_and_any_type(tmp_path):
    path = tmp_path / "oslobygg_2-forprosjekt_ark.ids"
    path.write_text(OSLOBYGG_IDS, encoding="utf-8")
    _, concepts, allowed = Validation_IFC.load_ids(path)
    model, external_wall, no_is_external = make_model()  # AVA.001 is external through its type
    run = ifcopenshell.api.run
    internal = run("root.create_entity", model, ifc_class="IfcWall", name="AVA.003")
    write_property(model, internal, "Pset_WallCommon", "IsExternal", "IFCBOOLEAN", False)
    # No datatype in the IDS: any value type passes
    write_property(model, external_wall, "NOOBF_Wall", "Load", "IFCINTEGER", 15)
    for name in ("01-E01001", "101", None):
        run("root.create_entity", model, ifc_class="IfcSpace", name=name)

    results = validate(model, concepts, allowed)
    walls = {r.element.Name: r.status for r in results if r.concept.group == "23 Yttervegger"}
    # Only the external wall is checked: the internal one and the one without IsExternal are not
    assert walls == {"AVA.001": "passed"}
    rooms = {r.element.Name: (r.status, r.value) for r in results if r.concept.group == "Rom"}
    assert rooms == {"01-E01001": ("passed", "01-E01001"), "101": ("failed", "101"), None: ("missing", None)}
    data = dashboard_data(results, model, "m.ifc", "Forprosjekt ARK", allowed, "oslobygg")
    room_requirement = next(r for r in data["requirements"] if r["label"] == "Romnummer")
    assert room_requirement["allowed"] == [r"re:\d{2}-[KUEMLT]\d{5}"]
    assert room_requirement["info"] == "Romnummer med ni tegn"
    assert data["title"] == "Oslobygg validation"


def test_oslobygg_phase_and_client(tmp_path):
    assert Validation_IFC.ids_phase("oslobygg_4-som_bygget_ark.ids", {}) == ("Som bygget", "ARK")
    assert Validation_IFC.client_of(Validation_IFC.OSLOBYGG_FOLDER / "x.ids") == "oslobygg"
    assert Validation_IFC.client_of(tmp_path / "statsbygg_b3-1-architectural.ids") == "statsbygg"
    # SIMBA rules leave out the Oslobygg folder
    (tmp_path / "Oslobygg Rules").mkdir()
    (tmp_path / "Oslobygg Rules" / "oslobygg_1-konseptvalg_ark.ids").write_text("x")
    (tmp_path / "simba.ids").write_text("x")
    found = find_rule_files([tmp_path], exclude=[tmp_path / "Oslobygg Rules"])
    assert [p.name for p in found] == ["simba.ids"]


def test_oslobygg_rules_shipped():
    """The converted Oslobygg files load, in phase order, with the common requirements in every phase"""
    files = sorted(Validation_IFC.OSLOBYGG_FOLDER.glob("oslobygg_*_ark.ids"))
    phases = [Validation_IFC.ids_phase(p, {})[0] for p in files]
    assert phases == ["Konseptvalg", "Forprosjekt", "Detaljering", "Som bygget"]
    for path in files:
        _, concepts, _ = Validation_IFC.load_ids(path)
        labels = {c.label for c in concepts}
        assert {"Forvaltnings-ID", "Bygg-ID", "Romnummer", "Pset_WallCommon.IsExternal"} <= labels


def test_storeys():
    """Objects are listed under their storey, lowest first, with the parts of aggregated objects"""
    model, typed_wall, lone_wall = make_model()
    run = ifcopenshell.api.run
    ground = run("root.create_entity", model, ifc_class="IfcBuildingStorey", name="E01")
    ground.Elevation = 0.0
    upper = run("root.create_entity", model, ifc_class="IfcBuildingStorey", name="E02")
    upper.Elevation = 3.5
    stair = run("root.create_entity", model, ifc_class="IfcStair", name="Trapp")
    flight = run("root.create_entity", model, ifc_class="IfcStairFlight", name="Løp")
    run("aggregate.assign_object", model, relating_object=stair, products=[flight])
    run("spatial.assign_container", model, relating_structure=upper, products=[lone_wall])
    run("spatial.assign_container", model, relating_structure=ground, products=[typed_wall, stair])
    floors = Validation_IFC.storeys(model)
    assert [f["name"] for f in floors] == ["E01", "E02"]
    assert floors[0]["steps"] == sorted([typed_wall.id(), stair.id(), flight.id()])
    assert floors[1]["steps"] == [lone_wall.id()]


# ---------- TFM codes (PA 0805) ----------

def test_tfm_problems():
    problems = Validation_IFC.tfm_problems
    assert problems("++123456=3600.001.04-RTA001%RTA.001.002") == []
    assert problems("++123456=2310.001-QQA001%QQA.001.001") == []
    assert problems("++123456=4330.003.155-UEA001%UEA.001.002") == []
    assert problems("++123456=2300.001-QQA001%QQA.001.001") == ["system code 2300 is not used by Statsbygg; use 2310, 2320, 2330 or 2340"]
    assert problems("++123456=3601.001-RTA001%RTA.001.002")[0].startswith("system code 3601 is not used")
    assert problems("++123456=2310.001.01-QQA001%QQA.001.001") == ["building systems (2xxx) have no sub-number (.01)"]
    assert problems("++123456=3200.001.06-SMA001%SMA.001.002") == ["sub-number .06 should be .04 (supply) or .05 (return)"]
    assert problems("++123456=4330.003.55-UEA001%UEA.001.002") == ["sub-number .55 should have 3 digits for 4xxx systems"]
    assert problems("++12345=2310.001-QA001%QQA.1") == [
        "location 12345 should be the 6-digit building number",
        "component -QA001 should be 3 letters and 3 digits, e.g. -RTA001",
        "type %QQA.1 should be BBB.nnn.nnn, e.g. %RTA.001.002"]
    assert problems("=3600.001-RTA001")[0].startswith("missing ++location, %type")
    assert problems("<A>++123456=3600.001-RTA001%RTA.001.002") == ["owner's overall ID <...> is not used by Statsbygg"]
    # With the code lists from the standards
    own = Validation_IFC.TfmCodes(ns3451=frozenset({"2310"}), ns3457=frozenset({"QQA"}))
    assert problems("++123456=2311.001-QQA001%QQB.001.001", own) == [
        "system code 2311 is not in NS 3451", "type code QQB is not in NS 3457-8"]
    assert problems("++123456=2310.001–QQA001%QQA.001.001") == ["use - (hyphen), not – (dash)"]
    assert problems("RTA001")[0].startswith("not a TFM code")


def test_tfm_pa0802():
    problems = Validation_IFC.tfm_problems
    codes = Validation_IFC.TfmCodes(pa0802_systems={"244": "yes", "310": "yes", "312": "no", "247": "no"},
                                    pa0802_components=frozenset({"DI", "JV"}))
    assert problems("+123456=244.001-DI001T", codes) == []
    assert problems("+123456=244.001-DI001", codes) == []
    assert problems("+123456=244.001–DI001T", codes) == ["use - (hyphen), not – (dash)"]
    assert problems("+123456=312.001-DI001", codes) == ["system code 312 is not used in TFM (PA 0802); use 310"]
    assert problems("+123456=247.001-DI001", codes) == ["system code 247 is not used in TFM (PA 0802)"]
    assert problems("+123456=999.001-XX001", codes) == [
        "system code 999 is not in the PA 0802 system list", "component code XX is not in the PA 0802 component list"]
    assert problems("+1234=244.1-DIX01") == [
        "location 1234 should be the 6-digit building number",
        "system =244.1 should be NNN.nnn (3-digit code, 3-digit number)",
        "component -DIX01 should be 2 letters and 3 digits, with T for a component type, e.g. -DI001 or -DI001T"]
    assert problems("=244.001-DI001")[0] == "missing +location; expected +AAAAAA=NNN.nnn-BBnnn(T)"


def test_tfm_simba_13():
    problems = lambda code, codes=None: Validation_IFC.ns3457_7_problems(code, codes)[0]
    assert problems("<SE09>++ AUE = 3600 . 014 . 04 - SQZ 0023 % SQZ . 008 . 03") == []
    assert problems("++SE09 = 360 . 014 . 04 - SQZ 0023 % SQZ . 008 . 03") == []
    assert problems("++B01=2340.004.03-DUZ0013%DUZ.001") == []
    assert problems("<SE09>++AUE=3600.014-SQZ0023%SQZ.008==SQZ0001(++A101)") == []
    assert Validation_IFC.ns3457_7_problems("<SE09>++ AUE = 3600 . 014 . 04 - SQZ 0023 % SQZ . 008 . 03")[1] == {
        "RefComplex": "SE09", "RefPriSysLoc": "AUE", "RefSysClass": "3600", "RefSysNo": "014", "RefSysSubNo": "04",
        "RefCompClass": "SQZ", "RefCompNo": "0023", "RefCompClassNo1": "008", "RefCompClassNo2": "03"}
    assert problems("++SE09=36.1-SQ23%SQY.8") == [
        "system =36.1 should be NNNN.nnn or NNNN.nnn.nn, e.g. =3600.014.04",
        "component -SQ23 should be 3 letters and a number, e.g. -SQZ0023",
        "type %SQY.8 should be BBB.nnn or BBB.nnn.nn, e.g. %SQZ.008.03"]
    assert problems("++SE09=3600.014-SQZ0023%SQY.008") == ["type code SQY differs from component code SQZ"]
    assert problems("++SE09=3600.014–SQZ0023%SQZ.008") == ["use - (hyphen), not – (dash)"]
    assert problems("++SE09=3600.014-SQZ0023")[0].startswith("missing %type")
    own = Validation_IFC.TfmCodes(ns3451=frozenset({"3600"}), ns3457=frozenset({"SQZ"}))
    assert problems("++SE09=3700.014-QQZ0023%QQZ.008", own) == [
        "system code 3700 is not in NS 3451", "component code QQZ is not in NS 3457-8"]

    # The NOSSB_Reference parts
    model, typed_wall, lone_wall = make_model()
    base = Concept("200 Wall", "IfcWall", "NOSSB_Reference.RefString", None, None,
                   ("property", "NOSSB_Reference", "RefString", "IFCLABEL"))
    extra = Validation_IFC.tfm_concepts([base], prop=("NOSSB_Reference", "RefString"), style="ns3457-7")
    assert [c.label for c in extra] == ["NOSSB_Reference.RefString TFM format", "NOSSB_Reference parts match RefString"]
    code = "<SE09>++AUE=3600.014.04-SQZ0023%SQZ.008"
    for wall in (typed_wall, lone_wall):
        write_property(model, wall, "NOSSB_Reference", "RefString", "IFCLABEL", code)
        for name, value in (("RefComplex", "SE09"), ("RefPriSysLoc", "AUE"), ("RefSysNo", "014"),
                            ("RefSysSubNo", "04"), ("RefCompNo", "0023")):
            write_property(model, wall, "NOSSB_Reference", name, "IFCLABEL", value)
    write_property(model, typed_wall, "NOSSB_Reference", "RefSysClass", "IFCLABEL", "3600")
    write_property(model, typed_wall, "NOSSB_Reference", "RefCompClass", "IFCLABEL", "SQZ")
    write_property(model, typed_wall, "NOSSB_Reference", "RefCompClassNo1", "IFCINTEGER", 8)
    same = Validation_IFC.same_part
    assert same("265", "2650", True) and same("2650", "265", True) and same("3600", "3600", True)
    assert not same("265", "2651", True) and not same("265", "2650") and same("14", "014")
    write_property(model, lone_wall, "NOSSB_Reference", "RefSysClass", "IFCLABEL", "255")
    write_property(model, lone_wall, "NOSSB_Reference", "RefCompClassNo2", "IFCLABEL", "03")
    results = {r.element.Name: (r.status, r.value) for r in validate(model, extra[1:])}
    assert results == {
        "AVA.001": ("passed", code),
        "AVA.002": ("failed", code + " (RefSysClass 255, the code has 3600; no RefCompClass; "
                                     "RefCompClassNo2 03, the code has none)"),
    }


def test_tfm_statsbygg_lists_shipped():
    codes = Validation_IFC.tfm_code_lists(Validation_IFC.TOOL_FOLDER / "no such folder")
    assert codes.ns3451 is None and codes.ns3457 is None
    assert codes.pa0805["2310"] == "yes" and codes.pa0805["4700"] == "no"
    assert codes.pa0802_systems["244"] == "yes" and codes.pa0802_systems["312"] == "no"
    assert {"DI", "JV"} <= codes.pa0802_components
    problems = Validation_IFC.tfm_problems
    assert problems("++123456=4700.001-UEA001%UEA.001.001", codes) == ["system code 4700 is not used by Statsbygg (PA 0702)"]
    assert problems("+123456=360.001-JV401", codes) == []


def test_tfm_check_and_code_files(tmp_path):
    (tmp_path / "NS 3451 systemkoder.csv").write_text("Kode;Navn\n2310;Bærende ytterveggssystemer\n3600;Luftbehandling\n", encoding="utf-8")
    (tmp_path / "NS3457-8.csv").write_text("RTA,Ventil\nQQA,Vegg\n", encoding="utf-8")
    codes = Validation_IFC.load_tfm_codes(tmp_path)
    assert codes == ({"2310", "3600"}, {"RTA", "QQA"})

    model, typed_wall, lone_wall = make_model()
    base = Concept("200 Wall", "IfcWall", "NONS_Reference.RefString", None, None,
                   ("property", "NONS_Reference", "RefString", "IFCLABEL"))
    extra = Validation_IFC.tfm_concepts([base, base], Validation_IFC.TfmCodes(ns3451=frozenset(codes[0])))
    assert len(extra) == 1
    write_property(model, typed_wall, "NONS_Reference", "RefString", "IFCLABEL", "++123456=2310.001-QQA001%QQA.001.001")
    write_property(model, lone_wall, "NONS_Reference", "RefString", "IFCLABEL", "++123456=2320.001-QQA001%QQA.001.001")
    results = {r.element.Name: (r.status, r.value) for r in validate(model, extra)}
    assert results == {
        "AVA.001": ("passed", "++123456=2310.001-QQA001%QQA.001.001"),
        "AVA.002": ("failed", "++123456=2320.001-QQA001%QQA.001.001 (system code 2320 is not in NS 3451)"),
    }
