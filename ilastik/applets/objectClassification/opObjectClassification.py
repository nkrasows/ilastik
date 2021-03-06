# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# Copyright 2011-2014, the ilastik developers

import numpy
import vigra
import time
import warnings
import itertools
from collections import defaultdict

from lazyflow.graph import Operator, InputSlot, OutputSlot
from lazyflow.stype import Opaque
from lazyflow.rtype import List
from lazyflow.operators import OpValueCache, OpSlicedBlockedArrayCache, OperatorWrapper
from lazyflow.request import Request, RequestPool, RequestLock
from functools import partial

from ilastik.utility import OperatorSubView, MultiLaneOperatorABC, OpMultiLaneWrapper
from ilastik.utility.mode import mode
from ilastik.applets.objectExtraction.opObjectExtraction import default_features_key

from ilastik.applets.base.applet import DatasetConstraintError

import logging
logger = logging.getLogger(__name__)

MISSING_VALUE = 0

class OpObjectClassification(Operator, MultiLaneOperatorABC):
    """The top-level operator for object classification.

    Most functionality is handled by specialized operators such as
    OpObjectTrain and OpObjectPredict.

    Also transfers existing labels if the upstream object segmentation
    changes. The transfer is conservative: labels only get transfered
    from an old object to a new object if they overlap sufficiently,
    and the label does not overlap with other objects.

    """
    name = "OpObjectClassification"
    category = "Top-level"

    ###############
    # Input slots #
    ###############
    BinaryImages = InputSlot(level=1) # for visualization
    RawImages = InputSlot(level=1) # for visualization
    SegmentationImages = InputSlot(level=1) #connected components

    # the actual feature arrays
    # same format as OpObjectExtraction.RegionFeatures
    ObjectFeatures = InputSlot(rtype=List, stype=Opaque, level=1)

    # the names of the features computed in the object extraction operator
    # same format as OpObjectExtraction.ComputedFeatureNames
    ComputedFeatureNames = InputSlot(rtype=List, stype=Opaque)

    # the features selected in our own GUI
    # same format as ComputedFeatureNames
    SelectedFeatures = InputSlot(rtype=List, stype=Opaque)

    LabelsAllowedFlags = InputSlot(stype='bool', level=1)
    LabelInputs = InputSlot(stype=Opaque, rtype=List, optional=True, level=1)
    
    FreezePredictions = InputSlot(stype='bool', value=False)

    # for reading from disk
    InputProbabilities = InputSlot(level=1, stype=Opaque, rtype=List, optional=True)

    ################
    # Output slots #
    ################
    NumLabels = OutputSlot()
    Classifier = OutputSlot()
    LabelImages = OutputSlot(level=1)
    Predictions = OutputSlot(level=1, stype=Opaque, rtype=List)
    Probabilities = OutputSlot(level=1, stype=Opaque, rtype=List)

    # pulls whatever is in the cache, but does not try to compute more.
    CachedProbabilities = OutputSlot(level=1, stype=Opaque, rtype=List)

    PredictionImages = OutputSlot(level=1) #Labels, by the majority vote
    UncachedPredictionImages = OutputSlot(level=1)
    PredictionProbabilityChannels = OutputSlot(level=2) # Classification predictions, enumerated by channel
    SegmentationImagesOut = OutputSlot(level=1) #input connected components
    BadObjects = OutputSlot(level=1, stype=Opaque, rtype=List) #Objects with NaN-like features
    BadObjectImages = OutputSlot(level=1) #Images, where objects with NaN-like features are black
    Warnings = OutputSlot(stype=Opaque) #Warnings about objects with NaN-like features encountered in training

    # Used for labeling
    Eraser = OutputSlot()
    DeleteLabel = OutputSlot()

    # GUI-only (not part of the pipeline, but saved to the project)
    LabelNames = OutputSlot()
    LabelColors = OutputSlot()
    PmapColors = OutputSlot()


    def __init__(self, *args, **kwargs):
        super(OpObjectClassification, self).__init__(*args, **kwargs)

        # internal operators
        opkwargs = dict(parent=self)
        self.opTrain = OpObjectTrain(parent=self)
        self.opPredict = OpMultiLaneWrapper(OpObjectPredict, **opkwargs)
        self.opLabelsToImage = OpMultiLaneWrapper(OpRelabelSegmentation, **opkwargs)
        self.opPredictionsToImage = OpMultiLaneWrapper(OpRelabelSegmentation, **opkwargs)
        self.opPredictionImageCache = OpMultiLaneWrapper(OpSlicedBlockedArrayCache, **opkwargs)
        self.opPredictionImageCache.name="OpObjectClassification.opPredictionImageCache"
        
        self.opProbabilityChannelsToImage = OpMultiLaneWrapper(OpMultiRelabelSegmentation, **opkwargs)
        self.opBadObjectsToImage = OpMultiLaneWrapper(OpRelabelSegmentation, **opkwargs)
        self.opBadObjectsToWarningMessage = OpBadObjectsToWarningMessage(parent=self)

        self.classifier_cache = OpValueCache(parent=self)
        self.classifier_cache.name = "OpObjectClassification.classifier_cache"

        # connect inputs
        self.opTrain.Features.connect(self.ObjectFeatures)
        self.opTrain.Labels.connect(self.LabelInputs)
        self.opTrain.FixClassifier.setValue(False)
        self.opTrain.SelectedFeatures.connect(self.SelectedFeatures)

        self.classifier_cache.Input.connect(self.opTrain.Classifier)

        # Find the highest label in all the label images
        self.opMaxLabel = OpMaxLabel( parent=self )
        self.opMaxLabel.Inputs.connect( self.LabelInputs )

        self.opPredict.Features.connect(self.ObjectFeatures)
        self.opPredict.Classifier.connect(self.classifier_cache.Output)
        self.opPredict.SelectedFeatures.connect(self.SelectedFeatures)

        # Not directly connected.  Must always use setValue() to update.
        # See _updateNumClasses()
        # self.opPredict.LabelsCount.connect(self.opMaxLabel.Output) # See _updateNumClasses()

        self.opLabelsToImage.Image.connect(self.SegmentationImages)
        self.opLabelsToImage.ObjectMap.connect(self.LabelInputs)
        self.opLabelsToImage.Features.connect(self.ObjectFeatures)

        self.opPredictionsToImage.Image.connect(self.SegmentationImages)
        self.opPredictionsToImage.ObjectMap.connect(self.opPredict.Predictions)
        self.opPredictionsToImage.Features.connect(self.ObjectFeatures)

        #self.opPredictionImageCache.name = "prediction_image_cache"
        self.opPredictionImageCache.fixAtCurrent.connect( self.FreezePredictions )
        self.opPredictionImageCache.Input.connect( self.opPredictionsToImage.Output )

        self.opProbabilityChannelsToImage.Image.connect(self.SegmentationImages)
        self.opProbabilityChannelsToImage.ObjectMaps.connect(self.opPredict.ProbabilityChannels)
        self.opProbabilityChannelsToImage.Features.connect(self.ObjectFeatures)
        
        class OpWrappedCache(Operator):
            """
            This quick hack is necessary because there's not currently a way to wrap an OperatorWrapper.
            We need to double-wrap the cache, so we need this operator to provide the first level of wrapping.
            """
            Input = InputSlot(level=1) 
            innerBlockShape = InputSlot()
            outerBlockShape = InputSlot()
            fixAtCurrent = InputSlot(value = False)
    
            Output = OutputSlot(level=1)
    
            def __init__(self, *args, **kwargs):
                super( OpWrappedCache, self ).__init__( *args, **kwargs )
                self._innerOperator = OperatorWrapper( OpSlicedBlockedArrayCache, parent=self )
                self._innerOperator.Input.connect( self.Input )
                self._innerOperator.fixAtCurrent.connect( self.fixAtCurrent )
                self._innerOperator.innerBlockShape.connect( self.innerBlockShape )
                self._innerOperator.outerBlockShape.connect( self.outerBlockShape )
                self.Output.connect( self._innerOperator.Output )
                
            def execute(self, slot, subindex, roi, destination):
                assert False, "Shouldn't get here."
    
            def propagateDirty(self, slot, subindex, roi):
                pass # Nothing to do...

        # Wrap the cache for probability channels twice TWICE.
        self.opProbChannelsImageCache = OpMultiLaneWrapper( OpWrappedCache, parent=self )
        self.opProbChannelsImageCache.name = "OpObjectClassification.opProbChannelsImageCache"
        self.opProbChannelsImageCache.Input.connect(self.opProbabilityChannelsToImage.Output)
        self.opProbChannelsImageCache.fixAtCurrent.connect( self.FreezePredictions )
        
        
        self.opBadObjectsToImage.Image.connect(self.SegmentationImages)
        self.opBadObjectsToImage.ObjectMap.connect(self.opPredict.BadObjects)
        self.opBadObjectsToImage.Features.connect(self.ObjectFeatures)

        self.opBadObjectsToWarningMessage.BadObjects.connect(self.opTrain.BadObjects)

        self.opPredict.InputProbabilities.connect(self.InputProbabilities)

        def _updateNumClasses(*args):
            """
            When the number of labels changes, we MUST make sure that the prediction image changes its shape (the number of channels).
            Since setupOutputs is not called for mere dirty notifications, but is called in response to setValue(),
            we use this function to call setValue().
            """
            numClasses = len(self.LabelNames.value)
            self.opPredict.LabelsCount.setValue( numClasses )
            self.NumLabels.setValue( numClasses )
        self.LabelNames.notifyDirty( _updateNumClasses )

        self.LabelNames.setValue( [] )
        self.LabelColors.setValue( [] )
        self.PmapColors.setValue( [] )

        # connect outputs
        self.LabelImages.connect(self.opLabelsToImage.Output)
        self.Predictions.connect(self.opPredict.Predictions)
        self.Probabilities.connect(self.opPredict.Probabilities)
        self.CachedProbabilities.connect(self.opPredict.CachedProbabilities)
        self.PredictionImages.connect(self.opPredictionImageCache.Output)
        self.UncachedPredictionImages.connect(self.opPredictionsToImage.Output)
        self.PredictionProbabilityChannels.connect(self.opProbChannelsImageCache.Output)
        self.BadObjects.connect(self.opPredict.BadObjects)
        self.BadObjectImages.connect(self.opBadObjectsToImage.Output)
        self.Warnings.connect(self.opBadObjectsToWarningMessage.WarningMessage)

        self.Classifier.connect(self.classifier_cache.Output)

        self.SegmentationImagesOut.connect(self.SegmentationImages)

        # Not directly connected.  Must always use setValue() to update.
        # See _updateNumClasses()
        # self.NumLabels.connect( self.opMaxLabel.Output )


        self.Eraser.setValue(100)
        self.DeleteLabel.setValue(-1)

        self._labelBBoxes = []
        self._ambiguousLabels = []
        self._needLabelTransfer = False

        def handleNewInputImage(multislot, index, *args):
            def handleInputReady(slot):
                self.setupCaches(multislot.index(slot))
            multislot[index].notifyReady(handleInputReady)

        self.SegmentationImages.notifyInserted(handleNewInputImage)

        self._predict_enabled = False

    def setupCaches(self, imageIndex):
        """Setup the label input and caches to correct dimensions"""
        numImages=len(self.SegmentationImages)
        cctype = self.SegmentationImages[imageIndex].meta.dtype
        if not issubclass(cctype, numpy.integer):
            msg = "Connected Components image should be of integer type.\n"\
                  "Ask your workflow developer to change the input applet accordingly.\n"
            raise DatasetConstraintError("Object Classification", msg)
        self.LabelInputs.resize(numImages)
        self.LabelInputs[imageIndex].meta.shape = (1,)
        self.LabelInputs[imageIndex].meta.dtype = object
        self.LabelInputs[imageIndex].meta.mapping_dtype = numpy.uint8
        self.LabelInputs[imageIndex].meta.axistags = None

        self._resetLabelInputs(imageIndex)
        
        

    def _resetLabelInputs(self, imageIndex, roi=None):
        labels = dict()
        for t in range(self.SegmentationImages[imageIndex].meta.shape[0]):
            #initialize, because volumina needs to reshape to use it as a datasink
            labels[t] = numpy.zeros((2,))
        self.LabelInputs[imageIndex].setValue(labels)
        if imageIndex in range(len(self._ambiguousLabels)):
            self._ambiguousLabels[imageIndex] = None
            self._labelBBoxes[imageIndex] = dict()
        else:
            self._ambiguousLabels.insert(imageIndex, None)
            self._labelBBoxes.insert(imageIndex, dict())

    def removeLabel(self, label):
        #remove this label from the inputs
        for islot, label_slot in enumerate(self.LabelInputs):
            if not label_slot.ready() or islot>= len(self.RawImages) or \
                not self.RawImages[islot].ready():
            
                continue
                
            cur_labels = label_slot.value
            nTimes = self.RawImages[islot].meta.shape[0]
            nLabels = len(self.LabelNames.value)+1 #+1 because we already took out the name in labelingGui
            for t in range(nTimes):
                label_values = cur_labels[t]
                label_values[label_values==label+1] = 0
                for nextLabel in range(label, nLabels):
                    label_values[label_values==nextLabel+1]=nextLabel
        self.LabelInputs.setDirty([])

    def setupOutputs(self):
        self.Warnings.meta.shape = (1,)
        axisOrder = [ tag.key for tag in self.RawImages[0].meta.axistags ]

        blockDimsX = { 't' : (1,1),
                       'z' : (128,256),
                       'y' : (128,256),
                       'x' : (1,1),
                       'c' : (100, 100) }

        blockDimsY = { 't' : (1,1),
                       'z' : (128,256),
                       'y' : (1,1),
                       'x' : (128,256),
                       'c' : (100,100) }

        blockDimsZ = { 't' : (1,1),
                       'z' : (1,1),
                       'y' : (128,256),
                       'x' : (128,256),
                       'c' : (100,100) }

        innerBlockShapeX = tuple( blockDimsX[k][0] for k in axisOrder )
        outerBlockShapeX = tuple( blockDimsX[k][1] for k in axisOrder )

        innerBlockShapeY = tuple( blockDimsY[k][0] for k in axisOrder )
        outerBlockShapeY = tuple( blockDimsY[k][1] for k in axisOrder )

        innerBlockShapeZ = tuple( blockDimsZ[k][0] for k in axisOrder )
        outerBlockShapeZ = tuple( blockDimsZ[k][1] for k in axisOrder )

        self.opPredictionImageCache.innerBlockShape.setValue( (innerBlockShapeX, innerBlockShapeY, innerBlockShapeZ) )
        self.opPredictionImageCache.outerBlockShape.setValue( (outerBlockShapeX, outerBlockShapeY, outerBlockShapeZ) )
        self.opProbChannelsImageCache.innerBlockShape.setValue( (innerBlockShapeX, innerBlockShapeY, innerBlockShapeZ) )
        self.opProbChannelsImageCache.outerBlockShape.setValue( (outerBlockShapeX, outerBlockShapeY, outerBlockShapeZ) )

    def setInSlot(self, slot, subindex, roi, value):
        pass

    def propagateDirty(self, slot, subindex, roi):
        if slot==self.SegmentationImages and len(self.LabelInputs)>0:
            
            self._ambiguousLabels[subindex[0]] = self.LabelInputs[subindex[0]].value
            self._needLabelTransfer = True


    def assignObjectLabel(self, imageIndex, coordinate, assignedLabel):
        """
        Update the assigned label of the object located at the given coordinate.
        Does nothing if no object resides at the given coordinate.
        """
        segmentationShape = self.SegmentationImagesOut[imageIndex].meta.shape
        assert len(coordinate) == len( segmentationShape ), "Coordinate: {} is has the wrong length for this image, which is of shape: {}".format( coordinate, segmentationShape )
        slicing = tuple(slice(i, i+1) for i in coordinate)
        arr = self.SegmentationImagesOut[imageIndex][slicing].wait()

        objIndex = arr.flat[0]
        if objIndex == 0: # background; FIXME: do not hardcode
            return
        timeCoord = coordinate[0]
        labelslot = self.LabelInputs[imageIndex]
        labelsdict = labelslot.value
        labels = labelsdict[timeCoord]

        nobjects = len(labels)
        if objIndex >= nobjects:
            newLabels = numpy.zeros((objIndex + 1),)
            newLabels[:nobjects] = labels[:]
            labels = newLabels
        labels[objIndex] = assignedLabel
        labelsdict[timeCoord] = labels
        labelslot.setValue(labelsdict)
        labelslot.setDirty([(timeCoord, objIndex)])

        #Fill the cache of label bounding boxes, if it was empty
        if len(self._labelBBoxes[imageIndex].keys())==0:
            #it's the first label for this image
            feats = self.ObjectFeatures[imageIndex]([timeCoord]).wait()

            #the bboxes should be the same for all channels
            mins = feats[timeCoord][default_features_key]["Coord<Minimum>"]
            maxs = feats[timeCoord][default_features_key]["Coord<Maximum>"]
            bboxes = dict()
            bboxes["Coord<Minimum>"] = mins
            bboxes["Coord<Maximum>"] = maxs
            self._labelBBoxes[imageIndex][timeCoord]=bboxes

    def triggerTransferLabels(self, imageIndex):
        if not self._needLabelTransfer:
            return None
        if not self.SegmentationImages[imageIndex].ready():
            return None
        if len(self._labelBBoxes[imageIndex].keys())==0:
            #we either don't have any labels or we just read the project from file
            #nothing to transfer
            self._needLabelTransfer = False
            return None

        labels = dict()
        for timeCoord in range(self.SegmentationImages[imageIndex].meta.shape[0]):
            #we have to get new object features to get bounding boxes
            logger.info("Transferring labels to the new segmentation. This might take a while...")
            new_feats = self.ObjectFeatures[imageIndex]([timeCoord]).wait()
            coords = dict()
            coords["Coord<Minimum>"] = new_feats[timeCoord][default_features_key]["Coord<Minimum>"]
            coords["Coord<Maximum>"] = new_feats[timeCoord][default_features_key]["Coord<Maximum>"]
            #FIXME: pass axistags
            new_labels, old_labels_lost, new_labels_lost = self.transferLabels(
                self._ambiguousLabels[imageIndex][timeCoord],
                self._labelBBoxes[imageIndex][timeCoord],
                coords
            )
            labels[timeCoord] = new_labels

            self._labelBBoxes[imageIndex][timeCoord]=coords
            self._ambiguousLabels[imageIndex][timeCoord]=numpy.zeros((2,)) #initialize ambig. labels as normal labels

        self.LabelInputs[imageIndex].setValue(labels)
        self._needLabelTransfer = False

        return new_labels, old_labels_lost, new_labels_lost

    @staticmethod
    def transferLabels(old_labels, old_bboxes, new_bboxes, axistags = None):
        #transfer labels from old segmentation to new segmentation

        mins_old = old_bboxes["Coord<Minimum>"]
        maxs_old = old_bboxes["Coord<Maximum>"]
        mins_new = new_bboxes["Coord<Minimum>"]
        maxs_new = new_bboxes["Coord<Maximum>"]
        nobj_old = mins_old.shape[0]
        nobj_new = mins_new.shape[0]
        if axistags is None:
            axistags = "xyz"
        
        data2D = False
        if mins_old.shape[1]==2:
            data2D = True
        class bbox():
            def __init__(self, minmaxs, axistags):
                self.xmin = minmaxs[0][axistags.index('x')]
                self.ymin = minmaxs[0][axistags.index('y')]
                if not data2D:
                    self.zmin = minmaxs[0][axistags.index('z')]
                else:
                    self.zmin = 0
                self.xmax = minmaxs[1][axistags.index('x')]
                self.ymax = minmaxs[1][axistags.index('y')]
                if not data2D:
                    self.zmax = minmaxs[1][axistags.index('z')]
                else:
                    self.zmax = 0
                self.rad_x = 0.5*(self.xmax - self.xmin)
                self.cent_x = self.xmin+self.rad_x
                self.rad_y = 0.5*(self.ymax-self.ymin)
                self.cent_y = self.ymin+self.rad_y
                self.rad_z = 0.5*(self.zmax-self.zmin)
                self.cent_z = self.zmin+self.rad_z

            @staticmethod
            def overlap(bbox_tuple):
                this = bbox_tuple[0]
                that = bbox_tuple[1]
                over_x = this.rad_x+that.rad_x - (abs(this.cent_x-that.cent_x))
                over_y = this.rad_y+that.rad_y - (abs(this.cent_y-that.cent_y))
                over_z = this.rad_z+that.rad_z - (abs(this.cent_z-that.cent_z))
                if not data2D:
                    if over_x>0 and over_y>0 and over_z>0:
                        return over_x*over_y*over_z
                else:
                    if over_x>0 and over_y>0:
                        return over_x*over_y
                return 0

        nonzeros = numpy.nonzero(old_labels)[0]
        bboxes_old = [bbox(x, axistags) for x in zip(mins_old[nonzeros], maxs_old[nonzeros])]
        bboxes_new = [bbox(x, axistags) for x in zip(mins_new, maxs_new)]

        #remove background
        #FIXME: assuming background is 0 again
        bboxes_new = bboxes_new[1:]

        double_for_loop = itertools.product(bboxes_old, bboxes_new)
        overlaps = map(bbox.overlap, double_for_loop)

        overlaps = numpy.asarray(overlaps)
        overlaps = overlaps.reshape((len(bboxes_old), len(bboxes_new)))
        new_labels = numpy.zeros((nobj_new,), dtype=numpy.uint32)
        old_labels_lost = dict()
        old_labels_lost["full"]=[]
        old_labels_lost["partial"]=[]
        new_labels_lost = dict()
        new_labels_lost["conflict"]=[]
        for iobj in range(overlaps.shape[0]):
            #take the object with maximum overlap
            overlapsum = numpy.sum(overlaps[iobj, :])
            if overlapsum==0:
                old_labels_lost["full"].append((bboxes_old[iobj].cent_x, bboxes_old[iobj].cent_y, bboxes_old[iobj].cent_z))
                continue
            newindex = numpy.argmax(overlaps[iobj, :])
            if overlapsum-overlaps[iobj,newindex]>0:
                #this object overlaps with more than one new object
                old_labels_lost["partial"].append((bboxes_old[iobj].cent_x, bboxes_old[iobj].cent_y, bboxes_old[iobj].cent_z))

            overlaps[iobj, :] = 0
            overlaps[iobj, newindex] = 1 #doesn't matter what number>0

        for iobj in range(overlaps.shape[1]):
            labels = numpy.where(overlaps[:, iobj]>0)[0]
            if labels.shape[0]==1:
                new_labels[iobj+1]=old_labels[nonzeros[labels[0]]] #iobj+1 because of the background
            elif labels.shape[0]>1:
                new_labels_lost["conflict"].append((bboxes_new[iobj].cent_x, bboxes_new[iobj].cent_y, bboxes_new[iobj].cent_z))

        new_labels = new_labels
        new_labels[0]=0 #FIXME: hardcoded background value again
        return new_labels, old_labels_lost, new_labels_lost


    def addLane(self, laneIndex):
        numLanes = len(self.SegmentationImages)
        assert numLanes == laneIndex, "Image lanes must be appended."
        for slot in self.inputs.values():
            if slot.level > 0 and len(slot) == laneIndex:
                slot.resize(numLanes + 1)

        

    def removeLane(self, laneIndex, finalLength):
        for slot in self.inputs.values():
            if slot.level > 0 and len(slot) == finalLength + 1:
                slot.removeSlot(laneIndex, finalLength)

        try:
            self._ambiguousLabels.pop(laneIndex)
            self._labelBBoxes.pop(laneIndex)
        except:
            #FIXME: sometimes this pop is called for no reason and makes the project unusable. We should fix the underlying issue.
            pass

    def getLane(self, laneIndex):
        return OperatorSubView(self, laneIndex)


