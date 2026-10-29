"""
Site Setup - RDG QGIS Processing tool (part 1 of the new-project workflow)

Run this once you have created the project from the regional template and
saved it into the project folder.

It will:
  1. Set the project variables (code, client, address, town, state)
  2. Take the tax parcel(s) you selected and dissolve them into one site parcel
  3. Find the sub-basin(s) the parcel falls in and dissolve them into one
  4. Set the project CRS
  5. Build two AOI polygons:
       aoi_project  = site parcel buffered by the project buffer distance
       aoi_regional = site sub-basin buffered by the regional buffer distance
  6. Save site_parcel, site_subbasin, aoi_project and aoi_regional as layers in
     one GeoPackage, and add them to a "Site" group at the top of the project.
"""

import os

from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingMultiStepFeedback,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDistance,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFileDestination,
    QgsProcessingUtils,
    QgsExpressionContextUtils,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
import processing


PROJECT_VARS = [
    ('project_code', 'Project Code'),
    ('project_client', 'Project Client'),
    ('project_address', 'Project Address'),
    ('project_town', 'Project Town'),
    ('project_state', 'Project State'),
]

# Names of the layers written to the GeoPackage (the Prune tool looks for the aoi_* ones)
SITE_PARCEL = 'site_parcel'
SITE_SUBBASIN = 'site_subbasin'
AOI_PROJECT = 'aoi_project'
AOI_REGIONAL = 'aoi_regional'
SITE_GROUP = 'Site'


def _current_var(name):
    value = QgsExpressionContextUtils.projectScope(QgsProject.instance()).variable(name)
    return value if value is not None else ''


