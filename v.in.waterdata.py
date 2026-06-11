#!/usr/bin/env python3
############################################################################
#
# MODULE:       v.in.waterdata
#
# AUTHOR(S):    Andrew Wickert
#
# PURPOSE:      Import USGS Water Data stream gauge locations, upstream drainage
#               basins (via NLDI), discharge time series, rating curves, and
#               channel geometry into GRASS GIS
#
# COPYRIGHT:    (c) 2026 Andrew Wickert
#
#               This program is free software under the GNU General Public
#               License (>=v2). Read the file COPYING that comes with GRASS
#               for details.
#
#############################################################################

#%module
#% description: Import USGS Water Data stream gauge locations, upstream basins, discharge time series, rating curves, and channel geometry
#% keyword: vector
#% keyword: import
#% keyword: hydrology
#% keyword: USGS
#% keyword: waterdata
#% keyword: stream gauge
#% keyword: discharge
#% keyword: rating curve
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
#%  label: Comma-separated USGS site IDs (if omitted, searches within current region)
#%  required: no
#%end

#%option
#%  key: parameter_cd
#%  type: string
#%  label: USGS parameter code
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
#%  key: t
#%  description: Fetch daily discharge time series (requires start_date and end_date)
#%end

#%flag
#%  key: r
#%  description: Fetch current shift-adjusted rating curve
#%end

#%flag
#%  key: c
#%  description: Fetch channel geometry from field measurements
#%end

#%rules
#% requires_all: -t, start_date, end_date
#%end

import importlib
import os
import sqlite3
import tempfile
import atexit

import grass.script as gs

# System pyproj may not find its PROJ database when GRASS is launched from
# an environment where PROJ_DATA points elsewhere (e.g. an Anaconda install).
# Try the known system location before any pyproj import occurs.
if not os.environ.get('PROJ_DATA') and not os.environ.get('PROJ_LIB'):
    if os.path.exists('/usr/share/proj/proj.db'):
        os.environ['PROJ_DATA'] = '/usr/share/proj'

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

    if proj.get('proj') == 'll':
        return region['w'], region['s'], region['e'], region['n']

    # Project SW and NE corners from native CRS to geographic (lon/lat)
    coords = "{} {}\n{} {}".format(
        region['w'], region['s'], region['e'], region['n']
    )
    import subprocess as _sp
    proc = gs.start_command('m.proj', flags='i',
                            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE)
    stdout, _ = proc.communicate(coords.encode())
    out = stdout.decode().strip().split('\n')
    sw = [float(v) for v in out[0].split()[:2]]
    ne = [float(v) for v in out[1].split()[:2]]
    return sw[0], sw[1], ne[0], ne[1]


def geodataframe_to_grass(gdf, output):
    """Write a GeoDataFrame to a temp GeoPackage and import it into GRASS."""
    fd, tmp = tempfile.mkstemp(suffix='.gpkg')
    os.close(fd)
    os.remove(tmp)  # fiona must create the GeoPackage itself
    TMPFILES.append(tmp)

    # Coerce all non-geometry columns to object dtype so fiona (used by
    # older geopandas) doesn't choke on Arrow-backed or other non-numpy dtypes
    # (pandas 3.x uses Arrow strings by default; fiona expects object dtype)
    for col in [c for c in gdf.columns if c != 'geometry']:
        try:
            gdf[col] = gdf[col].astype(object)
        except (TypeError, ValueError):
            gdf[col] = gdf[col].astype(str).astype(object)

    gdf.to_file(tmp, driver='GPKG')
    gs.run_command('v.in.ogr', input=tmp, output=output,
                   overwrite=gs.overwrite(), quiet=True)


def _mapset_db_path():
    """Return the mapset SQLite database path, creating the directory if needed."""
    gisenv = gs.gisenv()
    db_dir = os.path.join(
        gisenv['GISDBASE'], gisenv['LOCATION_NAME'], gisenv['MAPSET'], 'sqlite'
    )
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, 'sqlite.db')


_NLDI_BASE = 'https://api.water.usgs.gov/nldi/linked-data/nwissite'


def _to_monitoring_id(site_no):
    """Convert a bare USGS site number to a USGS-prefixed monitoring location ID."""
    return site_no if site_no.startswith('USGS-') else 'USGS-{}'.format(site_no)


