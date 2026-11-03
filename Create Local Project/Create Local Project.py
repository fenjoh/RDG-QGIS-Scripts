# -*- coding: utf-8 -*-
"""
===============================================================================
INITIAL MAP SET PROCESS
Builds a small, self-contained project package from the RDG regional template.
===============================================================================

Install:  Processing Toolbox > Scripts (Python icon) > Add Script to Toolbox...
          It then appears under Scripts > RDG > Initial Map Set Process.

Before running: open the template and File > Save As into the new project
folder. This script never writes to the template; it only modifies and saves
the project you currently have open.

-------------------------------------------------------------------------------
STRUCTURE OF THIS FILE
-------------------------------------------------------------------------------
The file is divided into five sections, in this order. Each section is
self-contained so the steps can be developed independently.

    SECTION 0   Shared helpers      naming, folders, project variables,
                                    layer repointing, reporting
    SECTION 1   Step 1 Preprocess   project setup, boundary, AOIs, pruning
    SECTION 2   Step 2 Localize     extract and copy data into the project
    SECTION 3   Step 3 LiDAR        DEMs and terrain analysis
    SECTION 4   Algorithm shell     the Processing dialog that drives them

The three Step classes are plain Python objects, not Processing algorithms.
They take a RunContext and a feedback object and do their work. Only the thin
class in SECTION 4 touches the Processing framework. That keeps each step
readable and editable on its own.

-------------------------------------------------------------------------------
WHAT EACH STEP DOES
-------------------------------------------------------------------------------
STEP 1 - PREPROCESS
  Sets the project CRS and the project variables, then builds the site
  geometry from the tax parcels you have selected:
    - dissolves the selected parcel features into an Estimated Property
      Boundary
    - builds a Project Scale AOI at twice the width and twice the height of
      that boundary's bounding box
    - builds a Region Scale AOI, a square (85,000 ft by default, which is
      17 inches at 1 inch = 5,000 ft) centred on the same point
    - copies the Project Scale AOI to serve as the LiDAR AOI
    - pulls the watershed subbasins that the boundary falls in or overlaps
  Each is written to its own GeoPackage in "Local Layers_<code>", and the
  matching placeholder layers in Site Layers are repointed at them.
  Finally it prunes any layer in a scale group whose data lies entirely
  outside that group's AOI.

STEP 2 - LOCALIZE LAYERS
  Works only on top-level groups whose name contains "PROJECT SCALE" or
  "REGION SCALE". Everything else is left alone.
    - vector layers keep every feature that touches the AOI, whole and
      unclipped, written to a GeoPackage
    - raster layers are cut to the AOI rectangle on their own pixel grid,
      so data type, bands, pixel size and nodata are unchanged
    - join tables are reduced to the rows the kept features reference
    - layers with nothing inside the AOI are removed from the project
  Every layer is then repointed at its local copy, so the project folder can
  be zipped and sent with no broken links.

STEP 3 - LIDAR ANALYSIS
  Takes either a Ground DEM and an All DEM, or a point cloud to build them
  from, and writes the standard RDG terrain products into
  "Lidar Analysis_<code>": smoothed ground, slope, aspect, contours,
  drainages, tree canopy, high and low points, and duration of insolation
  for the equinox and both solstices. The matching layers in RDG ANALYSIS
  are repointed at the new files.

-------------------------------------------------------------------------------
NOTES WORTH READING ONCE
-------------------------------------------------------------------------------
* RUN WITH "DRY RUN" TICKED FIRST. It writes nothing and reports what would
  happen, how large the output would be and roughly how long it would take.

* SPATIAL INDEXES. Several source shapefiles are multi-gigabyte files with
  millions of features. With an index the script reads only the features near
  your site; without one it must read the whole file. Index building is on by
  default and writes a small .qix file next to the source data, which speeds
  up every future run for everyone on the team.

* GEOPACKAGES AND CLOUD SYNC. GeoPackage is SQLite, and writing one into a
  folder that Google Drive is actively syncing can corrupt or lock it. Pause
  sync while this runs if the project folder is inside a synced drive, and
  close the project before zipping so no -wal or -shm files are left behind.

* RESUME. Completed layers are recorded in a manifest in the output folder.
  If a run is cancelled or fails, tick "Resume" to carry on from where it
  stopped. The manifest records a fingerprint of the AOIs, so changing them
  invalidates the resume rather than silently mixing two extents.

* STREAMING LAYERS are left live. They cost nothing in the package and work
  for anyone with an internet connection. Localizing them is planned but not
  yet implemented.

* NAMES MATTER. Group keywords, the Site Layers placeholders, the watershed
  subbasin source, the wetlands buffer rule and the Step 3 layer mapping are
  all matched by name. Every one of them is listed in the dialog's help
  panel, which is generated from the constants in the code, so it stays
  correct as long as the constants are the only place names are written.
  If you add another name-matched behaviour, put the name in a constant at
  the top of its section and it documents itself.

* AUTHENTICATED LAYERS. Any layer whose source contains "authcfg=" depends on
  a credential in your local QGIS authentication database, not in the project
  file, so it will be broken for anyone else. These are flagged in the report.

===============================================================================
"""

import os
import re
import json
import math
import time
import datetime
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import processing

from qgis.PyQt.QtCore import QCoreApplication

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDataProvider,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsLayerTreeGroup,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterNumber,
    QgsProcessingParameterPointCloudLayer,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingUtils,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsRectangle,
    QgsUnitTypes,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsVectorLayerJoinInfo,
    QgsWkbTypes,
)

try:
    from osgeo import gdal
    gdal.UseExceptions()
    HAVE_GDAL = True
except ImportError:
    HAVE_GDAL = False


# -----------------------------------------------------------------------------
# QGIS 3 / QGIS 4 compatibility
#
# QGIS 4.0 removed API that was deprecated during the 3.x series. Each name
# below resolves to the modern form when it exists and the old one otherwise,
# so this single file runs on 3.34+ and on 4.x. Use these rather than the
# class attributes directly.
# -----------------------------------------------------------------------------

def _first_attr(owner, *names):
    """Return the first attribute that exists on owner, else raise."""
    for name in names:
        holder = owner
        found = True
        for part in name.split("."):
            if not hasattr(holder, part):
                found = False
                break
            holder = getattr(holder, part)
        if found:
            return holder
    raise AttributeError(
        f"None of {names} found on {getattr(owner, '__name__', owner)}. "
        "This QGIS version is not supported by this script.")


# Field types: QVariant-based QgsField construction was replaced by QMetaType.
try:
    from qgis.PyQt.QtCore import QMetaType
    FIELD_TYPE_STRING = QMetaType.Type.QString
    FIELD_TYPE_INT = QMetaType.Type.Int
except Exception:                                        # pragma: no cover
    from qgis.PyQt.QtCore import QVariant
    FIELD_TYPE_STRING = QVariant.String
    FIELD_TYPE_INT = QVariant.Int

# Where a field came from: QgsFields.OriginJoin moved to Qgis.FieldOrigin.Join.
try:
    FIELD_ORIGIN_JOIN = Qgis.FieldOrigin.Join
    FIELD_ORIGIN_EXPRESSION = Qgis.FieldOrigin.Expression
except AttributeError:                                   # pragma: no cover
    FIELD_ORIGIN_JOIN = QgsFields.OriginJoin
    FIELD_ORIGIN_EXPRESSION = QgsFields.OriginExpression

WRITER_NO_ERROR = _first_attr(
    QgsVectorFileWriter, "WriterError.NoError", "NoError")
WRITER_OVERWRITE = _first_attr(
    QgsVectorFileWriter,
    "ActionOnExistingFile.CreateOrOverwriteFile", "CreateOrOverwriteFile")

WKB_NO_GEOMETRY = _first_attr(Qgis, "WkbType.NoGeometry") \
    if hasattr(Qgis, "WkbType") else QgsWkbTypes.NoGeometry
WKB_MULTIPOLYGON = _first_attr(Qgis, "WkbType.MultiPolygon") \
    if hasattr(Qgis, "WkbType") else QgsWkbTypes.MultiPolygon

NUMBER_DOUBLE = _first_attr(
    Qgis, "ProcessingNumberParameterType.Double") \
    if hasattr(Qgis, "ProcessingNumberParameterType") \
    else QgsProcessingParameterNumber.Double

GEOMETRY_POLYGON = _first_attr(Qgis, "GeometryType.Polygon") \
    if hasattr(Qgis, "GeometryType") else QgsWkbTypes.PolygonGeometry
GEOMETRY_LINE = _first_attr(Qgis, "GeometryType.Line") \
    if hasattr(Qgis, "GeometryType") else QgsWkbTypes.LineGeometry


# =============================================================================
# SECTION 0 - SHARED HELPERS
# Naming, folders, project variables, layer repointing, reporting. Everything
# in this section is used by more than one step; nothing here knows about any
# particular step.
# =============================================================================

# --- how groups and placeholder layers are recognised ------------------------

PROJECT_SCALE_KEYWORD = "project scale"
REGION_SCALE_KEYWORD = "region scale"

# Providers that stream from the web. These cannot be made local (yet) and are
# left pointing at their service. arcgisfeatureserver is deliberately absent:
# it serves real features, so it is extracted like any other vector layer.
STREAMING_PROVIDERS = {
    "wms", "wmts", "xyz", "vectortile", "xyzvectortiles",
    "arcgisvectortileservice", "arcgismapserver", "arcgisrestserver",
}

# Placeholder layers in Site Layers that Step 1 repoints. Several layers can
# share one output file, which is why the value is a list of layer names.
SITE_LAYER_TARGETS = {
    "boundary":    ["Property Star", "Est. Property Boundary",
                    "Est. Property Boundary (Mask)"],
    "aoi_project": ["AOI, Project Scale"],
    "aoi_region":  ["AOI, Region Scale"],
    "aoi_lidar":   ["AOI, Lidar"],
    "subbasin":    ["Watershed Subbasin, Project",
                    "Watershed Subbasin, Project (Mask)"],
}

# Output file names for Step 1, before the _<code>_<date> suffix is added.
SITE_LAYER_FILENAMES = {
    "boundary":    "Est Property Boundary",
    "aoi_project": "AOI Project Scale",
    "aoi_region":  "AOI Region Scale",
    "aoi_lidar":   "AOI Lidar",
    "subbasin":    "Watershed Subbasin Project",
}


@dataclass(frozen=True)
class DerivedSiteLayer:
    """A site layer built by selecting from a group by spatial relationship.

    Add a new entry here rather than writing new code when another layer needs
    the same treatment as the watershed subbasins.
    """
    key: str                  # key into SITE_LAYER_TARGETS / FILENAMES
    source_group: str         # group whose layers are searched
    description: str


DERIVED_SITE_LAYERS = [
    DerivedSiteLayer(
        key="subbasin",
        source_group="Watershed Subbasins",
        description="watershed subbasins touching the property boundary",
    ),
]


# --- small utilities ---------------------------------------------------------

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_len: int = 64) -> str:
    """Make a layer name safe as a filename on both macOS and Windows."""
    name = re.sub(r'[\\/:*?"<>|]', "-", (name or "").strip())
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r"\s+", " ", name).strip().strip(" .-")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .-")
    if not name:
        name = "layer"
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name


def gpkg_table_name(name: str) -> str:
    """A safe SQLite table name for the layer inside a GeoPackage."""
    table = re.sub(r"[^0-9A-Za-z_]", "_", name).strip("_")
    table = re.sub(r"_+", "_", table)
    if not table or table[0].isdigit():
        table = "l_" + table
    return table[:60]


def human_size(num_bytes) -> str:
    if not num_bytes:
        return "0 B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def human_time(seconds) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} sec"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} sec"
    return f"{seconds // 3600} hr {(seconds % 3600) // 60} min"


def decode_source_path(layer) -> Tuple[str, str]:
    """Return (file path, layer name inside the file) for a file-based layer."""
    try:
        parts = QgsProviderRegistry.instance().decodeUri(
            layer.providerType(), layer.source())
        return parts.get("path", "") or "", parts.get("layerName", "") or ""
    except Exception:
        return "", ""


def source_size_bytes(path: str) -> int:
    """Size on disk, including shapefile sidecars and .gdb directory trees."""
    if not path:
        return 0
    if os.path.isfile(path):
        total = os.path.getsize(path)
        if path.lower().endswith(".shp"):
            stem = os.path.splitext(path)[0]
            folder = os.path.dirname(path)
            try:
                for name in os.listdir(folder):
                    full = os.path.join(folder, name)
                    if full != path and os.path.splitext(full)[0] == stem:
                        total += os.path.getsize(full)
            except OSError:
                pass
        return total
    if os.path.isdir(path):
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total
    return 0


def is_streaming(layer) -> bool:
    return layer.providerType() in STREAMING_PROVIDERS


def layer_has_geometry(layer) -> bool:
    return (isinstance(layer, QgsVectorLayer)
            and layer.wkbType() != WKB_NO_GEOMETRY)


class FileNamer:
    """Builds '<name>_<code>_<YYMMDD>.<ext>' and keeps every name unique.

    Collisions get _2, _3 and so on. Names already handed out in this run are
    remembered, so two layers with the same name never overwrite each other.
    """

    def __init__(self, code: str, date_stamp: str):
        self.code = code
        self.date = date_stamp
        self._used: Set[str] = set()
        # Everything handed out this run. Used by the end-of-run tidy-up to
        # tell this run's files from an earlier run's leftovers.
        self.issued: Set[str] = set()

    def build(self, folder: str, base_name: str, ext: str) -> str:
        base = sanitize_filename(base_name)
        stem = f"{base}_{self.code}_{self.date}" if self.code else f"{base}_{self.date}"
        candidate = os.path.join(folder, stem + ext)
        counter = 1
        while candidate.lower() in self._used or os.path.exists(candidate):
            counter += 1
            candidate = os.path.join(folder, f"{stem}_{counter}{ext}")
            if counter > 500:
                raise QgsProcessingException(
                    f"Could not find a free filename for '{base_name}'.")
        self._used.add(candidate.lower())
        self.issued.add(os.path.normcase(os.path.abspath(candidate)))
        return candidate

    def release(self, path: str) -> None:
        """Give a reserved name back, for outputs that turned out to be empty."""
        self._used.discard(path.lower())


class Report:
    """Collects the run log so it can be printed and written to a text file."""

    def __init__(self, feedback):
        self.feedback = feedback
        self.lines: List[str] = []
        self.warnings: List[str] = []

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def info(self, text: str) -> None:
        self.lines.append(text)
        self.feedback.pushInfo(text)

    def step(self, text: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.info(f"{stamp}  {text}")

    def warn(self, text: str) -> None:
        self.lines.append("WARNING: " + text)
        self.warnings.append(text)
        self.feedback.pushWarning(text)

    def gap(self, count: int = 1) -> None:
        """Blank lines, in the log as well as the report."""
        for _ in range(count):
            self.info("")

    def heading(self, text: str) -> None:
        self.line("")
        self.line("=" * 78)
        self.info(text)
        self.line("=" * 78)

    def write(self, folder: str, code: str, date_stamp: str,
              dry_run: bool) -> str:
        label = "Initial Map Set Process Dry Run" if dry_run \
            else "Initial Map Set Process"
        path = os.path.join(folder, f"{label}_{code}_{date_stamp}.txt")
        # Never overwrite an earlier report from the same day.
        counter = 1
        while os.path.exists(path):
            counter += 1
            path = os.path.join(folder, f"{label}_{code}_{date_stamp}_{counter}.txt")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(self.lines))
            self.feedback.pushInfo("\nReport written to: " + path)
            return path
        except OSError as exc:
            self.feedback.pushWarning(f"Could not write the report file: {exc}")
            return ""


class Manifest:
    """Per-layer record of what has been done, so a run can be resumed.

    The fingerprint ties the manifest to a specific set of AOIs. If the AOIs
    change, resuming would mix two different extents, so it is refused.
    """

    FILENAME = "_initial_map_set_manifest.json"

    def __init__(self, folder: str, fingerprint: str):
        self.path = os.path.join(folder, self.FILENAME)
        self.fingerprint = fingerprint
        self.entries: Dict[str, dict] = {}

    def load(self, strict: bool = True) -> bool:
        if not os.path.isfile(self.path):
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False
        if strict and data.get("fingerprint") != self.fingerprint:
            raise QgsProcessingException(
                "The manifest in the output folder was built with different AOIs "
                "or a different project CRS. Resuming would mix two extents.\n"
                "Either restore the original AOIs, or untick Resume and move the "
                "existing output folder aside first.")
        self.entries = data.get("layers", {})
        return True

    def get(self, layer_id: str) -> Optional[dict]:
        return self.entries.get(layer_id)

    def record(self, layer_id: str, **fields) -> None:
        self.entries[layer_id] = fields
        self.save()

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump({"fingerprint": self.fingerprint,
                           "layers": self.entries}, handle, indent=1)
        except OSError:
            pass          # a manifest we cannot write is not worth failing over

    @property
    def done_count(self) -> int:
        return sum(1 for e in self.entries.values() if e.get("status") == "done")


@dataclass
class RunContext:
    """Everything the three steps share. Built once by the algorithm shell."""

    project: QgsProject
    project_dir: str
    code: str
    date_stamp: str
    crs: QgsCoordinateReferenceSystem
    local_layers_dir: str
    lidar_dir: str
    namer: FileNamer
    report: Report
    dry_run: bool = False

    # Filled in by Step 1, read by Steps 2 and 3. When a step runs on its own
    # these are rebuilt from the project's own layers.
    boundary_geom: Optional[QgsGeometry] = None
    aoi_project: Optional[QgsGeometry] = None
    aoi_region: Optional[QgsGeometry] = None
    aoi_lidar: Optional[QgsGeometry] = None
    outputs: Dict[str, str] = field(default_factory=dict)
    # Group names that held at least one layer when the run began. Groups that
    # were already empty are left alone; ones emptied by this run are removed.
    groups_with_layers_at_start: Set[str] = field(default_factory=set)
    # (layer name, reason) for anything that did not end up as a local file.
    not_localized: List[Tuple[str, str]] = field(default_factory=list)
    # Layer ids Step 1 removed (or would remove, on a dry run). Step 2 skips
    # these so its plan and estimate match what a real run would do.
    pruned_layer_ids: Set[str] = field(default_factory=set)

    def ensure_dirs(self) -> None:
        if self.dry_run:
            return
        for folder in (self.local_layers_dir, self.lidar_dir):
            os.makedirs(folder, exist_ok=True)

    def fingerprint(self) -> str:
        """A short hash of the AOIs, used to validate a resume."""
        import hashlib
        digest = hashlib.sha256()
        for geom in (self.aoi_project, self.aoi_region, self.aoi_lidar):
            digest.update(bytes(geom.asWkb()) if geom else b"none")
        digest.update(self.crs.authid().encode("utf-8"))
        return digest.hexdigest()[:16]