class SiteSetup(QgsProcessingAlgorithm):
    PARCELS = 'PARCELS'
    SUBBASINS = 'SUBBASINS'
    CRS = 'CRS'
    PROJECT_BUFFER = 'PROJECT_BUFFER'
    REGIONAL_BUFFER = 'REGIONAL_BUFFER'
    OUTPUT_GPKG = 'OUTPUT_GPKG'
    SET_VARS = 'SET_VARS'

    def flags(self):
        # Run on the main thread: this tool changes the project (CRS, variables,
        # layers), which is not safe to do from a background thread.
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.PARCELS, 'Tax parcel(s) - select them on the map and tick "Selected features only"',
            [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.SUBBASINS, 'Sub-basins layer', [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, 'Project CRS (must be projected - feet or metres)', defaultValue='ProjectCrs'))

        project_buf = QgsProcessingParameterDistance(
            self.PROJECT_BUFFER, 'Project AOI buffer around parcel', defaultValue=100,
            parentParameterName=self.CRS, minValue=0)
        regional_buf = QgsProcessingParameterDistance(
            self.REGIONAL_BUFFER, 'Regional AOI buffer around sub-basin', defaultValue=1000,
            parentParameterName=self.CRS, minValue=0)
        self.addParameter(project_buf)
        self.addParameter(regional_buf)

        home = QgsProject.instance().homePath()
        default_gpkg = os.path.join(home, 'site_aoi.gpkg') if home else None
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_GPKG, 'Site GeoPackage', fileFilter='GeoPackage (*.gpkg)',
            defaultValue=default_gpkg))

        self.addParameter(QgsProcessingParameterBoolean(
            self.SET_VARS, 'Set project variables', defaultValue=True))
        for name, label in PROJECT_VARS:
            self.addParameter(QgsProcessingParameterString(
                name, label, optional=True, defaultValue=_current_var(name)))

    def processAlgorithm(self, parameters, context, model_feedback):
        feedback = QgsProcessingMultiStepFeedback(8, model_feedback)

        # ---- Check inputs -------------------------------------------------
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if not crs.isValid():
            raise QgsProcessingException('Please choose a project CRS.')
        if crs.isGeographic():
            raise QgsProcessingException(
                f'{crs.authid()} uses degrees. Pick a projected CRS (e.g. State Plane or UTM) '
                'so the buffer distances make sense.')

        project_buf = self.parameterAsDouble(parameters, self.PROJECT_BUFFER, context)
        regional_buf = self.parameterAsDouble(parameters, self.REGIONAL_BUFFER, context)
        gpkg = self.parameterAsFileOutput(parameters, self.OUTPUT_GPKG, context)
        if not gpkg:
            raise QgsProcessingException('Please choose where to save the site GeoPackage.')
        if not gpkg.lower().endswith('.gpkg'):
            gpkg += '.gpkg'

        parcels = self.parameterAsSource(parameters, self.PARCELS, context)
        if parcels is None or parcels.featureCount() == 0:
            raise QgsProcessingException(
                'No tax parcels found. Select the parcel(s) on the map and tick '
                '"Selected features only".')
        if parcels.featureCount() > 25:
            feedback.pushWarning(
                f'{parcels.featureCount()} parcels were used. Did you forget to tick '
                '"Selected features only"?')

        def run(alg, params, step):
            feedback.setCurrentStep(step)
            if feedback.isCanceled():
                raise QgsProcessingException('Canceled.')
            params['OUTPUT'] = QgsProcessing.TEMPORARY_OUTPUT
            return processing.run(alg, params, context=context, feedback=feedback,
                                  is_child_algorithm=True)['OUTPUT']

        def buffer(layer, distance, step):
            return run('native:buffer', {
                'INPUT': layer, 'DISTANCE': distance, 'SEGMENTS': 8,
                'END_CAP_STYLE': 0, 'JOIN_STYLE': 0, 'MITER_LIMIT': 2, 'DISSOLVE': True,
            }, step)

        # ---- Site parcel --------------------------------------------------
        parcel = run('native:reprojectlayer',
                     {'INPUT': parameters[self.PARCELS], 'TARGET_CRS': crs}, 0)
        parcel = run('native:dissolve', {'INPUT': parcel, 'FIELD': []}, 1)

        # ---- Site sub-basin -----------------------------------------------
        subbasin = run('native:reprojectlayer',
                       {'INPUT': parameters[self.SUBBASINS], 'TARGET_CRS': crs}, 2)
        subbasin = run('native:extractbylocation', {
            'INPUT': subbasin, 'PREDICATE': [0], 'INTERSECT': parcel,  # 0 = intersects
        }, 3)
        if QgsProcessingUtils.mapLayerFromString(subbasin, context).featureCount() == 0:
            raise QgsProcessingException('The parcel does not fall inside any sub-basin.')
        subbasin = run('native:dissolve', {'INPUT': subbasin, 'FIELD': []}, 4)

        # ---- AOIs ---------------------------------------------------------
        aoi_project = buffer(parcel, project_buf, 5)
        aoi_regional = buffer(subbasin, regional_buf, 6)

        # ---- Save to GeoPackage -------------------------------------------
        feedback.setCurrentStep(7)
        outputs = [
            (SITE_PARCEL, parcel),
            (SITE_SUBBASIN, subbasin),
            (AOI_PROJECT, aoi_project),
            (AOI_REGIONAL, aoi_regional),
        ]
        for name, layer_id in outputs:
            layer = QgsProcessingUtils.mapLayerFromString(layer_id, context)
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = 'GPKG'
            opts.layerName = name
            # Never wipe an existing GeoPackage - just replace these layers in it
            opts.actionOnExistingFile = (QgsVectorFileWriter.CreateOrOverwriteLayer
                                         if os.path.exists(gpkg)
                                         else QgsVectorFileWriter.CreateOrOverwriteFile)
            result = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, gpkg, context.transformContext(), opts)
            if result[0] != QgsVectorFileWriter.NoError:
                raise QgsProcessingException(f'Could not write {name} to {gpkg}: {result[1]}')
            feedback.pushInfo(f'Saved {name}')

        # ---- Update the project -------------------------------------------
        project = context.project() or QgsProject.instance()

        if self.parameterAsBoolean(parameters, self.SET_VARS, context):
            for name, _ in PROJECT_VARS:
                value = self.parameterAsString(parameters, name, context)
                QgsExpressionContextUtils.setProjectVariable(project, name, value)
            feedback.pushInfo('Project variables set.')

        project.setCrs(crs)
        feedback.pushInfo(f'Project CRS set to {crs.authid()}.')

        self._add_site_layers(project, gpkg, [n for n, _ in outputs], feedback)

        return {self.OUTPUT_GPKG: gpkg}

    def _add_site_layers(self, project, gpkg, names, feedback):
        root = project.layerTreeRoot()
        group = root.findGroup(SITE_GROUP) or root.insertGroup(0, SITE_GROUP)
        in_group = {n.layer().name(): n.layer() for n in group.findLayers() if n.layer()}

        for name in names:
            if name in in_group:  # re-run: layer already loaded, just refresh it
                in_group[name].reload()
                in_group[name].triggerRepaint()
                continue
            layer = QgsVectorLayer(f'{gpkg}|layername={name}', name, 'ogr')
            if not layer.isValid():
                feedback.reportError(f'Could not load {name} from {gpkg}')
                continue
            if name.startswith('aoi_'):
                self._outline_only(layer)
            project.addMapLayer(layer, False)
            group.addLayer(layer)

    @staticmethod
    def _outline_only(layer):
        # Make AOIs hollow so they don't hide the map underneath
        try:
            symbol_layer = layer.renderer().symbol().symbolLayer(0)
            symbol_layer.setBrushStyle(Qt.NoBrush)
            symbol_layer.setStrokeWidth(0.6)
        except AttributeError:
            pass

    def name(self):
        return 'site_setup'

    def displayName(self):
        return 'Site Setup (parcel, sub-basin, AOIs)'

    def group(self):
        return 'RDG'

    def groupId(self):
        return 'RDG'

    def shortHelpString(self):
        return """
<p>Run after creating the project from the regional template and saving it to the project folder.</p>
<p><b>Before running:</b> select the tax parcel(s) on the map, then tick <i>Selected features only</i>
next to the Tax parcel input.</p>
<p>The tool dissolves the selected parcels into one <b>site_parcel</b>, finds the sub-basin(s) the
parcel touches and dissolves them into <b>site_subbasin</b>, and buffers each one to make
<b>aoi_project</b> and <b>aoi_regional</b>. All four are saved in one GeoPackage and added to a
<b>Site</b> group. It also sets the project CRS and, if ticked, the project variables.</p>
<p>Buffer distances are in the units of the chosen CRS (feet for most State Plane CRSs, metres for UTM).</p>
<p>Next step: run <b>Prune to AOI</b>.</p>
"""

    def createInstance(self):
        return SiteSetup()
