#!/bin/sh
set -eu
# A versioned, private code volume is mounted read-only for all subsequent runs.
cd /source
mkdir -p /frozen/experiment_v3/v3_2 /frozen/experiment_v3/data /frozen/experiment_v3/odoo_addons/wms_research_v3 /frozen/series_py312/data
test ! -f /frozen/SNAPSHOT_COMPLETE
cp -a experiment_v3/src /frozen/experiment_v3/
cp experiment_v3/data/ridge.json /frozen/experiment_v3/data/
cp experiment_v3/odoo_addons/wms_research_v3/policy.py /frozen/experiment_v3/odoo_addons/wms_research_v3/
cp -a series_py312/data/normalized series_py312/data/manifests /frozen/series_py312/data/
cd experiment_v3/v3_2
cp ./*.py ./*.json /frozen/experiment_v3/v3_2/
cp -a design tools /frozen/experiment_v3/v3_2/
mkdir /frozen/experiment_v3/v3_2/preflight
cp preflight/design.json preflight/pilot_signature.json preflight/environment_before.json /frozen/experiment_v3/v3_2/preflight/
touch /frozen/SNAPSHOT_COMPLETE