# --- geometry and CRS helpers ------------------------------------------------

def transform_geometry(geom: QgsGeometry,
                       src: QgsCoordinateReferenceSystem,
                       dst: QgsCoordinateReferenceSystem,
                       project: QgsProject) -> QgsGeometry:
    """Reproject a copy of a geometry. Returns the original when CRSs match."""
    if src == dst or not src.isValid() or not dst.isValid():
        return QgsGeometry(geom)
    moved = QgsGeometry(geom)
    transform = QgsCoordinateTransform(src, dst, project)
    if moved.transform(transform) != 0:
        raise QgsProcessingException(
            f"Could not transform geometry from {src.authid()} to {dst.authid()}.")
    return moved


def rectangle_geometry(center_x: float, center_y: float,
                       width: float, height: float) -> QgsGeometry:
    """An axis-aligned rectangle of the given size centred on a point."""
    half_w, half_h = width / 2.0, height / 2.0
    return QgsGeometry.fromRect(QgsRectangle(
        center_x - half_w, center_y - half_h,
        center_x + half_w, center_y + half_h))


def convert_distance(value: float,
                     from_unit,
                     crs: QgsCoordinateReferenceSystem) -> float:
    """Convert a distance into the map units of a CRS."""
    try:
        factor = QgsUnitTypes.fromUnitToUnitFactor(from_unit, crs.mapUnits())
    except Exception:
        factor = 1.0
    return value * factor


def crs_is_angular(crs: QgsCoordinateReferenceSystem) -> bool:
    try:
        return crs.mapUnits() == Qgis.DistanceUnit.Degrees
    except AttributeError:
        return crs.isGeographic()


# --- writing vectors ---------------------------------------------------------

def build_output_fields(layer: QgsVectorLayer) -> Tuple[List[int], QgsFields, Optional[str]]:
    """Provider fields only; joined and virtual fields are rebuilt by QGIS.

    GeoPackage reserves 'fid', so a source field of that name is renamed.
    Returns (source field indices, output fields, renamed field or None).
    """
    source_fields = layer.fields()
    keep_indices: List[int] = []
    out_fields = QgsFields()
    renamed = None
    for index, source_field in enumerate(source_fields):
        origin = source_fields.fieldOrigin(index)
        if origin in (FIELD_ORIGIN_JOIN, FIELD_ORIGIN_EXPRESSION):
            continue
        keep_indices.append(index)
        new_field = QgsField(source_field)
        if new_field.name().lower() == "fid":
            new_field.setName("fid_src")
            renamed = source_field.name()
        out_fields.append(new_field)
    return keep_indices, out_fields, renamed


def create_gpkg_writer(path: str, fields: QgsFields, wkb_type,
                       crs: QgsCoordinateReferenceSystem,
                       table: str) -> QgsVectorFileWriter:
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = table
    options.fileEncoding = "UTF-8"
    options.actionOnExistingFile = WRITER_OVERWRITE
    writer = QgsVectorFileWriter.create(
        path, fields, wkb_type, crs,
        QgsProject.instance().transformContext(), options)
    if writer.hasError() != WRITER_NO_ERROR:
        raise QgsProcessingException(
            f"Could not create {path}: {writer.errorMessage()}")
    return writer


def write_single_polygon(path: str, table: str, geom: QgsGeometry,
                         crs: QgsCoordinateReferenceSystem,
                         attributes: Optional[Dict[str, object]] = None) -> None:
    """Write one polygon feature to a new GeoPackage. Used for the AOIs."""
    fields = QgsFields()
    attributes = attributes or {}
    for name, value in attributes.items():
        field_type = FIELD_TYPE_INT if isinstance(value, int) else FIELD_TYPE_STRING
        fields.append(QgsField(name, field_type))
    writer = create_gpkg_writer(path, fields, WKB_MULTIPOLYGON, crs, table)
    feature = QgsFeature(fields)
    out_geom = QgsGeometry(geom)
    out_geom.convertToMultiType()
    feature.setGeometry(out_geom)
    if attributes:
        feature.setAttributes(list(attributes.values()))
    writer.addFeature(feature)
    error, message = writer.hasError(), writer.errorMessage()
    del writer
    if error != WRITER_NO_ERROR:
        raise QgsProcessingException(f"Write error on {path}: {message}")


# --- repointing layers -------------------------------------------------------

def resolve_gpkg_table(path: str) -> Optional[str]:
    """Read back the table name inside a GeoPackage.

    Processing algorithms normally name the table after the file stem, but
    that is a convention rather than a promise, so it is read from the file.
    """
    try:
        sublayers = QgsProviderRegistry.instance().querySublayers(path)
        if sublayers:
            return sublayers[0].name()
    except Exception:
        pass
    return None


def repoint_layer(layer, path: str, table: Optional[str], provider: str,
                  report: Report) -> None:
    """Swap a layer's data source, keeping its id, name, styling and joins.

    setDataSource is what makes this whole approach work: the layer keeps its
    id, so print layouts, map themes and joins that reference it stay valid.
    Styling usually survives too, but it is saved and restored if the renderer
    type changes (which happens when field names differ).
    """
    uri = path.replace("\\", "/")
    if provider == "ogr" and table:
        uri = f"{uri}|layername={table}"

    style_backup = path + ".tmpstyle.qml"
    style_saved = False
    try:
        layer.saveNamedStyle(style_backup)
        style_saved = True
    except Exception:
        pass

    renderer_before = layer.renderer().type() if layer.renderer() else None
    layer.setDataSource(uri, layer.name(), provider,
                        QgsDataProvider.ProviderOptions())

    if not layer.isValid():
        raise QgsProcessingException(
            f"After repointing, layer '{layer.name()}' is invalid. URI: {uri}")

    # setDataSource does NOT recompute the extent. Without this the layer
    # keeps the extent of its previous source, in that source's coordinate
    # space, and the project is saved with those stale numbers. A later run
    # then compares degrees against feet and concludes the layer is nowhere
    # near the site.
    refresh_extent(layer)

    renderer_after = layer.renderer().type() if layer.renderer() else None
    if style_saved and renderer_before and renderer_before != renderer_after:
        try:
            layer.loadNamedStyle(style_backup)
        except Exception as exc:
            report.warn(f"Styling may have changed on '{layer.name()}': {exc}")
    if style_saved:
        try:
            os.remove(style_backup)
        except OSError:
            pass

    # Any provider filter was baked into the extracted file already.
    if isinstance(layer, QgsVectorLayer) and layer.subsetString():
        layer.setSubsetString("")

    layer.triggerRepaint()


def refresh_extent(layer) -> None:
    """Recompute a layer's extent from its current data source."""
    try:
        if isinstance(layer, QgsVectorLayer):
            layer.updateExtents(True)
        else:
            provider = layer.dataProvider()
            if provider is not None:
                layer.setExtent(provider.extent())
    except Exception:
        pass            # a stale extent is a nuisance, not a reason to stop


def source_is_inside(layer, folder: str) -> bool:
    """True when the layer's data already lives in the project folder."""
    path, _inner = decode_source_path(layer)
    if not path:
        return False
    target = os.path.normcase(os.path.abspath(folder))
    return os.path.normcase(os.path.abspath(path)).startswith(target)


def has_feature_in(layer: QgsVectorLayer, aoi_in_layer_crs: QgsGeometry) -> bool:
    """Ask the provider for one feature inside the AOI. Exact, and cheap when
    the source is indexed."""
    request = (QgsFeatureRequest()
               .setFilterRect(aoi_in_layer_crs.boundingBox())
               .setNoAttributes()
               .setLimit(1))
    for _feature in layer.getFeatures(request):
        return True
    return False


def layer_is_outside_aoi(layer, aoi: QgsGeometry,
                         crs: QgsCoordinateReferenceSystem,
                         project: QgsProject) -> Tuple[bool, str]:
    """Is this layer definitely empty inside the AOI? Returns (verdict, why).

    Deliberately cautious: anything uncertain comes back False, because a
    wrong "yes" deletes data while a wrong "no" only costs a little time.
    The extent is refreshed first, since a repointed layer carries the extent
    of whatever it used to point at.
    """
    try:
        aoi_here = transform_geometry(aoi, crs, layer.crs(), project)
    except QgsProcessingException as exc:
        return False, f"cannot transform the AOI to {layer.crs().authid()}: {exc}"

    refresh_extent(layer)
    extent = layer.extent()
    rect = aoi_here.boundingBox()

    if extent.isEmpty():
        return False, "the layer reports no extent, so it was left alone"
    if extent.intersects(rect):
        return False, "extents overlap"

    # The extents say no. Confirm it against the data when that is cheap.
    if isinstance(layer, QgsVectorLayer):
        path, _inner = decode_source_path(layer)
        if has_spatial_index(layer, path):
            if has_feature_in(layer, aoi_here):
                return False, ("extents disagreed but the data has features "
                               "here, so it was kept")
            return True, "no features inside the AOI"
        return True, (f"extent {extent.toString(0)} does not reach the AOI "
                      f"{rect.toString(0)} (no spatial index, so this is an "
                      "extent test only)")

    return True, f"extent {extent.toString(0)} does not reach {rect.toString(0)}"


def embed_style_in_gpkg(layer: QgsVectorLayer, report: Report) -> None:
    """Store the style inside the GeoPackage so the file stands on its own."""
    try:
        layer.saveStyleToDatabase(
            layer.name()[:60], "Localized from the RDG template", True, "")
    except Exception as exc:
        report.feedback.pushDebugInfo(
            f"Could not embed the style for '{layer.name()}': {exc}")


def unlink_layer(layer, report: Report) -> None:
    """Point a layer at nothing, so it is visibly empty rather than wrong.

    Used when a placeholder has no match for this site: better an obviously
    broken layer than one still showing a previous project's data.
    """
    try:
        layer.setDataSource("", layer.name(), layer.providerType(),
                            QgsDataProvider.ProviderOptions())
    except Exception as exc:
        report.warn(f"Could not clear the source of '{layer.name()}': {exc}")


def find_layers_by_name(project: QgsProject, names: Sequence[str]) -> List:
    """Match layers by name, ignoring case and stray whitespace."""
    wanted = {n.strip().lower() for n in names}
    return [lyr for lyr in project.mapLayers().values()
            if (lyr.name() or "").strip().lower() in wanted]


def find_layers_by_prefix(project: QgsProject, prefixes: Sequence[str]) -> List:
    """Match layers whose name STARTS WITH any of these, ignoring case.

    Prefix rather than exact match so a template can grow new variants of a
    layer without the script needing to know about them.
    """
    wanted = tuple(p.strip().lower() for p in prefixes)
    return [lyr for lyr in project.mapLayers().values()
            if (lyr.name() or "").strip().lower().startswith(wanted)]


def top_level_groups(project: QgsProject) -> List[QgsLayerTreeGroup]:
    return [child for child in project.layerTreeRoot().children()
            if isinstance(child, QgsLayerTreeGroup)]


def layers_in_group(group: QgsLayerTreeGroup) -> List:
    """Every layer under a group, at any depth, skipping unresolved nodes."""
    return [node.layer() for node in group.findLayers() if node.layer() is not None]


def group_for_layer(project: QgsProject, layer) -> Optional[QgsLayerTreeGroup]:
    node = project.layerTreeRoot().findLayer(layer.id())
    parent = node.parent() if node else None
    while parent is not None and parent.parent() is not None:
        if isinstance(parent, QgsLayerTreeGroup):
            return parent
        parent = parent.parent()
    return None


# --- joins -------------------------------------------------------------------

@dataclass
class JoinSpec:
    """A record of one vector join, so it can be checked and rebuilt."""
    target_id: str
    join_layer_id: str
    target_field: str
    join_field: str
    prefix: str


def collect_joins(project: QgsProject) -> Tuple[List[JoinSpec], Set[str]]:
    """Every join in the project, plus the ids of the layers used as tables."""
    specs: List[JoinSpec] = []
    table_ids: Set[str] = set()
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        for join in layer.vectorJoins():
            table_ids.add(join.joinLayerId())
            specs.append(JoinSpec(
                target_id=layer.id(),
                join_layer_id=join.joinLayerId(),
                target_field=join.targetFieldName(),
                join_field=join.joinFieldName(),
                prefix=join.prefix() or "",
            ))
    return specs, table_ids


def restore_joins(project: QgsProject, specs: Iterable[JoinSpec],
                  report: Report) -> None:
    """Re-attach any join that did not survive its layer being repointed.

    Layer ids are preserved by setDataSource, so joins normally survive on
    their own. This is a safety net.
    """
    for spec in specs:
        target = project.mapLayer(spec.target_id)
        join_table = project.mapLayer(spec.join_layer_id)
        if target is None or not isinstance(target, QgsVectorLayer):
            continue
        if join_table is None:
            report.warn(
                f"The join table for '{target.name()}' is gone, so the join was "
                "dropped. Check that layer's attributes.")
            continue
        existing = {(j.joinLayerId(), j.targetFieldName(), j.joinFieldName())
                    for j in target.vectorJoins()}
        if (spec.join_layer_id, spec.target_field, spec.join_field) in existing:
            continue
        info = QgsVectorLayerJoinInfo()
        info.setJoinLayer(join_table)
        info.setJoinFieldName(spec.join_field)
        info.setTargetFieldName(spec.target_field)
        info.setPrefix(spec.prefix)
        info.setUsingMemoryCache(True)
        if target.addJoin(info):
            report.info(f"    re-attached the join on '{target.name()}'")
        else:
            report.warn(f"Could not re-attach the join on '{target.name()}'.")


# --- spatial indexes ---------------------------------------------------------

def has_spatial_index(layer, path: str) -> bool:
    """Best guess at whether reads can use an index rather than scanning."""
    try:
        presence = layer.hasSpatialIndex()
        if presence == Qgis.SpatialIndexPresence.Present:
            return True
        if presence == Qgis.SpatialIndexPresence.NotPresent:
            return False
    except (AttributeError, TypeError):
        pass
    if path and path.lower().endswith(".shp"):
        stem = os.path.splitext(path)[0]
        return os.path.exists(stem + ".qix") or os.path.exists(stem + ".sbn")
    return True       # databases, GeoPackages and services index themselves


def build_spatial_index(layer, report: Report) -> bool:
    """Create a .qix next to a shapefile. Harmless, and permanently faster.

    Only meaningful for layers with geometry; a join table has nothing to
    index. Failures are reported and ignored, since the source data may sit on
    a read-only drive.
    """
    if not layer_has_geometry(layer):
        return False
    try:
        processing.run("native:createspatialindex", {"INPUT": layer})
        return True
    except Exception as exc:
        report.warn(f"Could not build a spatial index for '{layer.name()}': {exc}")
        return False


# =============================================================================
# SECTION 1 - STEP 1: PREPROCESS
#
# Sets up the project and builds the site geometry that the other two steps
# depend on:
#   1. set the project CRS and the project variables
#   2. dissolve the selected tax parcels into an Estimated Property Boundary
#   3. build the Project Scale, Region Scale and LiDAR AOIs from it
#   4. pull any derived site layers (watershed subbasins) out of their group
#   5. repoint the Site Layers placeholders at the new files
#   6. prune layers in the scale groups that fall outside their AOI
# =============================================================================

# The long side of an 11x17 sheet. The region AOI is a square this many
# inches across at the chosen scale, so it fills the sheet either way up.
SHEET_LONG_SIDE_INCHES = 17.0


@dataclass
class Step1Settings:
    """Everything Step 1 needs beyond the shared RunContext."""
    project_variables: Dict[str, str]
    aoi_method: int               # 0 = multiply the bounding box, 1 = buffer it
    aoi_multiplier: float         # used when aoi_method == 0
    aoi_buffer: float             # used when aoi_method == 1, in distance_unit
    region_scale: float           # map units per inch on the sheet
    distance_unit: object         # Qgis.DistanceUnit for the distances above