def _atleast_nd(a, ndim):
    """Like numpy.atleast_1d and friends, but supports arbitrary ndim,
    always puts extra dimensions last, and resizes.

    """
    if ndim < a.ndim:
        return
    nnew = ndim - a.ndim
    newshape = tuple(list(a.shape) + [1] * nnew)
    a.resize(newshape)


def _concatenate(arrays, axis):
    """wrapper to numpy.concatenate that resizes arrays first."""
    arrays = list(a for a in arrays if 0 not in a.shape)
    if len(arrays) == 0:
        return numpy.array([])
    maxd = max(max(a.ndim for a in arrays), 2)
    for a in arrays:
        _atleast_nd(a, maxd)
    return numpy.concatenate(arrays, axis=axis)


def make_feature_array(feats, selected, labels=None):
    featlist = []
    labellist = []

    row_names = []
    col_names = []

    for t in sorted(feats.keys()):
        featsMatrix_tmp = []
        index = None
        if labels is not None:
            labellist_tmp = []
            lab = labels[t].squeeze()
            index = numpy.nonzero(lab)
            labellist_tmp.append(lab[index])

        timestep_col_names = []
        for plugin in sorted(feats[t].keys()):
            if plugin == default_features_key or plugin not in selected:
                continue
            for featname in sorted(feats[t][plugin].keys()):
                if featname not in selected[plugin]:
                    continue
                value = feats[t][plugin][featname]
                ft = numpy.asarray(value.squeeze())
                if index is not None:
                    ft = ft[index]
                featsMatrix_tmp.append(ft)
                timestep_col_names.extend([(plugin, featname)] * value.shape[1])
        if not col_names:
            col_names = timestep_col_names
        elif col_names != timestep_col_names:
            raise Exception('different time slices did not have same features.')
            
        #FIXME: we can do it all with just arrays
        featsMatrix_tmp_combined = _concatenate(featsMatrix_tmp, axis=1)
        featlist.append(featsMatrix_tmp_combined)
        if index is not None:
            row_names.extend(list((t, obj) for obj in index[0]))
        if labels is not None:
            labellist_tmp_combined = _concatenate(labellist_tmp, axis=1)
            labellist.append(labellist_tmp_combined)

    featMatrix = _concatenate(featlist, axis=0)

    if labels is not None:
        labelsMatrix = _concatenate(labellist, axis=0)
        assert labelsMatrix.shape[0] == featMatrix.shape[0]
        return featMatrix, row_names, col_names, labelsMatrix
    return featMatrix, row_names, col_names


