"""Plugin for the CAEN DT5202 digitizer to read beam loss sensors around the beam pipe.
"""
# pylint: disable=invalid-name
from dataclasses import dataclass
from epicsdev import epicsdev

# Map of the board channels to the physical sensors numbers.
_sensorMap = {
    'J3i': [46,2,6,10,14,18,22,26,30,34,38,42],
    'J3o': [44,0,4,8,12,16,20,24,28,32,36,40],
     'J4i': [1,45,41,37,33,29,25,21,17,13,9,5],
     'J4o': [3,47,43,39,35,31,27,23,19,15,11,7],
     'J5': [48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63],
}

@dataclass(slots=True)
class C_:
    parent = None# reference to the main program class

def init(parent):
    """Initialize the plugin with a reference to the main data class."""
    C_.parent = parent

def get_pvdefs():
    """Return PV definitions for the beam loss sensors."""
    F = "features"
    T = "type"
    U = "units"
    LL = "limitLow"
    LH = "limitHigh"
    SET = "setter"
    pvdefs = [
        [key, f'Mean values of {key} sensors', [0.]] for key in _sensorMap
    ]
    pvdefs.append(['gainSelector','Gain selector for sensor arrays', ['LowGain','HighGain'], {F:'WD'}])
    return pvdefs

def publish():
    gainSelector = str(epicsdev.pvv(f"gainSelector"))
    #print(f"Publishing PVs for beam loss sensors with gainSelector: {gainSelector}")
    selected = 'b0HGMean' if gainSelector == 'HighGain' else 'b0LGMean'
    channels = epicsdev.pvv(selected)
    for det,smap in _sensorMap.items():
        shuffled = [channels[i] for i in smap]
        #print(f"Publishing PV {det} with values: {shuffled}")
        epicsdev.publish(det, shuffled)
    