class Step1Preprocess:
    """Builds the site geometry and prepares the project."""

    def __init__(self, ctx: RunContext, settings: Step1Settings):
        self.ctx = ctx
        self.settings = settings
        self.report = ctx.report

    # -- entry point ----------------------------------------------------------
    def run(self, feedback) -> None:
        self.report.heading("STEP 1 - PREPROCESS")

        self._apply_project_crs()
        self.ctx.ensure_dirs()

        boundary = self._build_property_boundary()
        self.ctx.boundary_geom = boundary

        # Set the variables once the boundary exists, so the acreage can be
        # calculated and stored alongside the typed-in values.
        self._apply_project_variables(boundary)

        self._build_aois(boundary)
        self._write_site_layers()
        self._build_derived_site_layers(feedback)

        self._prune_layers_outside_aois()

        self.report.step("Step 1 complete.")

    # -- project setup --------------------------------------------------------
    def _apply_project_crs(self) -> None:
        """The CRS must be set first: every AOI is measured in its units."""
        if crs_is_angular(self.ctx.crs):
            raise QgsProcessingException(
                f"The chosen project CRS ({self.ctx.crs.authid()}) uses degrees. "
                "AOI sizes cannot be measured in degrees. Choose a projected CRS "
                "in feet or metres.")
        if self.ctx.project.crs() != self.ctx.crs:
            self.report.step(
                f"Setting the project CRS to {self.ctx.crs.authid()} "
                f"({self.ctx.crs.description()})")
            if not self.ctx.dry_run:
                self.ctx.project.setCrs(self.ctx.crs)
        else:
            self.report.step(f"Project CRS is already {self.ctx.crs.authid()}")

    def _apply_project_variables(self, boundary: QgsGeometry) -> None:
        self.report.step("Setting project variables")
        values = dict(self.settings.project_variables)
        values["project_acres"] = self._acreage(boundary)
        for name, value in values.items():
            self.report.line(f"    {name} = {value}")
            if not self.ctx.dry_run:
                QgsExpressionContextUtils.setProjectVariable(
                    self.ctx.project, name, value)

    def _acreage(self, boundary: QgsGeometry) -> str:
        """Property area in acres, as a bare number for the project variable."""
        try:
            area_unit = QgsUnitTypes.distanceToAreaUnit(self.ctx.crs.mapUnits())
            factor = QgsUnitTypes.fromUnitToUnitFactor(
                area_unit, Qgis.AreaUnit.Acres)
        except Exception:
            self.report.warn("Could not work out the CRS area units, so "
                             "project_acres was left unset.")
            return ""
        return f"{boundary.area() * factor:.2f}"

    # -- the property boundary ------------------------------------------------
    def _build_property_boundary(self) -> QgsGeometry:
        """Dissolve whatever polygon features are selected, in any layer.

        Selecting across several layers is allowed on purpose: a site can
        straddle a town line and take parcels from two sources.
        """
        collected: List[QgsGeometry] = []
        sources: List[str] = []
        parcel_count = 0

        for layer in self.ctx.project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.geometryType() != GEOMETRY_POLYGON:
                continue
            if layer.selectedFeatureCount() == 0:
                continue
            sources.append(layer.name())
            for feature in layer.getSelectedFeatures():
                geom = feature.geometry()
                if geom.isEmpty():
                    continue
                if not geom.isGeosValid():
                    geom = geom.makeValid()
                collected.append(transform_geometry(
                    geom, layer.crs(), self.ctx.crs, self.ctx.project))
                parcel_count += 1

        if not collected:
            raise QgsProcessingException(
                "No polygon features are selected. Select the tax parcels that "
                "make up the site, in one or more layers, then run this again.")

        boundary = QgsGeometry.unaryUnion(collected)
        if boundary.isEmpty():
            raise QgsProcessingException(
                "The selected parcels did not combine into a usable polygon.")
        if not boundary.isGeosValid():
            boundary = boundary.makeValid()

        self.report.step(
            f"Estimated Property Boundary from {parcel_count} selected parcel(s) "
            f"in: {', '.join(sources)}")
        rect = boundary.boundingBox()
        self.report.line(
            f"    bounding box {rect.width():,.0f} x {rect.height():,.0f} units, "
            f"area {boundary.area():,.0f} square units")
        return boundary

    # -- the three AOIs -------------------------------------------------------
    def _build_aois(self, boundary: QgsGeometry) -> None:
        """Project AOI from the boundary's bounding box; region AOI a square.

        The LiDAR AOI is a copy of the project AOI, written to its own file so
        it can be edited later without disturbing the project AOI.
        """
        rect = boundary.boundingBox()
        center_x = rect.center().x()
        center_y = rect.center().y()

        if self.settings.aoi_method == 0:
            factor = self.settings.aoi_multiplier
            width = rect.width() * factor
            height = rect.height() * factor
            how = f"{factor:g}x the bounding box"
        else:
            pad = convert_distance(self.settings.aoi_buffer,
                                   self.settings.distance_unit, self.ctx.crs)
            width = rect.width() + 2 * pad
            height = rect.height() + 2 * pad
            how = f"the bounding box buffered by {pad:,.0f} units"

        self.ctx.aoi_project = rectangle_geometry(center_x, center_y, width, height)
        self.ctx.aoi_lidar = QgsGeometry(self.ctx.aoi_project)

        # 17 inches at 1 inch = <scale> units fills the long side of an 11x17
        # sheet, so a square that size works in either orientation.
        side = convert_distance(
            self.settings.region_scale * SHEET_LONG_SIDE_INCHES,
            self.settings.distance_unit, self.ctx.crs)
        self.ctx.aoi_region = rectangle_geometry(center_x, center_y, side, side)

        self.report.step("Areas of interest")
        self.report.line(f"    project : {width:,.0f} x {height:,.0f} units ({how})")
        self.report.line("    lidar   : copy of the project AOI")
        self.report.line(
            f"    region  : {side:,.0f} x {side:,.0f} units "
            f"(1 in = {self.settings.region_scale:,.0f} on an 11x17 sheet)")

    def _write_site_layers(self) -> None:
        """Write the boundary and AOIs, then repoint their placeholder layers."""
        to_write = [
            ("boundary", self.ctx.boundary_geom),
            ("aoi_project", self.ctx.aoi_project),
            ("aoi_region", self.ctx.aoi_region),
            ("aoi_lidar", self.ctx.aoi_lidar),
        ]
        for key, geom in to_write:
            path = self.ctx.namer.build(
                self.ctx.local_layers_dir, SITE_LAYER_FILENAMES[key], ".gpkg")
            table = gpkg_table_name(SITE_LAYER_FILENAMES[key])
            self.ctx.outputs[key] = path
            if self.ctx.dry_run:
                self.report.line(f"    would write {os.path.basename(path)}")
                continue
            write_single_polygon(path, table, geom, self.ctx.crs)
            self._repoint_placeholders(key, path, table)

    def _repoint_placeholders(self, key: str, path: str, table: str) -> None:
        """Point the named Site Layers placeholders at a newly written file."""
        targets = find_layers_by_name(self.ctx.project, SITE_LAYER_TARGETS[key])
        if not targets:
            self.report.warn(
                f"No placeholder layer found for {key} "
                f"(expected one of: {', '.join(SITE_LAYER_TARGETS[key])}). "
                f"The file was still written to {os.path.basename(path)}.")
            return
        for layer in targets:
            repoint_layer(layer, path, table, "ogr", self.report)
            self.report.line(
                f"    {layer.name()} -> {os.path.basename(path)}")

    # -- derived site layers (watershed subbasins, and anything like them) ----
    def _build_derived_site_layers(self, feedback) -> None:
        """Pull features from a group that touch the property boundary.

        Every layer in the source group is searched, so a site that bridges
        two states picks up subbasins from both and merges them into one file.
        """
        for rule in DERIVED_SITE_LAYERS:
            group = self.ctx.project.layerTreeRoot().findGroup(rule.source_group)
            if group is None:
                self.report.warn(
                    f"Group '{rule.source_group}' not found, so {rule.description} "
                    "could not be built.")
                continue

            matches: List[Tuple[QgsFeature, QgsVectorLayer]] = []
            template_layer: Optional[QgsVectorLayer] = None

            for layer in layers_in_group(group):
                if not isinstance(layer, QgsVectorLayer) or not layer_has_geometry(layer):
                    continue
                boundary_here = transform_geometry(
                    self.ctx.boundary_geom, self.ctx.crs, layer.crs(), self.ctx.project)
                engine = QgsGeometry.createGeometryEngine(boundary_here.constGet())
                engine.prepareGeometry()
                request = QgsFeatureRequest().setFilterRect(boundary_here.boundingBox())
                for feature in layer.getFeatures(request):
                    geom = feature.geometry()
                    if geom.isEmpty():
                        continue
                    if not geom.isGeosValid():
                        geom = geom.makeValid()
                    if engine.intersects(geom.constGet()):
                        matches.append((feature, layer))
                        if template_layer is None:
                            template_layer = layer

            self._finish_derived_layer(rule, matches, template_layer)

    def _finish_derived_layer(self, rule: DerivedSiteLayer,
                              matches: List[Tuple[QgsFeature, QgsVectorLayer]],
                              template_layer: Optional[QgsVectorLayer]) -> None:
        """Write the matched features, or clear the placeholders if none."""
        targets = find_layers_by_name(self.ctx.project, SITE_LAYER_TARGETS[rule.key])

        if not matches or template_layer is None:
            # No coverage for this site. Clear the placeholders rather than
            # leave them showing a previous project's data.
            self.report.warn(
                f"No {rule.description} found for this site. The placeholder "
                f"layer(s) have been emptied so the gap is obvious.")
            if not self.ctx.dry_run:
                for layer in targets:
                    unlink_layer(layer, self.report)
            return

        path = self.ctx.namer.build(
            self.ctx.local_layers_dir, SITE_LAYER_FILENAMES[rule.key], ".gpkg")
        table = gpkg_table_name(SITE_LAYER_FILENAMES[rule.key])
        self.ctx.outputs[rule.key] = path

        self.report.step(
            f"{rule.description}: {len(matches)} feature(s) kept -> "
            f"{os.path.basename(path)}")
        if self.ctx.dry_run:
            return

        # Fields come from whichever layer matched first. Features from a
        # second layer are written positionally against those fields.
        keep_indices, out_fields, _renamed = build_output_fields(template_layer)
        wkb_type = QgsWkbTypes.multiType(template_layer.wkbType())
        writer = create_gpkg_writer(path, out_fields, wkb_type, self.ctx.crs, table)

        for feature, layer in matches:
            geom = transform_geometry(
                feature.geometry(), layer.crs(), self.ctx.crs, self.ctx.project)
            geom.convertToMultiType()
            out = QgsFeature(out_fields)
            out.setGeometry(geom)
            if layer is template_layer:
                out.setAttributes([feature.attributes()[i] for i in keep_indices])
            else:
                out.setAttributes(self._match_attributes(
                    feature, layer, out_fields))
            writer.addFeature(out)

        error, message = writer.hasError(), writer.errorMessage()
        del writer
        if error != WRITER_NO_ERROR:
            raise QgsProcessingException(f"Write error on {path}: {message}")

        for layer in targets:
            repoint_layer(layer, path, table, "ogr", self.report)
            self.report.line(f"    {layer.name()} -> {os.path.basename(path)}")

    @staticmethod
    def _match_attributes(feature: QgsFeature, layer: QgsVectorLayer,
                          out_fields: QgsFields) -> List:
        """Line up a feature from a second layer against the output fields.

        Matching is by field name; anything missing is left null. Subbasin
        layers from different states rarely share a schema.
        """
        values = []
        source_fields = layer.fields()
        for out_field in out_fields:
            index = source_fields.indexFromName(out_field.name())
            values.append(feature.attributes()[index] if index >= 0 else None)
        return values

    # -- pruning --------------------------------------------------------------
    def _prune_layers_outside_aois(self) -> None:
        """Remove scale-group layers that have nothing inside their AOI.

        An optimisation, not a correctness step: Step 2 removes anything that
        comes out empty anyway. So this errs heavily towards keeping. Two
        rules matter most:

        * A layer already pointing inside the project folder is never pruned.
          It is finished work from an earlier run, and judging it again can
          only do harm.
        * A prune is confirmed against the data whenever the source has a
          spatial index, rather than trusting extents alone.
        """
        verb = "would be removed" if self.ctx.dry_run else "removed"
        self.report.step("Pruning layers that have nothing inside their AOI")

        # Snapshot the joins first. Once a soils polygon layer is removed its
        # join goes with it, and the tabular half would no longer look like an
        # orphan, so it would be copied in full.
        joins_before, join_tables_before = collect_joins(self.ctx.project)

        removed: List[str] = []
        kept_local = 0

        for group in top_level_groups(self.ctx.project):
            aoi = self._aoi_for_group(group.name())
            if aoi is None:
                continue
            for layer in layers_in_group(group):
                if is_streaming(layer) or not layer_has_geometry(layer):
                    continue
                if source_is_inside(layer, self.ctx.project_dir):
                    kept_local += 1
                    continue

                outside, why = layer_is_outside_aoi(
                    layer, aoi, self.ctx.crs, self.ctx.project)
                if not outside:
                    continue

                removed.append(f"{layer.name()}  (in {group.name()})")
                self.report.line(f"      reason: {why}")
                self.ctx.pruned_layer_ids.add(layer.id())
                if not self.ctx.dry_run:
                    self.ctx.project.removeMapLayer(layer.id())

        if kept_local:
            self.report.line(f"    {kept_local} layer(s) already local, left "
                             "alone")
        removed += self._prune_orphaned_join_tables(joins_before,
                                                    join_tables_before)

        if removed:
            self.report.info(f"    {len(removed)} layer(s) {verb}:")
            for name in removed:
                self.report.info(f"      - {name}")
        else:
            self.report.line("    nothing to prune")

    def _prune_orphaned_join_tables(self, specs: List[JoinSpec],
                                    table_ids: Set[str]) -> List[str]:
        """Drop join tables whose only parent layers have just been pruned.

        Without this the tabular half of an out-of-state soils pair survives
        alone. Nothing references it, so nothing tells Step 2 which rows
        matter, and the whole table gets copied. The join list has to be the
        one captured before pruning, because the joins themselves disappear
        along with their layers.
        """
        still_used = {spec.join_layer_id for spec in specs
                      if spec.target_id not in self.ctx.pruned_layer_ids}

        orphans: List[str] = []
        for table_id in table_ids - still_used:
            table = self.ctx.project.mapLayer(table_id)
            if table is None or table_id in self.ctx.pruned_layer_ids:
                continue
            orphans.append(f"{table.name()}  (join table, its layer was pruned)")
            self.ctx.pruned_layer_ids.add(table_id)
            if not self.ctx.dry_run:
                self.ctx.project.removeMapLayer(table_id)
        return orphans

    def _aoi_for_group(self, group_name: str) -> Optional[QgsGeometry]:
        """Which AOI applies to a top-level group, by keyword in its name."""
        lowered = group_name.lower()
        if PROJECT_SCALE_KEYWORD in lowered:
            return self.ctx.aoi_project
        if REGION_SCALE_KEYWORD in lowered:
            return self.ctx.aoi_region
        return None


def load_aois_from_project(ctx: RunContext) -> bool:
    """Rebuild the AOIs from the Site Layers placeholders.

    Lets Step 2 or Step 3 run on their own in a later session, after Step 1
    has already written and repointed those layers.
    """
    def geometry_of(names: Sequence[str]) -> Optional[QgsGeometry]:
        for layer in find_layers_by_name(ctx.project, names):
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            geoms = [f.geometry() for f in layer.getFeatures()
                     if f.hasGeometry() and not f.geometry().isEmpty()]
            if not geoms:
                continue
            merged = QgsGeometry.unaryUnion(geoms)
            return transform_geometry(merged, layer.crs(), ctx.crs, ctx.project)
        return None

    ctx.boundary_geom = geometry_of(SITE_LAYER_TARGETS["boundary"])
    ctx.aoi_project = geometry_of(SITE_LAYER_TARGETS["aoi_project"])
    ctx.aoi_region = geometry_of(SITE_LAYER_TARGETS["aoi_region"])
    ctx.aoi_lidar = geometry_of(SITE_LAYER_TARGETS["aoi_lidar"]) or ctx.aoi_project
    return ctx.aoi_project is not None and ctx.aoi_region is not None


# =============================================================================
# SECTION 2 - STEP 2: LOCALIZE LAYERS
#
# Copies the data a project actually needs into the project folder and points
# the layers at it, so the folder can be zipped and sent with no broken links.
#
# Only top-level groups whose name contains "PROJECT SCALE" or "REGION SCALE"
# are touched. Within those:
#   vectors      keep every feature that touches the AOI, whole and unclipped
#   rasters      cut to the AOI rectangle on their own pixel grid
#   join tables  reduced to the rows the kept features reference
#   streaming    left live and logged
# Layers with nothing inside the AOI are removed from the project.
#
# The expensive part is reading the source data, so the extraction is done in
# two stages: a bounding-box request that the provider's spatial index can
# answer, then an exact geometry test only on the features that survive it.
# =============================================================================

# Rough constants for the dry run's time estimate. The manifest records real
# timings, so these can be recalibrated from an actual run on your hardware.
EST_SCAN_MB_PER_SEC = 55.0        # reading an unindexed source front to back
EST_INDEXED_MB_PER_SEC = 400.0    # reading an indexed source by seeking
EST_SEC_PER_FEATURE = 0.00005     # per candidate feature, test and write
EST_RASTER_MB_PER_SEC = 25.0      # windowed read and write through GDAL
GPKG_SIZE_FACTOR = 0.6            # GeoPackage is usually smaller than shapefile
BIG_OUTPUT_WARN_BYTES = 50 * 1024 * 1024   # flag anything heavier than this


@dataclass(frozen=True)
class DerivedBuffer:
    """A buffer layer generated from other layers after they are localized.

    Generating these locally is both cheaper and smaller than localizing the
    statewide dissolved buffers: those are a single multipart feature covering
    an entire state, several hundred megabytes each.
    """
    target_layer: str          # layer in the project to repoint
    distance_feet: float
    source_name_contains: str  # which layers to buffer, matched on name
    source_name_excludes: Tuple[str, ...]


# "est. wetlands" rather than just "wetland", so the buffers are built from
# the mapped wetlands layers only. A looser match also pulled in the Biomap
# conservation designations and the out-of-state wetlands.
DERIVED_BUFFERS = (
    DerivedBuffer("Est. Wetlands Buffer, 50ft", 50.0, "est. wetlands", ("buffer",)),
    DerivedBuffer("Est. Wetlands Buffer, 100ft", 100.0, "est. wetlands", ("buffer",)),
)

BUFFER_SEGMENTS = 8            # quadrant segments when rounding buffer corners


@dataclass
class LayerPlan:
    """One layer's place in the work queue, with what the dry run estimated."""
    layer: object
    kind: str                     # vector | join_table | raster | streaming
    group_name: str
    aoi: Optional[QgsGeometry]
    source_path: str
    source_bytes: int
    indexed: bool
    feature_count: int
    estimated_candidates: int
    estimated_seconds: float
    estimated_bytes: int
    note: str = ""


@dataclass
class Step2Settings:
    """Step 2 has one switch. The rest are always-on behaviours.

    Spatial indexes, multipart trimming and pruning used to be options. They
    are right in every case we have met, so they are no longer asked about.
    localize_streaming is the hook for the not-yet-written branch that bakes
    web services into local files.
    """
    resume: bool
    build_indexes: bool = True
    trim_multipart: bool = True
    localize_streaming: bool = False