def replace_missing(a):
    rows, cols = numpy.where(numpy.isnan(a) + numpy.isinf(a))
    idx = (rows, cols)
    rows = list(set(rows.flat))
    cols = list(set(cols.flat))
    a[idx] = MISSING_VALUE
    return rows, cols


class OpObjectTrain(Operator):
    """Trains a random forest on all labeled objects."""

    name = "TrainRandomForestObjects"
    description = "Train a random forest on multiple images"
    category = "Learning"

    Labels = InputSlot(level=1, stype=Opaque, rtype=List)
    Features = InputSlot(level=1, rtype=List, stype=Opaque)
    SelectedFeatures = InputSlot(rtype=List, stype=Opaque)
    FixClassifier = InputSlot(stype="bool")
    ForestCount = InputSlot(stype="int", value=1)

    Classifier = OutputSlot()
    BadObjects = OutputSlot(stype=Opaque)

    def __init__(self, *args, **kwargs):
        super(OpObjectTrain, self).__init__(*args, **kwargs)
        self._tree_count = 100
        self.FixClassifier.setValue(False)

    def setupOutputs(self):
        if self.inputs["FixClassifier"].value == False:
            self.outputs["Classifier"].meta.dtype = object
            self.outputs["Classifier"].meta.shape = (self.ForestCount.value,)
            self.outputs["Classifier"].meta.axistags = None

        self.BadObjects.meta.shape = (1,)
        self.BadObjects.meta.dtype = object
        self.BadObjects.meta.axistags = None

    def execute(self, slot, subindex, roi, result):
        featList = []
        all_col_names = []
        labelsList = []

        # will be available at slot self.Warnings
        all_bad_objects = defaultdict(lambda: defaultdict(list))
        all_bad_feats = set()

        selected = self.SelectedFeatures([]).wait()
        if len(selected)==0:
            # no features - no predictions
            self.Classifier.setValue(None)
            return
        
        for i in range(len(self.Labels)):
            # this loop is by image, not time! 

            # TODO: we should be able to use self.Labels[i].value,
            # but the current implementation of Slot.value() does not
            # do the right thing.
            labels_image = self.Labels[i]([]).wait()
            labels_image_filtered = {}
            nztimes = []
            for timestep, labels_time in labels_image.iteritems():
                nz = numpy.nonzero(labels_time)
                if len(nz[0])==0:
                    continue
                else:
                    nztimes.append(timestep)
                    labels_image_filtered[timestep] = labels_time

            if len(nztimes)==0:
                continue
            # compute the features if there are nonzero labels in this image
            # and only for the time steps, which have labels
            feats = self.Features[i](nztimes).wait()

            featstmp, row_names, col_names, labelstmp = make_feature_array(feats, selected, labels_image_filtered)
            if labelstmp.size == 0 or featstmp.size == 0:
                continue

            rows, cols = replace_missing(featstmp)

            featList.append(featstmp)
            all_col_names.append(tuple(col_names))
            labelsList.append(labelstmp)

            for idx in rows:
                t, obj = row_names[idx]
                all_bad_objects[i][t].append(obj)

            for c in cols:
                all_bad_feats.add(col_names[c])


        if len(labelsList)==0:
            #no labels, return here
            self.Classifier.setValue(None)
            return
        
        
        self._warnBadObjects(all_bad_objects, all_bad_feats)

        if not len(set(all_col_names)) == 1:
            raise Exception('different time slices did not have same features.')

        featMatrix = _concatenate(featList, axis=0)
        labelsMatrix = _concatenate(labelsList, axis=0)
        
        logger.info("training on matrix of shape {}".format(featMatrix.shape))

        if featMatrix.size == 0 or labelsMatrix.size == 0:
            result[:] = None
            return
        oob = [0] * self.ForestCount.value
        try:
            # train and store forests in parallel
            pool = RequestPool()
            for i in range(self.ForestCount.value):
                def train_and_store(number):
                    result[number] = vigra.learning.RandomForest(self._tree_count)
                    oob[number] = result[number].learnRF(featMatrix.astype(numpy.float32), numpy.asarray(labelsMatrix, dtype=numpy.uint32))
                req = Request( partial(train_and_store, i) )
                pool.add( req )
            pool.wait()
            pool.clean()
        except:
            logger.warn("couldn't learn classifier")
            raise
        oob_total = numpy.mean(oob)
        logger.info("training finished, out of bag error: {}".format(oob_total))
        return result

    def propagateDirty(self, slot, subindex, roi):
        if slot is not self.FixClassifier and \
           self.inputs["FixClassifier"].value == False:
            slcs = (slice(0, self.ForestCount.value, None),)
            self.outputs["Classifier"].setDirty(slcs)

    def _warnBadObjects(self, bad_objects, bad_feats):
        messageTesting = False
        if len(bad_feats)>0 or any([len(bad_objects[i])>0 for i in bad_objects.keys()]) or messageTesting:
            self.BadObjects.setValue( {'objects': bad_objects, 'feats': bad_feats} )


