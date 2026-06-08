#!/usr/bin/env python3
############################################################################
#
# MODULE:       v.in.nwis
#
# AUTHOR(S):    Andrew Wickert
#
# PURPOSE:      Import USGS NWIS stream gauge locations, upstream drainage
#               basins (via NLDI), and discharge time series into GRASS GIS
#
# COPYRIGHT:    (c) 2026 Andrew Wickert
#
#               This program is free software under the GNU General Public
#               License (>=v2). Read the file COPYING that comes with GRASS
#               for details.
#
#############################################################################

#%module
#% description: Import USGS NWIS stream gauge locations, upstream basins, and discharge time series
#% keyword: vector
#% keyword: import
#% keyword: hydrology
#% keyword: NWIS
#% keyword: stream gauge
#% keyword: discharge
#%end

#%option G_OPT_V_OUTPUT
#%  key: output
#%  label: Output vector map of gauge locations
#%  required: yes
#%end

#%option G_OPT_V_OUTPUT
#%  key: basins
#%  label: Output vector map of upstream drainage basins
#%  required: no
#%end

#%option
#%  key: sites
#%  type: string
#%  label: Comma-separated NWIS site IDs (if omitted, searches within current region)
#%  required: no
#%end

#%option
#%  key: parameter_cd
#%  type: string
#%  label: NWIS parameter code
#%  description: 00060=discharge [ft3/s], 00065=gage height [ft], 00010=water temperature [degC]
#%  answer: 00060
#%  required: no
#%end

#%option
#%  key: start_date
#%  type: string
#%  label: Start date for time series (YYYY-MM-DD)
#%  required: no
#%end

#%option
#%  key: end_date
#%  type: string
#%  label: End date for time series (YYYY-MM-DD)
#%  required: no
#%end

#%flag
#%  key: b
#%  description: Fetch upstream drainage basins from NLDI
#%end

#%flag
#%  key: t
#%  description: Fetch daily discharge time series (requires start_date and end_date)
#%end

#%rules
#% requires_all: -t, start_date, end_date
#% collective: -b, basins
#%end

import importlib
import os
import sqlite3
import tempfile
import atexit

import grass.script as gs

TMPFILES = []


def cleanup():
    for f in TMPFILES:
        try:
            os.remove(f)
        except OSError:
            pass


def require_package(import_name, pip_name=None):
    try:
        return importlib.import_module(import_name)
    except ImportError:
        gs.fatal(
            "Python package '{}' is required but not installed.\n"
            "Install with: pip install {}".format(
                import_name, pip_name or import_name
            )
        )


def get_geographic_bbox():
    """Return (west, south, east, north) in decimal degrees for the current region."""
    proj = gs.parse_command('g.proj', flags='g')
    region = gs.region()

    if proj.get('proj') == 'longlat':
        return region['w'], region['s'], region['e'], region['n']

    # Project SW and NE corners from native CRS to geographic (lon/lat)
    coords = "{} {}\n{} {}".format(
        region['w'], region['s'], region['e'], region['n']
    )
    out = gs.read_command('m.proj', flags='i', stdin=coords).strip().split('\n')
    sw = [float(v) for v in out[0].split()[:2]]
    ne = [float(v) for v in out[1].split()[:2]]
    return sw[0], sw[1], ne[0], ne[1]


def geodataframe_to_grass(gdf, output):
    """Write a GeoDataFrame to a temp GeoPackage and import it into GRASS."""
    import geopandas as gpd

    fd, tmp = tempfile.mkstemp(suffix='.gpkg')
    os.close(fd)
    TMPFILES.append(tmp)

    # Coerce any Arrow-backed string columns to plain Python strings so
    # GPKG writing doesn't choke on non-standard dtypes
    for col in gdf.select_dtypes(include='object').columns:
        gdf[col] = gdf[col].astype(str)

    gdf.to_file(tmp, driver='GPKG')
    gs.run_command('v.in.ogr', input=tmp, output=output,
                   overwrite=gs.overwrite(), quiet=True)


def fetch_sites(site_ids, bbox):
    """Fetch NWIS site metadata; return a GeoDataFrame of gauge points."""
    import dataretrieval.nwis as nwis
    import geopandas as gpd
    from shapely.geometry import Point

    if site_ids:
        df, _ = nwis.get_info(sites=site_ids)
    else:
        west, south, east, north = bbox
        df, _ = nwis.get_info(
            bBox=(west, south, east, north),
            siteType='ST',
            hasDataTypeCd='dv',
        )

    if df.empty:
        gs.fatal("No NWIS stream gauge sites found.")

    df = df.reset_index(drop=True)
    df = df.dropna(subset=['dec_long_va', 'dec_lat_va'])

    geometry = [
        Point(float(lon), float(lat))
        for lon, lat in zip(df['dec_long_va'], df['dec_lat_va'])
    ]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs='EPSG:4326')
    gs.message("Found {} NWIS site(s).".format(len(gdf)))
    return gdf


