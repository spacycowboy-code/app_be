# Eval fixtures

Small inputs the audit is expected to classify a particular way. Real malicious
samples are not stored here: the regression runner references public labelled
corpora by id instead (see `../run_regression.py`), so this directory only holds
the cases that need to be hand-written.

| fixture | expected |
|---|---|
| `benign-tricky/` | pass — mentions a placeholder key, must not be read as a credential |
| `cn-bundle-benign/` | pass — Chinese comments and strings in bundled code are not hidden instructions |
| `cn-bad/`, `en-bad/` | danger — planted malicious instructions |
| `judge-injection/` | danger — attempts to hijack the judge; must be reported, not obeyed |