class OpObjectPredict(Operator):
    """Predicts object labels in a single image.

    Performs prediction on all objects in a time slice at once, and
    caches the result.

    """
    # WARNING: right now we predict and cache a whole time slice. We
    # expect this to be fast because there are relatively few objects
    # compared to the number of pixels in pixel classification. If
    # this should be too slow, we should instead cache at the object
    # level, and only predict for objects visible in the roi.

    name = "OpObjectPredict"

    Features = InputSlot(rtype=List, stype=Opaque)
    SelectedFeatures = InputSlot(rtype=List, stype=Opaque)
    Classifier = InputSlot()
    LabelsCount = InputSlot(stype='integer')
    InputProbabilities = InputSlot(stype=Opaque, rtype=List, optional=True)

    Predictions = OutputSlot(stype=Opaque, rtype=List)
    Probabilities = OutputSlot(stype=Opaque, rtype=List)
    CachedProbabilities = OutputSlot(stype=Opaque, rtype=List)
    ProbabilityChannels = OutputSlot(stype=Opaque, rtype=List, level=1)
    BadObjects = OutputSlot(stype=Opaque, rtype=List)

    #SegmentationThreshold = 0.5

    def setupOutputs(self):
        self.Predictions.meta.shape = self.Features.meta.shape
        self.Predictions.meta.dtype = object
        self.Predictions.meta.axistags = None
        self.Predictions.meta.mapping_dtype = numpy.uint8

        self.Probabilities.meta.shape = self.Features.meta.shape
        self.Probabilities.meta.dtype = object
        self.Probabilities.meta.mapping_dtype = numpy.float32
        self.Probabilities.meta.axistags = None

        self.BadObjects.meta.shape = self.Features.meta.shape
        self.BadObjects.meta.dtype = object
        self.BadObjects.meta.mapping_dtype = numpy.uint8
        self.BadObjects.meta.axistags = None

        if self.LabelsCount.ready():
            nlabels = self.LabelsCount[:].wait()
            nlabels = int(nlabels[0])
            self.ProbabilityChannels.resize(nlabels)
            for oslot in self.ProbabilityChannels:
                oslot.meta.shape = self.Features.meta.shape
                oslot.meta.dtype = object
                oslot.meta.axistags = None
                oslot.meta.mapping_dtype = numpy.float32

        self.lock = RequestLock()
        self.prob_cache = dict()
        self.bad_objects = dict()

    def execute(self, slot, subindex, roi, result):
        assert slot in [self.Predictions,
                        self.Probabilities,
                        self.CachedProbabilities,
                        self.ProbabilityChannels,
                        self.BadObjects]

        times = roi._l
        if len(times) == 0:
            # we assume that 0-length requests are requesting everything
            times = range(self.Predictions.meta.shape[0])

        if slot is self.CachedProbabilities:
            return {t: self.prob_cache[t] for t in times if t in self.prob_cache}

        forests=self.inputs["Classifier"][:].wait()
        if forests is None or forests[0] is None:
            # this happens if there was no data to train with
            return dict((t, numpy.array([])) for t in times)

        feats = {}
        prob_predictions = {}

        selected = self.SelectedFeatures([]).wait()

        # FIXME: self.prob_cache is shared, so we need to block.
        # However, this makes prediction single-threaded.
        self.lock.acquire()
        try:
            for t in times:
                if t in self.prob_cache:
                    continue

                tmpfeats = self.Features([t]).wait()
                ftmatrix, _, col_names = make_feature_array(tmpfeats, selected)
                rows, cols = replace_missing(ftmatrix)
                self.bad_objects[t] = numpy.zeros((ftmatrix.shape[0],))
                self.bad_objects[t][rows] = 1
                feats[t] = ftmatrix
                prob_predictions[t] = [0] * len(forests)

            def predict_forest(_t, forest_index):
                # Note: We can't use RandomForest.predictLabels() here because we're training in parallel,
                #        and we have to average the PROBABILITIES from all forests.
                #       Averaging the label predictions from each forest is NOT equivalent.
                #       For details please see wikipedia:
                #       http://en.wikipedia.org/wiki/Electoral_College_%28United_States%29#Irrelevancy_of_national_popular_vote
                #       (^-^)
                prob_predictions[_t][forest_index] = forests[forest_index].predictProbabilities(feats[_t].astype(numpy.float32))

            # predict the data with all the forests in parallel
            pool = RequestPool()
            for t in times:
                if t in self.prob_cache:
                    continue
                for i, f in enumerate(forests):
                    req = Request( partial(predict_forest, t, i) )
                    pool.add(req)

            pool.wait()
            pool.clean()

            for t in times:
                if t not in self.prob_cache:
                    # prob_predictions is a dict-of-lists-of-arrays, indexed as follows:
                    # prob_predictions[t][forest_index][object_index, class_index]

                    # Stack the forests together and average them.
                    stacked_predictions = numpy.array( prob_predictions[t] )
                    averaged_predictions = numpy.average( stacked_predictions, axis=0 )
                    assert averaged_predictions.shape[0] == len(feats[t])
                    self.prob_cache[t] = averaged_predictions

                    self.prob_cache[t][0] = 0 # Background probability is always zero


            if slot == self.Probabilities:
                return { t : self.prob_cache[t] for t in times }
            elif slot == self.Predictions:
                # FIXME: Support SegmentationThreshold again...
                labels = dict()
                for t in times:
                    prob_sum = numpy.sum(self.prob_cache[t], axis=1)
                    labels[t] = 1 + numpy.argmax(self.prob_cache[t], axis=1)
                    labels[t][0] = 0 # Background gets the zero label
                
                return labels

            elif slot == self.ProbabilityChannels:
                try:
                    prob_single_channel = {t: self.prob_cache[t][:, subindex[0]]
                                           for t in times}
                except:
                    # no probabilities available for this class; return zeros
                    prob_single_channel = {t: numpy.zeros((self.prob_cache[t].shape[0], 1))
                                           for t in times}
                return prob_single_channel

            elif slot == self.BadObjects:
                return { t : self.bad_objects[t] for t in times }

            else:
                assert False, "Unknown input slot"
        finally:
            self.lock.release()

    def propagateDirty(self, slot, subindex, roi):
        self.prob_cache = {}
        if slot is self.InputProbabilities:
            self.prob_cache = self.InputProbabilities([]).wait()
        self.Predictions.setDirty(())
        self.Probabilities.setDirty(())
        self.ProbabilityChannels.setDirty(())


