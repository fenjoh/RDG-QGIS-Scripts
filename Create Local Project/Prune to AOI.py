"""
Prune to AOI - RDG QGIS Processing tool (part 2 of the new-project workflow)

Removes layers from the project that don't overlap their AOI:
  - layers in the "Project" group are checked against aoi_project
  - layers in the "Regional" group are checked against aoi_regional
(group names can be changed in the tool dialog; sub-groups are included)

Each layer is checked in two passes:
  1. Bounding box: is the layer's extent anywhere near the AOI? (fast)
  2. Geometry: does any feature actually touch the AOI? (vector layers only;
     stops at the first feature that does)

Only the layer is removed from the project - no data files are deleted.
If you change your mind, close the project without saving.
"""

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingOutputNumber,
    QgsProject,
    QgsGeometry,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeatureRequest,
    QgsVectorLayer,
    QgsRasterLayer,
)

# Web/remote layers are always kept: their extent is often "the whole world"
# and checking their features would mean downloading them.
REMOTE_PROVIDERS = {
    'wms', 'wcs', 'wfs', 'xyz', 'arcgismapserver', 'arcgisfeatureserver',
    'vectortile', 'oapif',
}


class PruneToAoi(QgsProcessingAlgorithm):
    PROJECT_AOI = 'PROJECT_AOI'
    REGIONAL_AOI = 'REGIONAL_AOI'
    PROJECT_GROUP = 'PROJECT_GROUP'
    REGIONAL_GROUP = 'REGIONAL_GROUP'
    DRY_RUN = 'DRY_RUN'
    REMOVED = 'REMOVED'
    KEPT = 'KEPT'

    def flags(self):
        # Removing layers changes the project, so run on the main thread.
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.PROJECT_AOI, 'Project AOI', [QgsProcessing.TypeVectorPolygon],
            defaultValue='aoi_project'))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.REGIONAL_AOI, 'Regional AOI', [QgsProcessing.TypeVectorPolygon],
            defaultValue='aoi_regional'))
        self.addParameter(QgsProcessingParameterString(
            self.PROJECT_GROUP, 'Layer group to check against the Project AOI',
            defaultValue='Project'))
        self.addParameter(QgsProcessingParameterString(
            self.REGIONAL_GROUP, 'Layer group to check against the Regional AOI',
            defaultValue='Regional'))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DRY_RUN, "Report only (don't remove anything)", defaultValue=False))
        self.addOutput(QgsProcessingOutputNumber(self.REMOVED, 'Layers removed'))
        self.addOutput(QgsProcessingOutputNumber(self.KEPT, 'Layers kept'))

    def processAlgorithm(self, parameters, context, feedback):
        project = context.project() or QgsProject.instance()
        root = project.layerTreeRoot()
        dry_run = self.parameterAsBoolean(parameters, self.DRY_RUN, context)

        aoi_project = self.parameterAsVectorLayer(parameters, self.PROJECT_AOI, context)
        aoi_regional = self.parameterAsVectorLayer(parameters, self.REGIONAL_AOI, context)
        protected = {aoi_project.id(), aoi_regional.id()}

        jobs = [
            (self.parameterAsString(parameters, self.PROJECT_GROUP, context), aoi_project),
            (self.parameterAsString(parameters, self.REGIONAL_GROUP, context), aoi_regional),
        ]

        # Collect the layers to check up front so progress can be reported
        checks = []
        for group_name, aoi in jobs:
            group = root.findGroup(group_name)
            if group is None:
                feedback.pushWarning(f'No layer group called "{group_name}" - skipped.')
                continue
            aoi_geom = self._aoi_geometry(aoi)
            for node in group.findLayers():
                layer = node.layer()
                if layer is not None and layer.id() not in protected:
                    checks.append((group_name, layer, aoi, aoi_geom))

        if not checks:
            feedback.pushWarning('No layers to check.')
            return {self.REMOVED: 0, self.KEPT: 0}

        to_remove = {}
        kept = 0
        current_group = None
        for i, (group_name, layer, aoi, aoi_geom) in enumerate(checks):
            if feedback.isCanceled():
                raise QgsProcessingException('Canceled - no layers were removed.')
            feedback.setProgress(100 * i / len(checks))
            if group_name != current_group:
                current_group = group_name
                feedback.pushInfo(f'\n--- {group_name} (against {aoi.name()}) ---')

            keep, reason = self._overlaps(layer, aoi_geom, aoi.crs(), project, feedback)
            if keep:
                kept += 1
                feedback.pushInfo(f'  keep    {layer.name()}  ({reason})')
            else:
                to_remove[layer.id()] = layer.name()
                feedback.pushInfo(f'  REMOVE  {layer.name()}  ({reason})')

        if dry_run:
            feedback.pushInfo(f'\nReport only: {len(to_remove)} layer(s) would be removed.')
        elif to_remove:
            project.removeMapLayers(list(to_remove))
            feedback.pushInfo(f'\nRemoved {len(to_remove)} layer(s). '
                              'Close without saving to undo.')

        return {self.REMOVED: 0 if dry_run else len(to_remove), self.KEPT: kept}

    @staticmethod
    def _aoi_geometry(aoi):
        geoms = [f.geometry() for f in aoi.getFeatures() if f.hasGeometry()]
        if not geoms:
            raise QgsProcessingException(f'The AOI layer "{aoi.name()}" has no polygons.')
        return QgsGeometry.unaryUnion(geoms)

    @staticmethod
    def _overlaps(layer, aoi_geom, aoi_crs, project, feedback):
        """Return (keep, reason). Anything that can't be checked is kept."""
        if not layer.isValid():
            return True, 'not checked - layer is broken'
        if layer.providerType() in REMOTE_PROVIDERS:
            return True, 'not checked - web layer'
        if not isinstance(layer, (QgsVectorLayer, QgsRasterLayer)):
            return True, 'not checked - unsupported layer type'
        if isinstance(layer, QgsVectorLayer) and not layer.isSpatial():
            return True, 'not checked - table without geometry'

        # Bring the AOI into the layer's CRS (cheaper than transforming every feature)
        aoi = QgsGeometry(aoi_geom)
        try:
            aoi.transform(QgsCoordinateTransform(aoi_crs, layer.crs(), project))
        except QgsCsException:
            return True, 'not checked - could not transform AOI to layer CRS'
        aoi_bbox = aoi.boundingBox()

        extent = layer.extent()
        if extent.isNull() or extent.isEmpty():
            return True, 'not checked - layer extent is empty'

        # Pass 1: bounding box
        if not extent.intersects(aoi_bbox):
            return False, 'outside AOI bounding box'

        # Rasters: compare the raster's footprint with the real AOI shape
        if isinstance(layer, QgsRasterLayer):
            if aoi.intersects(QgsGeometry.fromRect(extent)):
                return True, 'raster overlaps AOI'
            return False, 'raster outside AOI'

        # Pass 2: real geometry, only for features inside the AOI's bounding box
        engine = QgsGeometry.createGeometryEngine(aoi.constGet())
        engine.prepareGeometry()
        request = QgsFeatureRequest().setFilterRect(aoi_bbox).setNoAttributes()
        for feature in layer.getFeatures(request):
            if feedback.isCanceled():
                return True, 'canceled'
            if feature.hasGeometry() and engine.intersects(feature.geometry().constGet()):
                return True, 'has features in AOI'
        return False, 'no features in AOI'

    def name(self):
        return 'prune_to_aoi'

    def displayName(self):
        return 'Prune to AOI'

    def group(self):
        return 'RDG'

    def groupId(self):
        return 'RDG'

    def shortHelpString(self):
        return """
<p>Removes layers from the project that don't overlap their AOI. Run after <b>Site Setup</b>.</p>
<ul>
<li>Layers in the <b>Project</b> group (and its sub-groups) are checked against <b>aoi_project</b>.</li>
<li>Layers in the <b>Regional</b> group are checked against <b>aoi_regional</b>.</li>
</ul>
<p>Each layer is checked quickly by bounding box first; vector layers that pass are then checked
feature by feature, stopping at the first feature that touches the AOI.</p>
<p>Web layers, tables, broken layers and anything that can't be checked are always kept.
Layers are only removed from the project; no files are deleted. Tick <i>Report only</i> to see what
would be removed first, or close the project without saving to undo.</p>
"""

    def createInstance(self):
        return PruneToAoi()