class Step2Localize:
    """Extracts and copies the scale-group data into the project folder."""

    def __init__(self, ctx: RunContext, settings: Step2Settings):
        self.ctx = ctx
        self.settings = settings
        self.report = ctx.report
        self.join_specs: List[JoinSpec] = []
        self.join_table_ids: Set[str] = set()
        # Collected while extracting, used to trim the join tables afterwards.
        self.needed_join_keys: Dict[str, Set[str]] = {}

    # -- entry point ----------------------------------------------------------
    def run(self, context, feedback) -> None:
        self.report.heading("STEP 2 - LOCALIZE LAYERS")

        if self.settings.localize_streaming:
            raise QgsProcessingException(
                "Localizing streaming layers is not implemented yet. Untick that "
                "option; streaming layers will be left live and still work for "
                "anyone with an internet connection.")

        if self.ctx.aoi_project is None or self.ctx.aoi_region is None:
            if not load_aois_from_project(self.ctx):
                raise QgsProcessingException(
                    "The AOIs could not be found. Run Step 1 first, or tick it on "
                    "this run.")

        self.join_specs, self.join_table_ids = collect_joins(self.ctx.project)
        plans = self._build_plan()

        if self.ctx.dry_run:
            self._report_plan(plans)
            return

        self.ctx.ensure_dirs()
        manifest = Manifest(self.ctx.local_layers_dir, self.ctx.fingerprint())
        if self.settings.resume and manifest.load(strict=True):
            self.report.info(f"Resuming: {manifest.done_count} layer(s) already done.")

        if self.settings.build_indexes:
            self._build_missing_indexes(plans, feedback)

        self._process(plans, manifest, feedback)

        self._build_derived_buffers(context, feedback)
        restore_joins(self.ctx.project, self.join_specs, self.report)
        self.report.step("Step 2 complete.")

    # -- planning -------------------------------------------------------------
    def _build_plan(self) -> List[LayerPlan]:
        """Work out what each layer needs, and roughly what it will cost.

        Spatial layers come first and join tables last, because trimming a
        join table needs the keys collected while extracting its parent.
        """
        plans: List[LayerPlan] = []

        for group in top_level_groups(self.ctx.project):
            aoi = self._aoi_for_group(group.name())
            if aoi is None:
                continue
            for layer in layers_in_group(group):
                # Already pruned by Step 1 (or would be, on a dry run).
                if layer.id() in self.ctx.pruned_layer_ids:
                    continue
                # Buffer layers are rebuilt from the localized wetlands at the
                # end of the step, so there is no point localizing the
                # statewide originals first.
                if self._is_buffer_target(layer):
                    self.report.info(
                        f"    [{layer.name()}] will be regenerated from the "
                        "localized wetlands layers")
                    continue
                plans.append(self._plan_layer(layer, group.name(), aoi))

        plans.sort(key=lambda p: 1 if p.kind == "join_table" else 0)
        return plans

    def _plan_layer(self, layer, group_name: str, aoi: QgsGeometry) -> LayerPlan:
        path, _inner = decode_source_path(layer)
        size = source_size_bytes(path)

        if "authcfg=" in (layer.source() or ""):
            self.report.warn(
                f"Layer '{layer.name()}' uses a stored credential (authcfg). It "
                "will be broken for anyone who opens this project elsewhere.")

        if is_streaming(layer):
            return LayerPlan(layer, "streaming", group_name, aoi, path, size,
                             True, 0, 0, 0.0, 0,
                             note="streaming service, left live")

        if isinstance(layer, QgsRasterLayer):
            return self._plan_raster(layer, group_name, aoi, path, size)

        if isinstance(layer, QgsVectorLayer):
            if not layer_has_geometry(layer):
                kind = "join_table" if layer.id() in self.join_table_ids else "table"
                return LayerPlan(layer, kind, group_name, aoi, path, size,
                                 True, layer.featureCount(), layer.featureCount(),
                                 max(0.5, size / (1024 * 1024) / EST_SCAN_MB_PER_SEC),
                                 min(size, 2 * 1024 * 1024))
            return self._plan_vector(layer, group_name, aoi, path, size)

        return LayerPlan(layer, "other", group_name, aoi, path, size,
                         True, 0, 0, 0.0, 0,
                         note=f"layer type not handled ({layer.type()})")

    def _plan_vector(self, layer, group_name, aoi, path, size) -> LayerPlan:
        indexed = has_spatial_index(layer, path)
        count = max(layer.featureCount(), 0)
        extent = layer.extent()

        # Guess how many features sit near the AOI from how much of the layer's
        # extent it covers. Crude, but it separates "a few" from "most of them".
        overlap = 0.0
        try:
            aoi_here = transform_geometry(aoi, self.ctx.crs, layer.crs(),
                                          self.ctx.project)
            if not extent.isEmpty():
                intersection = extent.intersect(aoi_here.boundingBox())
                if not intersection.isEmpty() and extent.width() and extent.height():
                    overlap = ((intersection.width() * intersection.height())
                               / (extent.width() * extent.height()))
        except QgsProcessingException:
            pass

        candidates = max(1, int(count * min(1.0, overlap * 3)))
        size_mb = size / (1024.0 * 1024.0)
        rate = EST_INDEXED_MB_PER_SEC if indexed else EST_SCAN_MB_PER_SEC
        seconds = size_mb / rate + candidates * EST_SEC_PER_FEATURE

        # Features are kept whole, so output size follows the source's own
        # bytes per feature. A layer of one statewide polygon really does copy
        # that whole polygon across.
        per_feature = (size / count) if count else 600
        out_bytes = max(4096, int(candidates * per_feature * GPKG_SIZE_FACTOR))

        return LayerPlan(layer, "vector", group_name, aoi, path, size,
                         indexed, count, candidates, seconds, out_bytes)

    def _plan_raster(self, layer, group_name, aoi, path, size) -> LayerPlan:
        overlap = 0.0
        try:
            aoi_here = transform_geometry(aoi, self.ctx.crs, layer.crs(),
                                          self.ctx.project)
            extent = layer.extent()
            intersection = extent.intersect(aoi_here.boundingBox())
            if not intersection.isEmpty() and extent.width() and extent.height():
                overlap = ((intersection.width() * intersection.height())
                           / (extent.width() * extent.height()))
        except QgsProcessingException:
            pass

        out_bytes = int(size * overlap * 0.9)
        size_mb = size / (1024.0 * 1024.0)
        seconds = max(0.5, size_mb * max(overlap, 0.002) / EST_RASTER_MB_PER_SEC)
        return LayerPlan(layer, "raster", group_name, aoi, path, size,
                         True, 0, 0, seconds, out_bytes)

    def _aoi_for_group(self, group_name: str) -> Optional[QgsGeometry]:
        lowered = group_name.lower()
        if PROJECT_SCALE_KEYWORD in lowered:
            return self.ctx.aoi_project
        if REGION_SCALE_KEYWORD in lowered:
            return self.ctx.aoi_region
        return None

    @staticmethod
    def _is_buffer_target(layer) -> bool:
        wanted = {b.target_layer.strip().lower() for b in DERIVED_BUFFERS}
        return (layer.name() or "").strip().lower() in wanted

    def _report_plan(self, plans: List[LayerPlan]) -> None:
        """The dry run output: what would happen, how big, how long."""
        total_seconds = sum(p.estimated_seconds for p in plans)
        total_bytes = sum(p.estimated_bytes for p in plans)
        unindexed = [p for p in plans
                     if not p.indexed and p.source_path.lower().endswith(".shp")]

        by_kind: Dict[str, List[LayerPlan]] = {}
        for plan in plans:
            by_kind.setdefault(plan.kind, []).append(plan)

        for kind in ("vector", "raster", "join_table", "table", "streaming", "other"):
            group = by_kind.get(kind)
            if not group:
                continue
            self.report.line("")
            self.report.line("-" * 78)
            self.report.line(f"{kind.upper()}  ({len(group)} layers)")
            self.report.line("-" * 78)
            for plan in sorted(group, key=lambda p: -p.estimated_seconds):
                detail = [plan.group_name]
                if plan.source_bytes:
                    detail.append(human_size(plan.source_bytes) + " source")
                if plan.feature_count:
                    detail.append(f"{plan.feature_count:,} features")
                if plan.kind == "vector":
                    detail.append(f"~{plan.estimated_candidates:,} candidates")
                    detail.append("indexed" if plan.indexed else "NO SPATIAL INDEX")
                if plan.estimated_bytes:
                    detail.append("~" + human_size(plan.estimated_bytes) + " out")
                if plan.estimated_seconds >= 1:
                    detail.append("est " + human_time(plan.estimated_seconds))
                if plan.note:
                    detail.append(plan.note)
                self.report.line(f"  - {plan.layer.name() or '<unnamed>'}")
                self.report.line(f"      {'  |  '.join(detail)}")

        heavy = [p for p in plans
                 if p.estimated_bytes >= BIG_OUTPUT_WARN_BYTES]
        if heavy:
            self.report.line("")
            self.report.line("-" * 78)
            self.report.line(f"LAYERS THAT WILL DOMINATE THE PACKAGE ({len(heavy)})")
            self.report.line("-" * 78)
            self.report.line("Features are kept whole, not clipped, so a layer made of")
            self.report.line("a few very large polygons copies those polygons in full.")
            self.report.line("These figures ignore multipart trimming, so a dissolved")
            self.report.line("statewide layer will usually come out far smaller.")
            for plan in sorted(heavy, key=lambda p: -p.estimated_bytes):
                self.report.line(
                    f"  - {plan.layer.name()}  ~{human_size(plan.estimated_bytes)}"
                    f"  (from {plan.feature_count:,} features, "
                    f"~{plan.estimated_candidates:,} kept)")

        if unindexed:
            self.report.line("")
            self.report.line("-" * 78)
            self.report.line(f"SHAPEFILES WITH NO SPATIAL INDEX ({len(unindexed)})")
            self.report.line("-" * 78)
            self.report.line("These must be read front to back, which is the slowest")
            self.report.line("part of the run. Leave 'Build missing spatial indexes'")
            self.report.line("ticked and the script will create a .qix for each one.")
            for plan in sorted(unindexed, key=lambda p: -p.source_bytes):
                self.report.line(
                    f"  - {plan.layer.name()}  ({human_size(plan.source_bytes)})")

        self.report.line("")
        self.report.line("Estimated output size : " + human_size(total_bytes))
        self.report.line("Estimated runtime     : " + human_time(total_seconds))
        self.report.line("The runtime is a rough guide. It depends heavily on disk and")
        self.report.line("network speed and on whether the source data is cached locally.")

    # -- spatial indexes ------------------------------------------------------
    def _build_missing_indexes(self, plans: List[LayerPlan], feedback) -> None:
        """Create .qix files for unindexed shapefiles that have geometry."""
        targets = [p for p in plans
                   if p.kind == "vector" and not p.indexed
                   and p.source_path.lower().endswith(".shp")]
        if not targets:
            return
        self.report.step(f"Building spatial indexes for {len(targets)} shapefile(s). "
                         "This is slow the first time and permanent afterwards.")
        for plan in targets:
            if feedback.isCanceled():
                return
            self.report.line(f"    indexing {plan.layer.name()} "
                             f"({human_size(plan.source_bytes)})")
            if build_spatial_index(plan.layer, self.report):
                plan.indexed = True

    # -- the work -------------------------------------------------------------
    def _process(self, plans: List[LayerPlan], manifest: Manifest, feedback) -> None:
        total = len(plans)
        counts = {"done": 0, "removed": 0, "left": 0, "failed": 0, "resumed": 0}

        for index, plan in enumerate(plans):
            if feedback.isCanceled():
                self.report.warn("Cancelled. Re-run with Resume ticked to continue.")
                return

            layer = plan.layer
            name = layer.name() or "<unnamed>"
            feedback.setProgress(100.0 * index / max(total, 1))
            feedback.setProgressText(f"[{index + 1}/{total}] {name}")

            # Already written on an earlier run: just make sure it points there.
            entry = manifest.get(layer.id())
            if (entry and entry.get("status") == "done"
                    and os.path.exists(entry.get("output", ""))):
                try:
                    repoint_layer(layer, entry["output"], entry.get("table"),
                                  entry.get("provider", "ogr"), self.report)
                    counts["resumed"] += 1
                    continue
                except QgsProcessingException as exc:
                    self.report.warn(f"Could not repoint '{name}': {exc}")

            started = time.time()
            try:
                result = self._process_one(plan, feedback)
            except QgsProcessingException as exc:
                self.report.warn(f"FAILED on '{name}': {exc}")
                self.ctx.not_localized.append((name, f"failed: {exc}"))
                counts["failed"] += 1
                manifest.record(layer.id(), name=name, status="failed", error=str(exc))
                continue
            except Exception as exc:                       # noqa: BLE001
                import traceback
                self.report.warn(f"FAILED on '{name}': {exc}")
                feedback.pushDebugInfo(traceback.format_exc())
                self.ctx.not_localized.append((name, f"failed: {exc}"))
                counts["failed"] += 1
                manifest.record(layer.id(), name=name, status="failed", error=str(exc))
                continue

            elapsed = time.time() - started
            status = result["status"]

            if status == "empty":
                self.report.step(f"[{name}] nothing inside the AOI, layer removed")
                counts["removed"] += 1
                manifest.record(layer.id(), name=name, status="removed",
                                seconds=round(elapsed, 1))
                self.ctx.project.removeMapLayer(layer.id())

            elif status == "done":
                size = (os.path.getsize(result["output"])
                        if os.path.isfile(result["output"]) else 0)
                self.report.step(
                    f"[{name}] {result.get('detail', '')} -> "
                    f"{os.path.basename(result['output'])} "
                    f"({human_size(size)}, {human_time(elapsed)})")
                counts["done"] += 1
                manifest.record(layer.id(), name=name, status="done",
                                output=result["output"], table=result.get("table"),
                                provider=result.get("provider", "ogr"),
                                bytes=size, seconds=round(elapsed, 1))

            else:
                note = result.get("note", "left as is")
                self.report.info(f"    [{name}] {note}")
                self.ctx.not_localized.append((name, note))
                counts["left"] += 1
                manifest.record(layer.id(), name=name, status="left",
                                note=result.get("note", ""))

        self.report.line("")
        self.report.info(
            f"Localized {counts['done']}, resumed {counts['resumed']}, "
            f"removed {counts['removed']}, left {counts['left']}, "
            f"failed {counts['failed']}")

    # -- buffers generated from the localized data ---------------------------
    def _build_derived_buffers(self, context, feedback) -> None:
        """Buffer the localized wetlands and repoint the buffer layers.

        Runs after the main loop so the wetlands layers already point at their
        small local copies. Buffering the statewide originals would not finish
        in reasonable time.

        Each source layer is buffered with DISSOLVE on, which collapses its
        features inside the algorithm, and only the handful of results are
        combined afterwards. That is far quicker than unioning sixty thousand
        geometries in Python first, and gives the same answer: the buffer of a
        union equals the union of the buffers.
        """
        for spec in DERIVED_BUFFERS:
            if feedback.isCanceled():
                return
            targets = find_layers_by_name(self.ctx.project, [spec.target_layer])
            if not targets:
                continue          # the template has no layer for this buffer

            sources = self._buffer_sources(spec)
            if not sources:
                self.report.warn(
                    f"No wetlands layers were found to build "
                    f"'{spec.target_layer}' from, so it was left unchanged.")
                self.ctx.not_localized.append(
                    (spec.target_layer, "no source wetlands layers survived"))
                continue

            pieces: List[QgsGeometry] = []
            counted = 0
            for layer in sources:
                counted += max(layer.featureCount(), 0)
                piece = self._dissolved_buffer(layer, spec.distance_feet,
                                               context, feedback)
                if piece is not None and not piece.isEmpty():
                    pieces.append(piece)

            if not pieces:
                self.report.warn(
                    f"The wetlands layers hold no features here, so "
                    f"'{spec.target_layer}' was left unchanged.")
                self.ctx.not_localized.append(
                    (spec.target_layer, "no wetland features inside the AOI"))
                continue

            merged = pieces[0] if len(pieces) == 1 else QgsGeometry.unaryUnion(pieces)
            if merged.isEmpty():
                self.report.warn(f"Buffering produced nothing for "
                                 f"'{spec.target_layer}'.")
                continue

            path = self.ctx.namer.build(
                self.ctx.local_layers_dir, spec.target_layer, ".gpkg")
            table = gpkg_table_name(sanitize_filename(spec.target_layer))
            write_single_polygon(path, table, merged, self.ctx.crs)

            for layer in targets:
                repoint_layer(layer, path, table, "ogr", self.report)
            self.report.step(
                f"[{spec.target_layer}] {spec.distance_feet:g} ft buffer of "
                f"{counted:,} feature(s) from {len(sources)} layer(s) -> "
                f"{os.path.basename(path)}")
            self.report.info("    sources: "
                             + ", ".join(l.name() for l in sources))

    def _dissolved_buffer(self, layer: QgsVectorLayer, distance_feet: float,
                          context, feedback) -> Optional[QgsGeometry]:
        """One dissolved buffer polygon for a whole layer, in the project CRS.

        The distance is converted into the layer's own units, since the buffer
        algorithm works in those, and the result is brought back to the
        project CRS afterwards.
        """
        distance = convert_distance(distance_feet, Qgis.DistanceUnit.Feet,
                                    layer.crs())
        try:
            output = processing.run("native:buffer", {
                "INPUT": layer,
                "DISTANCE": distance,
                "SEGMENTS": BUFFER_SEGMENTS,
                "END_CAP_STYLE": 0,      # round
                "JOIN_STYLE": 0,         # round
                "MITER_LIMIT": 2,
                "DISSOLVE": True,
                "OUTPUT": "TEMPORARY_OUTPUT",
            }, context=context, feedback=feedback,
                is_child_algorithm=True)["OUTPUT"]
            buffered = QgsProcessingUtils.mapLayerFromString(output, context)
        except Exception as exc:                            # noqa: BLE001
            self.report.warn(f"Could not buffer '{layer.name()}': {exc}")
            return None

        if buffered is None:
            self.report.warn(f"The buffer of '{layer.name()}' produced nothing.")
            return None

        geoms = [f.geometry() for f in buffered.getFeatures()
                 if f.hasGeometry() and not f.geometry().isEmpty()]
        if not geoms:
            return None
        merged = geoms[0] if len(geoms) == 1 else QgsGeometry.unaryUnion(geoms)
        return transform_geometry(merged, buffered.crs(), self.ctx.crs,
                                  self.ctx.project)

    def _buffer_sources(self, spec: DerivedBuffer) -> List[QgsVectorLayer]:
        """Wetlands layers to buffer: name matches, and is not itself a buffer."""
        found = []
        for layer in self.ctx.project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            if not layer_has_geometry(layer):
                continue
            name = (layer.name() or "").lower()
            if spec.source_name_contains not in name:
                continue
            if any(word in name for word in spec.source_name_excludes):
                continue
            if layer.geometryType() not in (GEOMETRY_POLYGON, GEOMETRY_LINE):
                continue
            found.append(layer)
        return sorted(found, key=lambda l: l.name() or "")

    def _process_one(self, plan: LayerPlan, feedback) -> dict:
        if plan.kind == "vector":
            return self._extract_vector(plan, feedback)
        if plan.kind in ("join_table", "table"):
            return self._copy_table(plan, feedback)
        if plan.kind == "raster":
            return self._clip_raster(plan, feedback)
        return {"status": "left", "note": plan.note or "not handled"}

    # -- vectors --------------------------------------------------------------
    def _extract_vector(self, plan: LayerPlan, feedback) -> dict:
        """Keep whole features that touch the AOI. Nothing is clipped.

        Three tests, cheapest first:
          1. the provider's bounding-box request, answered by the index
          2. feature bbox entirely inside the AOI rectangle, so it is kept
             without any geometry work at all
          3. a prepared-geometry intersects test, only for the few features
             that straddle the AOI edge
        """
        layer = plan.layer
        if not layer.crs().isValid():
            return {"status": "left", "note": "layer has no valid CRS"}

        aoi_here = transform_geometry(plan.aoi, self.ctx.crs, layer.crs(),
                                      self.ctx.project)
        search_rect = aoi_here.boundingBox()

        # The same cautious test Step 1 uses. A plain extent comparison is
        # not safe on a layer that has been repointed, because the extent it
        # reports may still belong to its previous source.
        outside, _why = layer_is_outside_aoi(layer, plan.aoi, self.ctx.crs,
                                             self.ctx.project)
        if outside:
            return {"status": "empty"}

        engine = QgsGeometry.createGeometryEngine(aoi_here.constGet())
        engine.prepareGeometry()

        keep_indices, out_fields, renamed = build_output_fields(layer)
        if renamed:
            self.report.warn(
                f"Layer '{layer.name()}' has a field called '{renamed}'. "
                "GeoPackage reserves that name, so it was written as 'fid_src'.")

        wkb_type = QgsWkbTypes.multiType(layer.wkbType())
        path = self.ctx.namer.build(self.ctx.local_layers_dir, layer.name(), ".gpkg")
        table = gpkg_table_name(sanitize_filename(layer.name()))
        writer = create_gpkg_writer(path, out_fields, wkb_type, self.ctx.crs, table)

        to_project = None
        if layer.crs() != self.ctx.crs:
            to_project = QgsCoordinateTransform(layer.crs(), self.ctx.crs,
                                                self.ctx.project)

        # Any provider filter is applied by getFeatures, which is how the
        # filter gets baked into the output.
        request = QgsFeatureRequest().setFilterRect(search_rect)
        request.setSubsetOfAttributes(keep_indices)

        join_key = self._join_key_for(layer)
        written = scanned = trimmed = 0

        for feature in layer.getFeatures(request):
            if feedback.isCanceled():
                del writer
                raise QgsProcessingException(f"Cancelled during '{layer.name()}'")
            scanned += 1
            if scanned % 50000 == 0:
                feedback.pushInfo(f"    ...{scanned:,} scanned, {written:,} kept")

            geom = feature.geometry()
            if geom.isEmpty():
                continue

            # Fast path: a feature whose bbox is inside the AOI must intersect.
            bbox = geom.boundingBox()
            if not search_rect.contains(bbox):
                if not geom.isGeosValid():
                    geom = geom.makeValid()
                    if geom.isEmpty():
                        continue
                try:
                    if not engine.intersects(geom.constGet()):
                        continue
                except Exception:
                    continue

            out_geom = QgsGeometry(geom)

            # A dissolved statewide layer is often one multipart feature whose
            # parts cover the whole state. Keeping only the parts that reach
            # the AOI drops the distant ones without ever cutting a part, so
            # every piece that is kept is still whole.
            if self.settings.trim_multipart and out_geom.isMultipart():
                out_geom, dropped = self._parts_touching(out_geom, engine, search_rect)
                if out_geom is None:
                    continue
                trimmed += dropped

            if to_project is not None and out_geom.transform(to_project) != 0:
                continue
            if QgsWkbTypes.isMultiType(wkb_type):
                out_geom.convertToMultiType()

            out = QgsFeature(out_fields)
            out.setGeometry(out_geom)
            out.setAttributes([feature.attributes()[i] for i in keep_indices])
            writer.addFeature(out)
            written += 1

            if join_key:
                value = feature.attribute(join_key["field"])
                if value is not None:
                    self.needed_join_keys.setdefault(
                        join_key["join_layer_id"], set()).add(
                            str(value).strip().lower())

        error, message = writer.hasError(), writer.errorMessage()
        del writer
        if error != WRITER_NO_ERROR:
            raise QgsProcessingException(f"Write error on {path}: {message}")

        if written == 0:
            self._discard(path)
            return {"status": "empty"}

        repoint_layer(layer, path, table, "ogr", self.report)
        embed_style_in_gpkg(layer, self.report)
        detail = f"{written:,} of {scanned:,} features"
        if trimmed:
            detail += f", {trimmed:,} distant parts dropped"
        return {"status": "done", "output": path, "table": table, "provider": "ogr",
                "detail": detail}

    @staticmethod
    def _parts_touching(geom: QgsGeometry, engine, aoi_rect: QgsRectangle):
        """Rebuild a multipart geometry from only the parts reaching the AOI.

        Returns (geometry, parts dropped), or (None, n) if no part reaches it.
        The geometry comes back untouched when every part is wanted, so the
        common case costs one comparison.
        """
        parts = geom.asGeometryCollection()
        if len(parts) < 2:
            return geom, 0

        kept = []
        for part in parts:
            if not aoi_rect.intersects(part.boundingBox()):
                continue
            if not part.isGeosValid():
                part = part.makeValid()
                if part.isEmpty():
                    continue
            try:
                if engine.intersects(part.constGet()):
                    kept.append(part)
            except Exception:
                continue

        dropped = len(parts) - len(kept)
        if not kept:
            return None, dropped
        if not dropped:
            return geom, 0
        rebuilt = QgsGeometry.collectGeometry(kept)
        return (rebuilt if not rebuilt.isEmpty() else None), dropped

    def _join_key_for(self, layer) -> Optional[dict]:
        """If this layer drives a join, which field holds the key."""
        joins = layer.vectorJoins()
        if not joins:
            return None
        return {"field": joins[0].targetFieldName(),
                "join_layer_id": joins[0].joinLayerId()}

    # -- tables ---------------------------------------------------------------
    def _copy_table(self, plan: LayerPlan, feedback) -> dict:
        """Copy a non-spatial table, trimmed to the keys the site needs.

        The soils tabular files run to several gigabytes each, and a project
        only ever references a few hundred map unit symbols.
        """
        layer = plan.layer
        wanted = self.needed_join_keys.get(layer.id())
        join_field = next((s.join_field for s in self.join_specs
                           if s.join_layer_id == layer.id()), None)

        if wanted is not None and not wanted:
            return {"status": "empty"}

        keep_indices, out_fields, _renamed = build_output_fields(layer)
        path = self.ctx.namer.build(self.ctx.local_layers_dir, layer.name(), ".gpkg")
        table = gpkg_table_name(sanitize_filename(layer.name()))
        writer = create_gpkg_writer(path, out_fields, WKB_NO_GEOMETRY,
                                    QgsCoordinateReferenceSystem(), table)

        written = 0
        request = QgsFeatureRequest().setSubsetOfAttributes(keep_indices)
        for feature in layer.getFeatures(request):
            if feedback.isCanceled():
                del writer
                raise QgsProcessingException(f"Cancelled during '{layer.name()}'")
            if wanted is not None and join_field:
                value = feature.attribute(join_field)
                if value is None or str(value).strip().lower() not in wanted:
                    continue
            out = QgsFeature(out_fields)
            out.setAttributes([feature.attributes()[i] for i in keep_indices])
            writer.addFeature(out)
            written += 1

        error, message = writer.hasError(), writer.errorMessage()
        del writer
        if error != WRITER_NO_ERROR:
            raise QgsProcessingException(f"Write error on {path}: {message}")

        if written == 0:
            self._discard(path)
            return {"status": "empty"}

        repoint_layer(layer, path, table, "ogr", self.report)
        return {"status": "done", "output": path, "table": table, "provider": "ogr",
                "detail": f"{written:,} rows"}

    # -- rasters --------------------------------------------------------------
    def _clip_raster(self, plan: LayerPlan, feedback) -> dict:
        """Cut a raster on its own pixel grid: no resampling, no reprojection.

        The window is computed in pixel coordinates and passed to GDAL as a
        srcWin, so data type, band count, pixel size, nodata value and colour
        table all come through unchanged.
        """
        if not HAVE_GDAL:
            return {"status": "left", "note": "GDAL Python bindings unavailable"}

        layer = plan.layer
        path = plan.source_path
        if not path or not os.path.exists(path):
            return {"status": "left", "note": "source is not a readable file"}

        dataset = gdal.Open(path)
        if dataset is None:
            return {"status": "left", "note": "GDAL could not open the source"}

        transform = dataset.GetGeoTransform()
        if transform is None or transform[2] or transform[4]:
            dataset = None
            return {"status": "left", "note": "rotated raster, not handled"}

        aoi_here = transform_geometry(plan.aoi, self.ctx.crs, layer.crs(),
                                      self.ctx.project)
        rect = aoi_here.boundingBox()
        origin_x, pixel_w, _, origin_y, _, pixel_h = transform

        cols = [(rect.xMinimum() - origin_x) / pixel_w,
                (rect.xMaximum() - origin_x) / pixel_w]
        rows = [(rect.yMaximum() - origin_y) / pixel_h,
                (rect.yMinimum() - origin_y) / pixel_h]

        col_min = max(0, int(math.floor(min(cols))))
        col_max = min(dataset.RasterXSize, int(math.ceil(max(cols))))
        row_min = max(0, int(math.floor(min(rows))))
        row_max = min(dataset.RasterYSize, int(math.ceil(max(rows))))

        if col_max <= col_min or row_max <= row_min:
            dataset = None
            return {"status": "empty"}

        band = dataset.GetRasterBand(1)
        data_type = gdal.GetDataTypeName(band.DataType)
        options = ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
        if band.GetColorTable() is None:
            options.append("PREDICTOR=" +
                           ("3" if data_type in ("Float32", "Float64") else "2"))

        out_path = self.ctx.namer.build(self.ctx.local_layers_dir,
                                        layer.name(), ".tif")
        gdal.Translate(
            out_path, dataset,
            srcWin=[col_min, row_min, col_max - col_min, row_max - row_min],
            creationOptions=options,
            callback=lambda pct, msg, obj: not feedback.isCanceled())
        dataset = None

        if not os.path.exists(out_path):
            raise QgsProcessingException(
                f"gdal.Translate produced no output for '{layer.name()}'.")

        repoint_layer(layer, out_path, None, "gdal", self.report)
        return {"status": "done", "output": out_path, "provider": "gdal",
                "detail": f"{col_max - col_min} x {row_max - row_min} px"}

    def _discard(self, path: str) -> None:
        """Delete an output that turned out to have nothing in it."""
        try:
            os.remove(path)
        except OSError:
            pass
        self.ctx.namer.release(path)


