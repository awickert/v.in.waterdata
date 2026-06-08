#!/usr/bin/env bash
# Example: Mississippi River at St. Paul, MN (USGS gauge 05331000)
#
# Imports the full daily discharge record (1892–present) and the
# contributing drainage basin, then demonstrates basic queries.
#
# Run inside an active GRASS session, e.g.:
#   grass --tmp-project EPSG:4326 --exec bash mississippi_stpaul.sh
#
# The gauge and basin are delivered in EPSG:4326 (WGS84) and will be
# reprojected automatically if the GRASS location uses a different CRS.

set -e

GAUGE=mississippi_stpaul
BASIN=mississippi_stpaul_basin
SITE=05331000

# ------------------------------------------------------------------
# 1. Set the computational region around the gauge
# ------------------------------------------------------------------
g.region n=45.1 s=44.8 e=-92.9 w=-93.3 res=0:00:30

# ------------------------------------------------------------------
# 2. Import gauge location, upstream drainage basin, and full
#    discharge record (parameter 00060 = discharge in ft³/s)
# ------------------------------------------------------------------
v.in.nwis \
    output=${GAUGE} \
    basins=${BASIN} \
    sites=${SITE} \
    parameter_cd=00060 \
    start_date=1892-03-01 \
    end_date=$(date +%Y-%m-%d) \
    -bt

# ------------------------------------------------------------------
# 3. Gauge metadata
#    drain_area_va is the NWIS-reported drainage area in square miles
# ------------------------------------------------------------------
echo "--- Gauge attributes ---"
db.select sql="SELECT site_no, station_nm, dec_lat_va, dec_long_va,
                      drain_area_va
               FROM ${GAUGE}"

# ------------------------------------------------------------------
# 4. Basin area computed from the imported polygon
#    (cross-check against the NWIS reported value above)
# ------------------------------------------------------------------
v.to.db map=${BASIN} option=area columns=area_sq_m units=meters
v.to.db map=${BASIN} option=area columns=area_sq_km units=kilometers

echo "--- Basin area ---"
db.select sql="SELECT site_no,
                      ROUND(area_sq_km, 0)        AS area_km2,
                      ROUND(area_sq_km / 2.590,0) AS area_mi2
               FROM ${BASIN}"

# ------------------------------------------------------------------
# 5. Time series summary
# ------------------------------------------------------------------
TS=${GAUGE}_timeseries

echo "--- Time series record extent and basic statistics ---"
db.select sql="SELECT COUNT(*)           AS n_days,
                      MIN(datetime)      AS first_date,
                      MAX(datetime)      AS last_date,
                      ROUND(MIN(value),0)  AS min_cfs,
                      ROUND(AVG(value),0)  AS mean_cfs,
                      ROUND(MAX(value),0)  AS max_cfs
               FROM ${TS}
               WHERE value IS NOT NULL"

echo "--- First five records ---"
db.select sql="SELECT site_no, SUBSTR(datetime,1,10) AS date, value AS discharge_cfs, flag
               FROM ${TS}
               ORDER BY datetime ASC
               LIMIT 5"

echo "--- Five highest discharge days on record ---"
db.select sql="SELECT site_no, SUBSTR(datetime,1,10) AS date, value AS discharge_cfs, flag
               FROM ${TS}
               WHERE value IS NOT NULL
               ORDER BY value DESC
               LIMIT 5"