def fetch_sites(site_ids, bbox):
    """Fetch USGS monitoring location metadata; return a GeoDataFrame."""
    import dataretrieval.waterdata as waterdata
    import geopandas as gpd

    try:
        if site_ids:
            mon_ids = [_to_monitoring_id(s) for s in site_ids]
            gdf, _ = waterdata.get_monitoring_locations(monitoring_location_id=mon_ids)
        else:
            west, south, east, north = bbox
            gdf, _ = waterdata.get_monitoring_locations(
                bbox=[west, south, east, north],
                site_type_code='ST',
            )
    except Exception as e:
        gs.fatal("Water Data site query failed: {}".format(e))

    if gdf is None or gdf.empty:
        gs.fatal("No stream gauge sites found.")

    gdf = gdf.reset_index(drop=True)

    # get_monitoring_locations returns a GeoDataFrame but without CRS set
    if not isinstance(gdf, gpd.GeoDataFrame):
        from shapely import wkt as shapely_wkt
        gdf['geometry'] = gdf['geometry'].apply(
            lambda g: shapely_wkt.loads(str(g)) if g is not None else None
        )
        gdf = gpd.GeoDataFrame(gdf, geometry='geometry')
    if gdf.crs is None:
        gdf = gdf.set_crs('EPSG:4326')

    # site_no: bare numeric ID used as join key with basins and SQLite tables
    gdf['site_no'] = gdf['monitoring_location_number'].astype(str)

    gs.message("Found {} site(s).".format(len(gdf)))
    return gdf


def fetch_basins(site_nos):
    """Fetch upstream basin polygons from NLDI; return a GeoDataFrame."""
    import io
    import requests
    import geopandas as gpd
    import pandas as pd

    parts = []
    for site_no in site_nos:
        url = '{}/USGS-{}/basin'.format(_NLDI_BASE, site_no)
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            gdf = gpd.read_file(io.StringIO(r.text))
            gdf['site_no'] = site_no
            parts.append(gdf)
        except Exception as e:
            gs.warning("Basin fetch failed for site {}: {}".format(site_no, e))

    if not parts:
        gs.fatal("No upstream basins could be retrieved from NLDI.")
    return pd.concat(parts, ignore_index=True)


def write_timeseries(site_nos, parameter_cd, start_date, end_date, table_name):
    """Fetch USGS daily values and store in the mapset SQLite database."""
    import dataretrieval.waterdata as waterdata

    mon_ids = [_to_monitoring_id(s) for s in site_nos]
    time_range = '{}/{}'.format(start_date, end_date)

    gs.message("Fetching daily values ({} – {})...".format(start_date, end_date))
    try:
        df, _ = waterdata.get_daily(
            monitoring_location_id=mon_ids,
            parameter_code=parameter_cd,
            time=time_range,
        )
    except Exception as e:
        gs.fatal("Water Data time series query failed: {}".format(e))

    if df is None or df.empty:
        gs.warning("No time series data returned.")
        return

    df['site_no'] = df['monitoring_location_id'].str.replace(
        r'^[A-Z]+-', '', regex=True
    )

    db_path = _mapset_db_path()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('DROP TABLE IF EXISTS "{}"'.format(table_name))
    cur.execute('''
        CREATE TABLE "{}" (
            site_no          TEXT,
            datetime         TEXT,
            value            REAL,
            unit_of_measure  TEXT,
            approval_status  TEXT,
            qualifier        TEXT
        )
    '''.format(table_name))

    rows = []
    for _, row in df.iterrows():
        try:
            val = float(row['value'])
        except (TypeError, ValueError):
            val = None
        qual = row.get('qualifier')
        if isinstance(qual, list):
            qual = '; '.join(str(q) for q in qual) if qual else None
        else:
            q_str = str(qual).strip() if qual is not None else ''
            qual = q_str if q_str not in ('', 'None', 'nan', '[]') else None
        rows.append((
            str(row['site_no']),
            str(row['time']),
            val,
            str(row.get('unit_of_measure', '')) or None,
            str(row.get('approval_status', '')) or None,
            qual,
        ))

    cur.executemany(
        'INSERT INTO "{}" VALUES (?, ?, ?, ?, ?, ?)'.format(table_name), rows
    )
    conn.commit()
    conn.close()

    gs.message(
        "Time series stored: table '{}', {} records.".format(table_name, len(rows))
    )
    gs.message(
        'Query with: db.select sql="SELECT * FROM {} LIMIT 10"'.format(table_name)
    )


