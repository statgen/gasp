/* global d3 */
/* eslint no-console: "off" */

var Ideogram = function(selector) {
    this.selector = selector;
    if (!selector) {
        throw("Must supply a selector to initialize ideogram");
    }
    this.svg = d3.select(selector).append("svg")
        .attr("width",800)
        .attr("height", 500);
    this.svg.append("g")
        .attr("class", "chromosome");
    this.svg.append("g")
        .attr("class", "outline");
    if (this.svg.empty()) {
        throw("Could not find '" + selector + "', unable to initialize ideogram");
    }

    if (d3.tip) {
        this.tooltip = d3.tip()
            .attr("class", "d3-tip")
            .offset([-10,0])
            .html(function(d) {
                return d.tooltip;
            });
        this.svg.call(this.tooltip);
    }

    this.regions = [];
};

Ideogram.prototype.draw = function() {
    var layout = this.getLayout(this.chrs_hg19);
    var positions = this.calculatePositions(layout);
    this.drawOutlines(positions);
    this.drawRegions(positions, this.regions);
    this.drawLabels(positions);
};

Ideogram.prototype.setRegions = function(regions) {
    this.regions = regions;
};

Ideogram.prototype.toPointsString = function(d) {
    return d.points.map(function(d) {return [d[0].toFixed(2), d[1].toFixed(2)].join(",");}).join(" ");
};

Ideogram.prototype.drawLabels = function(positions, opts) {
    opts = opts || {};
    var labels = [];
    positions.positions.forEach(function(ele) {
        if (ele.type && ele.type=="label") {
            labels.push({
                text: ele.text,
                x: (ele.x0 + ele.x1)/2,
                y: (ele.y0 + ele.y1)/2
            });
        }
    }.bind(this));
    this.svg.select("g.outline").selectAll("text.label")
        .data(labels)
        .enter().append("text")
        .attr("x", function(d) {return d.x;})
        .attr("y", function(d) {return d.y+5;})
        .attr("text-anchor", "middle")
        .text(function(d) {return d.text;});
};

Ideogram.prototype.drawOutlines = function(positions, opts) {
    opts = opts || {fill: "none", stroke: "black", "stroke-width": 1.5};
    var regions = []; 
    positions.positions.forEach(function(ele) {
        if (ele.type && ele.type=="chr") {
            regions.push({points: this.getRegionPath(positions, ele.name)});
        }
    }.bind(this));
    this.svg.select("g.outline").selectAll("polygon.chroutline")
        .data(regions)
        .enter().append("polygon")
        .attr("class"," chroutline")
        .attr("points", this.toPointsString)
        .attr(opts);
};

Ideogram.prototype.drawRegions = function(positions, regions, opts) {
    opts = opts || {fill: "#c8c8c8"};
    var polys = []; 
    var hasToolTips = false;
    for(var i=0; i<regions.length; i++) {
        polys.push({points: this.getRegionPath(positions,
            regions[i].chrom, 
            regions[i].start,
            regions[i].stop),
        fill: regions[i].fill,
        tooltip: regions[i].tooltip});
        hasToolTips |= !!(regions[i].tooltip);
    }
    var r = this.svg.select("g.chromosome").selectAll("polygon.region")
        .data(polys);
    r.enter().append("polygon")
        .attr("class", "region");
    r.exit().remove();
    r.attr("points", this.toPointsString);
    r.attr("fill", function(d) {return d.fill || opts.fill;});
    if (hasToolTips) {
        if (this.tooltip) {
            r.on("mouseover", function(d) {
                if (d.tooltip) {
                    this.tooltip.show(d);
                }
            }.bind(this));
            r.on("mouseout", this.tooltip.hide);
        } else {
            console.warning("Tooltips not available (include d3.tip)");
        }
    }
};

Ideogram.prototype.chrs_hg19 = [
    {chr: "chr1", center: 125000000, end: 249250621},
    {chr: "chr2", center: 93300000, end: 243199373},
    {chr: "chr3", center: 91000000, end: 198022430},
    {chr: "chr4", center: 50400000, end: 191154276},
    {chr: "chr5", center: 48400000, end: 180915260},
    {chr: "chr6", center: 61000000, end: 171115067},
    {chr: "chr7", center: 59900000, end: 159138663},
    {chr: "chr8", center: 45600000, end: 146364022},
    {chr: "chr9", center: 49000000, end: 141213431},
    {chr: "chr10", center: 40200000, end: 135534747},
    {chr: "chr11", center: 53700000, end: 135006516},
    {chr: "chr12", center: 35800000, end: 133851895},
    {chr: "chr13", center: 17900000, end: 115169878},
    {chr: "chr14", center: 17600000, end: 107349540},
    {chr: "chr15", center: 19000000, end: 102531392},
    {chr: "chr16", center: 36600000, end: 90354753},
    {chr: "chr17", center: 24000000, end: 81195210},
    {chr: "chr18", center: 17200000, end: 78077248},
    {chr: "chr19", center: 26500000, end: 59128983},
    {chr: "chr20", center: 27500000, end: 63025520},
    {chr: "chr21", center: 13200000, end: 48129895},
    {chr: "chr22", center: 14700000, end: 51304566},
    {chr: "chrX", center: 60600000, end: 155270560},
    {chr: "chrY", center: 12500000, end: 59373566}
];


