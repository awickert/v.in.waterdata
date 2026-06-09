#!/usr/bin/env bash
# Example: Mississippi River at St. Paul, MN (USGS gauge 05331000)
#
# Imports the full daily discharge record (1892–present), the contributing
# drainage basin, the shift-adjusted rating curve, and channel geometry from
# field measurements, then demonstrates basic queries.
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
# 2. Import gauge location, upstream drainage basin, full discharge
#    record (parameter 00060 = discharge in ft³/s), shift-adjusted
#    rating curve, and channel geometry from field measurements
# ------------------------------------------------------------------
v.in.waterdata \
    output=${GAUGE} \
    basins=${BASIN} \
    sites=${SITE} \
    parameter_cd=00060 \
    start_date=1892-03-01 \
    end_date=$(date +%Y-%m-%d) \
    -btrc

# ------------------------------------------------------------------
# 3. Gauge metadata
#    drainage_area is the USGS-reported drainage area in square miles
# ------------------------------------------------------------------
echo "--- Gauge attributes ---"
db.select sql="SELECT site_no, monitoring_location_name,
                      drainage_area
               FROM ${GAUGE}"

# ------------------------------------------------------------------
# 4. Basin area computed from the imported polygon
#    (cross-check against the USGS reported value above)
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
db.select sql="SELECT COUNT(*)              AS n_days,
                      MIN(datetime)         AS first_date,
                      MAX(datetime)         AS last_date,
                      ROUND(MIN(value),0)   AS min_cfs,
                      ROUND(AVG(value),0)   AS mean_cfs,
                      ROUND(MAX(value),0)   AS max_cfs
               FROM ${TS}
               WHERE value IS NOT NULL"

echo "--- First five records ---"
db.select sql="SELECT site_no, SUBSTR(datetime,1,10) AS date,
                      value AS discharge_cfs, approval_status
               FROM ${TS}
               ORDER BY datetime ASC
               LIMIT 5"

echo "--- Five highest discharge days on record ---"
db.select sql="SELECT site_no, SUBSTR(datetime,1,10) AS date,
                      value AS discharge_cfs, approval_status
               FROM ${TS}
               WHERE value IS NOT NULL
               ORDER BY value DESC
               LIMIT 5"

# ------------------------------------------------------------------
# 6. Rating curve — current shift-adjusted stage-discharge table
# ------------------------------------------------------------------
RATINGS=${GAUGE}_ratings

echo "--- Rating curve (stage 4–8 ft showing taper of applied shift) ---"
db.select sql="SELECT site_no,
                      ROUND(shift_ft, 2)      AS shift_ft,
                      ROUND(stage_ft, 1)      AS stage_ft,
                      ROUND(discharge_cfs, 0) AS discharge_cfs
               FROM ${RATINGS}
               WHERE stage_ft IN (4.0, 5.0, 6.0, 7.0, 8.0)
               ORDER BY stage_ft"

# ------------------------------------------------------------------
# 7. Channel geometry — most recent 5 field measurement visits
# ------------------------------------------------------------------
CHANNEL=${GAUGE}_channel

echo "--- Most recent channel measurements ---"
db.select sql="SELECT site_no,
                      SUBSTR(datetime, 1, 10)   AS date,
                      channel_width             AS width_ft,
                      ROUND(channel_area, 0)    AS area_ft2,
                      ROUND(channel_velocity,2) AS vel_fps,
                      ROUND(channel_flow, 0)    AS flow_cfs
               FROM ${CHANNEL}
               ORDER BY datetime DESC
               LIMIT 5"