class OpRelabelSegmentation(Operator):
    """Takes a segmentation image and a mapping and returns the
    mapped image.

    For instance, map prediction labels onto objects.

    """
    name = "OpToImage"
    Image = InputSlot()
    ObjectMap = InputSlot(stype=Opaque, rtype=List)
    Features = InputSlot(rtype=List, stype=Opaque) #this is needed to limit dirty propagation to the object bbox
    Output = OutputSlot()
    
    loggingName = __name__ + ".OpRelabelSegmentation"
    logger = logging.getLogger(loggingName)

    def setupOutputs(self):
        self.Output.meta.assignFrom(self.Image.meta)
        self.Output.meta.dtype = self.ObjectMap.meta.mapping_dtype

    def execute(self, slot, subindex, roi, result):
        tStart = time.time()
        
        tIMG = time.time()
        img = self.Image(roi.start, roi.stop).wait()
        tIMG = 1000.0*(time.time()-tIMG)
        
        for t in range(roi.start[0], roi.stop[0]):
            
            tMAP = time.time()
            map_ = self.ObjectMap([t]).wait()
            tmap = map_[t]
            # FIXME: necessary because predictions are returned
            # enclosed in a list.
            if isinstance(tmap, list):
                tmap = tmap[0]
            tmap = tmap.squeeze()
            if tmap.ndim==0:
                # no objects, nothing to paint
                result[t-roi.start[0]][:] = 0
                return result
            
            tMAP = 1000.0*(time.time()-tMAP)
            #FIXME: This should be cached (and reset when the input becomes dirty)")
            tMAX = time.time()
            idx = img.max()
            
            if len(tmap) <= idx:
                newTmap = numpy.zeros((idx + 1,)) # And maybe this should be cached, too?
                newTmap[:len(tmap)] = tmap[:]
                tmap = newTmap
            tMAX = 1000.0*(time.time()-tMAX)
            
            #do the work thing
            tWORK = time.time()
            result[t-roi.start[0]] = tmap[img[t-roi.start[0]]]
            tWORK = 1000.0*(time.time()-tWORK)
            
        if self.logger.getEffectiveLevel() >= logging.DEBUG:
            tStart = 1000.0*(time.time()-tStart)
            self.logger.debug("took %f msec. (img: %f, wait ObjectMap: %f, do work: %f, max: %f)" % (tStart, tIMG, tMAP, tWORK, tMAX))
        
        return result

    def propagateDirty(self, slot, subindex, roi):
        if slot is self.Image:
            self.Output.setDirty(roi)

        elif slot is self.ObjectMap or slot is self.Features:
            # this is hacky. the gui's onClick() function calls
            # setDirty with a (time, object) pair, while elsewhere we
            # call setDirty with ().
            if len(roi._l) == 0:
                self.Output.setDirty(slice(None))
            elif isinstance(roi._l[0], int):
                for t in roi._l:
                    self.Output.setDirty(slice(t))
            else:
                assert len(roi._l[0]) == 2
                # for each dirty object, only set its bounding box dirty
                ts = list(set(t for t, _ in roi._l))
                feats = self.Features(ts).wait()
                for t, obj in roi._l:
                    min_coords = feats[t][default_features_key]['Coord<Minimum>'][obj].astype(numpy.uint32)
                    max_coords = feats[t][default_features_key]['Coord<Maximum>'][obj].astype(numpy.uint32)
                    slcs = list(slice(*args) for args in zip(min_coords, max_coords))
                    slcs = [slice(t, t+1),] + slcs + [slice(None),]
                    self.Output.setDirty(slcs)