Ideogram.prototype.getLayout = function(chrs, options) {
    options = options || {};
    var rows = [];
    var maxextent = 0;
    var lookup = {};
    var drawSex = false;
    var add2col = function(i, a,b) {
        var cols = [];
        var rowextent = a.end + b.end;
        if (rowextent > maxextent) {maxextent = rowextent;}
        cols.push({type: "label", width: "45px", text: a.chr});
        cols.push({type: "chr", name: a.chr, center: a.center, end: a.end});
        cols.push({type: "gap", min_width: "25px"});
        cols.push({type: "chr", name: b.chr, center: b.center, end: b.end});
        cols.push({type: "label", width: "45px", text: b.chr});
        rows.push({cols: cols});
        lookup[a.chr] = [i, 1];
        lookup[b.chr] = [i, 3];
    };
    for(var i=0; i<11; i++) {
        var a = chrs[i];
        var b = chrs[21-i];
        add2col(i,a,b);
    }
    if (drawSex) {
        add2col(i, chrs[22], chrs[23]);
    }
    return {rows: rows, max_row_extent: maxextent,
        corner_ease: 5, chr: lookup};
};

Ideogram.prototype.getRowWidths = function(row, width, scale) {
    var fixed = 0;
    var gap = -1;
    var data = 0;
    var widths = row.cols.map(function() {return 0;});
    var remainWidth = width;
    row.cols.map(function(col, idx) {
        if (col.width) {
            widths[idx] = parseFloat(col.width);
            fixed += widths[idx];
            remainWidth -= widths[idx];
        } else if (col.min_width) {
            fixed = fixed + parseFloat(col.min_width);
        } else if (col.end) {
            data = data + col.end;
        }
        if (col.type && col.type=="gap") {
            gap = idx;
        }
    });
    scale = (width - fixed)/scale;
    row.cols.map(function(col, idx) {
        if (col.type =="chr") {
            var w = Math.round(col.end * scale);
            widths[idx] = w;
            remainWidth -= w;
        }
    });
    if (gap > -1 && remainWidth>0) {
        widths[gap] = remainWidth; 
    }
    return widths;
};

Ideogram.prototype.getContour = function(start, stop, landmarks, ease) {
    var contour =[];
    var xp = [landmarks[0], landmarks[0] + ease,
        landmarks[1]-ease, landmarks[1], landmarks[1]+ease,
        landmarks[2]-ease, landmarks[2]];
    var yp = [ease, 0, 0, ease, 0, 0, ease];
    if (start < xp[0]) {start = xp[0];}
    if (stop > xp[xp.length-1]) {stop = xp[xp.length-1];}
    var interp = function(val, i) {
        return [val-xp[0], (val-xp[i])/(xp[i+1]-xp[i]) * (yp[i+1]-yp[i]) + yp[i]];
    };
    for(var i=0; i < xp.length-1; i++) {
        if (start >= xp[i] && start < xp[i+1]) {
            contour.push(interp(start, i));
        }
        if (stop > xp[i] && stop <= xp[i+1]) {
            contour.push(interp(stop, i));
        }
        if (start < xp[i+1] && stop > xp[i+1]) {
            contour.push([xp[i+1]-xp[0], yp[i+1]]);
        }
    }
    return contour;
};

Ideogram.prototype.getRegionPath = function(positions, chr, start, stop) {
    var cell = positions.positions[positions.names[chr]];
    var y0 = cell.y0;
    var y1 = cell.y1;
    var cellWidth = cell.x1 - cell.x0;
    start = start || 0;
    stop = stop || cell.end;
    var landmarks = [ cell.x0,
        cell.x0 + cellWidth * (cell.center/cell.end), 
        cell.x1];

    var r0 = landmarks[0] + cellWidth * (start/cell.end); 
    var r1 = landmarks[0] + cellWidth * (stop/cell.end);

    var contour = this.getContour(r0, r1, landmarks, 5);
    var points = [];
    for(var i =0; i<contour.length; i++) {
        var diff = contour[i];
        points.push([landmarks[0]+diff[0], y0+diff[1]]);
    }
    for(i=contour.length-1; i>-1; i--) {
        diff = contour[i];
        points.push([landmarks[0]+diff[0], y1-diff[1]]);
    }
    points.push([landmarks[0]+diff[0], y0+diff[1]]);
    return points;
};

Ideogram.prototype.calculatePositions = function(layout) {
    layout = layout || this.layout;
    if (!layout) {throw("No layout supplied");}
    var height = layout.height || 400;
    var width = layout.width || 800;
    var paddingY = (layout.padding && layout.padding.y) || layout.padding || 3;
    var rowHeight = Math.floor((height - 2*paddingY*layout.rows.length)/layout.rows.length);
    var positions = [];
    var names = {};
    for(var i=0; i<layout.rows.length; i++) {
        var row = layout.rows[i];
        var y0 = (rowHeight + 2*paddingY) * i + paddingY;
        var y1 = y0 + rowHeight - paddingY;
        var widths = this.getRowWidths(row, width, layout.max_row_extent); 
        var x0 = 0, x1 = 0;
        for(var j=0; j<row.cols.length; j++) {
            var col = JSON.parse(JSON.stringify(row.cols[j]));
            col.y0 = y0;
            col.y1 = y1;
            col.x0 = (x0 = x1);
            col.x1 = (x1 = x0 + widths[j]);
            positions.push(col);
            if (col.name) {
                names[col.name] = positions.length-1;
            }
        }
    }
    return {positions: positions, names: names};
};