from __future__ import annotations

import importlib.util
import asyncio
from pathlib import Path
import sys
import types

import pytest


def _install_ha_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    vol_mod = types.ModuleType("voluptuous")
    vol_mod.ALLOW_EXTRA = object()

    class _Invalid(Exception):
        pass

    def _apply_validator(validator, value):
        if isinstance(validator, list):
            item_validator = validator[0]
            return [_apply_validator(item_validator, item) for item in value]
        if callable(validator):
            return validator(value)
        return value

    class _Schema:
        def __init__(self, schema, **_kwargs):
            self.schema = schema

        def __call__(self, value):
            if not isinstance(self.schema, dict):
                return _apply_validator(self.schema, value)
            result = dict(value)
            for key, validator in self.schema.items():
                if key in result:
                    result[key] = _apply_validator(validator, result[key])
            return result

    def _all(*validators):
        def validate(value):
            for validator in validators:
                value = _apply_validator(validator, value)
            return value

        return validate

    vol_mod.Invalid = _Invalid
    vol_mod.Required = lambda key, default=None: key
    vol_mod.Optional = lambda key, default=None: key
    vol_mod.All = _all
    vol_mod.Schema = _Schema
    monkeypatch.setitem(sys.modules, "voluptuous", vol_mod)

    ha_pkg = types.ModuleType("homeassistant")
    monkeypatch.setitem(sys.modules, "homeassistant", ha_pkg)

    config_entries_mod = types.ModuleType("homeassistant.config_entries")
    config_entries_mod.SOURCE_IMPORT = "import"
    config_entries_mod.ConfigEntry = object
    config_entries_mod.ConfigFlow = type("ConfigFlow", (), {"__init_subclass__": classmethod(lambda cls, **kwargs: None)})
    config_entries_mod.OptionsFlow = object
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries_mod)

    core_mod = types.ModuleType("homeassistant.core")
    core_mod.HomeAssistant = object
    core_mod.callback = lambda fn: fn
    monkeypatch.setitem(sys.modules, "homeassistant.core", core_mod)

    helpers_pkg = types.ModuleType("homeassistant.helpers")
    cv_mod = types.ModuleType("homeassistant.helpers.config_validation")
    cv_mod.string = str
    cv_mod.entity_id = str
    cv_mod.ensure_list = lambda value: value if isinstance(value, list) else [value]
    helpers_pkg.config_validation = cv_mod
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers_pkg)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.config_validation", cv_mod)

    selector_mod = types.ModuleType("homeassistant.helpers.selector")
    selector_mod.EntitySelector = lambda *_args, **_kwargs: object()
    selector_mod.EntitySelectorConfig = lambda *_args, **_kwargs: object()
    selector_mod.LabelSelector = lambda *_args, **_kwargs: object()
    selector_mod.LabelSelectorConfig = lambda *_args, **_kwargs: object()
    selector_mod.TextSelector = lambda *_args, **_kwargs: object()
    selector_mod.TextSelectorConfig = lambda *_args, **_kwargs: object()
    selector_mod.TextSelectorType = types.SimpleNamespace(TEXT="text")
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.selector", selector_mod)


@pytest.fixture
def modules(monkeypatch: pytest.MonkeyPatch):
    _install_ha_stubs(monkeypatch)
    root = Path(__file__).resolve().parents[1]

    const_spec = importlib.util.spec_from_file_location(
        "custom_components.calendar_merge.const",
        root / "custom_components" / "calendar_merge" / "const.py",
    )
    const_mod = importlib.util.module_from_spec(const_spec)
    assert const_spec and const_spec.loader
    const_spec.loader.exec_module(const_mod)
    monkeypatch.setitem(sys.modules, "custom_components.calendar_merge.const", const_mod)

    init_spec = importlib.util.spec_from_file_location(
        "custom_components.calendar_merge.__init__",
        root / "custom_components" / "calendar_merge" / "__init__.py",
    )
    init_mod = importlib.util.module_from_spec(init_spec)
    assert init_spec and init_spec.loader
    init_spec.loader.exec_module(init_mod)

    flow_spec = importlib.util.spec_from_file_location(
        "custom_components.calendar_merge.config_flow",
        root / "custom_components" / "calendar_merge" / "config_flow.py",
    )
    flow_mod = importlib.util.module_from_spec(flow_spec)
    assert flow_spec and flow_spec.loader
    flow_spec.loader.exec_module(flow_mod)

    return init_mod, flow_mod, const_mod


def test_readme_yaml_example_validates(modules):
    init_mod, _flow_mod, _const = modules
    cfg = {
        "calendar_merge": [
            {
                "name": "Work",
                "entity_id": ["calendar.google_work", "calendar.outlook_shared"],
                "default_calendar": "calendar.google_work",
            },
            {
                "name": "Family",
                "entity_id": [
                    "calendar.google_family",
                    "calendar.birthdays",
                    "calendar.school_holidays",
                ],
            },
        ]
    }
    validated = init_mod.CONFIG_SCHEMA(cfg)
    assert validated["calendar_merge"][0]["default_calendar"] == "calendar.google_work"


