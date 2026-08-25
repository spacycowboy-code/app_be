"""Department usage report: the whole report as one self-contained HTML page."""
from app.services.usage_report.render import OrgSnapshotMissing, build_html

__all__ = ["OrgSnapshotMissing", "build_html"]
