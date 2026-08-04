/* 
   jrenta@bnl.gov	
   
   heatmapScript gets the following Widget Macros:
   		* A value from a SiPM Channel
   		* A Heatmap Minimum Limit - specified by user externally
   		* A Heatmap Maximum Limit - specified by user externally
   
   heatmapScript creates a (HOT) Heatmap pattern
   Normalizes the heatmap limits
   Finds the color associated to the SiPM Channel's Value 
   Updates the background color of the widget using this Script
	 
	 Rev 0x00 jcr Init
*/	 

// Imported Classes
importClass(org.csstudio.display.builder.runtime.script.PVUtil);
importClass(org.csstudio.display.builder.runtime.script.ColorFontUtil);
importClass(org.csstudio.display.builder.runtime.script.ScriptUtil);


// Widget Macros
var value  = PVUtil.getDouble(pvs[0]); // SiPM Channel Value
var minVal = PVUtil.getDouble(pvs[1]); // Heatmap's min
var maxVal = PVUtil.getDouble(pvs[2]); // Heatmap's max
 


// Normalizing 0 to 1
var t = (value - minVal) / (maxVal - minVal); 
if(t < 0.0) t = 0.0;
if(t > 1.0) t = 1.0;

// lerp(start, end, fraction btwn them)
function lerp(a,b,x){
    return a + (b-a) * x;
}

// calls the lerp function 3 times for the Red, Green, & Blue
function lerpColor(c1, c2, x){
    return [
        Math.round(lerp(c1[0], c2[0], x)),
        Math.round(lerp(c1[1], c2[1], x)),
        Math.round(lerp(c1[2], c2[2], x))
    ];
}


// Heatmap Color Stops (Black, Dark Red, Red, ORG, YEL, WHT)

var stops = [
    [0,  0, 0],
    [80, 0, 0],
    [200,0, 0], 
    [255,120,0],
    [255,255,0],
    [255,255,255]
];

// Pick which segment we are in
var n = stops.length - 1;
var pos = t * n;						// The position where the value falls within the Stops (0 - 5)
var idx = Math.floor(pos);	

if(idx >= n) idx = n -1;

var localT = pos - idx;		  // How far into the interval 
var rgb = lerpColor(stops[idx], stops[idx + 1], localT);



// This updates the widget that this script is being attached to
widget.setPropertyValue(
    "background_color",
    ColorFontUtil.getColorFromRGB(rgb[0], rgb[1], rgb[2])
);




