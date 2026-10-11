# Test fixture provenance

`pa_pattern.3mf` is copied from BambuStudio's
`resources/calib/pressure_advance/pa_pattern.3mf` at commit `47a13eeb1d9af65ef2371e361c856e21d4f6ca72`.
It exercises Bambu object metadata, mesh/component transforms, and multiple instances.
Its upstream license is preserved in `../../LICENSE`.

`slicer_options.json` records live option names from `src/libslic3r/PrintConfig.cpp`
at the same commit, plus the exact source SHA-256. It is a compatibility snapshot,
not proof that a newer installed binary supports every option. Refresh it against
a supported BambuStudio release when updating the catalog; exclude commented-out definitions.
The unit suite needs neither a BambuStudio checkout nor an installed slicer.
