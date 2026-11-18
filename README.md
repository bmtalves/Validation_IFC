# Validation_IFC

Checks an IFC model against a client's BIM requirements: Statsbygg's SIMBA 2.1 requirements, or Oslobygg's BIM requirements (architects, ARK, so far; see [Oslobygg](#oslobygg)), and reports what is missing or wrong.

The requirements are read from Statsbygg's own rule files in the `Simba Rules` folder, in either of two formats:

- **mvdXML** (architects, ARK): splits each phase into the steps .1, .5 and .9, and includes the PredefinedType and georeferencing checks.
- **IDS** (all 11 disciplines): the format Statsbygg now uses in its own checking platform. Each file covers a whole phase at the .9 level.

The tool has no property names written into it, so a new release of the rule files can be dropped in without changing the code.

## What it does

1. **Validates** the model against one delivery phase: B3.1 Skisseprosjekt, B3.2 Forprosjekt, B4.1 Detaljprosjekt or B5.1 Ferdigstillelse.
2. **Reports** the results as a CSV file, an HTML dashboard, or both.
3. **Copies old status values:** if the model still uses `NOSSB_Process.DesignedStatus`, the tool offers to copy it into the new `NONS_Process.ProcessStatus`, and then saves the result as a new IFC file. The original file is never changed.

The tool doesn't let you edit other values. Missing or wrong information has to be fixed in the authoring tool, using the report to find it.

## Installation

```bash
git clone https://github.com/bmtalves/Validation_IFC.git
cd Validation_IFC
pip install ifcopenshell pytest
```

## Requirements

