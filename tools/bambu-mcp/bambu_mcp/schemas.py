"""Typed tool inputs/outputs. FastMCP derives each tool's inputSchema/outputSchema from these."""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .runner import Metrics, Variant

SessionId = Annotated[str, Field(description="id returned by project_open")]
ObjectId = Annotated[str, Field(description="object id as shown by project_inspect (string)", pattern=r"^\d+$")]
SettingValue = bool | int | float | str


class Flags(BaseModel):
    """Slice-time transform flags; unset fields are left alone."""
    model_config = ConfigDict(extra="forbid")

    orient: Literal[0, 1] | None = Field(None, description="1 = Bambu auto-orient all objects, 0 = leave as is")
    arrange: Literal[0, 1] | None = Field(None, description="1 = auto-arrange, 0 = keep positions")
    allow_rotations: bool | None = Field(None, description="arrange may rotate objects")
    avoid_extrusion_cali_region: bool | None = Field(None, description="arrange avoids the calibration strip")
    allow_multicolor_oneplate: bool | None = Field(None, description="arrange may mix colours on one plate")
    ensure_on_bed: bool | None = Field(None, description="lift objects that sink below the bed")


class VariantSpec(BaseModel):
    """A nonempty change set applied on top of the session's working variant (it does not replace it)."""
    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, SettingValue] = Field(default_factory=dict, description="global print settings, catalog keys")
    object_overrides: dict[str, dict[str, SettingValue]] = Field(
        default_factory=dict, description="per-object settings: {object id: {key: value}}")
    object_rotation: dict[str, tuple[float, float, float]] = Field(
        default_factory=dict, description="{object id: [rx, ry, rz] degrees}, applied X then Y then Z")
    flags: Flags = Field(default_factory=Flags)

    @model_validator(mode="after")
    def require_change(self) -> "VariantSpec":
        if any(not settings for settings in self.object_overrides.values()):
            raise ValueError("object_overrides entries must contain at least one setting")
        if not (self.overrides or self.object_overrides or self.object_rotation or
                self.flags.model_dump(exclude_none=True)):
            raise ValueError("variant must contain at least one change in overrides, object_overrides, "
                             "object_rotation, or flags")
        return self


class VariantOut(BaseModel):
    overrides: dict[str, str] = Field(description="global overrides as passed to the CLI")
    object_overrides: dict[str, dict[str, str]]
    object_rotation: dict[str, list[float]]
    flags: dict[str, Any]

    @classmethod
    def of(cls, v: Variant) -> "VariantOut":
        return cls(overrides=v.overrides, object_overrides=v.object_overrides,
                   object_rotation={k: list(r) for k, r in v.object_rotation.items()}, flags=v.flags)


class StudioInfo(BaseModel):
    binary: str = Field(description="Bambu Studio executable in use")
    version: str = Field(description="version line from --help, '?' if unknown")
    profile_roots: list[str] = Field(description="directories searched for system/user presets")
    presets: dict[str, int] = Field(description="preset count per kind (machine, process, filament)")


class ParamSpec(BaseModel):
    kind: Literal["int", "float", "percent", "bool", "enum"]
    range: tuple[float, float] | None = None
    values: list[str] | None = Field(None, description="allowed values for enums")
    floor: int | None = Field(None, description="functional-part minimum the optimizer respects")
    note: str | None = Field(None, description="time / material / quality trade-off")


class ObjectInfo(BaseModel):
    name: str | None
    size_mm: list[float] | None = Field(description="[x, y, z] world bbox size; null if it could not be computed")
    settings: dict[str, str] = Field(description="per-object settings stored in the 3MF")


class ProjectSummary(BaseModel):
    source: str = Field(description="working copy of the project (the original is never modified)")
    objects: dict[str, ObjectInfo]
    working_variant: VariantOut
    baseline: Metrics | None = Field(None, description="first successful slice, if any")
    effective_settings: dict[str, str] = Field(
        default_factory=dict,
        description="catalog keys as they will apply: 3MF project settings, then chosen presets, then working overrides")
    settings_errors: list[str] = Field(
        default_factory=list,
        description="presets that could not be resolved; non-empty = effective_settings is incomplete and slicing will fail")


class ProjectOpened(ProjectSummary):
    session_id: str


class SettingsState(BaseModel):
    overrides: dict[str, str]
    object_overrides: dict[str, dict[str, str]]


class TransformState(BaseModel):
    rotations: dict[str, list[float]]
    flags: dict[str, Any]


class FlagsState(BaseModel):
    flags: dict[str, Any]


class BatchRow(BaseModel):
    input_index: int = Field(description="position of this variant in the request's `variants` list")
    variant: VariantOut = Field(description="the full variant that was sliced (working variant + this change set)")
    metrics: Metrics


class CompareRow(Metrics):
    delta_time_s: float | None = Field(None, description="time minus the first run's time")
    delta_g: float | None = Field(None, description="grams minus the first run's grams")


class HistoryEntry(BaseModel):
    stage: str
    overrides: dict[str, str]
    rotation: dict[str, list[float]]
    flags: dict[str, Any]
    ok: bool
    error: str
    time_s: float
    filament_g: float
    score: float | None = Field(description="null when infeasible or failed")
    run_id: str


class OptimizeResult(BaseModel):
    baseline: Metrics
    best: Metrics
    time_saved_pct: float
    material_saved_pct: float
    best_variant: VariantOut
    applied: bool = Field(description="best variant became the session's working variant")
    slices_used: int
    history: list[HistoryEntry]
    report: dict[str, Any] = Field(default_factory=dict, description="fastest, least-material, conservative alternatives and resolved print specification")


class ExportResult(BaseModel):
    path: str


class LaunchResult(BaseModel):
    launched: bool