# =============================================================================
# SECTION 3 - STEP 3: LIDAR ANALYSIS
#
# Two halves:
#   A. If a point cloud is given, build the Ground DEM and the All DEM from it
#      (ported from the "Lidar Process Point Data" model). Needs the PDAL
#      Processing provider, which not every QGIS build ships.
#   B. Run the standard RDG terrain analysis on those two DEMs (ported from
#      "Lidar Analysis 220707" and its three sub-models), then repoint the
#      matching layers in RDG ANALYSIS at the results.
#
# Requires: SAGA NextGen (sagang:) and WhiteboxTools (wbt:) Processing
# providers, plus PDAL (pdal:) for the point cloud half. Availability is
# checked at run time rather than by QGIS version number, because which
# providers are present depends on how QGIS was built and what is installed.
# =============================================================================

# Which output feeds which template layers, matched on the START of the layer
# name rather than the whole of it. "Hillshade" therefore claims both
# "Hillshade" and "Hillshade Deepen", and adding "Hillshade Soft" to the
# template needs no change here. Any layer in the fallback group that no
# output claims is listed as a warning at the end of the step.
LIDAR_LAYER_MAP = {
    "hillshade":     ["Hillshade"],
    "slope":         ["Slope"],
    "aspect":        ["Aspect"],
    "contours":      ["Topography"],
    "drainage":      ["Drainage"],
    "TreeCanopy":    ["Tree Canopy"],
    "HighPoint":     ["High Point"],
    "LowPoint":      ["Low Point"],
    "DOI_Equinox":   ["Equinox"],
    "DOI_Summer":    ["Summer Solstice"],
    "DOI_Winter":    ["Winter Solstice"],
    "Wetness Index": ["Wetness Index"],
}

# Group that new layers go into when no placeholder exists for an output.
LIDAR_FALLBACK_GROUP = "RDG ANALYSIS"

# Processing providers this step relies on, with what each is used for.
REQUIRED_PROVIDERS = {
    "sagang:gaussianfilter": "SAGA NextGen (smoothing, drainages, insolation)",
}

# High and low points come from Whitebox, but which plugin provides them
# depends on the QGIS version. QGIS 4 ships "Whitebox Workflows", a different
# engine from the older "WhiteboxTools". Both are tried, newest first.
#
# The Workflows signature was read from the running algorithm on QGIS 4.0.3:
#   input    QgsProcessingParameterRasterLayer
#   out_type QgsProcessingParameterEnum  ['lowest', 'highest', 'both']
#   output   QgsProcessingParameterVectorDestination
# The older WhiteboxTools tool took the same three names and enum order.
WHITEBOX_HIGH_LOW = (
    ("whitebox_workflows:find_lowest_or_highest_points",
     {"input": "input", "out_type": "out_type", "output": "output",
      "lowest": 0, "highest": 1}),
    ("wbt:FindLowestOrHighestPoints",
     {"input": "input", "out_type": "out_type", "output": "output",
      "lowest": 0, "highest": 1}),
)
PDAL_PROVIDERS = {
    "pdal:reproject": "PDAL point cloud tools",
    "pdal:exportrastertin": "PDAL point cloud tools",
}


@dataclass
class Step3Settings:
    ground_dem: Optional[QgsRasterLayer]
    all_dem: Optional[QgsRasterLayer]
    point_cloud: object                  # QgsPointCloudLayer or None
    ground_expression: str
    dem_resolution: float                # applied to both DEMs
    scale_z_to_feet: bool


