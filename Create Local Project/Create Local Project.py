"""
Model exported as python.
Name : project_clip_setup
Group : Temp_RDG
With QGIS : 32600
"""

from qgis.core import QgsProcessing
from qgis.core import QgsProcessingAlgorithm
from qgis.core import QgsProcessingMultiStepFeedback
from qgis.core import QgsProcessingFeedback
from qgis.core import QgsProcessingParameterRasterLayer
from qgis.core import QgsProcessingParameterVectorLayer
from qgis.core import QgsProcessingParameterMultipleLayers
from qgis.core import QgsProcessingParameterExtent
from qgis.core import QgsProcessingParameterFile
from qgis.core import QgsProcessingParameterFeatureSource
from qgis.core import QgsProcessingParameterRasterDestination
from qgis.core import QgsProcessingParameterFeatureSink
from qgis.core import QgsProcessingParameterDistance
from qgis.core import QgsExpressionContextUtils
from qgis.core import QgsProject
from qgis.core import QgsProcessingParameterString
from qgis.core import QgsProcessingParameterBoolean
import processing
from pathlib import Path
import os


class Project_clip_setup(QgsProcessingAlgorithm):

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterExtent('clip_extent', 'Clip Extent', defaultValue=None))
        self.addParameter(QgsProcessingParameterMultipleLayers('vector_layers', 'Vector Layers', optional=True, layerType=QgsProcessing.TypeVectorAnyGeometry, defaultValue=None))
        self.addParameter(QgsProcessingParameterMultipleLayers('raster_layers', 'Raster Layers', optional=True, layerType=QgsProcessing.TypeRaster, defaultValue=None))
        self.addParameter(QgsProcessingParameterFile('destination_folder', 'Destination Folder', behavior=QgsProcessingParameterFile.Folder, fileFilter='All files (*.*)', defaultValue=None))
        self.addParameter(QgsProcessingParameterBoolean('set_project_variables', 'Set Project Variables', optional=True, defaultValue=True))
        self.addParameter(QgsProcessingParameterString('project_code', 'Project Code', optional=True, multiLine=False, defaultValue= QgsExpressionContextUtils.projectScope(QgsProject.instance()).variable('project_code')))
        self.addParameter(QgsProcessingParameterString('project_client', 'Project Client', optional=True, multiLine=False, defaultValue= QgsExpressionContextUtils.projectScope(QgsProject.instance()).variable('project_client')))
        self.addParameter(QgsProcessingParameterString('project_address', 'Project Address', optional=True, multiLine=False, defaultValue=QgsExpressionContextUtils.projectScope(QgsProject.instance()).variable('project_address')))
        self.addParameter(QgsProcessingParameterString('project_town', 'Project Town', optional=True, multiLine=False, defaultValue=QgsExpressionContextUtils.projectScope(QgsProject.instance()).variable('project_town')))
        self.addParameter(QgsProcessingParameterString('project_state', 'Project State', optional=True, multiLine=False, defaultValue=QgsExpressionContextUtils.projectScope(QgsProject.instance()).variable('project_state')))


    def processAlgorithm(self, parameters, context, model_feedback):
        # Use a multi-step feedback, so that individual child algorithm progress reports are adjusted for the
        # overall progress through the model
        feedback = QgsProcessingMultiStepFeedback(7, model_feedback)
        results = {}
        outputs = {}
        
        # Define function to replace existing layer with clipped layer
        def replaceLayer():
            new_data_source = outFile
            base_name = layInfo.name()
            provider = layInfo.providerType()
            options = layInfo.dataProvider().ProviderOptions()
            layInfo.setDataSource(
                new_data_source,
                base_name,
                provider,
                options
            )
        
        # If set project variables is checked set these project variables
        if parameters['set_project_variables']:    
            # Set project variable
            alg_params = {
                'NAME': 'project_code',
                'VALUE': parameters['project_code']
            }
            outputs['SetProjectVariable'] = processing.run('native:setprojectvariable', alg_params, context=context, feedback=feedback, is_child_algorithm=True)

            alg_params = {
                'NAME': 'project_client',
                'VALUE': parameters['project_client']
            }
            outputs['SetProjectVariable'] = processing.run('native:setprojectvariable', alg_params, context=context, feedback=feedback, is_child_algorithm=True)

            feedback.setCurrentStep(1)
            if feedback.isCanceled():
                return {}

            # Set project variable
            alg_params = {
                'NAME': 'project_address',
                'VALUE': parameters['project_address']
            }
            outputs['SetProjectVariable'] = processing.run('native:setprojectvariable', alg_params, context=context, feedback=feedback, is_child_algorithm=True)

            feedback.setCurrentStep(2)
            if feedback.isCanceled():
                return {}

            # Set project variable
            alg_params = {
                'NAME': 'project_state',
                'VALUE': parameters['project_state']
            }
            outputs['SetProjectVariable'] = processing.run('native:setprojectvariable', alg_params, context=context, feedback=feedback, is_child_algorithm=True)

            feedback.setCurrentStep(3)
            if feedback.isCanceled():
                return {}

            # Set project variable
            alg_params = {
                'NAME': 'project_town',
                'VALUE': parameters['project_town']
            }
            outputs['SetProjectVariable'] = processing.run('native:setprojectvariable', alg_params, context=context, feedback=feedback, is_child_algorithm=True)

            feedback.setCurrentStep(4)
            if feedback.isCanceled():
                return {}
                
        projectCode = QgsExpressionContextUtils.projectScope(QgsProject.instance()).variable('project_code')

        # Raster processing
                        
        # Check if there are any raster layers
        if parameters['raster_layers']: 
            # Loop through raster layers
            for layer in parameters['raster_layers']:
                try:
                        
                    # Get layer info and name
                    layInfo = QgsProject.instance().mapLayer(layer)
                    oldLayerName = Path(layInfo.source()).stem
                    newLayerName = oldLayerName + '_' + projectCode + '.tif'
                    
                    # Create file path to save file to
                    outFile = os.path.join(parameters['destination_folder'], newLayerName)
                    
                    # Check if file already exists in destination 
                    # Todo - if there are files with the same name coming from differnt folders or a file with same name that is actualy a differnt dataset it will be set wrong.
                    if not Path(outFile).is_file():

                        # Run Clip Raster by Extent
                        alg_params = {
                            'DATA_TYPE': 0,  # Use Input Layer Data Type
                            'EXTRA': '',
                            'INPUT': layer,
                            'NODATA': None,
                            'OPTIONS': '',
                            'OVERCRS': False,
                            'PROJWIN': parameters['clip_extent'],#outputs['ExtractLayerExtent']['OUTPUT'],
                            'OUTPUT': QgsProcessing.TEMPORARY_OUTPUT
                        }
                        outputs['ClipRasterByExtent'] = processing.run('gdal:cliprasterbyextent', alg_params, context=context, feedback=feedback, is_child_algorithm=True)
                        feedback.pushInfo('extracted'+layer)
                                                        
                        # Write to the log? And be able to stop the algo?
                        feedback.setCurrentStep(5)
                        if feedback.isCanceled():
                            return {}
                            
                        # Warp (reproject)- This is so all the layers are in the project CRS
                        alg_params = {
                            'DATA_TYPE': 0,  # Use Input Layer Data Type
                            'EXTRA': '-crop_to_cutline',
                            'INPUT': outputs['ClipRasterByExtent']['OUTPUT'],
                            'MULTITHREADING': False,
                            'NODATA': None,
                            'OPTIONS': '',
                            'RESAMPLING': 0,  # NN not Bilinear (1)
                            'SOURCE_CRS': None,
                            'TARGET_CRS': 'ProjectCrs',
                            'TARGET_EXTENT': None,
                            'TARGET_EXTENT_CRS': None,
                            'TARGET_RESOLUTION': None,
                            'OUTPUT': outFile   #parameters['R_layer']
                        }
                        outputs['WarpReproject'] = processing.run('gdal:warpreproject', alg_params, context=context, feedback=feedback, is_child_algorithm=True)
                        
                        #results['R_layer'] = outputs['WarpReproject']['OUTPUT']
                        
                    # Replace layer datasource in QGIS with new clipped file
                    replaceLayer()

                    feedback.setCurrentStep(6)
                    if feedback.isCanceled():
                        return {}
                
                except:
                    feedback.reportError('there was an issue with ' + oldLayerName)
            