def fetch_ratings(site_nos, table_name):
    """Fetch current shift-adjusted rating curves; store in the mapset SQLite database."""
    import dataretrieval.waterdata as waterdata
    import pandas as pd

    gs.message("Fetching rating curves...")
    parts = []
    for site_no in site_nos:
        mon_id = _to_monitoring_id(site_no)
        try:
            result = waterdata.get_ratings(monitoring_location_id=mon_id)
            for df in result.values():
                df = df.copy()
                df['site_no'] = site_no
                parts.append(df)
        except Exception as e:
            gs.warning("Rating curve fetch failed for site {}: {}".format(site_no, e))

    if not parts:
        gs.warning("No rating curves retrieved.")
        return

    df_all = pd.concat(parts, ignore_index=True)

    db_path = _mapset_db_path()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('DROP TABLE IF EXISTS "{}"'.format(table_name))
    cur.execute('''
        CREATE TABLE "{}" (
            site_no       TEXT,
            stage_ft      REAL,
            shift_ft      REAL,
            discharge_cfs REAL
        )
    '''.format(table_name))

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    rows = [
        (str(row['site_no']), _f(row.get('INDEP')), _f(row.get('SHIFT')), _f(row.get('DEP')))
        for _, row in df_all.iterrows()
    ]
    cur.executemany(
        'INSERT INTO "{}" VALUES (?, ?, ?, ?)'.format(table_name), rows
    )
    conn.commit()
    conn.close()

    gs.message(
        "Rating curve stored: table '{}', {} rows.".format(table_name, len(rows))
    )
    gs.message(
        'Query with: db.select sql="SELECT * FROM {} LIMIT 10"'.format(table_name)
    )


def fetch_channel(site_nos, table_name):
    """Fetch channel geometry from field measurements; store in the mapset SQLite database."""
    import dataretrieval.waterdata as waterdata
    import pandas as pd

    gs.message("Fetching channel geometry from field measurements...")
    parts = []
    for site_no in site_nos:
        mon_id = _to_monitoring_id(site_no)
        try:
            df, _ = waterdata.get_channel(monitoring_location_id=mon_id)
            if not df.empty:
                df = df.copy()
                df['site_no'] = site_no
                parts.append(df)
        except Exception as e:
            gs.warning("Channel data fetch failed for site {}: {}".format(site_no, e))

    if not parts:
        gs.warning("No channel geometry data retrieved.")
        return

    df_all = pd.concat(parts, ignore_index=True)

    db_path = _mapset_db_path()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('DROP TABLE IF EXISTS "{}"'.format(table_name))
    cur.execute('''
        CREATE TABLE "{}" (
            site_no           TEXT,
            datetime          TEXT,
            channel_width     REAL,
            channel_area      REAL,
            channel_velocity  REAL,
            channel_flow      REAL,
            channel_stability TEXT,
            channel_material  TEXT,
            measurement_type  TEXT
        )
    '''.format(table_name))

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _s(v):
        s = str(v) if v is not None else ''
        return s if s not in ('', 'None', 'nan', 'NaT', 'Unspecified') else None

    rows = [
        (
            str(row['site_no']),
            _s(row.get('time')),
            _f(row.get('channel_width')),
            _f(row.get('channel_area')),
            _f(row.get('channel_velocity')),
            _f(row.get('channel_flow')),
            _s(row.get('channel_stability')),
            _s(row.get('channel_material')),
            _s(row.get('measurement_type')),
        )
        for _, row in df_all.iterrows()
    ]
    cur.executemany(
        'INSERT INTO "{}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)'.format(table_name), rows
    )
    conn.commit()
    conn.close()

    gs.message(
        "Channel data stored: table '{}', {} records.".format(table_name, len(rows))
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
    flag_ts = flags['t']
    flag_ratings = flags['r']
    flag_channel = flags['c']

    atexit.register(cleanup)

    require_package('dataretrieval')
    require_package('geopandas')

    site_ids = [s.strip() for s in sites_str.split(',')] if sites_str else None
    bbox = None if site_ids else get_geographic_bbox()

    # Gauge locations
    sites_gdf = fetch_sites(site_ids, bbox)
    geodataframe_to_grass(sites_gdf, output)
    gs.message("Gauge locations imported to '{}'.".format(output))

    site_nos = sites_gdf['site_no'].astype(str).tolist()

    # Upstream basins
    if basins_map:
        gs.message("Fetching upstream basins from NLDI...")
        try:
            basins_gdf = fetch_basins(site_nos)
            geodataframe_to_grass(basins_gdf, basins_map)
            gs.message("Upstream basins imported to '{}'.".format(basins_map))
        except Exception as e:
            gs.warning("Basin retrieval failed: {}".format(e))

    # Time series
    if flag_ts:
        write_timeseries(
            site_nos, parameter_cd, start_date, end_date,
            '{}_timeseries'.format(output),
        )

    # Rating curves
    if flag_ratings:
        fetch_ratings(site_nos, '{}_ratings'.format(output))

    # Channel geometry
    if flag_channel:
        fetch_channel(site_nos, '{}_channel'.format(output))


if __name__ == '__main__':
    main()
