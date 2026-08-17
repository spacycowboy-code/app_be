# Dashboard backups

Snapshots of what is in Grafana, produced by `tools/export_dashboards.py`.

The UI is where dashboards are edited, so Grafana's database is the source of
truth and these files are a copy taken from it. They exist because that database
is a SQLite file in a container: one recreate without a volume, or one lost
volume, and the boards are gone with no record of what they contained.

**This directory is not provisioning.** Grafana never reads it. The provisioned
seed is `../dashboards/`, and a snapshot placed there would be re-applied within
seconds and start overwriting the UI it was copied from.

To restore one: Dashboards -> New -> Import -> upload the JSON. `id` and
`version` are stripped on export, so an import creates or updates cleanly.