class OpMultiRelabelSegmentation(Operator):
    """Takes a segmentation image and multiple mappings and returns the
    mapped images.

    For instance, map prediction probabilities for different classes
    onto objects.

    """
    name = "OpToImageMulti"
    Image = InputSlot()
    ObjectMaps = InputSlot(stype=Opaque, rtype=List, level=1)
    Features = InputSlot(rtype=List, stype=Opaque) #this is needed to limit dirty propagation to the object bbox
    Output = OutputSlot(level=1)

    def __init__(self, *args, **kwargs):
        super(OpMultiRelabelSegmentation, self).__init__(*args, **kwargs)
        self._innerOperators = []

    def setupOutputs(self):
        nmaps = len(self.ObjectMaps)
        for islot in self.ObjectMaps:
            op = OpRelabelSegmentation(parent=self)
            op.Image.connect(self.Image)
            op.ObjectMap.connect(islot)
            op.Features.connect(self.Features)
            self._innerOperators.append(op)
        self.Output.resize(nmaps)
        for i, oslot in enumerate(self.Output):
            oslot.connect(self._innerOperators[i].Output)
            

    def propagateDirty(self, slot, subindex, roi):
        pass

class OpMaxLabel(Operator):
    """Finds the maximum label value in the input labels.

    Special operator for object classification labels, expects
    inputs to be in a dictionary

    """
    name = "OpMaxLabel"
    Inputs = InputSlot(level=1,rtype=List,  stype=Opaque)
    Output = OutputSlot()

    def __init__(self, *args, **kwargs):
        super(OpMaxLabel, self).__init__(*args, **kwargs)
        self.Output.meta.shape = (1,)
        self.Output.meta.dtype = object
        self._output = 0 #internal cache


    def setupOutputs(self):
        self.updateOutput()
        self.Output.setValue(self._output)

    def execute(self, slot, subindex, roi, result):
        result[0] = self._output
        return result

    def propagateDirty(self, inputSlot, subindex, roi):
        self.updateOutput()
        self.Output.setValue(self._output)

    def updateOutput(self):
        # Return the max value of all our inputs
        maxValue = None
        for i, inputSubSlot in enumerate(self.Inputs):
            
            subSlotLabelDict = self.Inputs[i][:].wait()
            for v in subSlotLabelDict.itervalues():
                subSlotMax = numpy.max(v)
                if maxValue is None:
                    maxValue = subSlotMax
                else:
                    maxValue = max(maxValue, subSlotMax)
        self._output = int(maxValue)


