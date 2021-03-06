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

from lazyflow.graph import OperatorWrapper
from ilastik.utility import OperatorSubView

class OpMultiLaneWrapper( OperatorWrapper ):
    """
    An extension of the ``OperatorWrapper`` that provides the functions needed to satisfy :py:class:`MultiLaneOperatorABC`.
    See ``OperatorWrapper`` class documentation for details on that class.
    """
    
    def addLane(self, laneIndex):
        """
        Add an image lane to this object.  Simply inserts a new inner operator into the base ``OperatorWrapper``.
        """
        numLanes = len(self.innerOperators)
        assert numLanes == laneIndex, "Image lanes must be appended."        
        self._insertInnerOperator(numLanes, numLanes+1)

    def removeLane(self, laneIndex, finalLength):
        """
        Remove an image lane.  Simply removes the appropriate inner operator from the base ``OperatorWrapper``.
        """
        numLanes = len(self.innerOperators)
        self._removeInnerOperator(laneIndex, numLanes-1)

    def getLane(self, laneIndex):
        """
        Create sub-view that exposes the correct inner operator from the base ``OperatorWrapper``.
        """
        return OperatorSubView(self, laneIndex)

if __name__ == "__main__":
    from lazyflow.graph import Graph, Operator, InputSlot
    from ilastik.utility import MultiLaneOperatorABC
    
    class Op(Operator):
        Input = InputSlot()
    
    graph = Graph()
    op = OpMultiLaneWrapper( Op, graph=graph )
    
    assert isinstance( op, MultiLaneOperatorABC )
