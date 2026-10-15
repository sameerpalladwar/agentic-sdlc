# Existing LinkStore library

Python 3.12 in-memory URL mapping library. `LinkStore.create(url)` returns a unique
code; `resolve(code)` returns the original URL or None. Links are not fetched.
Data is lost on process exit. This example has no web server or database.

This directory is read-only scenario input. Enhancements are generated separately
under workspace/output_code/<run_id>/; the harness never executes this input code
or automatically installs its dependencies.