class Step3Lidar:
    """Builds the DEMs if needed, runs the terrain analysis, repoints layers."""

    def __init__(self, ctx: RunContext, settings: Step3Settings):
        self.ctx = ctx
        self.settings = settings
        self.report = ctx.report
        self.outputs: Dict[str, str] = {}   # map key -> written file path
        self._whitebox: Optional[Tuple[str, Dict[str, str]]] = None

    # -- entry point ----------------------------------------------------------
    def run(self, context, feedback) -> None:
        self.report.heading("STEP 3 - LIDAR ANALYSIS")
        self._check_providers(bool(self.settings.point_cloud))

        if self.ctx.aoi_lidar is None and not load_aois_from_project(self.ctx):
            raise QgsProcessingException(
                "The LiDAR AOI could not be found. Run Step 1 first, or tick it "
                "on this run.")

        self.ctx.ensure_dirs()

        if self.ctx.dry_run:
            self.report.line("Dry run: Step 3 would write these to "
                             + self.ctx.lidar_dir)
            for key in ("GroundDEM", "AllDEM", *LIDAR_LAYER_MAP):
                self.report.line(f"    {key}_{self.ctx.code}_{self.ctx.date_stamp}")
            return

        ground, all_data = self._resolve_dems(context, feedback)
        self._run_analysis(ground, all_data, context, feedback)
        self._repoint_results()
        self.report.step("Step 3 complete.")

    # -- preflight ------------------------------------------------------------
    def _check_providers(self, needs_pdal: bool) -> None:
        """Fail early and clearly if a Processing provider is missing."""
        registry = QgsApplication.processingRegistry()
        required = dict(REQUIRED_PROVIDERS)
        if needs_pdal:
            required.update(PDAL_PROVIDERS)

        missing = sorted({label for alg_id, label in required.items()
                          if registry.algorithmById(alg_id) is None})

        self._whitebox = next(
            ((alg_id, params) for alg_id, params in WHITEBOX_HIGH_LOW
             if registry.algorithmById(alg_id) is not None), None)
        if self._whitebox is None:
            missing.append(
                "Whitebox (high and low points). Install \"Whitebox Workflows "
                "for QGIS\" on QGIS 4, or \"WhiteboxTools\" on QGIS 3.")

        if missing:
            raise QgsProcessingException(
                "Step 3 needs Processing providers that are not available here:\n  "
                + "\n  ".join(missing)
                + "\n\nSee the script's help text for how to install them.")

    def _out(self, prefix: str, ext: str) -> str:
        """Build an output path and remember it against its mapping key."""
        path = self.ctx.namer.build(self.ctx.lidar_dir, prefix, "." + ext)
        self.outputs[prefix] = path
        return path

    def _run(self, alg_id: str, params: dict, context, feedback) -> dict:
        """Run a child algorithm, sending every unset output to a temp file.

        A graphical model quietly routes unset outputs to temporary files;
        processing.run does not. Every destination is filled, including the
        optional ones: SAGA marks several of its outputs optional, and an
        unset optional output is not an error, it just comes back empty. That
        silently produces an empty path which the next algorithm then refuses
        with "Could not load source layer for INPUT: invalid value".
        """
        if feedback.isCanceled():
            raise QgsProcessingException("Cancelled")
        feedback.pushInfo(f"    {alg_id}")
        try:
            alg = QgsApplication.processingRegistry().algorithmById(alg_id)
            if alg is None:
                raise QgsProcessingException(
                    f"Algorithm '{alg_id}' is not available in this QGIS install.")
            # Provider parameters come and go between QGIS releases. Drop
            # anything this build does not know about rather than failing.
            known = {d.name() for d in alg.parameterDefinitions()}
            for name in [n for n in params if n not in known]:
                params.pop(name)
                feedback.pushDebugInfo(
                    f"    {alg_id}: ignoring parameter '{name}', not present "
                    "in this QGIS version")

            for definition in alg.destinationParameterDefinitions():
                params.setdefault(definition.name(), "TEMPORARY_OUTPUT")
        except QgsProcessingException:
            raise
        except Exception as exc:                            # noqa: BLE001
            feedback.pushWarning(f"Could not pre-fill outputs for {alg_id}: {exc}")

        results = processing.run(alg_id, params, context=context,
                                 feedback=feedback, is_child_algorithm=True)

        # An empty output path means the algorithm ran but wrote nothing. It
        # is better to stop here, naming the algorithm, than to hand an empty
        # string to the next step and get a confusing error there.
        for key, value in results.items():
            if isinstance(value, str) and not value.strip():
                raise QgsProcessingException(
                    f"{alg_id} returned an empty '{key}'. The algorithm ran but "
                    "produced no file.")
        return results

    # -- A. DEMs from a point cloud -------------------------------------------
    def _resolve_dems(self, context, feedback):
        """Return (ground DEM, all DEM), building them from a cloud if needed."""
        if self.settings.point_cloud is None:
            if self.settings.ground_dem is None or self.settings.all_dem is None:
                raise QgsProcessingException(
                    "Step 3 needs either both DEMs, or a point cloud to build them "
                    "from.")
            self.report.step("Using the Ground DEM and All DEM supplied.")
            return self.settings.ground_dem, self.settings.all_dem

        self.report.step("Building the Ground DEM and All DEM from the point cloud")
        return self._dems_from_point_cloud(context, feedback)

    def _dems_from_point_cloud(self, context, feedback):
        """Ported from the Lidar Process Point Data model.

        Chain: reproject to the project CRS, clip to the LiDAR AOI, scale Z
        from metres to feet, then split into a ground surface (triangulated
        from ground-classified points) and an all-returns surface (first
        returns, binned). Both are hole-filled and written as Float32.
        """
        cloud = self.settings.point_cloud
        resolution = self.settings.dem_resolution

        # The clip needs a polygon layer, so write the AOI out first.
        aoi_path = self.ctx.namer.build(self.ctx.lidar_dir, "AOI Lidar Clip", ".gpkg")
        write_single_polygon(aoi_path, "aoi_lidar", self.ctx.aoi_lidar, self.ctx.crs)
        aoi_layer = QgsVectorLayer(f"{aoi_path}|layername=aoi_lidar", "aoi", "ogr")

        # VPC_OUTPUT_FORMAT is deliberately not passed to any pdal step. It
        # only matters for virtual point cloud inputs, and its parameter type
        # changed between QGIS 4.0 and 4.2, so passing it breaks on one or the
        # other. Leaving it unset uses the provider's own default.
        reprojected = self._run("pdal:reproject", {
            "INPUT": cloud, "CRS": self.ctx.crs, "OPERATION": None,
        }, context, feedback)["OUTPUT"]

        clipped = self._run("pdal:clip", {
            "INPUT": reprojected, "OVERLAY": aoi_layer,
            "FILTER_EXPRESSION": None, "FILTER_EXTENT": None,
        }, context, feedback)["OUTPUT"]

        # The model had "ELSE 0" here, which flattens every Z to zero when the
        # checkbox is off. 1 leaves the values untouched, which is the intent.
        scale_z = 3.28084 if self.settings.scale_z_to_feet else 1.0
        transformed = self._run("pdal:transformpointcloud", {
            "INPUT": clipped,
            "SCALE_X": 1, "SCALE_Y": 1, "SCALE_Z": scale_z,
            "ROTATE_X": 0, "ROTATE_Y": 0, "ROTATE_Z": 0,
            "TRANSLATE_X": 0, "TRANSLATE_Y": 0, "TRANSLATE_Z": 0,
        }, context, feedback)["OUTPUT"]

        # Ground surface: ground-classified points, triangulated.
        ground_points = self._run("pdal:filter", {
            "INPUT": transformed,
            "FILTER_EXPRESSION": self.settings.ground_expression,
            "FILTER_EXTENT": None,
        }, context, feedback)["OUTPUT"]

        # The ground-classified cloud is always kept: it is the expensive part
        # to recreate and it is what the DEM is built from.
        ground_cloud_path = self._out("GroundPointCloud", "laz")
        self._run("pdal:convertformat", {
            "INPUT": ground_points, "OUTPUT": ground_cloud_path,
        }, context, feedback)
        self.report.line(f"    ground point cloud -> "
                         f"{os.path.basename(ground_cloud_path)}")

        ground_raw = self._run("pdal:exportrastertin", {
            "INPUT": ground_points, "RESOLUTION": resolution, "TILE_SIZE": 1000,
            "FILTER_EXPRESSION": None, "FILTER_EXTENT": None,
            "ORIGIN_X": None, "ORIGIN_Y": None,
        }, context, feedback)["OUTPUT"]

        # All-returns surface: first returns only, binned at the same grid.
        all_raw = self._run("pdal:exportraster", {
            "INPUT": transformed, "ATTRIBUTE": "Z",
            "RESOLUTION": resolution, "TILE_SIZE": 1000,
            "FILTER_EXPRESSION": "ReturnNumber = 1", "FILTER_EXTENT": None,
            "ORIGIN_X": None, "ORIGIN_Y": None,
        }, context, feedback)["OUTPUT"]

        ground_dem = self._finish_dem(ground_raw, "GroundDEM", context, feedback)
        all_dem = self._finish_dem(all_raw, "AllDEM", context, feedback)
        return ground_dem, all_dem

    def _finish_dem(self, raster, prefix: str, context, feedback) -> QgsRasterLayer:
        """Fill small holes and write a compressed Float32 GeoTIFF."""
        filled = self._run("gdal:fillnodata", {
            "INPUT": raster, "BAND": 1, "DISTANCE": 100, "ITERATIONS": 0,
            "MASK_LAYER": None, "OPTIONS": "", "EXTRA": "",
        }, context, feedback)["OUTPUT"]

        path = self._out(prefix, "tif")
        self._run("gdal:translate", {
            "INPUT": filled, "TARGET_CRS": None, "NODATA": None,
            "COPY_SUBDATASETS": False, "OPTIONS": "COMPRESS=DEFLATE",
            "EXTRA": "", "DATA_TYPE": 6,          # 6 = Float32
            "OUTPUT": path,
        }, context, feedback)

        layer = QgsRasterLayer(path, prefix)
        if not layer.isValid():
            raise QgsProcessingException(f"Could not open the {prefix} just written: "
                                        f"{path}")
        self.report.line(f"    {prefix} -> {os.path.basename(path)}")
        return layer

    # -- B. the terrain analysis ----------------------------------------------
    def _run_analysis(self, ground, all_data, context, feedback) -> None:
        """Ported from Lidar Analysis 220707 plus its three sub-models."""
        units = ground.crs().mapUnits()
        if units not in (Qgis.DistanceUnit.Feet, Qgis.DistanceUnit.FeetUSSurvey):
            raise QgsProcessingException(
                f"The Ground DEM CRS uses {units}. It must be in a CRS measured in "
                "feet, with elevation values in feet.")

        boundary = self._boundary_layer()

        # 1. smoothed ground surface, saved as hillshade_* for historical reasons
        self.report.step("Smoothed ground surface")
        filled = self._run("gdal:fillnodata", {
            "INPUT": ground, "BAND": 1, "DISTANCE": 20, "ITERATIONS": 0,
            "MASK_LAYER": None, "OPTIONS": "", "EXTRA": "",
        }, context, feedback)["OUTPUT"]

        smoothed = self._run("sagang:gaussianfilter", {
            "INPUT": filled, "SIGMA": 1, "KERNEL_TYPE": 1, "KERNEL_RADIUS": 2,
            "RESULT": "TEMPORARY_OUTPUT",
        }, context, feedback)["RESULT"]

        warped = self._run("gdal:warpreproject", {
            "INPUT": smoothed, "SOURCE_CRS": ground.crs(),
            "TARGET_CRS": self.ctx.crs, "RESAMPLING": 0, "NODATA": None,
            "TARGET_RESOLUTION": None, "DATA_TYPE": 0,
            "OPTIONS": "COMPRESS=DEFLATE|PREDICTOR=2|ZLEVEL=9",
            "TARGET_EXTENT": None, "TARGET_EXTENT_CRS": None,
            "MULTITHREADING": True, "EXTRA": "",
            "OUTPUT": self._out("hillshade", "tif"),
        }, context, feedback)["OUTPUT"]

        # 2. slope and aspect off the smoothed surface
        self.report.step("Slope and aspect")
        self._run("gdal:slope", {
            "INPUT": warped, "BAND": 1, "SCALE": 1, "AS_PERCENT": True,
            "COMPUTE_EDGES": False, "ZEVENBERGEN": False, "OPTIONS": "", "EXTRA": "",
            "OUTPUT": self._out("slope", "tif"),
        }, context, feedback)
        self._run("gdal:aspect", {
            "INPUT": warped, "BAND": 1, "TRIG_ANGLE": False, "ZERO_FLAT": False,
            "COMPUTE_EDGES": False, "ZEVENBERGEN": False, "OPTIONS": "", "EXTRA": "",
            "OUTPUT": self._out("aspect", "tif"),
        }, context, feedback)

        # 3. 1 ft contours, simplified
        self.report.step("Contours")
        contours = self._run("gdal:contour", {
            "INPUT": warped, "BAND": 1, "INTERVAL": 1, "OFFSET": 0,
            "FIELD_NAME": "ELEV", "CREATE_3D": True, "IGNORE_NODATA": False,
            "NODATA": None, "EXTRA": "",
        }, context, feedback)["OUTPUT"]
        self._run("native:simplifygeometries", {
            "INPUT": contours, "METHOD": 0, "TOLERANCE": 1,
            "OUTPUT": self._gpkg_out("contours"),
        }, context, feedback)

        self._build_wetness_index(ground, context, feedback)
        self._build_drainages(ground, context, feedback)
        self._build_tree_canopy(warped, all_data, context, feedback)
        self._build_high_low_points(filled, boundary, context, feedback)
        self._build_insolation(all_data, context, feedback)

    def _gpkg_out(self, prefix: str) -> str:
        """Vector outputs go to GeoPackage, to match the rest of the project."""
        return self._out(prefix, "gpkg")

    def _boundary_layer(self) -> QgsVectorLayer:
        """The property boundary, as a layer the clip algorithms can take."""
        for layer in find_layers_by_name(self.ctx.project,
                                         SITE_LAYER_TARGETS["boundary"]):
            if isinstance(layer, QgsVectorLayer) and layer.isValid():
                return layer
        if self.ctx.boundary_geom is None:
            raise QgsProcessingException(
                "No Estimated Property Boundary found. Run Step 1 first.")
        path = self.ctx.namer.build(self.ctx.lidar_dir, "Boundary For Clip", ".gpkg")
        write_single_polygon(path, "boundary", self.ctx.boundary_geom, self.ctx.crs)
        return QgsVectorLayer(f"{path}|layername=boundary", "boundary", "ogr")

    def _build_wetness_index(self, ground, context, feedback) -> None:
        """SAGA Wetness Index off the raw Ground DEM; only the TWI is kept.

        Every option is passed explicitly rather than left to SAGA's defaults,
        because those have changed between SAGA versions (Type of Area was
        "square root" up to SAGA 7 and "specific" from 8.4). Passing them keeps
        the output the same whichever SAGA is installed.
        """
        self.report.step("Wetness index")
        twi = self._run("sagang:sagawetnessindex", {
            "DEM": ground,
            "WEIGHT": None,
            "SUCTION": 10,
            "AREA_TYPE": 2,        # specific catchment area
            "SLOPE_TYPE": 1,       # catchment slope
            "SLOPE_MIN": 0,
            "SLOPE_OFF": 0.1,
            "SLOPE_WEIGHT": 1,
            # AREA, SLOPE and AREA_MOD are the other three outputs. They are
            # not wanted, and _run sends them to temporary files.
        }, context, feedback)["TWI"]

        # SAGA writes .sdat with .sgrd/.mgrd/.prj sidecars. Converting to a
        # compressed GeoTIFF keeps the output folder tidy and consistent with
        # the other rasters.
        self._run("gdal:translate", {
            "INPUT": twi, "TARGET_CRS": None, "NODATA": None,
            "COPY_SUBDATASETS": False,
            "OPTIONS": "COMPRESS=DEFLATE|PREDICTOR=3|ZLEVEL=9",
            "EXTRA": "", "DATA_TYPE": 0,          # 0 = keep the input type
            "OUTPUT": self._out("Wetness Index", "tif"),
        }, context, feedback)

    def _build_drainages(self, ground, context, feedback) -> None:
        """Fill sinks, derive the channel network, give it a CRS, save it.

        The original sub-model built an assign-projection step but exported the
        raw SAGA output, which had no CRS. The projected version is saved here.
        """
        self.report.step("Drainages")
        filled = self._run("gdal:fillnodata", {
            "INPUT": ground, "BAND": 1, "DISTANCE": 30, "ITERATIONS": 0,
            "MASK_LAYER": None, "OPTIONS": "", "EXTRA": "",
        }, context, feedback)["OUTPUT"]

        no_sinks = self._run("sagang:fillsinksxxlwangliu", {
            "ELEV": filled, "MINSLOPE": 0.1, "FILLED": "TEMPORARY_OUTPUT",
        }, context, feedback)["FILLED"]

        warped = self._run("gdal:warpreproject", {
            "INPUT": no_sinks, "SOURCE_CRS": ground.crs(),
            "TARGET_CRS": ground.crs(), "RESAMPLING": 0, "NODATA": None,
            "TARGET_RESOLUTION": None, "DATA_TYPE": 0,
            "OPTIONS": "COMPRESS=DEFLATE|PREDICTOR=2|ZLEVEL=9",
            "TARGET_EXTENT": None, "TARGET_EXTENT_CRS": None,
            "MULTITHREADING": True, "EXTRA": "",
        }, context, feedback)["OUTPUT"]

        network = self._run("sagang:channelnetworkanddrainagebasins", {
            "DEM": warped, "THRESHOLD": 5, "SEGMENTS": "TEMPORARY_OUTPUT",
        }, context, feedback)

        self._run("native:assignprojection", {
            "INPUT": network["SEGMENTS"], "CRS": ground.crs(),
            "OUTPUT": self._gpkg_out("drainage"),
        }, context, feedback)

    def _build_tree_canopy(self, ground_surface: str, all_data,
                           context, feedback) -> None:
        """All returns minus smoothed ground, kept between 3 and 150 feet.

        The raster calculator needs both grids identical, so the all-returns
        raster is snapped to the ground raster's CRS, extent and cell size
        first. That alignment was missing from the original sub-model and is
        the most likely cause of its failures.
        """
        self.report.step("Tree canopy")
        ground_layer = QgsRasterLayer(ground_surface, "ground_smoothed")
        if not ground_layer.isValid():
            raise QgsProcessingException(
                f"Could not open the smoothed ground raster: {ground_surface}")
        crs = ground_layer.crs()
        extent = ground_layer.extent()
        extent_str = (f"{extent.xMinimum()},{extent.xMaximum()},"
                      f"{extent.yMinimum()},{extent.yMaximum()}")
        if crs.authid():
            extent_str += f" [{crs.authid()}]"

        ground_filled = self._run("gdal:fillnodata", {
            "INPUT": ground_surface, "BAND": 1, "DISTANCE": 10, "ITERATIONS": 0,
            "MASK_LAYER": None, "OPTIONS": "", "EXTRA": "",
        }, context, feedback)["OUTPUT"]

        all_filled = self._run("gdal:fillnodata", {
            "INPUT": all_data, "BAND": 1, "DISTANCE": 10, "ITERATIONS": 0,
            "MASK_LAYER": None, "OPTIONS": "", "EXTRA": "",
        }, context, feedback)["OUTPUT"]

        all_aligned = self._run("gdal:warpreproject", {
            "INPUT": all_filled, "SOURCE_CRS": all_data.crs(), "TARGET_CRS": crs,
            "RESAMPLING": 0, "NODATA": None,
            "TARGET_RESOLUTION": ground_layer.rasterUnitsPerPixelX(),
            "OPTIONS": "COMPRESS=DEFLATE|PREDICTOR=2|ZLEVEL=9", "DATA_TYPE": 0,
            "TARGET_EXTENT": extent_str, "TARGET_EXTENT_CRS": crs,
            "MULTITHREADING": False, "EXTRA": "",
        }, context, feedback)["OUTPUT"]

        canopy_height = self._run("gdal:rastercalculator", {
            "INPUT_A": all_aligned, "BAND_A": 1,
            "INPUT_B": ground_filled, "BAND_B": 1,
            "FORMULA": "A-B", "NO_DATA": None, "EXTENT_OPT": 0, "PROJWIN": None,
            "RTYPE": 5, "OPTIONS": "COMPRESS=DEFLATE|PREDICTOR=2|ZLEVEL=9",
            "EXTRA": "",
        }, context, feedback)["OUTPUT"]

        # Heights from 3 to 150 ft become 1; everything else is nodata.
        canopy_mask = self._run("native:reclassifybytable", {
            "INPUT_RASTER": canopy_height, "RASTER_BAND": 1,
            "TABLE": ["3", "150", "1"], "NO_DATA": 255, "RANGE_BOUNDARIES": 0,
            "NODATA_FOR_MISSING": True, "DATA_TYPE": 0,
        }, context, feedback)["OUTPUT"]

        polygons = self._run("gdal:polygonize", {
            "INPUT": canopy_mask, "BAND": 1, "FIELD": "DN",
            "EIGHT_CONNECTEDNESS": False, "EXTRA": "",
        }, context, feedback)["OUTPUT"]

        self._run("native:smoothgeometry", {
            "INPUT": polygons, "ITERATIONS": 1, "OFFSET": 0.25, "MAX_ANGLE": 180,
            "OUTPUT": self._gpkg_out("TreeCanopy"),
        }, context, feedback)

    def _build_high_low_points(self, filled_ground, boundary,
                               context, feedback) -> None:
        self.report.step("High and low points")
        clipped = self._run("gdal:cliprasterbymasklayer", {
            "INPUT": filled_ground, "MASK": boundary, "SOURCE_CRS": None,
            "TARGET_CRS": self.ctx.crs, "TARGET_EXTENT": None, "NODATA": None,
            "ALPHA_BAND": False, "CROP_TO_CUTLINE": True, "KEEP_RESOLUTION": False,
            "SET_RESOLUTION": False, "X_RESOLUTION": None, "Y_RESOLUTION": None,
            "MULTITHREADING": False, "OPTIONS": "", "DATA_TYPE": 0, "EXTRA": "",
        }, context, feedback)["OUTPUT"]

        # Whitebox writes shapefiles; they are converted to GeoPackage
        # afterwards so the whole package stays in one format.
        alg_id, keys = self._whitebox
        # Written as a shapefile because the Whitebox backend is given the
        # path directly and Shapefile is the format it is known to write.
        for which, prefix in (("lowest", "LowPoint"), ("highest", "HighPoint")):
            temp_shp = os.path.join(self.ctx.lidar_dir, f"_tmp_{prefix}.shp")
            self._run(alg_id, {
                keys["input"]: clipped,
                keys["out_type"]: keys[which],
                keys["output"]: temp_shp,
            }, context, feedback)
            self._run("native:savefeatures", {
                "INPUT": temp_shp, "OUTPUT": self._gpkg_out(prefix),
            }, context, feedback)
            self._remove_shapefile(temp_shp)

    def _site_latitude(self) -> Optional[float]:
        """Latitude of the AOI centre, in degrees, via QGIS's own transform.

        SAGA grids carry no CRS, so asking SAGA to work the location out from
        the grid gave a longitude near zero instead of about -71. Supplying
        the latitude removes its coordinate guesswork. Over a site a few
        thousand feet across, a single latitude is exact enough.
        """
        aoi = self.ctx.aoi_lidar or self.ctx.aoi_project
        if aoi is None:
            return None
        try:
            centre = aoi.boundingBox().center()
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            transform = QgsCoordinateTransform(self.ctx.crs, wgs84,
                                               self.ctx.project)
            return transform.transform(centre).y()
        except Exception as exc:                        # noqa: BLE001
            self.report.warn(
                f"Could not work out the site latitude ({exc}), so SAGA will "
                "calculate it from the grid instead.")
            return None

    def _build_insolation(self, all_data, context, feedback) -> None:
        """Duration of insolation for the equinox and both solstices."""
        self.report.step("Duration of insolation")
        latitude = self._site_latitude()
        # LOCATION 0 uses the latitude given; 1 asks SAGA to derive it.
        location_mode = 0 if latitude is not None else 1
        if latitude is not None:
            self.report.line(f"    site latitude {latitude:.4f} degrees")
        for day, prefix in (("2023-03-20", "DOI_Equinox"),
                            ("2023-06-20", "DOI_Summer"),
                            ("2023-12-20", "DOI_Winter")):
            self._run("sagang:potentialincomingsolarradiation", {
                "GRD_DEM": all_data, "GRD_DURATION": self._out(prefix, "tif"),
                "DAY": day, "DAY_STOP": day, "DAYS_STEP": 5, "PERIOD": 1,
                "HOUR_STEP": 0.5, "MOMENT": 12, "METHOD": 2, "LUMPED": 70,
                "GRD_LINKE_DEFAULT": 3, "GRD_VAPOUR_DEFAULT": 10,
                "ATMOSPHERE": 12000, "PRESSURE": 1013, "WATER": 1.68, "DUST": 100,
                "SOLARCONST": 1367,
                "LOCATION": location_mode,
                "LATITUDE": latitude if latitude is not None else 42.0,
                "SHADOW": 1, "LOCALSVF": True,
                "UNITS": 0,
            }, context, feedback)

    # -- results back into the project ----------------------------------------
    def _repoint_results(self) -> None:
        """Point the RDG ANALYSIS layers at the new files, or add what is new."""
        self.report.step("Updating the project's analysis layers")
        claimed: Set[str] = set()
        for key, layer_names in LIDAR_LAYER_MAP.items():
            path = self.outputs.get(key)
            if not path or not os.path.exists(path):
                self.report.warn(f"Expected output missing for '{key}', so "
                                 f"{', '.join(layer_names)} was left alone.")
                continue

            is_vector = path.lower().endswith(".gpkg")
            provider = "ogr" if is_vector else "gdal"
            table = resolve_gpkg_table(path) if is_vector else None

            targets = find_layers_by_prefix(self.ctx.project, layer_names)
            if targets:
                for layer in targets:
                    try:
                        repoint_layer(layer, path, table, provider, self.report)
                        claimed.add(layer.id())
                        self.report.info(f"    {layer.name()} -> "
                                         f"{os.path.basename(path)}")
                    except QgsProcessingException as exc:
                        self.report.warn(f"Could not repoint '{layer.name()}': {exc}")
            else:
                self._add_new_layer(key, path, table, provider)

        self._warn_about_unclaimed(claimed)

    def _warn_about_unclaimed(self, claimed: Set[str]) -> None:
        """Flag layers in the analysis group that no output repointed.

        Usually means a new layer was added to the template whose name does
        not start with any of the prefixes in LIDAR_LAYER_MAP, so it is still
        pointing at whatever it pointed at before.
        """
        group = self.ctx.project.layerTreeRoot().findGroup(LIDAR_FALLBACK_GROUP)
        if group is None:
            return
        stragglers = [lyr.name() or "<unnamed>" for lyr in layers_in_group(group)
                      if lyr.id() not in claimed]
        if stragglers:
            self.report.warn(
                f"These layers in '{LIDAR_FALLBACK_GROUP}' were not updated, "
                "because their names do not start with any prefix in "
                "LIDAR_LAYER_MAP: " + ", ".join(sorted(stragglers)))

    def _add_new_layer(self, key: str, path: str, table: Optional[str],
                       provider: str) -> None:
        """No placeholder exists, so add the output as a new layer instead."""
        uri = f"{path}|layername={table}" if table else path
        layer = (QgsVectorLayer(uri, key, "ogr") if provider == "ogr"
                 else QgsRasterLayer(uri, key))
        if not layer.isValid():
            self.report.warn(f"Could not load the output for '{key}': {path}")
            return
        self.ctx.project.addMapLayer(layer, False)
        group = self.ctx.project.layerTreeRoot().findGroup(LIDAR_FALLBACK_GROUP)
        (group or self.ctx.project.layerTreeRoot()).addLayer(layer)
        self.report.line(f"    added '{key}' (no placeholder layer existed)")

    # -- small helpers --------------------------------------------------------
    @staticmethod
    def _remove_shapefile(path: str) -> None:
        stem = os.path.splitext(path)[0]
        folder = os.path.dirname(path)
        try:
            for name in os.listdir(folder):
                full = os.path.join(folder, name)
                if os.path.splitext(full)[0] == stem:
                    os.remove(full)
        except OSError:
            pass



