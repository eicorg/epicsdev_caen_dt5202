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
_planeMap = {'J3i':'J3', 'J3o':'J3', 'J4i':'J4', 'J4o':'J4', 'J5':'J5'}

@dataclass(slots=True)
class C_:
    parent = None# reference to the main program class

# helper functions for the plugin
def polarPoligon(arr, max_value=None):
    """Convert one-dimensional array values into Cartesian polygon points.
    Angles are distributed uniformly in [0, 2*pi) and each radius is
    normalized by the maximum value in the input array and scaled accordingly.
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
        if key == 'J5':
            continue
        pvdefs.append([f"{key}_x", f'X coordinates of polar presentation of {key}', [0.]])
        pvdefs.append([f"{key}_y", f'Y coordinates of polar presentation of {key}', [0.]])
        pvdefs.append([f"{key}_max",f"Intensity level of the ring {key}", 0])
    
    for plane in set(_planeMap.values()):
        pvdefs.append([f"{plane}_scale", f"Scale factor for {plane} sensor intensities", 1.0, {F:'W'}])
        if plane == 'J5':
            continue
        pvdefs.append([f"{plane}_hitMapLow", f"Low threshold for hit map of {plane} sensors",
                        0, {F:'W', LH:4095}])
        pvdefs.append([f"{plane}_hitMapHigh", f"High threshold for hit map of {plane} sensors",
                       4095, {F:'W', LH:4095}])
        pvdefs.append([f"{plane}_hitMapAuto", f"Auto threshold for hit map of {plane} sensors",
                      ['MANUAL','AUTO'], {F:'WD'}])

        # coordinates of 64-point ring
        ring_angles = np.linspace(0.0, 2.0 * np.pi, num=64, endpoint=True)
        ringX = np.cos(ring_angles)
        ringY = np.sin(ring_angles)
        pvdefs.append([f"{plane}_x ","X coordinates of a reference ring", ringX.tolist()])
        pvdefs.append([f"{plane}_y","Y coordinates of a reference ring", ringY.tolist()])

    pvdefs.append(['gainSelector','Gain selector for sensor arrays', ['LowGain','HighGain'], {F:'WD'}])
    return pvdefs

def publish():
    """Publish the current values of the beam loss sensors to their respective PVs."""
    gainSelector = str(epicsdev.pvv("gainSelector"))
    #print(f"Publishing PVs for beam loss sensors with gainSelector: {gainSelector}")
    selected = 'b0HGMean' if gainSelector == 'HighGain' else 'b0LGMean'
    channels = epicsdev.pvv(selected)

    # Shuffle the scaled channels according to the sensor map
    shuffled = {}
    for sensorArray,smap in _sensorMap.items():
        scale = epicsdev.pvv(f"{_planeMap[sensorArray]}_scale")
        shuffled[sensorArray] = [channels[i] * scale for i in smap]

    maxPlaneIntensityMap = {
        'J3': max(max(shuffled['J3i']), max(shuffled['J3o'])),
        'J4': max(max(shuffled['J4i']), max(shuffled['J4o'])),
        'J5': max(shuffled['J5']),
    }
    #TODO: publish planes max ring

    #samePlane = None
    for ring in _sensorMap:
        plane = _planeMap[ring]
        scale = epicsdev.pvv(f"{plane}_scale")
        #print(f"Publishing PVs for {ring} with plane {plane}, scale {scale}")
        epicsdev.publish(ring, shuffled[ring])
        if plane == 'J5':
            continue

        # publish the polar coordinates and normalization value for the ring
        maxPlaneIntensity = maxPlaneIntensityMap[plane]
        x, y = polarPoligon(shuffled[ring], max_value=maxPlaneIntensity)
        epicsdev.publish(f"{ring}_x", x)
        epicsdev.publish(f"{ring}_y", y)
        v = np.ceil(maxPlaneIntensity/2)*2# round up to the nearest even number
        epicsdev.publish(f"{ring}_max", v)