# Vector Processing
        
        # Check if there are any vector layers
        if parameters['vector_layers']: 
            # Loop through all the Vector layers 
            for layer in parameters['vector_layers']:
                try:
                    # Get layer name
                    layInfo = QgsProject.instance().mapLayer(layer)
                    oldLayerName = Path(layInfo.source()).stem
                    newLayerName = oldLayerName + '_' + projectCode + '.gpkg'

                    
                    # Create file path to save file to
                    # outFile = parameters['destination_folder'] + '//' + LayerName + '_' + projectCode + '.shp'
                    outFile = os.path.join(parameters['destination_folder'], newLayerName)

                    
                    # Check if file already exists in destination 
                    # Todo - if there are files with the same name coming from differnt folders or a file with same name that is actualy a differnt dataset it will be set wrong.
                    if not Path(outFile).is_file():
                        # Extract Vector layer by extent
                        alg_params = {
                            'CLIP': False,
                            'EXTENT': parameters['clip_extent'],#outputs['ExtractLayerExtent']['OUTPUT'],
                            'INPUT': layer,
                            'OUTPUT': QgsProcessing.TEMPORARY_OUTPUT
                        }
                        outputs['ExtractclipByExtent'] = processing.run('native:extractbyextent', alg_params, context=context, feedback=feedback, is_child_algorithm=True)

                        feedback.setCurrentStep(7)
                        if feedback.isCanceled():
                            return {}
                    
                        # Reproject layer - to make sure all layers are in the CRS of the project
                        alg_params = {
                            'INPUT': outputs['ExtractclipByExtent']['OUTPUT'],
                            'OPERATION': '',
                            'TARGET_CRS': 'ProjectCrs',
                            'OUTPUT': outFile #parameters['V_layer']
                        }
                        outputs['ReprojectLayer'] = processing.run('native:reprojectlayer', alg_params, context=context, feedback=feedback, is_child_algorithm=True)
                        #results['V_layer'] = outputs['ReprojectLayer']['OUTPUT']
                    
                    # Replace layer datasource in QGIS with new clipped file
                    replaceLayer()
                
                except:
                    feedback.reportError('there was an issue with ' + oldLayerName)

        return results

    def name(self):
        return 'Create_Local_project4'

    def displayName(self):
        return 'Create Local Project new'

    def group(self):
        return 'RDG'

    def groupId(self):
        return 'RDG'

    def shortHelpString(self):
        return """<html><body><p><!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN" "http://www.w3.org/TR/REC-html40/strict.dtd">
<html><head><meta name="qrichtext" content="1" /><style type="text/css">
</style></head><body style=" font-family:'MS Shell Dlg 2'; font-size:7.8pt; font-weight:400; font-style:normal;">
<p style=" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;">This tool creates an extracted or clipped version of the selected layers that are saved to the destination folder you select. 
The CRS of the layers will be reprojected to be in the CRS of the project. This will not create a new project file so you might want to do a save as first.</p>
<p style=" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;">The layers selected will have their data source replaced with the extracted or clipped version. </p></body></html></p>
<h2>Input parameters</h2>
<h3>Extent</h3>
<p>Select the extent that you want to have the layers clipped/selected by  
This can be the extent of the current map view or a layer or a selection box.</p>
<h3>Vector Layers</h3>
<p>Select the vector layers that you want to have extracted.
All features that intersect with the extent you selected will be exported as shapefiles.</p>
<h3>Raster Layers</h3>
<p>Select the Raster layers that you want to have clipped.
They will be clipped to the extent that you set.
If you try to export 2 raster layers that reference the same file it will crash QGIS! Run this tool twice instead.</p>
<h3>Project Variables</h3>
<p>If you don't want the project varaibles to be set uncheck the box otherwise make sure you fill out the variables. The prefilled values are the values currently set in the project</p>
<p><!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN" "http://www.w3.org/TR/REC-html40/strict.dtd">
<html><head><meta name="qrichtext" content="1" /><style type="text/css">
</style></head><body style=" font-family:'MS Shell Dlg 2'; font-size:7.8pt; font-weight:400; font-style:normal;">
<p style="-qt-paragraph-type:empty; margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;"><br /></p></body></html></p><br></body></html>"""

    def createInstance(self):
        return Project_clip_setup()