# =============================================================================
# SECTION 4 - THE PROCESSING ALGORITHM
#
# A thin shell. It reads the dialog, builds a RunContext, and calls whichever
# steps are ticked. All the real work lives in the three Step classes above.
#
# Note: QGIS registers exactly one algorithm per script file, which is why the
# three steps are checkboxes on one dialog rather than three separate entries.
# =============================================================================

DISTANCE_UNIT_CHOICES = ["feet", "metres"]
DISTANCE_UNIT_VALUES = [Qgis.DistanceUnit.Feet, Qgis.DistanceUnit.Meters]

AOI_METHOD_CHOICES = [
    "Multiply the parcel bounding box",
    "Buffer the parcel bounding box",
]


class InitialMapSetProcess(QgsProcessingAlgorithm):
    """Builds a local project package from the RDG regional template."""

    # Step switches
    P_RUN_STEP1 = "RUN_STEP1"
    P_RUN_STEP2 = "RUN_STEP2"
    P_RUN_STEP3 = "RUN_STEP3"

    # Always visible
    P_CODE = "PROJECT_CODE"
    P_CRS = "PROJECT_CRS"
    P_CLIENT = "PROJECT_CLIENT"
    P_ADDRESS = "PROJECT_ADDRESS"
    P_TOWN = "PROJECT_TOWN"
    P_STATE = "PROJECT_STATE"
    P_LIDAR = "PROJECT_LIDAR"
    P_ORTHO = "PROJECT_ORTHO"
    P_GROUND_DEM = "GROUND_DEM"
    P_ALL_DEM = "ALL_DEM"
    P_POINT_CLOUD = "POINT_CLOUD"
    P_DRY_RUN = "DRY_RUN"

    # Advanced
    P_AOI_METHOD = "AOI_METHOD"
    P_AOI_MULTIPLIER = "AOI_MULTIPLIER"
    P_AOI_BUFFER = "AOI_BUFFER"
    P_REGION_SCALE = "REGION_SCALE"
    P_DISTANCE_UNIT = "DISTANCE_UNIT"
    P_RESUME = "RESUME"
    P_GROUND_EXPRESSION = "GROUND_EXPRESSION"
    P_DEM_RESOLUTION = "DEM_RESOLUTION"
    P_SCALE_Z = "SCALE_Z"
    P_SAVE = "SAVE_PROJECT"
    P_TIDY = "TIDY_OUTPUTS"

    # -- identity -------------------------------------------------------------
    def tr(self, text: str) -> str:
        return QCoreApplication.translate("InitialMapSetProcess", text)

    def createInstance(self):
        return InitialMapSetProcess()

    def name(self):
        return "initialmapsetprocess"

    def displayName(self):
        return self.tr("Initial Map Set Process")

    def group(self):
        return self.tr("RDG")

    def groupId(self):
        return "rdg"

    def flags(self):
        # Must run on the main thread: it modifies the open project.
        base = super().flags()
        for owner, attr in ((Qgis, "ProcessingAlgorithmFlag.NoThreading"),
                            (QgsProcessingAlgorithm, "Flag.FlagNoThreading"),
                            (QgsProcessingAlgorithm, "FlagNoThreading")):
            try:
                return base | _first_attr(owner, attr)
            except AttributeError:
                continue
        return base

    def shortHelpString(self):
        # The naming section is generated from the constants, so it can never
        # drift from what the code actually matches on.
        return self.tr(HELP_TEXT) + name_dependencies_html()

    # -- parameters -----------------------------------------------------------
    def initAlgorithm(self, config=None):
        project = QgsProject.instance()
        variables = QgsExpressionContextUtils.projectScope(project)

        def project_variable(name: str) -> str:
            value = variables.variable(name)
            return str(value) if value else ""

        def advanced(param):
            """Mark a parameter advanced so the dialog stays short."""
            try:
                param.setFlags(param.flags()
                               | Qgis.ProcessingParameterFlag.Advanced)
            except AttributeError:
                param.setFlags(param.flags()
                               | QgsProcessingParameterDefinition.FlagAdvanced)
            return param

        # --- which steps to run
        self.addParameter(QgsProcessingParameterBoolean(
            self.P_RUN_STEP1, self.tr("Step 1 - Preprocess (boundary, AOIs, pruning)"),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.P_RUN_STEP2, self.tr("Step 2 - Localize layers"), defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.P_RUN_STEP3, self.tr("Step 3 - LiDAR analysis"), defaultValue=True))

        # --- project identity. These become project variables, which the
        # --- print layouts read, so they are all required.
        self.addParameter(QgsProcessingParameterString(
            self.P_CODE, self.tr("Project code"),
            defaultValue=project_variable("project_code")))
        self.addParameter(QgsProcessingParameterCrs(
            self.P_CRS, self.tr("Project CRS"), defaultValue=project.crs()))
        self.addParameter(QgsProcessingParameterString(
            self.P_CLIENT, self.tr("Client"),
            defaultValue=project_variable("project_client")))
        self.addParameter(QgsProcessingParameterString(
            self.P_ADDRESS, self.tr("Address"),
            defaultValue=project_variable("project_address")))
        self.addParameter(QgsProcessingParameterString(
            self.P_TOWN, self.tr("Town"),
            defaultValue=project_variable("project_town")))
        self.addParameter(QgsProcessingParameterString(
            self.P_STATE, self.tr("State"),
            defaultValue=project_variable("project_state")))
        self.addParameter(QgsProcessingParameterString(
            self.P_LIDAR, self.tr("LiDAR source"),
            defaultValue=project_variable("project_lidar"), optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.P_ORTHO, self.tr("Ortho source"),
            defaultValue=project_variable("project_ortho"), optional=True))
        # project_acres is calculated from the property boundary in Step 1,
        # so it is not asked for here.

        # --- LiDAR inputs: either the two DEMs, or a point cloud to make them
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.P_GROUND_DEM, self.tr("Step 3: Ground DEM (leave empty if "
                                       "supplying a point cloud)"), optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.P_ALL_DEM, self.tr("Step 3: All DEM"), optional=True))
        self.addParameter(QgsProcessingParameterPointCloudLayer(
            self.P_POINT_CLOUD, self.tr("Step 3: Point cloud (builds both DEMs)"),
            optional=True))

        self.addParameter(QgsProcessingParameterBoolean(
            self.P_RESUME,
            self.tr("Resume an interrupted run (skip layers already written)"),
            defaultValue=False))

        # --- advanced, starting with the dry run
        self.addParameter(advanced(QgsProcessingParameterBoolean(
            self.P_DRY_RUN, self.tr("Dry run (report only, write nothing)"),
            defaultValue=False)))

        self.addParameter(advanced(QgsProcessingParameterEnum(
            self.P_AOI_METHOD, self.tr("Project AOI method"),
            options=AOI_METHOD_CHOICES, defaultValue=0)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.P_AOI_MULTIPLIER, self.tr("Project AOI multiplier"),
            type=NUMBER_DOUBLE,
            defaultValue=3.0, minValue=1.0)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.P_AOI_BUFFER, self.tr("Project AOI buffer distance"),
            type=NUMBER_DOUBLE,
            defaultValue=500.0, minValue=0.0)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.P_REGION_SCALE,
            self.tr("Region AOI scale, units per inch on an 11x17 sheet "
                    "(5000 gives an 85,000 ft square)"),
            type=NUMBER_DOUBLE,
            defaultValue=5000.0, minValue=1.0)))
        self.addParameter(advanced(QgsProcessingParameterEnum(
            self.P_DISTANCE_UNIT, self.tr("Units for AOI distances"),
            options=DISTANCE_UNIT_CHOICES, defaultValue=0)))

        self.addParameter(advanced(QgsProcessingParameterString(
            self.P_GROUND_EXPRESSION, self.tr("Point cloud: ground classification"),
            defaultValue="Classification = 2")))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.P_DEM_RESOLUTION, self.tr("Point cloud: DEM resolution "
                                           "(project CRS units)"),
            type=NUMBER_DOUBLE,
            defaultValue=1.0, minValue=0.01)))
        self.addParameter(advanced(QgsProcessingParameterBoolean(
            self.P_SCALE_Z, self.tr("Point cloud: scale Z from metres to feet"),
            defaultValue=True)))

        self.addParameter(advanced(QgsProcessingParameterBoolean(
            self.P_TIDY,
            self.tr("Delete output files from earlier runs that nothing uses"),
            defaultValue=True)))
        self.addParameter(advanced(QgsProcessingParameterBoolean(
            self.P_SAVE, self.tr("Save the project when finished"),
            defaultValue=True)))

    def checkParameterValues(self, parameters, context):
        """Validate combinations up front.

        A Processing script dialog cannot show or hide parameters based on
        what is already filled in, so the next best thing is to catch an
        impossible combination before any work starts.
        """
        ok, message = super().checkParameterValues(parameters, context)
        if not ok:
            return ok, message

        if not self.parameterAsBool(parameters, self.P_RUN_STEP3, context):
            return True, ""

        cloud = self.parameterAsPointCloudLayer(
            parameters, self.P_POINT_CLOUD, context)
        ground = self.parameterAsRasterLayer(parameters, self.P_GROUND_DEM, context)
        all_dem = self.parameterAsRasterLayer(parameters, self.P_ALL_DEM, context)

        if cloud is not None and (ground is not None or all_dem is not None):
            return False, self.tr(
                "Step 3 takes either a point cloud or the two DEMs, not both. "
                "Clear whichever you are not using.")
        if cloud is None and (ground is None or all_dem is None):
            return False, self.tr(
                "Step 3 needs either a point cloud, or both the Ground DEM and "
                "the All DEM.")
        return True, ""

    # -- the run --------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        project = QgsProject.instance()
        report = Report(feedback)
        started = time.time()

        run_step1 = self.parameterAsBool(parameters, self.P_RUN_STEP1, context)
        run_step2 = self.parameterAsBool(parameters, self.P_RUN_STEP2, context)
        run_step3 = self.parameterAsBool(parameters, self.P_RUN_STEP3, context)
        if not any((run_step1, run_step2, run_step3)):
            raise QgsProcessingException("No steps are ticked, so there is nothing "
                                         "to do.")

        dry_run = self.parameterAsBool(parameters, self.P_DRY_RUN, context)
        save_at_end = self.parameterAsBool(parameters, self.P_SAVE, context)
        self._tidy_outputs = self.parameterAsBool(parameters, self.P_TIDY, context)

        ctx = self._build_context(parameters, context, project, report, dry_run)
        self._write_header(ctx, (run_step1, run_step2, run_step3))

        # A failure part-way through still leaves real work on disk, so the
        # report is written either way and the error is re-raised afterwards.
        failure: Optional[BaseException] = None
        try:
            if run_step1:
                Step1Preprocess(
                    ctx, self._step1_settings(parameters, context)).run(feedback)

            if run_step2 and not feedback.isCanceled():
                Step2Localize(
                    ctx, self._step2_settings(parameters, context)).run(
                        context, feedback)

            if run_step3 and not feedback.isCanceled():
                Step3Lidar(ctx, self._step3_settings(parameters, context)).run(
                    context, feedback)
        except BaseException as exc:                        # noqa: BLE001
            failure = exc
            report.warn(f"The run stopped early: {exc}")

        if not dry_run:
            self._finalize(ctx, save_at_end)

        report.gap(3)
        report.heading("SUMMARY")
        report.gap()
        report.info(f"Total time : {human_time(time.time() - started)}")
        report.gap()
        report.info(f"Warnings   : {len(report.warnings)}")
        for warning in report.warnings:
            report.info("   - " + warning)
        report.gap()
        self._report_not_localized(ctx)
        if not dry_run:
            report.line("")
            report.line("Before sending this folder to a client: close the project "
                        "in QGIS so no GeoPackage journal files are left behind, "
                        "then check that the layer tree draws correctly.")

        report_path = report.write(ctx.project_dir, ctx.code,
                                   ctx.date_stamp, dry_run)
        if failure is not None:
            raise failure
        return {"REPORT": report_path,
                "LOCAL_LAYERS": ctx.local_layers_dir,
                "LIDAR_ANALYSIS": ctx.lidar_dir,
                "WARNINGS": len(report.warnings)}

    # -- building the context -------------------------------------------------
    def _build_context(self, parameters, context, project, report,
                       dry_run) -> RunContext:
        """Validate the project state and assemble what every step needs."""
        if not project.fileName():
            raise QgsProcessingException(
                "This project has never been saved. Use File > Save As to save it "
                "into the new project folder first, then run this again.")

        project_dir = project.absolutePath()
        if not os.path.isdir(project_dir):
            raise QgsProcessingException(
                f"Cannot find the project folder: {project_dir}")
        if not os.access(project_dir, os.W_OK):
            raise QgsProcessingException(
                f"The project folder is not writable: {project_dir}")

        if "template" in project_dir.lower():
            report.warn(
                "The open project looks like it is still in a template folder:\n  "
                f"{project_dir}\nIf so, cancel and use File > Save As into the new "
                "project folder first.")

        raw_code = self.parameterAsString(parameters, self.P_CODE, context).strip()
        if not raw_code:
            raise QgsProcessingException("A project code is required.")
        code = re.sub(r'[\\/:*?"<>|\s]+', "", raw_code)

        crs = self.parameterAsCrs(parameters, self.P_CRS, context)
        if not crs.isValid():
            raise QgsProcessingException("A valid project CRS is required.")

        # Note which groups already hold layers, so the tidy-up at the end can
        # tell "emptied by this run" from "deliberately empty".
        populated = {group.name() for group in top_level_groups(project)
                     if layers_in_group(group)}
        for group in top_level_groups(project):
            for node in group.findGroups():
                if layers_in_group(node):
                    populated.add(node.name())

        date_stamp = datetime.datetime.now().strftime("%y%m%d")
        return RunContext(
            project=project,
            project_dir=project_dir,
            code=code,
            date_stamp=date_stamp,
            crs=crs,
            local_layers_dir=os.path.join(project_dir, f"Local Layers_{code}"),
            lidar_dir=os.path.join(project_dir, f"Lidar Analysis_{code}"),
            namer=FileNamer(code, date_stamp),
            report=report,
            dry_run=dry_run,
            groups_with_layers_at_start=populated,
        )

    def _write_header(self, ctx: RunContext, steps) -> None:
        run1, run2, run3 = steps
        ctx.report.info("=" * 78)
        ctx.report.info("INITIAL MAP SET PROCESS"
                        + ("   [DRY RUN]" if ctx.dry_run else ""))
        ctx.report.line("=" * 78)
        ctx.report.line("Run at        : "
                        + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        ctx.report.line("Project file  : " + ctx.project.fileName())
        ctx.report.line("Project code  : " + ctx.code)
        ctx.report.line("Date stamp    : " + ctx.date_stamp)
        ctx.report.line("Project CRS   : "
                        f"{ctx.crs.authid()}  {ctx.crs.description()}")
        ctx.report.line("Local layers  : " + ctx.local_layers_dir)
        ctx.report.line("Lidar output  : " + ctx.lidar_dir)
        ctx.report.line("Steps         : "
                        + ", ".join(label for label, on in
                                    (("1 preprocess", run1), ("2 localize", run2),
                                     ("3 lidar", run3)) if on))

    # -- per-step settings ----------------------------------------------------
    def _step1_settings(self, parameters, context) -> Step1Settings:
        unit_index = self.parameterAsEnum(parameters, self.P_DISTANCE_UNIT, context)
        return Step1Settings(
            project_variables={
                "project_code": self.parameterAsString(
                    parameters, self.P_CODE, context).strip(),
                "project_client": self.parameterAsString(
                    parameters, self.P_CLIENT, context).strip(),
                "project_address": self.parameterAsString(
                    parameters, self.P_ADDRESS, context).strip(),
                "project_town": self.parameterAsString(
                    parameters, self.P_TOWN, context).strip(),
                "project_state": self.parameterAsString(
                    parameters, self.P_STATE, context).strip(),
                "project_lidar": self.parameterAsString(
                    parameters, self.P_LIDAR, context).strip(),
                "project_ortho": self.parameterAsString(
                    parameters, self.P_ORTHO, context).strip(),
            },
            aoi_method=self.parameterAsEnum(parameters, self.P_AOI_METHOD, context),
            aoi_multiplier=self.parameterAsDouble(
                parameters, self.P_AOI_MULTIPLIER, context),
            aoi_buffer=self.parameterAsDouble(parameters, self.P_AOI_BUFFER, context),
            region_scale=self.parameterAsDouble(
                parameters, self.P_REGION_SCALE, context),
            distance_unit=DISTANCE_UNIT_VALUES[unit_index],
        )

    def _step2_settings(self, parameters, context) -> Step2Settings:
        return Step2Settings(
            resume=self.parameterAsBool(parameters, self.P_RESUME, context))

    def _step3_settings(self, parameters, context) -> Step3Settings:
        return Step3Settings(
            ground_dem=self.parameterAsRasterLayer(
                parameters, self.P_GROUND_DEM, context),
            all_dem=self.parameterAsRasterLayer(
                parameters, self.P_ALL_DEM, context),
            point_cloud=self.parameterAsPointCloudLayer(
                parameters, self.P_POINT_CLOUD, context),
            ground_expression=self.parameterAsString(
                parameters, self.P_GROUND_EXPRESSION, context).strip()
            or "Classification = 2",
            dem_resolution=self.parameterAsDouble(
                parameters, self.P_DEM_RESOLUTION, context),
            scale_z_to_feet=self.parameterAsBool(parameters, self.P_SCALE_Z, context),
        )

    @staticmethod
    def _report_not_localized(ctx: RunContext) -> None:
        """Say what did not end up local, and why.

        The second half walks every remaining layer and flags any whose data
        still lives outside the project folder, which is the check that
        matters before the folder is zipped and sent.
        """
        outside: List[str] = []
        project_dir = os.path.normcase(os.path.abspath(ctx.project_dir))
        for layer in ctx.project.mapLayers().values():
            if is_streaming(layer):
                continue
            path, _inner = decode_source_path(layer)
            if not path:
                continue
            full = os.path.normcase(os.path.abspath(path))
            if not full.startswith(project_dir):
                outside.append(f"{layer.name() or '<unnamed>'}  --  still points "
                               f"outside the project folder: {path}")

        if not ctx.not_localized and not outside:
            ctx.report.info("All items complete. Every non-streaming layer "
                            "points inside the project folder.")
            return

        ctx.report.info("All items complete except the following:")
        for name, reason in ctx.not_localized:
            ctx.report.info(f"  {name}  --  {reason}")
        for entry in outside:
            ctx.report.info(f"  {entry}")

    @staticmethod
    def _remove_orphan_outputs(ctx: RunContext) -> None:
        """Delete output files from earlier runs that nothing points at.

        Re-running Step 1 writes a fresh Est Property Boundary and AOIs, and
        the old ones are left behind as _2, _3 and so on. A file is removed
        only when all three are true: this run did not write it, no layer in
        the project uses it, and its name carries this project's code, so a
        file someone dropped in by hand is never touched.
        """
        used = set()
        for layer in ctx.project.mapLayers().values():
            path, _inner = decode_source_path(layer)
            if path:
                used.add(os.path.normcase(os.path.abspath(path)))

        marker = f"_{ctx.code}_"
        removed, freed = [], 0
        for folder in (ctx.local_layers_dir, ctx.lidar_dir):
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                full = os.path.normcase(os.path.abspath(os.path.join(folder, name)))
                if marker not in name or name.lower().endswith(".txt"):
                    continue
                if full in ctx.namer.issued or full in used:
                    continue
                # Shapefile sidecars follow their .shp; judge them together.
                stem = os.path.splitext(full)[0]
                if any(u.startswith(stem + ".") for u in used | ctx.namer.issued):
                    continue
                try:
                    size = os.path.getsize(full)
                    os.remove(full)
                    removed.append(name)
                    freed += size
                except OSError as exc:
                    ctx.report.warn(f"Could not remove the unused file "
                                    f"{name}: {exc}")

        if removed:
            ctx.report.info(f"Removed {len(removed)} unused file(s) from earlier "
                            f"runs, freeing {human_size(freed)}:")
            for name in removed:
                ctx.report.info("      " + name)

    # -- finishing up ---------------------------------------------------------
    def _finalize(self, ctx: RunContext, save_at_end: bool) -> None:
        """Relative paths make the folder portable; then save the project."""
        try:
            ctx.project.setFilePathStorage(Qgis.FilePathType.FilePathRelative)
        except Exception:                                   # noqa: BLE001
            ctx.project.writeEntryBool("Paths", "/Absolute", False)

        self._remove_newly_emptied_groups(ctx)
        if self._tidy_outputs:
            self._remove_orphan_outputs(ctx)

        if save_at_end:
            if ctx.project.write():
                ctx.report.step("Project saved: " + ctx.project.fileName())
            else:
                ctx.report.warn("QGIS reported a problem saving the project. "
                                "Save it manually before closing.")

    @staticmethod
    def _remove_newly_emptied_groups(ctx: RunContext) -> None:
        """Remove groups that lost all their layers during this run.

        Groups that were already empty before the run (Subbasins, Lidar Data)
        are left alone, since they are there on purpose.
        """
        def tidy(node) -> None:
            for child in list(node.children()):
                if not isinstance(child, QgsLayerTreeGroup):
                    continue
                tidy(child)
                if (not child.children()
                        and child.name() in ctx.groups_with_layers_at_start):
                    ctx.report.line(f"    removed the now-empty group "
                                    f"'{child.name()}'")
                    node.removeChildNode(child)

        tidy(ctx.project.layerTreeRoot())


# -----------------------------------------------------------------------------
# Help text shown in the Processing dialog sidebar.
#
# The "names this script depends on" section is built from the constants at
# the top of each section rather than written out by hand, so renaming a
# layer in the code updates the documentation with it.
# -----------------------------------------------------------------------------

def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _rows(pairs) -> str:
    """A two-column table body from (left, right) pairs."""
    return "".join(
        f"<tr><td valign='top'><code>{_esc(a)}</code></td>"
        f"<td valign='top'>{_esc(b)}</td></tr>"
        for a, b in pairs)


def name_dependencies_html() -> str:
    """Everything the script matches by name, listed from the live constants.

    Rename any of these in the template and the matching entry in the code
    has to change with it, so they are all gathered in one place.
    """
    parts = ["<h3>Layer and group names this script depends on</h3>",
             "<p>These are matched by name, ignoring case and surrounding "
             "spaces. Rename one in the template and it must be renamed in "
             "the script too, or that feature quietly stops working.</p>"]

    parts.append("<p><b>Top-level groups</b> are routed by a keyword anywhere "
                 "in the group name. Groups matching neither are left "
                 "untouched.</p><table>")
    parts.append(_rows([
        (PROJECT_SCALE_KEYWORD, "filtered to the Project Scale AOI"),
        (REGION_SCALE_KEYWORD, "filtered to the Region Scale AOI"),
    ]))
    parts.append("</table>")

    parts.append("<p><b>Step 1 writes these files and repoints these layers.</b> "
                 "Several layers can share one file.</p><table>")
    parts.append(_rows([
        (SITE_LAYER_FILENAMES[key] + ".gpkg", ", ".join(names))
        for key, names in SITE_LAYER_TARGETS.items()
    ]))
    parts.append("</table>")

    parts.append("<p><b>Derived site layers</b> are pulled from a group by "
                 "what touches the property boundary.</p><table>")
    parts.append(_rows([
        (rule.source_group,
         f"{rule.description} -> {', '.join(SITE_LAYER_TARGETS[rule.key])}")
        for rule in DERIVED_SITE_LAYERS
    ]))
    parts.append("</table>")

    parts.append("<p><b>Buffers</b> are generated in Step 2 from the localized "
                 "layers whose name contains the first term and none of the "
                 "excluded terms. The statewide buffer files are never "
                 "copied.</p><table>")
    parts.append(_rows([
        (spec.target_layer,
         f"{spec.distance_feet:g} ft buffer of layers matching "
         f"'{spec.source_name_contains}', excluding "
         f"{', '.join(repr(w) for w in spec.source_name_excludes)}")
        for spec in DERIVED_BUFFERS
    ]))
    parts.append("</table>")

    parts.append("<p><b>Step 3 outputs</b> are matched to these layers. Where "
                 "no layer of that name exists, the output is added to "
                 f"<i>{_esc(LIDAR_FALLBACK_GROUP)}</i> instead.</p><table>")
    parts.append(_rows([(key, ", ".join(names))
                        for key, names in LIDAR_LAYER_MAP.items()]))
    parts.append("</table>")

    parts.append("<p><b>Project variables</b> written by Step 1: "
                 "<code>project_code</code>, <code>project_client</code>, "
                 "<code>project_address</code>, <code>project_town</code>, "
                 "<code>project_state</code>, <code>project_lidar</code>, "
                 "<code>project_ortho</code>, and <code>project_acres</code>, "
                 "which is calculated from the property boundary.</p>")

    parts.append("<p><b>Providers</b> whose streaming layers are left live: "
                 + ", ".join(f"<code>{_esc(p)}</code>"
                             for p in sorted(STREAMING_PROVIDERS)) + ".</p>")

    return "".join(parts)


HELP_TEXT = """\
<p>Turns the RDG regional template into a small, self-contained project folder \
that can be zipped and sent to a client with no broken links.</p>

<p><b>Before running:</b> open the template and use File &gt; Save As to save it \
into the new project folder. This script never writes to the template. Then \
select the tax parcels that make up the site, in one or more layers.</p>

<p><b>Run with Dry run ticked first.</b> It writes nothing and reports what \
would happen, how large the output would be and roughly how long it would \
take.</p>

<h3>Step 1 &ndash; Preprocess</h3>
<p>Sets the project CRS and the project variables. Dissolves the selected \
parcels into an <i>Estimated Property Boundary</i>, then builds three areas of \
interest from it: a <i>Project Scale AOI</i> at three times the width and height \
of the boundary's bounding box, a <i>Region Scale AOI</i> sized from a drawing scale (at the default \
1 inch = 5,000 ft that is an 85,000&nbsp;ft square, which fills an 11x17 \
sheet either way up), and a <i>LiDAR AOI</i> copied from the project AOI. It also pulls the watershed subbasins the site falls in. Each is \
written to <i>Local Layers_&lt;code&gt;</i> and the matching placeholder \
layers in Site Layers are repointed at them. Finally it removes any layer in a \
scale group whose data lies entirely outside that group's AOI.</p>

<h3>Step 2 &ndash; Localize layers</h3>
<p>Works only on top-level groups whose name contains <b>PROJECT SCALE</b> or \
<b>REGION SCALE</b>; everything else is left alone. Vector layers keep every \
feature that touches the AOI, whole and unclipped. Rasters are cut to the AOI \
rectangle on their own pixel grid, so data type, bands, pixel size and nodata \
are unchanged. Join tables are reduced to the rows the kept features \
reference. Layers with nothing inside the AOI are removed. Everything is \
written to GeoPackage or GeoTIFF in <i>Local Layers_&lt;code&gt;</i> and the \
layers are repointed, keeping their symbology, labels, joins, tree position, \
print layouts and map themes.</p>
<p>Afterwards it builds <i>Est. Wetlands Buffer, 50ft</i> and \
<i>Est. Wetlands Buffer, 100ft</i> by buffering every localized wetlands layer \
and repointing those two layers at the result. The statewide buffer files are \
never copied, which saves several hundred megabytes.</p>
<p>The report ends with a <b>NOT LOCALIZED</b> list: everything skipped, left \
live or failed, and any layer whose data still sits outside the project \
folder. If that last list is empty, the folder is safe to zip and send.</p>

<h3>Step 3 &ndash; LiDAR analysis</h3>
<p>Takes either a Ground DEM and an All DEM, or a point cloud to build them \
from. Writes the standard RDG terrain products into \
<i>Lidar Analysis_&lt;code&gt;</i>: smoothed ground, slope, aspect, 1&nbsp;ft \
contours, drainages, wetness index, tree canopy, high and low points, and \
duration of insolation for the equinox and both solstices. The matching layers in RDG \
ANALYSIS are repointed at the results.</p>

<h3>Notes</h3>
<ul>
<li><b>Spatial indexes.</b> Several source shapefiles are multi-gigabyte files \
with millions of features. Index building is on by default and writes a small \
.qix file next to the source data, which makes every future run faster for \
everyone.</li>
<li><b>Cloud sync.</b> GeoPackage is SQLite. Pause Google Drive sync while this \
runs if the project folder is inside a synced drive, and close the project \
before zipping.</li>
<li><b>Resume.</b> If a run is cancelled or fails, tick Resume to carry on. \
Changing the AOIs invalidates the resume rather than mixing two extents.</li>
<li><b>Project variables.</b> The values typed in above are written to the \
project, where the print layouts read them. <code>project_acres</code> is not \
asked for: it is calculated from the Estimated Property Boundary.</li>
<li><b>Streaming layers</b> are left live. They cost nothing in the package and \
work for anyone online.</li>
<li><b>Stored credentials.</b> A layer whose source contains <code>authcfg=\
</code> depends on your local QGIS authentication database and will be broken \
for anyone else. These are flagged in the report.</li>
</ul>

<h3>Required Processing providers</h3>
<p>Step 3 needs <b>SAGA NextGen</b> and a Whitebox provider, both installed \
from Plugins &gt; Manage and Install Plugins. On QGIS 4 install \
<b>Whitebox Workflows for QGIS</b>; on QGIS 3 the older <b>WhiteboxTools</b> \
plugin also works. The script detects whichever is present. The point cloud option also \
needs the <b>PDAL</b> provider. Availability is checked when the step starts, \
so a missing provider gives a clear message rather than a failure part-way \
through.</p>

<h3>Installing PDAL</h3>
<p>The PDAL tools appear in the Processing Toolbox under "Point cloud" once \
QGIS can find the <code>pdal_wrench</code> executable. They are bundled with \
most official QGIS builds but not with every one.</p>
<p><b>macOS.</b> Use the official installer from \
<code>qgis.org/download</code>, which bundles PDAL. Builds from Homebrew or \
MacPorts often do not include <code>pdal_wrench</code>; if the Point cloud \
group is missing, reinstall from the official .dmg. Check it with Processing \
&gt; Toolbox and search for "export raster".</p>
<p><b>Windows.</b> Use the OSGeo4W installer from \
<code>qgis.org/download</code>. Choose Advanced Install, then on the package \
list select <code>pdal</code> in addition to <code>qgis</code>. If QGIS is \
already installed, re-run the OSGeo4W setup and add the <code>pdal</code> \
package; no reinstall of QGIS is needed. The standalone MSI installer also \
bundles PDAL.</p>
<p>After installing, restart QGIS. If the tools still do not appear, check \
Settings &gt; Options &gt; Processing &gt; Providers that PDAL is enabled.</p>
"""
