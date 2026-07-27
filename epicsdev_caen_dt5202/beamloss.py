"""Plugin for the CAEN DT5202 digitizer to read beam loss sensors around the beam pipe.
"""
# pylint: disable=invalid-name
from dataclasses import dataclass
import numpy as np
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

# helper functions for the plugin
def polarPoligon(arr, max_value=None):
    """Convert one-dimensional array values into Cartesian polygon points.

    Angles are distributed uniformly in [0, 2*pi) and each radius is
    normalized by the maximum value in the input array.

    Returns:
        tuple[np.ndarray, np.ndarray]: x and y coordinate arrays.
    """
    values = np.asarray(arr, dtype=float).ravel()
    if values.size == 0:
        return np.array([], dtype=float), np.array([], dtype=float)

    if max_value is None:
        max_value = float(np.max(values))
    if max_value == 0.0:
        radii = np.zeros_like(values)
    else:
        radii = values / max_value

    angles = np.linspace(0.0, 2.0 * np.pi, num=values.size, endpoint=False)
    x = radii * np.cos(np.pi/2 - angles)
    y = radii * np.sin(np.pi/2 - angles)
    return x, y

# Plugin interface functions
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

    pvdefs = []
    for key,val in _sensorMap.items():
        pvdefs.append([key, f'Mean values of {key} sensors', [0.]])
        pvdefs.append([f"{key}_x", f'X coordinates of polar presentation of {key}', [0.]])
        pvdefs.append([f"{key}_y", f'Y coordinates of polar presentation of {key}', [0.]])
        pvdefs.append([f"{key}_norm",f"Normalization factor for {key}", 0.])

    # coordinates of 64-point ring
    ring_angles = np.linspace(0.0, 2.0 * np.pi, num=64, endpoint=True)
    ringX = np.cos(ring_angles)
    ringY = np.sin(ring_angles)
    pvdefs.append(["ringX","X coordinates of a reference ring", ringX.tolist()])
    pvdefs.append(["ringY","Y coordinates of a reference ring", ringY.tolist()])

    pvdefs.append(['gainSelector','Gain selector for sensor arrays', ['LowGain','HighGain'], {F:'WD'}])
    return pvdefs

def publish():
    gainSelector = str(epicsdev.pvv(f"gainSelector"))
    #print(f"Publishing PVs for beam loss sensors with gainSelector: {gainSelector}")
    selected = 'b0HGMean' if gainSelector == 'HighGain' else 'b0LGMean'
    channels = epicsdev.pvv(selected)
    shuffled = {}
    for det,smap in _sensorMap.items():
        shuffled[det] = [channels[i] for i in smap]
        #print(f"Publishing PV {det} with values: {shuffled}")
    
    maxJ3 = max(max(shuffled['J3i']), max(shuffled['J3o']))
    print
                
    for det in ['J3i','J3o']:
        epicsdev.publish(det, shuffled[det])
        x, y = polarPoligon(shuffled[det], max_value=maxJ3)
        epicsdev.publish(f"{det}_x", x)
        epicsdev.publish(f"{det}_y", y)
        epicsdev.publish(f"{det}_norm", maxJ3)