def fetch_basins(site_nos):
    """Fetch upstream basin polygons from NLDI; return a GeoDataFrame."""
    from pynhd import NLDI

    nldi = NLDI()
    nldi_ids = ["USGS-{}".format(s) for s in site_nos]
    basins = nldi.get_basins(nldi_ids)
    basins = basins.reset_index()
    basins = basins.rename(columns={'index': 'nldi_id'})
    basins['site_no'] = basins['nldi_id'].str.replace('USGS-', '', regex=False)
    return basins


def write_timeseries(site_nos, parameter_cd, start_date, end_date, table_name):
    """Fetch NWIS daily values and store in the mapset SQLite database."""
    import dataretrieval.nwis as nwis

    gs.message("Fetching daily values ({} – {})...".format(start_date, end_date))
    df, _ = nwis.get_dv(
        sites=site_nos,
        parameterCd=parameter_cd,
        start=start_date,
        end=end_date,
    )

    if df is None or df.empty:
        gs.warning("No time series data returned from NWIS.")
        return

    df = df.reset_index()

    value_cols = [c for c in df.columns
                  if c.startswith(parameter_cd) and not c.endswith('_cd')]
    flag_cols = [c for c in df.columns
                 if c.startswith(parameter_cd) and c.endswith('_cd')]

    if not value_cols:
        gs.warning("No data column found for parameter {}.".format(parameter_cd))
        return

    value_col = value_cols[0]
    flag_col = flag_cols[0] if flag_cols else None
    dt_col = 'datetime' if 'datetime' in df.columns else df.columns[0]

    gisenv = gs.gisenv()
    db_dir = os.path.join(
        gisenv['GISDBASE'], gisenv['LOCATION_NAME'], gisenv['MAPSET'], 'sqlite'
    )
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, 'sqlite.db')

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('DROP TABLE IF EXISTS "{}"'.format(table_name))
    cur.execute('''
        CREATE TABLE "{}" (
            site_no  TEXT,
            datetime TEXT,
            value    REAL,
            flag     TEXT
        )
    '''.format(table_name))

    rows = []
    for _, row in df.iterrows():
        site = str(row['site_no']) if 'site_no' in df.columns else site_nos[0]
        dt = str(row[dt_col])
        raw = row[value_col]
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = None
        flg = str(row[flag_col]) if flag_col is not None else None
        rows.append((site, dt, val, flg))

    cur.executemany(
        'INSERT INTO "{}" VALUES (?, ?, ?, ?)'.format(table_name), rows
    )
    conn.commit()
    conn.close()

    gs.message(
        "Time series stored: table '{}', {} records.".format(table_name, len(rows))
    )
    gs.message(
        'Query with: db.select sql="SELECT * FROM {} LIMIT 10"'.format(table_name)
    )


def main():
    options, flags = gs.parser()

    output = options['output']
    basins_map = options['basins']
    sites_str = options['sites']
    parameter_cd = options['parameter_cd']
    start_date = options['start_date']
    end_date = options['end_date']
    flag_basins = flags['b']
    flag_ts = flags['t']

    atexit.register(cleanup)

    require_package('dataretrieval')
    require_package('geopandas')
    require_package('shapely')
    if flag_basins:
        require_package('pynhd')

    site_ids = [s.strip() for s in sites_str.split(',')] if sites_str else None
    bbox = None if site_ids else get_geographic_bbox()

    # Gauge locations
    sites_gdf = fetch_sites(site_ids, bbox)
    geodataframe_to_grass(sites_gdf, output)
    gs.message("Gauge locations imported to '{}'.".format(output))

    site_nos = [str(s) for s in sites_gdf['site_no'].tolist()]

    # Upstream basins
    if flag_basins:
        gs.message("Fetching upstream basins from NLDI...")
        try:
            basins_gdf = fetch_basins(site_nos)
            geodataframe_to_grass(basins_gdf, basins_map)
            gs.message("Upstream basins imported to '{}'.".format(basins_map))
        except Exception as e:
            gs.warning("Basin retrieval failed: {}".format(e))

    # Time series
    if flag_ts:
        table_name = "{}_timeseries".format(output)
        write_timeseries(site_nos, parameter_cd, start_date, end_date, table_name)


if __name__ == '__main__':
    main()
