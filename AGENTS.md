# Repository Instructions

## Scope

- Muyuan contains benchmark suites and independently usable plugins.
- Read `README.md` and `docs/architecture.md` before changing the top-level structure.

## Organization

- Keep benchmark-specific code, configurations, experiments, and reports under `benchmarks/<name>/`.
- Keep each plugin's code, tests, documentation, and build files under `plugins/<name>/`.
- Benchmarks should use a plugin's public interface rather than depend on its internal implementation.
- Do not add shared root modules or new top-level categories until there is a demonstrated need.

## Changes

- Keep root documentation concise; place implementation details in the closest owning module.
- Keep `benchmarks/templates/report-template.md` and `report-template.html` aligned when shared report content changes.
- Do not commit credentials, secrets, or local environment files.
- Preserve existing user changes and avoid modifying unrelated files.

## Verification

- Run the closest module's documented tests after code changes.
- For documentation and templates, run `git diff --check` and verify affected relative links.
- State which checks could not be run and why.