def test_yaml_import_updates_existing_entry_and_clears_options(modules):
    _init_mod, flow_mod, const = modules

    class FakeEntry:
        entry_id = "abc"
        data = {const.CONF_CALENDAR_NAME: "Work"}
        options = {const.CONF_SOURCE_CALENDARS: ["calendar.stale"]}

    updated = {}
    reloaded = []

    class FakeConfigEntries:
        def async_update_entry(self, entry, data, options):
            updated["entry"] = entry
            updated["data"] = data
            updated["options"] = options

        async def async_reload(self, entry_id):
            reloaded.append(entry_id)

    flow = flow_mod.CalendarMergeConfigFlow()
    flow.hass = types.SimpleNamespace(config_entries=FakeConfigEntries())
    flow._async_current_entries = lambda: [FakeEntry()]
    flow.async_abort = lambda **kwargs: kwargs

    result = asyncio.run(
        flow.async_step_import(
            {
                const.CONF_CALENDAR_NAME: "Work",
                const.CONF_SOURCE_CALENDARS: ["calendar.google_work"],
                const.CONF_DEFAULT_CALENDAR: "calendar.google_work",
            }
        )
    )

    assert result == {"reason": "already_configured"}
    assert updated["entry"].entry_id == "abc"
    assert updated["data"][const.CONF_DEFAULT_CALENDAR] == "calendar.google_work"
    assert updated["options"] == {}
    assert reloaded == ["abc"]


def test_yaml_rejects_default_calendar_outside_sources(modules):
    init_mod, _flow_mod, const = modules
    cfg = {
        "calendar_merge": [
            {
                "name": "Work",
                "entity_id": ["calendar.google_work"],
                "default_calendar": "calendar.personal",
            }
        ]
    }

    with pytest.raises(Exception) as err:
        init_mod.CONFIG_SCHEMA(cfg)

    assert const.CONF_DEFAULT_CALENDAR in str(err.value)


def test_yaml_labels_only_validates(modules):
    init_mod, _flow_mod, const = modules
    cfg = {
        "calendar_merge": [
            {
                "name": "Personal",
                "labels": ["personal", "Personal"],
            }
        ]
    }
    validated = init_mod.CONFIG_SCHEMA(cfg)
    assert validated["calendar_merge"][0][const.CONF_SOURCE_LABELS] == [
        "personal",
        "Personal",
    ]


def test_yaml_labels_combined_with_entity_ids_validate(modules):
    init_mod, _flow_mod, const = modules
    cfg = {
        "calendar_merge": [
            {
                "name": "Work",
                "entity_id": ["calendar.google_work"],
                "labels": ["work"],
                "default_calendar": "calendar.google_work",
            }
        ]
    }
    validated = init_mod.CONFIG_SCHEMA(cfg)
    entry = validated["calendar_merge"][0]
    assert entry[const.CONF_SOURCE_CALENDARS] == ["calendar.google_work"]
    assert entry[const.CONF_SOURCE_LABELS] == ["work"]


def test_yaml_rejects_entry_without_sources_or_labels(modules):
    init_mod, _flow_mod, const = modules
    cfg = {"calendar_merge": [{"name": "Empty"}]}

    with pytest.raises(Exception) as err:
        init_mod.CONFIG_SCHEMA(cfg)

    assert const.CONF_SOURCE_LABELS in str(err.value)


def test_yaml_allows_default_calendar_outside_sources_when_labels_present(modules):
    init_mod, _flow_mod, const = modules
    cfg = {
        "calendar_merge": [
            {
                "name": "Work",
                "entity_id": ["calendar.google_work"],
                "labels": ["work"],
                "default_calendar": "calendar.label_matched",
            }
        ]
    }
    validated = init_mod.CONFIG_SCHEMA(cfg)
    assert validated["calendar_merge"][0][const.CONF_DEFAULT_CALENDAR] == (
        "calendar.label_matched"
    )


def test_config_flow_validate_accepts_label_only_sources(modules):
    _init_mod, flow_mod, const = modules
    errors = flow_mod.CalendarMergeConfigFlow._validate(
        {
            const.CONF_CALENDAR_NAME: "Personal",
            const.CONF_SOURCE_LABELS: ["personal"],
        }
    )
    assert errors == {}


def test_config_flow_validate_requires_source_or_label(modules):
    _init_mod, flow_mod, const = modules
    errors = flow_mod.CalendarMergeConfigFlow._validate(
        {const.CONF_CALENDAR_NAME: "Empty"}
    )
    assert errors[const.CONF_SOURCE_CALENDARS] == "no_sources"


def test_config_flow_validate_still_rejects_default_outside_sources_without_labels(modules):
    _init_mod, flow_mod, const = modules
    errors = flow_mod.CalendarMergeConfigFlow._validate(
        {
            const.CONF_CALENDAR_NAME: "Work",
            const.CONF_SOURCE_CALENDARS: ["calendar.google_work"],
            const.CONF_DEFAULT_CALENDAR: "calendar.personal",
        }
    )
    assert errors[const.CONF_DEFAULT_CALENDAR] == "default_not_in_sources"


def test_config_flow_validate_allows_default_outside_sources_with_labels(modules):
    _init_mod, flow_mod, const = modules
    errors = flow_mod.CalendarMergeConfigFlow._validate(
        {
            const.CONF_CALENDAR_NAME: "Work",
            const.CONF_SOURCE_CALENDARS: ["calendar.google_work"],
            const.CONF_SOURCE_LABELS: ["work"],
            const.CONF_DEFAULT_CALENDAR: "calendar.label_matched",
        }
    )
    assert const.CONF_DEFAULT_CALENDAR not in errors
