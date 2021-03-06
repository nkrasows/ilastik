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

from ilastik.applets.base.standardApplet import StandardApplet
from ilastik.applets.objectExtraction.opObjectExtraction import OpObjectExtraction
from ilastik.applets.objectExtraction.objectExtractionSerializer import ObjectExtractionSerializer

class ObjectExtractionApplet(StandardApplet):
    """Calculates object features for each object in an image.

    Features are provided by plugins, which are responsible for
    performing the actual computation.

    """

    def __init__(self, name="Object Extraction", workflow=None,
                 projectFileGroupName="ObjectExtraction",
                 interactive=True):
        super(ObjectExtractionApplet, self).__init__(name=name, workflow=workflow, interactive=interactive)
        self._serializableItems = [ ObjectExtractionSerializer(self.topLevelOperator, projectFileGroupName) ]

    @property
    def singleLaneOperatorClass(self):
        return OpObjectExtraction

    @property
    def broadcastingSlots(self):
        return ['Features']

    @property
    def singleLaneGuiClass(self):
        from ilastik.applets.objectExtraction.objectExtractionGui import ObjectExtractionGui
        from ilastik.applets.objectExtraction.objectExtractionGui import ObjectExtractionGuiNonInteractive
        if self.interactive:
            return ObjectExtractionGui
        else:
            return ObjectExtractionGuiNonInteractive

    @property
    def dataSerializers(self):
        return self._serializableItems