- Python 3.10 or newer
- [IfcOpenShell](https://ifcopenshell.org/) (tested with 0.7.10)
- pytest (only for running the tests)

## Usage

Run from this folder:

```bash
python Validation_IFC.py model.ifc
```

If there are rules for both clients, the tool first asks whose to check against:

```
Rules for which client? (statsbygg/oslobygg)
```

For SIMBA, if both formats are there, it then asks which to use:

```
Rules format? mvdXML has the .1/.5/.9 steps, IDS covers every discipline (mvdxml/ids)
```

With mvdXML it lists every phase step found in the files:

```
Exchange requirements:
  1. B3.1.1 Outline conceptual design (B3.1 Skisseprosjekt) .1
  ...
  10. B5.1.1 Handover (B5.1 Ferdigstillelse) .1
Validate against (1-10):
```

With IDS it asks for the phase (B3.1, B3.2, B4.1, B5.1) and then the discipline (acoustical, architectural, electrical, environmental material, fire safety, interiors, landscaping, mechanical, security, structural, water supply and sewage).

For Oslobygg it asks for the phase: Konseptvalg, Forprosjekt, Detaljering or Som bygget.

To use one specific rules file instead (a file in `Oslobygg Rules` is checked as Oslobygg):

```bash
python Validation_IFC.py model.ifc rules.mvdxml
python Validation_IFC.py model.ifc rules.ids
```

### The steps

| Step | Prompt | Notes |
|---|---|---|
| Old status values | `Copy DesignedStatus into ProcessStatus?` | SIMBA only. Shown only if the model has `NOSSB_Process.DesignedStatus`. Objects that already have a ProcessStatus are skipped, and the old values are kept. |
| Summary | – | One `OK` / `FAIL` line per requirement with the number of failed and missing objects. |
| Report | `Export a report? (csv/html dashboard/both/no)` | Files are named `validation <model> <phase>.csv` / `.html`, e.g. `validation house B5.1.1.csv` (mvdXML) or `validation house B5.1 architectural.csv` (IDS), and `validation house Forprosjekt ARK.csv` for Oslobygg. The CSV has one row per object and requirement, with the value found and its status. |
| Save | `Save as new file?` | Only shown if status values were copied. Default name is `<model>_updated.ifc`. |

You can answer prompts with the first letter (`y`, `n`, `c`, `h`, `b`).

## Allowed values

The Statsbygg mvdXML files only require text properties such as `ProcessStatus` or `FireRating` to be filled in. They don't say which values are allowed. The IDS files have lists for about 23 properties, mostly security, MEP and EPD status, and these are used automatically. You can add your own lists in `allowed_values.csv`; a list there replaces the one from the IDS:

```csv
Property,Allowed values,Source
NONS_Process.ProcessStatus,000;100;125;150;175;200;225;250;275;300;325;350;375;400;425;450;475;500;600,MMI codes
NONS_Process.DuplicateOwnedBy,ARK;RIB;RIV;RIE;LARK,Discipline codes
NONS_Thermal.ThermalTransmittanceReq,"re:\d+([.,]\d+)?",U-value as a number
```

- **Format:** one property per line as `PsetName.PropertyName`, with allowed values separated by `;`.
- **Patterns:** `re:` starts a pattern (a Python regular expression) that the whole value must match. Put a cell in double quotes if it contains a comma.
- **Comments:** lines starting with `#` are ignored.
- **Shipped lists:** `NONS_Process.ProcessStatus` is limited to MMI codes, following SIMBA 2.1 Veiledning B.3.1. A primary code is a whole hundred (e.g. 200 Ferdig konsept, 300 Underlag for detaljering, 400 Produksjonsunderlag). A secondary code adds x25 etablert for kontroll, x50 kontrollert or x75 akseptert, e.g. `250`. The other lines are commented-out examples.
- **Validation:** a value that isn't on the list fails the requirement, just like a missing value. This applies whether the value is on the object or on its type.

## TFM codes (Statsbygg PA 0805 and PA 0802)

SIMBA 2.1 requires the TFM code in `NONS_Reference.RefString` (from B4.1) in "the building owner's preferred format". For Statsbygg that format is set by [PA 0805 Bruk av NS-TFM i Statsbygg](https://dok.statsbygg.no/wp-content/uploads/2024/02/PA-0805-Bruk-av-Standard-Norges-Tverrfaglig-Merkesystem-NS-TFM-i-Statsbygg-rev-04.pdf) (rev. 04, March 2025): the *normal TFM-ID with component type*. Statsbygg's older format, [PA 0802 Tverrfaglig merkesystem](https://dok.statsbygg.no/), is still maintained (code lists rev. 5, February 2026), so both are accepted. The tool tells them apart by the location sign: `++` is PA 0805, `+` is PA 0802.

```
++AAAAAA=NNNN.nnn.nn(n)-BBBnnn%BBB.nnn.nnn
++123456=3600.001.04-RTA001%RTA.001.002      PA 0805 (NS-TFM)
+123456=244.001-DI001T                       PA 0802, component type
+123456=244.001-DI001                        PA 0802, component (used in parallel with the type)
```

For Statsbygg rules, every object group that requires `RefString` gets an extra requirement, `NONS_Reference.RefString TFM format`. Objects without a RefString are left out, since the property's own requirement already reports them. A failing value says what is wrong, e.g. `++123456=2300.001-QQA001%QQA.001.001 (system code 2300 is not used by Statsbygg; use 2310, 2320, 2330 or 2340)`.

| Part | Checked |
|---|---|
| `++AAAAAA` location | the 6-digit building number |
| `=NNNN.nnn` system | 4-digit system code and 3-digit number. Not the codes PA 0805 rules out: 2300, 2400, 2500, 2600, 2700, 3100, 3300, 3400, 3500, 3601–3607 (air handling only uses 3600), 4600, 5210, 5230, 5240, 7600, 7700 |
| `.nn(n)` sub-number | none for 2xxx; `.04`/`.05` for 32, 35, 37; `.01`–`.08` for 36; 3 digits for 4xxx and 5xxx. Not used or not required elsewhere |
| `-BBBnnn` component | 3 letters and 3 digits, no separator |
| `%BBB.nnn.nnn` type | component code, 3-digit number and a 2- or 3-digit sub-number (PA 0805 shows `.nn` in its heading but `.nnn` in its examples, so both pass) |
| `<…>`, `==…`, `(…)` | not used by Statsbygg, so they fail |
| `–` (dash) | fails in both formats: use a plain hyphen `-` (the PA 0802 PDF prints an en dash) |

**PA 0802** (`+AAAAAA=NNN.nnn-BBnnn(T)`): the 6-digit building number, a 3-digit system code with a 3-digit number, and a 2-letter component code with a 3-digit number, plus `T` for a component type. The system code must be in the PA 0802 system list and marked for use in TFM. A code marked *Skal ikke benyttes i TFM* fails, with the group code when that one is used, e.g. `+123456=315.001-SQ001 (system code 315 is not used in TFM (PA 0802); use 310)`. The component code must be in the PA 0802 component list.

**Statsbygg's lists.** The `Statsbygg TFM` folder holds code lists taken from Statsbygg's public documents:

| File | From | Used for |
|---|---|---|
| `pa0802_system_codes.csv` | PA 0802 vedlegg 9.1 Systemkodeliste, rev. 5 | PA 0802 system codes, and whether each is used in TFM |
| `pa0802_component_codes.csv` | PA 0802 vedlegg 9.2 Komponentkodeliste | PA 0802 component codes |
| `pa0805_system_codes.csv` | PA 0702 vedlegg 1-2 Dokumentasjonskrav iht. PA 0805, rev. 5 | NS 3451 system codes marked *Skal ikke benyttes!* fail (adds 4700 and 4800 to PA 0805's list) |

When Statsbygg publishes a new revision, put the PDFs in `PDF_statsbygg` and run `python "Statsbygg TFM/make_tfm_lists.py"` (needs `pdftotext` from xpdf or poppler). Check the counts it prints.

**Do the NS-TFM codes exist?** PA 0702 only names the system codes Statsbygg has documentation requirements for, so a code missing from it isn't failed. The full lists of valid codes are in NS 3451:2022 (system codes) and NS 3457-8:2021 (component codes), which are sold by Standard Norge and can't be shipped with the tool. If you have them, put them in a `TFM codes` folder next to the tool, as Excel or CSV files with `3451` and `3457` in their names, e.g. `TFM codes/NS 3451 systemkoder.xlsx`. Every cell that starts with a code (`2310 Bærende ytterveggssystemer`, `RTA`) is read, so the layout doesn't matter. The tool then also fails codes that aren't in the standards. The folder is in `.gitignore`: keep it out of the GitHub repo.

## Cross-checks between properties

Some values must agree with each other. `cross_checks.csv` lists properties whose value must contain the values of other properties:

```csv
Property,Must contain,Source
# NONS_Reference.RefString,NONS_Reference.RefLocStorey;NONS_Reference.RefPriSysClass;NONS_Reference.RefCompClass;NONS_Reference.RefCompOccNo,TFM reference string
```

- The shipped row checks that `RefString` contains the storey, system, component class and occurrence number. It is switched off (`#`): SIMBA 2.1 doesn't require those four properties, and the RefString format is now checked by [PA 0805 / PA 0802](#tfm-codes-statsbygg-pa-0805-and-pa-0802). Remove the `#` if your project uses them.
- It runs for every object group that requires the first property, and shows up as its own requirement, e.g. `NONS_Reference.RefString contents`.
- Objects without the first property are left out: its own requirement already reports them as missing.
- The check fails if one of the listed properties is missing or its value isn't in the first one. The value shown says why, e.g. `+B01=360.001-02 (RefCompOccNo 07 not in it)`.

## Measure limits

Measures are also checked against IFC's own limits: `IFCPOSITIVELENGTHMEASURE` must be above 0 and `IFCNORMALISEDRATIOMEASURE` between 0 and 1.

## Statuses

Every check gets one of three statuses:

| Status | Meaning |
|---|---|
| **Passed** | The value is there and valid. |
| **Failed** | There is a value, but it is wrong: the wrong value type (e.g. text instead of a boolean), not on the allowed list, outside IFC's limits, or not matching a cross-check or the TFM format. |
| **Missing** | There is no value at all: the property, attribute, type or georeferencing is not in the model. |

The CSV's `Validation` column shows `PASSED`, `FAILED` or `MISSING`. The dashboard shows them in green, red and yellow, each with its own icon.

## The dashboard

`validation <model> <phase>.html` is a single file that can be shared as it is. Everything except the 3D model works offline. It shows:

- **Checks passed:** the overall pass rate, with a donut chart and legend showing how many checks passed, failed and are missing. Hover a slice for its count and share.
- **Summary tiles:** failing requirements, objects with issues, object groups with failures, and property sets with failures.
- **By object group** and **By property set:** the share of checks passed, worst first. Hover a bar for details.
- **3D model:** drop the IFC file onto the card, or click *Choose IFC file*, to see the model coloured by result: green if all its checks pass, red if any fail, yellow if any are missing, grey for objects with no requirement. Drag to rotate, right-drag to pan and scroll to zoom.
  - The file is read and triangulated **in your browser** by [IFClite](https://github.com/louistrue/ifc-lite) (Rust/WebAssembly, MPL-2.0) and drawn with [three.js](https://threejs.org). It is never uploaded. Only the two libraries are downloaded, from cdn.jsdelivr.net, so this part needs an internet connection.
  - Use the same IFC file the report was made from (the updated file from `Save as new file?` works too). Objects are matched by their number in the file (`#123=`), and the dashboard checks a few GlobalIds to make sure it is the right model.
  - Click an object, or a row in the Values or Objects table, to see all its checks and values. The selected object is drawn in blue on top of everything, so walls and slabs can't hide it.
  - *Colour by* switches from the worst result to a single requirement. Picking a requirement or a value in the Values section does the same, and greys out objects with other values.
  - *Only not passing* hides everything that passes.
  - *Objects in view*, to the right of the model, is a donut chart of how many of the coloured objects pass, fail or are missing values. It follows the floor, *Colour by*, *Only not passing* and any value picked in the Values section, so it always describes what you see. Objects without a requirement and see-through ones on other floors aren't counted.
  - *Floor* shows one storey in colour and the rest as see-through grey, and zooms to that storey. Objects are on the storey they are placed in (parts such as stair flights follow their stair). Jumping to an object from a table switches to its floor. The list only appears when the model has more than one storey.
  - The project, site, building, storeys, spaces and openings are not drawn.
  - The model is built by several background workers at once (one per spare CPU core, fewer for very big files to save memory) and appears while it loads. Files over 100 MB use a lower detail level for curved shapes such as pipes and railings. A 300 MB model takes about 25 seconds. The dashboard itself stays small, since it holds no geometry.
- **Requirements:** a table with the failed and missing count per requirement, which you can filter by status and group, search, and sort. A requirement shows as *Missing* when all its problems are missing values, and *Failing* when at least one value is wrong. Hover or tab to the ⓘ next to a requirement to read what it means (from the rule file) and its allowed values. The same ⓘ appears on each check in the 3D side panel.
- **Values:** pick a requirement (it opens on `NONS_Process.ProcessStatus`) to see every value in the model, how many objects have it, and whether it passes. Above the list it shows the requirement's description and, when it has a list of allowed values (from the IDS or `allowed_values.csv`), each allowed value with how many objects use it. Values nobody uses are drawn dashed, and the header counts the objects whose value isn't on the list. Objects with no value are grouped as *Missing*, and values of the wrong type show the type, e.g. `yes (IfcLabel)`. The list can be filtered by Passing, Failing or Missing. Click a value to list only its objects. The list shows each object's status, value, class, name, GlobalId and group, and can be filtered, searched and sorted.
- **Objects:** each object's class, name, GlobalId and failing requirements. It shows the objects with the most issues first; switch to *All objects* to list every object checked. You can filter by class and search by name or GlobalId.

It follows the system light/dark mode and has its own theme toggle.

## What is checked

Each mvdXML file has *concept roots* (object groups such as `200 Wall`), each with *concepts* (requirements). A requirement is checked when it is marked **mandatory** for the chosen phase. Each IDS file has *specifications* (the same object groups), each with attribute and property requirements; all of them are checked.

| Rule | Example |
|---|---|
| Property exists with the right value type | `Pset_WallCommon.IsExternal` as `IFCBOOLEAN` |
| Property value is allowed | values listed in `allowed_values.csv`; measures within IFC's limits |
| Attribute is filled in | `Name`, `Description`, `LongName`, `LandTitleNumber` |
| Predefined type is set (mvdXML only) | `PredefinedType` on the object or its type |
| Object has a type (mvdXML only) | `IfcWallType` assigned |
| Georeferencing (mvdXML only) | `IfcMapConversion` with an `EPSG:` CRS on the Body context |
| TFM code format (Statsbygg) | `RefString` follows PA 0805 or PA 0802 |
| Values agree (cross-checks) | `RefString` contains `RefLocStorey` … (off by default) |

### How the rules are interpreted

- **Inherited properties:** a property on the object's type counts, unless the object has the same property itself. In that case the object's own value is the one checked, so a wrong type or an empty value on the object fails even if the type is correct. This follows the `nor` rules in the mvdXML.
- **Example values:** some Statsbygg rules contain example values, e.g. project name `'1234567'` or building name `'Building name'`. For free-text attributes these are treated as "must be filled in", not "must equal this text".
- **`NOTDEFINED`:** a PredefinedType of `NOTDEFINED` counts as not set, so the type's PredefinedType is used, as IFC intends.
- **Subgroups:** groups such as *Covering Ceiling*, *Covering Flooring* and *Transport Element Elevator* only apply to objects with that PredefinedType. An object can be checked under both the general group and its subgroup.
- **Site requirements:** the rules only apply the site checks when the model is georeferenced with `EPSG:5555`, so models without it get no site checks.
- **Model schema:** SIMBA 2.1 requires IFC4. Other schemas are still validated, with a warning.

### How the IDS files are interpreted

Statsbygg's IDS files are IDS 0.9.6, exported from BIMQ. The tool reads the parts they use:

- **Classes:** a specification applies to the listed classes only, not their subclasses, as IDS defines it. An IFC2X3 `IfcWallStandardCase` is therefore not checked under `200 Wall`; in IFC4 models this makes no difference. Objects can also be limited by PredefinedType, e.g. *Covering Ceiling*.
- **Properties** are checked the same way as in the mvdXML: own value or the type's, with the right value type.
- **Missing applicability:** 133 specifications were exported with an empty "applies to". When the same specification name has one in another IDS file (e.g. `200 Ramp` → `IfcRamp`), that one is used. The 59 that have none anywhere (mostly electrical sensors and the air handler) are skipped, and the tool names them when you pick that file.
- **Example values:** a "list" with a single value is an example in these files (e.g. `BuildingID` = "Matrikkelens bygninngsnummer"), so it only means "must have a value". `UNSET.` is read as `UNSET`.

On the architect's model, IDS B4.1 and mvdXML B4.1.9 give the same result for every check they share; mvdXML adds the PredefinedType and georeferencing checks.

## Oslobygg

Oslobygg (Oslo kommune) publishes its requirements on [skok.no](https://skok.no/Manualer) in the BIM-manual and the *BIM egenskapsmatrise*, an Excel sheet with one tab per discipline and a column per phase. It isn't a file a program can read as it is: objects are named in Norwegian ("Yttervegger", "Bjelker"), not as IFC classes, and properties are written as `FireRating (f.eks: EI60)`. So `Oslobygg Rules/make_oslobygg_ids.py` converts it into IDS files, one per phase, that the tool reads like any other IDS:

```bash
cd "Oslobygg Rules"
python make_oslobygg_ids.py
```

Run it again when Oslobygg publishes a new matrix. It prints what it corrected or left out. Only the ARK tab is converted so far; RIB, RIV and RIE need their objects added to `OBJECTS` in the script.

How the matrix is read:

- **Objects to IFC classes:** the `OBJECTS` table in the script, e.g. *Bjelker* = IfcBeam, *System- og akustisk himling* = IfcCovering CEILING, *Glasstak* = IfcWindow SKYLIGHT, *Primærkonstruksjoner* (yttertak) = IfcRoof.
- **Exterior and interior:** walls, doors, windows and glass facades have different requirements outside and inside. They are told apart by `IsExternal`, so an object without it would be checked by neither. That is why `IsExternal` is its own requirement for all of them (*Vegger (alle)*, *Dører (alle)* …), and fixing it brings the rest of the checks in.
- **Property names and types** come from IFC4's own property sets. Misspellings are corrected (`HandicapAccesible` → `HandicapAccessible`). Properties IFC4 doesn't have (`Pset_SpaceCommon.Category`, `Compartmentation` on curtain walls and roofs, the `NOOBF_*` sets) keep the matrix's name; those marked *(True/False)* must be booleans, the others can be any type.
- **"f.eks." values** are examples, so they only mean "must have a value". They are shown as the requirement's description on the dashboard.
- **Left out:** rows marked *Ikke kravsatt*, and the groups with only general text (61 Prefabrikerte rom, 66/67 spesialutrustning, 72 Utendørs konstruksjoner).

Added to every phase, from the *Felles* tab and the BIM-manual:

| Requirement | Object | Must be |
|---|---|---|
| Forvaltnings-ID | IfcProject Name | 6 digits, e.g. `200635` |
| Tomtenavn | IfcSite Name | filled in |
| Bygg-ID | IfcBuilding Name | 2 digits, `01`–`99` |
| Etasjenavn | IfcBuildingStorey Name | filled in |
| Romnummer | IfcSpace Name | 9 characters, e.g. `01-E01001` (manual 5.4.1) |
| Romnavn | IfcSpace LongName | filled in |

The converted files are the tool's reading of the matrix, not official Oslobygg files. The manual says the matrix is guidance and each project fixes its own requirements in its BIM gjennomføringsplan, so edit the IDS files (or `OBJECTS` in the script) to match the project. The SIMBA-only steps (copying DesignedStatus, the IFC4 warning) are skipped for Oslobygg; `allowed_values.csv` and `cross_checks.csv` apply to both.

## Files

```
Validation_IFC/
├── Validation_IFC.py           the tool
├── dashboard_template.html       layout of the HTML dashboard (must stay next to Validation_IFC.py)
├── allowed_values.csv            your lists of allowed values (optional)
├── cross_checks.csv              properties that must contain other properties' values (optional)
├── TFM codes/                    your own NS 3451 / NS 3457-8 code lists (optional; never upload)
├── Statsbygg TFM/                Statsbygg's PA 0802 and PA 0702 code lists (CSV) and make_tfm_lists.py
├── PDF_statsbygg/                Statsbygg's PDFs the lists are made from (optional)
├── test_validation_ifc.py      tests
├── README.md
└── Simba Rules/
    ├── statsbygg-template-simba-2-1-…_b3-1_architect-ark.mvdxml   B3.1 Skisseprosjekt
    ├── statsbygg-template-simba-2-1-…_b3-2_architect-ark.mvdxml   B3.2 Forprosjekt
    ├── statsbygg-template-simba-2-1-…_b4-1_architect-ark.mvdxml   B4.1 Detaljprosjekt
    ├── statsbygg-template-simba-2-1-…_b5-1_architect-ark.mvdxml   B5.1 Ferdigstillelse
    └── SIMBA-2.1-kravsett-ALLE-FAG-IDS-096/                          44 IDS files: 4 phases × 11 disciplines
└── Oslobygg Rules/
    ├── BIM egenskapsmatrise.xlsx      Oslobygg's property matrix (from skok.no)
    ├── make_oslobygg_ids.py           converts it into the IDS files below
    └── oslobygg_1-konseptvalg_ark.ids … oslobygg_4-som_bygget_ark.ids
```

Every `*.mvdxml` and `*.ids` file in `Simba Rules` (or next to `Validation_IFC.py`), and in folders one level inside them, is picked up automatically as SIMBA rules; those in `Oslobygg Rules` as Oslobygg rules.

## Tests

```bash
python -m pytest test_validation_ifc.py
```

The tests build small IFC4 models in memory and cover:

- reading the rules (mvdXML and IDS)
- property inheritance from types
- wrong value types
- PredefinedType
- copying old status values
- the dashboard data
- allowed values and measure limits
- cross-checks
- TFM codes by PA 0805, with and without the code lists
- TFM codes by PA 0802, and the shipped Statsbygg lists
- finding the rules files
- Oslobygg: exterior/interior filters, the room number pattern, properties without a value type, and the converted files

## Limitations

- **Allowed values:** Statsbygg's mvdXML files contain no value lists and the IDS files only a few, so most text properties are only checked against the lists you put in `allowed_values.csv`.
- **IDS:** no .1/.5/.9 steps and no PredefinedType or georeferencing checks (they aren't in the files), and 59 specifications without an applicability are skipped. Only the parts of IDS that Statsbygg's files use are read (entity, attribute and property facets).
- **Old date not copied:** `NOSSB_Process.DesignedStatusDate` has no equivalent in SIMBA 2.1.
- **3D model:** whoever opens the dashboard also needs the IFC file, and an internet connection for the viewer libraries. Large models need a computer with plenty of memory (each worker reads its own copy of the file, so a 300 MB model can use 1.5 GB or more while loading).
- **Oslobygg:** only ARK so far. Room numbers are checked for their format, not for being unique. The file naming rule and NTM/NN2000 georeferencing in the BIM-manual aren't checked.
- **Rule syntax:** only the mvdXML rule syntax used in the Statsbygg files is supported, not the full mvdXML standard.