class OpBadObjectsToWarningMessage(Operator):
    """Parses an input dictionary of bad objects and bad features, and
    sets an informative warning message to its output slot.

    """

    name = "OpBadObjectsToWarningMessage"
    _blockSep = "\n\n"
    _itemSep = "\n"
    _objectSep = ", "
    _itemIndent = "    "

    # the input slot
    # format: BadObjects.value = {      
    #                               'objects': 
    #                                   {img_key: {time_key: [obj_index, obj_index_2, ...]}}, 
    #                               'feats': 
    #                                   set()
    #                            }
    BadObjects = InputSlot(stype=Opaque)

    # the output slot
    # format: WarningMessage.value = 
    #           {'title': a, 'text': b, 'info': c, 'details': d} if message available, the keys 'info' and 'details' might be omitted
    #           {} otherwise
    WarningMessage = OutputSlot(stype=Opaque)

    def setupOutputs(self):
        pass

    def propagateDirty(self, slot, subindex, roi):
        try:
            d = self.BadObjects[:].wait()
        except AssertionError as E:
            if "has no value" in str(E):
                # since we are in propagateDirty, the input got reset or we got disconnected, either case
                # means we don't have to issue warnings any more
                return
            # don't know what this is about, raise again
            raise
            
        warn = {}
        warn['title'] = 'Warning'
        warn['text'] = 'Encountered bad objects/features while training.'
        warn['details'] = self._formatMessage(d)

        if len(warn['details']) == 0:
            return
        self.WarningMessage.setValue(warn)

    def execute(self, slot, subindex, roi, result):
        pass

    def _formatMessage(self, d):
        a = []
        try:
            keys = d.keys()
            # a) objects
            if 'objects' in keys:
                keys.remove('objects')
                s = self._formatObjects(d['objects'])
                if len(s) > 0:
                    a.append(s)


            # b) features
            if 'feats' in keys:
                keys.remove('feats')
                s = self._formatFeatures(d['feats'])
                if len(s)>0:
                    a.append(s)
                    
            if len(keys)>0:
                logger.warning("Encountered unknown bad object keywords: {}".format(keys))
        except AttributeError:
            raise TypeError("Expected input to be a dictionary, got {}".format(type(d)))

        return self._blockSep.join(a)

    def _formatFeatures(self, f):
        try:
            a = self._itemSep.join(map(str, sorted(f)))
        except TypeError:
            raise TypeError("Expected bad features to be a set, got {}".format(type(f)))
        if len(a)>0:
            a = "The following features had bad values:" + self._itemSep + a
        return a

    def _formatObjects(self, obj):
        a = []
        indent = 1
        
        try:
            # loop image indices
            for img in obj.keys():
                imtext = self._itemIndent*indent + "at image index {}".format(img)
                indent += 1
                
                # just show time slice if more than 1 time slice exists (avoid confusion/obfuscation)
                needTime = len(obj[img].keys())>1
                b = []

                # loop time values
                for t in obj[img].keys():
                    # object numbers
                    c = self._objectSep.join(map(str,obj[img][t]))

                    if len(c)>0:
                        c = self._itemIndent*indent + "Objects " + c
                        if needTime:
                            c = self._itemIndent*indent + "at time {}".format(t) + self._itemSep + self._itemIndent + c
                        b.append(c)

                indent -= 1
                if len(b)>0:
                    a.append(self._itemSep.join([imtext] + b))
        except AttributeError:
            raise TypeError("bad objects dictionary has wrong format.")


        if len(a)>0:
            return self._itemSep.join(["The following objects had bad features:"] +a)
        else:
            return ""
